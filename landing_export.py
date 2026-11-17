"""Authenticated export of the durable canonical bee-landed outbox.

The exporter is deliberately a read-only view of ``<data_dir>/events/outbox``.
It never rebuilds an event from attempt journals or attempt event logs, never
silently rewrites an event, and never deletes one.

Pagination cursor
-----------------
The cursor is a durable, monotonically increasing integer *export position*,
not the canonical CloudEvents id.  Positions are assigned in outbox
append/registration order, so an event appended later is always exported after
the current cursor even when its canonical id sorts lower.

The position is persisted in ``<data_dir>/events/export-index.json``::

    {
      "schema": "rizzstack.bee-landed-export-index/2",
      "next_position": 4,
      "head_position": 3,
      "events": [
        {"position": 1, "event_id": "...", "sha256": "...",
         "file_sha256": "..."},
        ...
      ]
    }

Index updates are serialized with a lock file and published with an atomic
replace.  Each writer registers its event immediately after the immutable
outbox file is durable; if a crash lands between those two steps, the next
export scans the outbox and appends the unindexed file to the end of the index
without duplicating or reordering existing positions.

Wire shape for ``GET /v1/landing/events``::

    {
      "schema": "rizzstack.bee-landed-export/1",
      "events": [{"cursor": ..., "event_id": ..., "sha256": ..., "event": ...}],
      "next_cursor": ...,
      "has_more": false,
      "retention": {...}
    }

``sha256`` is the SHA-256 of the canonical CloudEvent content (sorted keys,
compact separators, trailing newline). ``file_sha256`` is the SHA-256 of the
exact stored outbox file, retained as an auditable file identity.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import threading
import uuid
from bisect import bisect_right
from operator import itemgetter
from pathlib import Path

if os.name == "nt":  # pragma: no cover - exercised by the Windows CI job
    import msvcrt
else:
    import fcntl

EXPORT_SCHEMA = "rizzstack.bee-landed-export/1"
_LEGACY_INDEX_SCHEMA = "rizzstack.bee-landed-export-index/1"
EXPORT_INDEX_SCHEMA = "rizzstack.bee-landed-export-index/2"
RETENTION_SCHEMA = "rizzstack.bee-landed-retention/1"
LANDED_EVENT_TYPE = "rizzstack.bee.landed.v1"
LANDED_DATA_SCHEMA = "rizzstack.bee-landed/1"

DEFAULT_LIMIT = 500
MIN_LIMIT = 1
MAX_LIMIT = 500
MAX_CURSOR_LENGTH = 512

# Retention is a property of the durable outbox, not of one page.  There is no
# automatic expiry or deletion anywhere in the launcher: an operator may remove
# old outbox files, but the export endpoint itself is append-only and read-only.
RETENTION = {
    "schema": RETENTION_SCHEMA,
    "mode": "append-only",
    "policy": "unbounded",
    "source": "events/outbox",
    "deletes": False,
    "delete_on_export": False,
    # Atomic publication/restart replay does not attest host power-loss survival.
    "power_loss_durability": "unproven",
    "max_events": None,
    "max_age_seconds": None,
    "empty_page": "No stored canonical event is at or after the cursor.",
    "description": (
        "Canonical outbox files are retained until an operator removes them. "
        "Export never deletes, rewrites, expires, or reconstructs events from "
        "attempt logs or journals."
    ),
}

_LEGACY_HASH = re.compile(r"^[0-9a-fA-F]{32,128}$")
_LEGACY_EVENT_PREFIXES = ("urn:rizzstack:bee-landed:",)
_THREAD_INDEX_LOCK = threading.RLock()


class LandingExportError(ValueError):
    """A caller-supplied pagination parameter is invalid."""

    code = "invalid_cursor"


class MalformedCursorError(LandingExportError):
    """The cursor is not a scalar non-negative integer export position."""

    code = "malformed_cursor"


class ExpiredCursorError(LandingExportError):
    """The cursor is beyond the durable head of the export index."""

    code = "cursor_expired"


class LegacyCursorError(LandingExportError):
    """The cursor is a pre-migration event-id/hash cursor."""

    code = "legacy_cursor"


class LandingContentConflict(ValueError):
    """The same export position was observed with different canonical content."""

    code = "landing_content_conflict"

    def __init__(self, position):
        super().__init__(f"canonical landing content conflicts at position {position}")
        self.position = position


def _resolve_outbox(source) -> Path:
    """Accept a data directory (normal) or an explicit outbox directory (tests)."""
    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
    else:
        path = getattr(source, "dir", None)
        if path is None:
            path = getattr(source, "data_dir", None)
        if path is None:
            raise LandingExportError("landing export needs a data_dir or outbox path")
        path = Path(path)
    if path.name == "outbox":
        return path
    return path / "events" / "outbox"


def _resolve_data_dir(source) -> Path:
    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
    else:
        path = getattr(source, "dir", None)
        if path is None:
            path = getattr(source, "data_dir", None)
        if path is None:
            raise LandingExportError("landing export needs a data_dir or outbox path")
        path = Path(path)
    if path.name == "outbox":
        return path.parent.parent if path.parent.name == "events" else path.parent
    return path


def landed_outbox(source) -> Path:
    """Return the canonical outbox directory for a data directory or launcher."""
    return _resolve_outbox(source)


def export_index_path(source) -> Path:
    """Return the durable export index path for a data directory/launcher."""
    return _resolve_data_dir(source) / "events" / "export-index.json"


def _normalize_limit(limit) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if isinstance(limit, bool):
        raise LandingExportError(f"limit must be an integer between {MIN_LIMIT} and {MAX_LIMIT}")
    if isinstance(limit, str):
        text = limit.strip()
        try:
            value = int(text, 10)
        except (TypeError, ValueError) as ex:
            raise LandingExportError(f"limit must be an integer between {MIN_LIMIT} and {MAX_LIMIT}") from ex
    elif isinstance(limit, int):
        value = limit
    else:
        raise LandingExportError(f"limit must be an integer between {MIN_LIMIT} and {MAX_LIMIT}")
    if not MIN_LIMIT <= value <= MAX_LIMIT:
        raise LandingExportError(f"limit must be an integer between {MIN_LIMIT} and {MAX_LIMIT}")
    return value


def _normalize_after(after):
    """Parse the scalar export position, rejecting legacy and malformed cursors."""
    if after is None:
        return None
    if isinstance(after, bool):
        raise MalformedCursorError("malformed after cursor: must be a non-negative integer export position")
    if isinstance(after, int):
        value = after
    elif isinstance(after, str):
        text = after.strip()
        if not text:
            return None
        if len(text) > MAX_CURSOR_LENGTH:
            raise MalformedCursorError("malformed after cursor: cursor is too long")
        if any(ord(ch) < 0x20 for ch in text):
            raise MalformedCursorError("malformed after cursor: contains control characters")
        if _LEGACY_HASH.fullmatch(text) or text.startswith(_LEGACY_EVENT_PREFIXES):
            raise LegacyCursorError(
                "after cursor is a legacy event-id/hash cursor; restart from the beginning "
                "of the durable export position sequence"
            )
        if not re.fullmatch(r"[0-9]+", text):
            raise MalformedCursorError(
                "malformed after cursor: must be a non-negative integer export position"
            )
        value = int(text, 10)
    else:
        raise MalformedCursorError("malformed after cursor: must be a non-negative integer export position")
    if value < 0:
        raise MalformedCursorError("malformed after cursor: must be a non-negative integer export position")
    return value


def _read_row(path: Path):
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        raw = path.read_bytes()
        event = json.loads(raw.decode("utf-8"))
    except (FileNotFoundError, ValueError):
        # Removed or malformed files are not events. Other I/O failures must
        # abort the operation before a cursor can pass an unreadable event.
        return None
    if not isinstance(event, dict):
        return None
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        # Legacy/canonical-shaped files may omit the envelope id; anything else
        # is not a landed event and must not be exported as one.
        data = event.get("data")
        canonical_shape = (event.get("type") == LANDED_EVENT_TYPE or
                           (isinstance(data, dict) and data.get("schema") == LANDED_DATA_SCHEMA))
        if not canonical_shape:
            return None
        event_id = path.stem
    event_id = event_id.strip()
    if not event_id:
        return None
    canonical = (
        json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        .encode("utf-8")
        + b"\n"
    )
    return {
        "event_id": event_id,
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "event": event,
    }


def _outbox_files(source):
    """Discover local regular files in the pre-index/crash recovery order."""
    outbox = _resolve_outbox(source)
    try:
        paths = list(outbox.iterdir())
    except FileNotFoundError:
        return []

    candidates = []
    for path in paths:
        name = path.name
        if name.startswith(".") or not name.endswith(".json"):
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode):
            candidates.append((path, info))
    return sorted(candidates, key=lambda item: (item[1].st_mtime_ns, item[1].st_ino, item[0].name))


def _empty_index():
    return {
        "schema": EXPORT_INDEX_SCHEMA,
        "next_position": 1,
        "head_position": 0,
        "events": [],
    }


def _load_index(path: Path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        # First use only: never renumber events because a read failed.
        return _empty_index()
    try:
        index = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as ex:
        raise OSError("export index is unreadable") from ex
    if not isinstance(index, dict):
        raise OSError("export index is unreadable")
    legacy = index.get("schema") == _LEGACY_INDEX_SCHEMA
    if not legacy and index.get("schema") != EXPORT_INDEX_SCHEMA:
        raise OSError(
            f"unsupported export index schema: {index.get('schema')!r}"
        )
    events = index.get("events")
    if not isinstance(events, list):
        raise OSError("export index is missing its events list")
    next_position = index.get("next_position")
    if type(next_position) is not int or next_position < 1:
        raise OSError("export index has an invalid next_position")

    clean = []
    seen_ids = set()
    seen_positions = set()
    max_position = 0
    for entry in events:
        if not isinstance(entry, dict):
            raise OSError("invalid export index entry")
        event_id = entry.get("event_id")
        if not isinstance(event_id, str) or not event_id or event_id in seen_ids:
            raise OSError("invalid or duplicate export event id")
        position = entry.get("position")
        if type(position) is not int or position < 1 or position in seen_positions:
            raise OSError("invalid or duplicate export position")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            # All /1 writers stored this commitment. A filename or raw hash
            # cannot reconstruct what an earlier consumer was served.
            raise OSError("export position has no valid canonical digest")
        seen_ids.add(event_id)
        seen_positions.add(position)
        max_position = max(max_position, position)
        clean.append({
            "position": position,
            "event_id": event_id,
            "sha256": digest,
            "file_sha256": entry.get("file_sha256"),
        })
        # Optional cache only. Recheck files once on upgrade from /1.
        hint = entry.get("file")
        if (not legacy and isinstance(hint, list) and len(hint) == 6 and isinstance(hint[0], str)
                and isinstance(entry.get("file_sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", entry["file_sha256"])):
            clean[-1]["file"] = hint
    clean.sort(key=lambda entry: entry["position"])
    if next_position <= max_position or type(index.get("head_position")) is not int or index["head_position"] != max_position:
        raise OSError("inconsistent export index head")
    validated = {
        "schema": EXPORT_INDEX_SCHEMA,
        "next_position": next_position,
        "head_position": max_position,
        "events": clean,
    }
    if legacy:
        # Under the index lock, fence older writers before rediscovery. Keep
        # their last recorded commitments; superseded history is unknowable.
        _write_index(path, validated)
    return validated


def _write_index(path: Path, index: dict):
    events_dir = path.parent
    events_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(index, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    payload = (payload + "\n").encode("utf-8")
    tmp = events_dir / (f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(events_dir)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _fsync_dir(path: Path):
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@contextlib.contextmanager
def _index_lock(source):
    data_dir = _resolve_data_dir(source)
    events_dir = data_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    lock_path = events_dir / "export-index.lock"
    with _THREAD_INDEX_LOCK:
        with open(lock_path, "a+b") as handle:
            if os.name == "nt":
                # Windows allows a byte-range lock beyond EOF. Every process
                # locks byte 0, even for an empty or pre-existing lock file.
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _pin_conflicting_prefix(source, index, position, first_new):
    if position >= first_new:
        # First discovery can itself contain divergent aliases. Keep the first
        # observed commitment, but don't assign later positions or issue a page.
        prefix = [e for e in index["events"] if e["position"] <= position]
        _write_index(export_index_path(source), dict(index, events=prefix,
                     head_position=position, next_position=position + 1))


def _reconcile(source, index, *, use_hints=True):
    """Append discovered IDs and locate retained files. Never open a path from the index."""
    by_id = {entry["event_id"]: entry for entry in index["events"]}
    by_name = {entry["file"][0]: entry for entry in index["events"] if "file" in entry}
    retained = {}
    present = set()
    changed = False
    first_new = index["next_position"]
    for path, info in _outbox_files(source):
        present.add(path.name)
        hint = [path.name, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        cached = by_name.get(path.name)
        if use_hints and cached is not None and cached["file"] == hint:
            retained.setdefault(cached["event_id"], path)
            continue
        row = _read_row(path)
        if row is None:
            continue
        event_id = row["event_id"]
        entry = by_id.get(event_id)
        if entry is None:
            position = index["next_position"]
            entry = {"event_id": event_id, "position": position, "sha256": row["sha256"]}
            index["events"].append(entry)
            by_id[event_id] = entry
            index["head_position"] = position
            index["next_position"] = position + 1
        elif entry["sha256"] != row["sha256"]:
            position = entry["position"]
            _pin_conflicting_prefix(source, index, position, first_new)
            raise LandingContentConflict(position)
        if event_id in retained:
            continue  # Same canonical content in another file.
        retained[event_id] = path
        evidence = {"file": hint, "file_sha256": row["file_sha256"]}
        if any(entry.get(k) != v for k, v in evidence.items()):
            entry.update(evidence)
            changed = True
    for entry in index["events"]:
        if entry["event_id"] not in retained and "file" in entry and entry["file"][0] in present:
            # The name was repurposed. Drop only the hint; mere removal does
            # not require an index write, and positions are never reclaimed.
            del entry["file"]
            changed = True
    return retained, changed


def register_landed_file(source, path=None):
    """Register a durable outbox file in append order.

    Called by the launcher immediately after an outbox file is persisted.  The
    registration is idempotent: a file that is already indexed keeps its
    original position, and a crash before this call is recovered by the next
    export's outbox scan.
    """
    index_file = export_index_path(source)
    with _index_lock(source):
        index = _load_index(index_file)
        _, changed = _reconcile(source, index)
        if changed:
            _write_index(index_file, index)


def export_events(source, after=None, limit=None):
    """Return one bounded, restart-stable page of the durable canonical outbox.

    ``after`` is the last scalar export position the caller already consumed.
    Positions are independent of canonical event ids and are compared with a
    strict "greater than" rule.  A cursor beyond the durable head, a malformed
    cursor, and a pre-migration event-id/hash cursor are explicit errors; an
    unknown cursor is never answered with an empty 200 page.
    """
    after_position = _normalize_after(after)
    page_limit = _normalize_limit(limit)
    index_file = export_index_path(source)
    with _index_lock(source):
        # On a concurrent removal/replacement (or stale file hint), rediscover
        # once from bytes. Never serve old evidence or advance past an I/O error.
        for use_hints in (True, False):
            index = _load_index(index_file)
            first_new = index["next_position"]
            retained, changed = _reconcile(source, index, use_hints=use_hints)
            try:
                page = _read_page(index, retained, after_position, page_limit)
            except LandingContentConflict as ex:
                _pin_conflicting_prefix(source, index, ex.position, first_new)
                raise
            if changed:
                _write_index(index_file, index)
            if page is not None:
                if after_position is not None and after_position > index["head_position"]:
                    raise ExpiredCursorError(
                        f"expired after cursor {after_position}: beyond the durable export head {index['head_position']}"
                    )
                return page
        raise OSError("outbox changed during export; retry with the same cursor")


def _read_page(index, retained, after_position, page_limit):
    page = []
    has_more = False
    entries = index["events"]
    start = bisect_right(entries, after_position or 0, key=itemgetter("position"))
    for i in range(start, len(entries)):
        entry = entries[i]
        path = retained.get(entry["event_id"])
        if path is None:
            continue
        row = _read_row(path)
        if row and row["event_id"] == entry["event_id"] and row["sha256"] != entry["sha256"]:
            raise LandingContentConflict(entry["position"])
        if row is None or any(row[k] != entry[k] for k in ("event_id", "sha256", "file_sha256")):
            return None
        if len(page) == page_limit:
            has_more = True
            break
        page.append({"cursor": entry["position"], **row})
    if after_position is None and index["head_position"]:
        after_position = 0
    next_cursor = page[-1]["cursor"] if page else after_position
    return {
        "schema": EXPORT_SCHEMA,
        "events": page,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "retention": dict(RETENTION),
    }


# Compatibility aliases for callers that describe the operation rather than the
# data: they all return the same page shape.
list_events = export_events
list_landed_events = export_events
export_landed_events = export_events
landing_events_page = export_events
page = export_events
