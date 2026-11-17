"""Read-only worker-origin replication over the existing launcher listener."""

from contextlib import closing, contextmanager
import hmac
import ipaddress
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import parse_qsl

from rails_emitter import contracts, privacy
from rails_log_source import (
    ATTEMPT,
    BATCH_BYTES,
    MAX_INT,
    OBSERVED,
    RECORD_BYTES,
    SAFE_CALL,
    STATES,
    TOOL_NAMES,
)

_CHAIN = re.compile(r"m-[0-9a-f]{12}-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_CODES = {
    "invalid_row",
    "missing_lseq",
    "log_replaced",
    "opaque_record",
    "bytes_consumed",
    "tool_update",
    *(kind.replace(":", "_") for kind in OBSERVED),
    *("state_" + s for s in STATES),
}


class ExportError(Exception):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status

    def response(self):
        return {
            "ok": False,
            "error": {"code": self.code, "retryable": self.status == 503, "request_id": uuid.uuid4().hex},
        }


def _require(condition):
    if not condition:
        raise ExportError("source_fork")


def _count(v):
    return type(v) is int and 0 <= v <= MAX_INT


def _safe_envelope(e, seq, chain, prev, indexed_hash, models):
    # The replication page cannot redact in place: changing even one character
    # invalidates the origin hash. Refuse unexpected content rather than export it.
    _require(
        set(e)
        == {
            "schema",
            "seq",
            "chain_id",
            "event_id",
            "dedupe_key",
            "source",
            "scope",
            "lane_id",
            "parent_lane_id",
            "parent_event_id",
            "kind",
            "occurred_at",
            "recorded_at",
            "duration_ms",
            "timing_quality",
            "authority",
            "redaction_policy",
            "payload",
            "prev_hash",
            "hash",
        }
    )
    _require(
        e["schema"] == contracts.SCHEMA
        and type(e["seq"]) is int
        and e["seq"] == seq
        and e["event_id"] == f"{chain}:{seq}"
        and e["chain_id"] == chain
        and e["prev_hash"] == prev
        and e["hash"] == indexed_hash
        and indexed_hash == contracts.rails_hash(prev, {k: v for k, v in e.items() if k != "hash"})
    )
    _require(
        e["authority"] == "worker_report"
        and e["redaction_policy"] == "rails-safe/1"
        and e["duration_ms"] is None
        and e["timing_quality"] == "unknown"
        and contracts.STAMP_RE.fullmatch(e["recorded_at"])
        and (e["occurred_at"] is None or contracts.STAMP_RE.fullmatch(e["occurred_at"]))
    )
    aid = e["scope"]["attempt_id"]
    _require(
        ATTEMPT.fullmatch(aid) and e["scope"] == dict(dict.fromkeys(contracts.SCOPE_FIELDS), attempt_id=aid)
    )
    src = e["source"]
    rid = src["record_id"]
    _require(
        src == {"adapter": "launcher-log", "instance": "worker-1", "record_id": rid, "revision": "v1"}
        and re.fullmatch(aid + r":g\d{1,16}:(?:s|byte|offset|gap|rotation|receipt)\d{1,16}", rid)
    )
    lane = e["lane_id"]
    _require(
        re.fullmatch(aid + r":g\d{1,16}:r\d{1,16}", lane)
        and e["parent_lane_id"] is None
        and e["parent_event_id"] is None
    )
    p, kind = e["payload"], e["kind"]
    _require(e["dedupe_key"] == (aid + ":final" if kind == "usage.observed" else rid))
    if kind in ("tool.start", "tool.finish"):
        start = kind == "tool.start"
        _require(
            set(p) == {"call_id", "tool_name", "command_family", "outcome", "summary", "evidence_ref"}
            and (SAFE_CALL.fullmatch(p["call_id"]) or re.fullmatch(r"unpaired:\d{1,16}", p["call_id"]))
            and p["tool_name"] in TOOL_NAMES | {"unknown"}
            and p["command_family"] is None
            and p["outcome"] in (("pending",) if start else ("ok", "failed", "cancelled"))
            and p["summary"] == ("Tool call observed" if start else "Tool completion observed")
            and p["evidence_ref"] == rid
        )
    elif kind in ("source.checkpoint", "source.error", "source.gap"):
        _require(
            set(p) == {"range_start", "range_end", "source_id", "cursor", "code", "count", "digest"}
            and p["code"] in _CODES
            and p["source_id"] == "launcher-log"
            and all(_count(p[k]) for k in ("range_start", "range_end", "count"))
            and p["cursor"] == str(p["range_end"])
            and contracts.HEX64_RE.fullmatch(p["digest"])
        )
    elif kind in ("lane.open", "lane.close"):
        _require(set(p) == {"status"} and p["status"] in STATES)
    elif kind == "usage.observed":
        _require(
            set(p)
            == {
                "usage_record_id",
                "metric_scope",
                "usage_basis",
                "provider",
                "model",
                "input_tokens",
                "output_tokens",
                "thinking_tokens",
            }
            and p["usage_record_id"] == aid + ":final"
            and p["metric_scope"] == aid
            and p["usage_basis"] == "final"
            and p["provider"] in (None, "go", "deepseek", "zoo", "free", "agy")
            and (p["model"] is None or p["model"] in models)
            and all(
                p[k] is None or _count(p[k]) for k in ("input_tokens", "output_tokens", "thinking_tokens")
            )
        )
    else:
        raise ExportError("source_fork")
    _require(privacy.scrub_structure(e) == e)


