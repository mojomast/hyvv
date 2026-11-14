"""Authenticated export of the durable canonical bee-landed outbox.

The exporter is deliberately a read-only view of ``<data_dir>/events/outbox``.
It never rebuilds an event from attempt journals or attempt event logs, never
silently rewrites an event, and never deletes one.  The cursor is the canonical
CloudEvents id (which is also the outbox file stem), so ordering and ``after``
pagination are stable across process restarts: the same immutable files always
produce the same lexicographic sequence.

Wire shape for ``GET /v1/landing/events``::

    {
      "schema": "rizzstack.bee-landed-export/1",
      "events": [{"cursor": ..., "event_id": ..., "sha256": ..., "event": ...}],
      "next_cursor": ...,
      "has_more": false,
      "retention": {...}
    }

``sha256`` is the SHA-256 of the exact bytes stored on disk.  A client can use
it to prove the exported ``event`` is byte-identical to the producer outbox.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

EXPORT_SCHEMA = "rizzstack.bee-landed-export/1"
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


class LandingExportError(ValueError):
    """A caller-supplied pagination parameter is invalid."""


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


def landed_outbox(source) -> Path:
    """Return the canonical outbox directory for a data directory or launcher."""
    return _resolve_outbox(source)


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
    if after is None:
        return None
    if isinstance(after, int) and not isinstance(after, bool):
        after = str(after)
    if not isinstance(after, str):
        raise LandingExportError("after must be a cursor string")
    after = after.strip()
    if not after or after == "0":
        return None
    if len(after) > MAX_CURSOR_LENGTH:
        raise LandingExportError("after cursor is too long")
    if any(ord(ch) < 0x20 for ch in after):
        raise LandingExportError("after cursor contains control characters")
    return after


def _read_entries(source):
    """Read every complete canonical outbox file; never consult attempt logs."""
    outbox = _resolve_outbox(source)
    try:
        paths = sorted(outbox.iterdir(), key=lambda p: p.name)
    except (OSError, FileNotFoundError):
        return []

    entries = []
    by_cursor = {}
    for path in paths:
        name = path.name
        if name.startswith(".") or not name.endswith(".json"):
            continue
        try:
            if path.is_symlink() or not path.is_file():
                continue
            raw = path.read_bytes()
            event = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            # A torn or non-canonical file is not an exportable event.
            continue
        if not isinstance(event, dict):
            continue
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id.strip():
            # Legacy/canonical-shaped files may omit the envelope id; anything
            # else is not a landed event and must not be exported as one.
            data = event.get("data")
            canonical_shape = (event.get("type") == LANDED_EVENT_TYPE or
                               (isinstance(data, dict) and data.get("schema") == LANDED_DATA_SCHEMA))
            if not canonical_shape:
                continue
            event_id = path.stem
        event_id = event_id.strip()
        if not event_id:
            continue
        cursor = event_id
        if cursor in by_cursor:
            # Canonical outbox filenames are unique; keep the first stable path.
            continue
        row = {
            "cursor": cursor,
            "event_id": event_id,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "event": event,
        }
        by_cursor[cursor] = row
        entries.append(row)
    entries.sort(key=lambda row: row["cursor"])
    return entries


def export_events(source, after=None, limit=None):
    """Return one bounded, restart-stable page of the durable canonical outbox.

    ``after`` is the last cursor the caller already consumed.  It is compared to
    the stable cursor sequence with a strict "greater than" rule.  An empty page
    keeps ``after`` as ``next_cursor`` (or ``None`` for a fresh empty outbox) and
    sets ``has_more`` to false, so a tailing caller can resume without replaying.
    """
    after_cursor = _normalize_after(after)
    page_limit = _normalize_limit(limit)
    entries = _read_entries(source)
    if after_cursor is not None:
        entries = [row for row in entries if row["cursor"] > after_cursor]
    page = entries[:page_limit]
    has_more = len(entries) > len(page)
    next_cursor = page[-1]["cursor"] if page else after_cursor
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
