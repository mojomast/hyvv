"""Read the launcher's JSONL in bounded pages; never run commands or publish prose.

Parser v1: legacy Cua ``[tool execute] bash`` / ``[tool completed] …``
records, or the structured Cua 0.5 AgentEvent fields. A row without a native
call id gets an observation-local id: it cannot close another row's call.
"""

import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

MAX_INT = 2**53 - 1
RECORD_BYTES = 64 * 1024
BATCH_BYTES = 1024 * 1024
ATTEMPT = re.compile(r"att_[0-9a-f]{8,32}")
TOOL_NAMES = frozenset(
    "bash read write edit glob grep list patch apply_patch todowrite webfetch task".split()
)
STATES = frozenset("queued starting running backoff cancel_requested succeeded failed cancelled".split())
TERMINAL = {"succeeded", "failed", "cancelled"}
OBSERVED = {
    "heartbeat",
    "worker:message",
    "worker:thought",
    "worker:turn_started",
    "worker:turn_ended",
    "worker:exited",
    "worker:error",
}
OUTCOME = {"completed": "ok", "failed": "failed", "cancelled": "cancelled"}
SAFE_CALL = re.compile(
    r"(?:call_|tool_)?[0-9a-fA-F]{16,64}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
)


def sdk_tool_fields(event):
    """Only these structured SDK fields may be added to the private journal."""
    if event.kind not in ("tool_call", "tool_update"):
        return {}
    out = {}
    call = getattr(event, "tool_id", None)
    if isinstance(call, str) and SAFE_CALL.fullmatch(call):
        out["tool_id"] = call
    title = getattr(event, "tool_title", None)
    if isinstance(title, str) and title in TOOL_NAMES:
        out["tool_name"] = title
    status = getattr(event, "tool_status", None)
    if isinstance(status, str) and status in {*OUTCOME, "pending", "in_progress"}:
        out["tool_status"] = status
    return out


