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
      "schema": "rizzstack.bee-landed-export-index/1",
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
import threading
import uuid
from pathlib import Path

try:  # pragma: no cover - POSIX is the supported deployment platform
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

EXPORT_SCHEMA = "rizzstack.bee-landed-export/1"
EXPORT_INDEX_SCHEMA = "rizzstack.bee-landed-export-index/1"
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


def _canonical_row(path: Path, raw: bytes, event: dict, order_key):
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
        "_order": order_key,
    }


def _scan_outbox(source):
    """Read every complete canonical outbox file in append order.

    Files are ordered by their durable write timestamp and name.  An event that
    was written before the index existed (or before a crash prevented its
    registration) is therefore appended in the same order it became durable.
    """
    outbox = _resolve_outbox(source)
    try:
        paths = list(outbox.iterdir())
    except (OSError, FileNotFoundError):
        return []

    candidates = []
    for path in paths:
        name = path.name
        if name.startswith(".") or not name.endswith(".json"):
            continue
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
            raw = path.read_bytes()
            event = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            # A torn or non-canonical file is not an exportable event.
            continue
        if not isinstance(event, dict):
            continue
        row = _canonical_row(path, raw, event, (stat.st_mtime_ns, stat.st_ino, name))
        if row is not None:
            candidates.append(row)

    candidates.sort(key=lambda row: row["_order"])
    by_id = {}
    for row in candidates:
        # Canonical outbox filenames are unique; keep the first stable path.
        by_id.setdefault(row["event_id"], row)
    return list(by_id.values())


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
    except (OSError, FileNotFoundError):
        return _empty_index()
    try:
        index = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as ex:
        raise LandingExportError("export index is unreadable") from ex
    if not isinstance(index, dict):
        raise LandingExportError("export index is unreadable")
    if index.get("schema") != EXPORT_INDEX_SCHEMA:
        raise LandingExportError(
            f"unsupported export index schema: {index.get('schema')!r}"
        )
    events = index.get("events")
    if not isinstance(events, list):
        raise LandingExportError("export index is missing its events list")
    try:
        next_position = int(index.get("next_position"))
    except (TypeError, ValueError) as ex:
        raise LandingExportError("export index has an invalid next_position") from ex
    if next_position < 1:
        raise LandingExportError("export index has an invalid next_position")

    clean = []
    seen_ids = set()
    max_position = 0
    for entry in events:
        if not isinstance(entry, dict):
            continue
        event_id = entry.get("event_id")
        if not isinstance(event_id, str) or not event_id or event_id in seen_ids:
            continue
        try:
            position = int(entry.get("position"))
        except (TypeError, ValueError):
            continue
        if position < 1:
            continue
        seen_ids.add(event_id)
        max_position = max(max_position, position)
        clean.append({
            "position": position,
            "event_id": event_id,
            "sha256": entry.get("sha256"),
            "file_sha256": entry.get("file_sha256"),
        })
    clean.sort(key=lambda entry: entry["position"])
    next_position = max(next_position, max_position + 1)
    return {
        "schema": EXPORT_INDEX_SCHEMA,
        "next_position": next_position,
        "head_position": max_position,
        "events": clean,
    }


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
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _reconcile(index: dict, rows):
    """Append never-seen outbox rows at the durable head; keep existing order."""
    by_id = {entry["event_id"]: entry for entry in index["events"]}
    next_position = int(index["next_position"])
    changed = False
    for row in rows:
        existing = by_id.get(row["event_id"])
        if existing is None:
            entry = {
                "position": next_position,
                "event_id": row["event_id"],
                "sha256": row["sha256"],
                "file_sha256": row["file_sha256"],
            }
            index["events"].append(entry)
            by_id[row["event_id"]] = entry
            next_position += 1
            changed = True
        else:
            if (existing.get("sha256") != row["sha256"] or
                    existing.get("file_sha256") != row["file_sha256"]):
                existing["sha256"] = row["sha256"]
                existing["file_sha256"] = row["file_sha256"]
                changed = True
    if changed:
        index["events"].sort(key=lambda entry: entry["position"])
        index["next_position"] = next_position
        index["head_position"] = max(
            (entry["position"] for entry in index["events"]), default=0
        )
    return changed


def _state(source):
    """Load+reconcile the durable index and return it with fresh row content."""
    data_dir = _resolve_data_dir(source)
    index_file = data_dir / "events" / "export-index.json"
    with _index_lock(source):
        index = _load_index(index_file)
        rows = _scan_outbox(source)
        changed = _reconcile(index, rows)
        if changed:
            _write_index(index_file, index)
        by_id = {row["event_id"]: row for row in rows}
        return index, by_id


def register_landed_file(source, path=None):
    """Register a durable outbox file in append order.

    Called by the launcher immediately after an outbox file is persisted.  The
    registration is idempotent: a file that is already indexed keeps its
    original position, and a crash before this call is recovered by the next
    export's outbox scan.
    """
    _state(source)


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
    index, rows = _state(source)
    head_position = index["head_position"]
    if after_position is not None and after_position > head_position:
        raise ExpiredCursorError(
            f"expired after cursor {after_position}: beyond the durable export head {head_position}"
        )

    if after_position is None:
        if head_position == 0 and not index["events"]:
            return {
                "schema": EXPORT_SCHEMA,
                "events": [],
                "next_cursor": None,
                "has_more": False,
                "retention": dict(RETENTION),
            }
        after_position = 0

    available = []
    for entry in index["events"]:
        if entry["position"] <= after_position:
            continue
        row = rows.get(entry["event_id"])
        if row is None:
            # The file may have been removed by an operator; its position is
            # still consumed so remaining events keep their durable order.
            continue
        available.append((entry, row))

    page_entries = available[:page_limit]
    has_more = len(available) > len(page_entries)
    page = []
    for entry, row in page_entries:
        page.append({
            "cursor": entry["position"],
            "event_id": entry["event_id"],
            "sha256": row["sha256"],
            "file_sha256": row["file_sha256"],
            "event": row["event"],
        })
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