@contextmanager
def _snapshot(path):
    try:
        path = Path(path).absolute()
        if str(path).startswith(("//", "\\\\")):
            raise ExportError("source_unavailable", 503)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            identity = conn.execute("SELECT value FROM ledger_meta WHERE key='chain_id'").fetchone()
            _require(identity and _CHAIN.fullmatch(identity[0]))
            chain = identity[0]
            head = conn.execute(
                "SELECT seq,rails_hash FROM event_rows WHERE chain_id=? ORDER BY seq DESC LIMIT 1", (chain,)
            ).fetchone()
            _require(
                head is None or _count(head[0]) and head[0] > 0 and contracts.HEX64_RE.fullmatch(head[1])
            )
            yield conn, chain, {"seq": head[0], "hash": head[1]} if head else None
    except (sqlite3.Error, OSError):
        raise ExportError("source_unavailable", 503) from None
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, contracts.RailsRefusal):
        raise ExportError("source_fork") from None


def export_head(path):
    with _snapshot(path) as (_, chain, head):
        return {"chain_id": chain, "head": head}


def export_page(path, after=0, limit=100, *, models=()):
    if not _count(after) or type(limit) is not int or not 1 <= limit <= 500:
        raise ExportError("invalid_request", 400)
    with _snapshot(path) as (db, chain, head):
        last = head["seq"] if head else 0
        if after > last:
            raise ExportError("reset_required")
        prev = "genesis"
        if after:
            prior = db.execute(
                "SELECT rails_hash FROM event_rows WHERE chain_id=? AND seq=?", (chain, after)
            ).fetchone()
            if not prior:
                raise ExportError("source_gap")
            prev = prior[0]
        rows, size, cursor = [], 1024, after  # reserve bounded wrapping metadata
        # Limit the bytes fetched per record too; corrupt/off-policy rows fail
        # closed rather than materializing an arbitrary SQLite string.
        for n, digest, raw in db.execute(
            "SELECT seq,rails_hash,substr(envelope_json,1,?) FROM event_rows "
            "WHERE chain_id=? AND seq>? ORDER BY seq LIMIT ?",
            (RECORD_BYTES + 1, chain, after, limit),
        ):
            if n != cursor + 1:
                raise ExportError("source_gap")
            _require(len(raw) <= RECORD_BYTES)
            if size + len(raw) + 1 > BATCH_BYTES:
                break
            e = json.loads(raw)
            _require(contracts.canonicalize(e).decode("ascii") == raw)
            _safe_envelope(e, n, chain, prev, digest, models)
            rows.append(e)
            size += len(raw) + 1
            prev, cursor = digest, n
        return {
            "schema": "rizzstack.rails-export/1",
            "chain_id": chain,
            "after": after,
            "events": rows,
            "next_cursor": cursor,
            "head": head,
            "has_more": cursor < last,
        }


class ReadAccess:
    def __init__(self, cfg, admin):
        self.token, self.peers = b"", frozenset()
        if cfg.get("rails_enabled") is not True:
            return
        try:
            path = os.environ.get("HYVV_RAILS_READ_TOKEN_FILE") or cfg["rails_read_token_file"]
            with Path(path).open("rb") as f:
                raw = f.read(514)
            token = raw.rstrip(b"\r\n")
            if hmac.compare_digest(token, admin.encode()):
                raise RuntimeError("Rails replication requires a distinct read credential")
            if len(raw) > 513 or not re.fullmatch(rb"[!-~]{32,512}", token):
                raise ValueError()
            peers = cfg["rails_read_peers"]
            if not isinstance(peers, list) or not peers:
                raise ValueError()
            self.peers = frozenset(ipaddress.ip_address(p) for p in peers)
            self.token = token
        except (ValueError, OSError, KeyError, TypeError):
            pass  # export unavailable; invalid config never weakens admin auth

    def authorize(self, handler):
        headers = handler.headers
        values = headers.get_all("Authorization") or []
        if (
            not self.token
            or len(values) != 1
            or not values[0].startswith("Bearer ")
            or not hmac.compare_digest(values[0][7:].encode(), self.token)
        ):
            raise ExportError("auth_required", 401)
        if ipaddress.ip_address(handler.client_address[0]) not in self.peers:
            raise ExportError("forbidden", 403)
        host, port = handler.connection.getsockname()[:2]
        host = "[" + host + "]" if ":" in host else host
        authorities = {host + ":" + str(port)}
        if port == 80:
            authorities.add(host)
        hosts = headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0] not in authorities or "Origin" in headers:
            raise ExportError("forbidden", 403)


def handle_read(handler, launcher, access, models):
    try:
        access.authorize(handler)
        path, _, query = handler.path.partition("?")
        if path not in ("/v1/rails/events", "/v1/rails/head"):
            raise ExportError("not_found", 404)
        if not launcher.rails:
            raise ExportError("source_unavailable", 503)
        if path.endswith("/head"):
            if query:
                raise ExportError("invalid_request", 400)
            data = export_head(launcher.rails.path)
            data.update(origin="available", machine_alias="worker-1", **launcher.rails.health())
        else:
            if len(query) > 128:
                raise ExportError("invalid_request", 400)
            args = {}
            for k, v in parse_qsl(query, strict_parsing=True, keep_blank_values=True, max_num_fields=2):
                if k in args or k not in ("after", "limit") or not re.fullmatch(r"0|[1-9][0-9]{0,15}", v):
                    raise ExportError("invalid_request", 400)
                args[k] = int(v)
            data = export_page(launcher.rails.path, models=models, **args)
        return 200, data
    except ValueError:
        ex = ExportError("invalid_request", 400)
        return ex.status, ex.response()
    except ExportError as ex:
        return ex.status, ex.response()