def utc(value):
    if type(value) not in (float, int):
        return None
    try:
        return (
            datetime.fromtimestamp(value, timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    except (ValueError, OSError, OverflowError):
        return None


def candidate(aid, generation, pos, suffix, kind, payload, stamp=None, run=0):
    record = f"{aid}:g{generation}:{suffix}{pos}"
    scope = dict.fromkeys(
        (
            "repo",
            "campaign_id",
            "job_id",
            "task_id",
            "attempt_id",
            "session_id",
            "action_id",
            "contract_sha256",
            "code_rev",
        )
    )
    scope["attempt_id"] = aid
    return dict(
        schema="rizzstack.rails-candidate/1",
        kind=kind,
        authority="worker_report",
        dedupe_key=record,
        scope=scope,
        lane_id=f"{aid}:g{generation}:r{run}",
        parent_lane_id=None,
        parent_event_id=None,
        redaction_policy="rails-safe/1",
        source={"adapter": "launcher-log", "instance": "worker-1", "record_id": record, "revision": "v1"},
        payload=payload,
        occurred_at=stamp,
        duration_ms=None,
        timing_quality="unknown",
    )


def diagnostic(aid, c, kind, code, lo, hi, suffix, pos):
    # Only safe observation identity contributes to this evidence digest, never raw source bytes.
    digest = hashlib.sha256(f"{aid}:{c.generation}:{code}:{lo}:{hi}".encode("ascii")).hexdigest()
    return candidate(
        aid,
        c.generation,
        pos,
        suffix,
        kind,
        dict(
            range_start=lo,
            range_end=hi,
            source_id="launcher-log",
            cursor=str(hi),
            count=hi - lo + 1,
            code=code,
            digest=digest,
        ),
        run=c.run,
    )


def normalize(aid, c, row, n):
    kind = row.get("kind")
    kind = kind if isinstance(kind, str) else ""
    state = row.get("state")

    def event(mark, payload):
        return candidate(aid, c.generation, n, "s", mark, payload, utc(row.get("ts")), c.run)

    if kind == "state" and isinstance(state, str) and state in STATES:
        if state in TERMINAL or state == "running":
            return event("lane.close" if state in TERMINAL else "lane.open", {"status": state})
        code = "state_" + state
    elif kind in ("worker:tool_call", "worker:tool_update"):
        line = row.get("line")
        line = line if isinstance(line, str) else ""
        name = row.get("tool_name")
        if not isinstance(name, str) or name not in TOOL_NAMES:
            m = re.fullmatch(r"\[tool (?:execute|read|edit|search|other)\] ([a-z_]{1,20})", line)
            name = m[1] if m and m[1] in TOOL_NAMES else "unknown"
        status = row.get("tool_status")
        if not isinstance(status, str):
            m = re.match(r"\[tool (completed|failed|cancelled|in_progress|pending)\](?: |$)", line)
            status = m[1] if m else None
        call = row.get("tool_id")
        call = call if isinstance(call, str) and SAFE_CALL.fullmatch(call) else f"unpaired:{n}"
        start = kind == "worker:tool_call" and status not in OUTCOME
        if start or status in OUTCOME:
            return event(
                "tool.start" if start else "tool.finish",
                {
                    "call_id": call,
                    "tool_name": name,
                    "command_family": None,
                    "outcome": "pending" if start else OUTCOME[status],
                    "summary": "Tool call observed" if start else "Tool completion observed",
                    "evidence_ref": f"{aid}:g{c.generation}:s{n}",
                },
            )
        code = "tool_update"  # no fabricated finish for an in-progress/ambiguous update
    else:
        code = kind.replace(":", "_") if kind in OBSERVED else "opaque_record"
    return diagnostic(aid, c, "source.checkpoint", code, n, n, "s", n)


@dataclass
class Cursor:
    generation: int = 1
    offset: int = 0
    file_id: str = ""
    anchor: str = ""
    seq: int = 0
    run: int = 0
    oversize: bool = False
    oversize_start: int = 0
    damaged: bool = False

    @classmethod
    def decode(cls, value):
        if value is None:
            return cls()
        if not isinstance(value, str) or len(value) > 1024:
            raise ValueError("invalid_cursor")
        c = cls(**json.loads(value, object_pairs_hook=_unique))
        if (
            not all(
                type(v) is int and 0 <= v <= MAX_INT
                for v in (c.generation, c.offset, c.run, c.seq, c.oversize_start)
            )
            or not re.fullmatch(r"[0-9:]{1,128}", c.file_id)
            or not re.fullmatch(r"[a-f0-9]{64}", c.anchor)
            or type(c.oversize) is not bool
            or type(c.damaged) is not bool
        ):
            raise ValueError("invalid_cursor")
        return c


def _anchor(f, end):
    # Private replacement detector only (not exported, not evidence). Works when
    # Windows stat lacks a useful file index, including same-size replacements.
    length = min(end, 256)
    f.seek(0)
    head = f.read(length)
    f.seek(end - length)
    return hashlib.sha256(head + f.read(length)).hexdigest()


def _unique(pairs):
    row = dict(pairs)
    if len(row) != len(pairs):
        raise ValueError("duplicate key")
    return row


class LogSource:
    def __init__(self, path, aid):
        if not isinstance(aid, str) or not ATTEMPT.fullmatch(aid):
            raise ValueError("unsupported attempt identity")
        self.path, self.aid = path, aid
        self._last_success = None
        self._state = "unavailable"
        self._error = "not_read"

    def describe(self):
        return {
            "schema": "rizzstack.rails-describe/1",
            "adapter_id": "launcher-log",
            "version": "1",
            "source_instance": "worker-1",
            "capabilities": ["bounded_replay"],
            "supported_formats": ["jsonl"],
        }

    def health(self):
        return {
            "schema": "rizzstack.rails-source-health/1",
            "state": self._state,
            "last_success": self._last_success,
            "error_code": self._error,
        }

    def read_batch(self, cursor=None, limit=500, byte_limit=BATCH_BYTES):
        if (
            type(limit) != int
            or not 1 <= limit <= 500
            or type(byte_limit) != int
            or not 1 <= byte_limit <= BATCH_BYTES
        ):
            raise ValueError("invalid source bounds")
        c = Cursor.decode(cursor)
        rows = []
        # The caller limits both the source bytes examined and normalized
        # candidate bytes. Never silently advance over a fact that didn't fit.
        output_left = byte_limit

        def offer(mark):
            nonlocal output_left
            size = len(json.dumps(mark, separators=(",", ":"), ensure_ascii=True))
            if size > output_left:
                if not rows:
                    raise ValueError("byte_limit")
                return False
            rows.append(mark)
            output_left -= size
            return True

        partial = False
        self._state = "stale" if self._last_success else "unavailable"
        self._error = "source_unavailable"
        # Source logs are configured local regular files, never a symlink/pipe.
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
        ):
            raise ValueError("invalid_source")
        with self.path.open("rb") as f:
            st = os.fstat(f.fileno())
            fid = f"{st.st_dev}:{st.st_ino}:{getattr(st, 'st_birthtime_ns', 0)}"
            if c.file_id and (fid != c.file_id or st.st_size < c.offset or _anchor(f, c.offset) != c.anchor):
                c = Cursor(generation=c.generation + 1, damaged=True)
                offer(diagnostic(self.aid, c, "source.gap", "log_replaced", 0, 0, "rotation", 0))
            c.file_id = fid
            f.seek(c.offset)
            remaining = byte_limit
            while len(rows) < limit and remaining and f.tell() < st.st_size:
                pos = f.tell()
                chunk = f.readline(min(remaining, RECORD_BYTES + 1, st.st_size - pos))
                remaining -= len(chunk)
                if not chunk.endswith(b"\n"):
                    if len(chunk) > RECORD_BYTES or c.oversize:
                        if not c.oversize:
                            c.oversize_start = pos
                        c.oversize = True
                        c.offset = f.tell()
                        partial = c.offset == st.st_size
                        continue
                    partial = f.tell() == st.st_size
                    if not rows and c.offset == pos and not partial and byte_limit <= RECORD_BYTES:
                        raise ValueError("byte_limit")
                    break
                row = None
                if not c.oversize and len(chunk) <= RECORD_BYTES:
                    try:
                        row = json.loads(chunk.decode("utf-8"), object_pairs_hook=_unique)
                    except (ValueError, RecursionError):
                        pass
                n = row.get("lseq") if isinstance(row, dict) else None
                if type(n) is not int or not 1 <= n <= MAX_INT:
                    pos = c.oversize_start if c.oversize else pos
                    if not offer(
                        diagnostic(self.aid, c, "source.error", "invalid_row", pos, pos, "byte", pos)
                    ):
                        break
                    c.damaged = True
                else:
                    if n > c.seq + 1:
                        if not offer(
                            diagnostic(self.aid, c, "source.gap", "missing_lseq", c.seq + 1, n - 1, "gap", n)
                        ):
                            break
                        c.seq = n - 1
                        c.damaged = True
                        if len(rows) == limit:
                            break
                    old_run = c.run
                    if row.get("kind") == "state" and row.get("state") == "running":
                        c.run = n
                    if not offer(normalize(self.aid, c, row, n)):
                        c.run = old_run
                        break
                    c.seq = max(c.seq, n)
                c.offset, c.oversize, c.oversize_start = f.tell(), False, 0
            c.anchor = _anchor(f, c.offset)
            warnings = ["partial_journal"] if partial or c.oversize or c.damaged else []
            self._state, self._error = ("stale", warnings[0]) if warnings else ("live", None)
            self._last_success = (
                datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
            )
            return {
                "schema": "rizzstack.rails-read-batch-result/1",
                "records": rows,
                "next_cursor": json.dumps(asdict(c), separators=(",", ":")),
                "has_more": c.offset < st.st_size and not partial,
                "source_head": str(st.st_size),
                "diagnostics": warnings,
            }
