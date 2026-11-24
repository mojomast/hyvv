"""Pure parsers for bee resource samples (resource.sample/v1).

This module never runs docker. It turns already-captured ``docker stats``
JSON lines and ``docker inspect`` State objects into schema rows, and rolls
one attempt's rows into a single attempt_rollup row.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

STATS_ARGV = ("docker", "stats", "--no-stream", "--format", "{{json .}}")
SAMPLE_INTERVAL_S = 10

_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TS_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(Z|[+-][0-9]{2}:[0-9]{2})$"
)
_SIZE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)$")

_SIZE_UNITS = {
    "B": 1,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
    "kB": 1000,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
}

_SOURCES = frozenset({"docker_stats", "cgroup", "proc", "unknown"})


def parse_size(text: Any) -> int | None:
    """Parse a docker size string, using binary for KiB/MiB/GiB/TiB and decimal for kB/KB/MB/GB."""
    if not isinstance(text, str):
        return None
    match = _SIZE_RE.fullmatch(text.strip())
    if match is None:
        return None
    factor = _SIZE_UNITS.get(match.group(2))
    if factor is None:
        return None
    value = float(match.group(1)) * factor
    if value < 0:
        return None
    return int(round(value))


def _parse_percent(text: Any) -> float | None:
    if not isinstance(text, str):
        return None
    value = text.strip()
    if not value.endswith("%"):
        return None
    try:
        number = float(value[:-1])
    except ValueError:
        return None
    if number < 0:
        return None
    return number / 100.0


def _parse_nonnegative_int(text: Any) -> int | None:
    if isinstance(text, bool) or not isinstance(text, (str, int)):
        return None
    try:
        value = int(str(text).strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def _split_size_pair(text: Any) -> tuple[int | None, int | None]:
    if not isinstance(text, str) or "/" not in text:
        return None, None
    left, right = text.split("/", 1)
    return parse_size(left), parse_size(right)


def _id_or_none(value: Any) -> str | None:
    if isinstance(value, str) and _ID_RE.fullmatch(value):
        return value
    return None


def _number_or_none(value: Any, *, maximum: float | None = None) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or (maximum is not None and value > maximum):
        return None
    return value


def _valid_timestamp(value: Any) -> str:
    if isinstance(value, str) and _TS_RE.fullmatch(value):
        return value
    raise ValueError("ts must be an RFC 3339 UTC timestamp")


def _validate_alias(host_alias: Any) -> str:
    if not isinstance(host_alias, str) or not _ALIAS_RE.fullmatch(host_alias):
        raise ValueError("host_alias must match ^[a-z0-9][a-z0-9-]{0,31}$")
    return host_alias


def parse_stats_line(
    line: Any,
    *,
    ts: Any,
    host_alias: Any,
    attempt_id: Any,
    project: Any,
    cpu_limit: Any,
) -> dict[str, Any] | None:
    """Parse one ``docker stats --no-stream --format '{{json .}}'`` line."""
    alias = _validate_alias(host_alias)
    timestamp = _valid_timestamp(ts)

    if not isinstance(line, str):
        return None
    try:
        payload = json.loads(line)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    mem_bytes, mem_limit_bytes = _split_size_pair(payload.get("MemUsage"))
    net_rx_bytes, net_tx_bytes = _split_size_pair(payload.get("NetIO"))

    name = payload.get("Name")
    container_label = None
    if isinstance(name, str):
        container_label = _id_or_none(name.lstrip("/"))

    return {
        "schema": "resource.sample/v1",
        "schema_version": "v1",
        "ts": timestamp,
        "scope": "container",
        "source": "docker_stats",
        "host_alias": alias,
        "attempt_id": _id_or_none(attempt_id),
        "project": _id_or_none(project),
        "container_label": container_label,
        "mem_bytes": mem_bytes,
        "mem_limit_bytes": mem_limit_bytes,
        "cpu_ratio": _parse_percent(payload.get("CPUPerc")),
        "cpu_limit": _number_or_none(cpu_limit, maximum=256),
        "pids": _parse_nonnegative_int(payload.get("PIDs")),
        "oom_killed": None,
        "exit_code": None,
        "net_rx_bytes": net_rx_bytes,
        "net_tx_bytes": net_tx_bytes,
    }


def apply_inspect(row: dict[str, Any], state: Any) -> dict[str, Any]:
    """Merge a ``docker inspect`` State object into a container sample row."""
    if not isinstance(row, dict) or not isinstance(state, dict):
        return row

    updated = dict(row)

    oom = state.get("OOMKilled")
    updated["oom_killed"] = oom if isinstance(oom, bool) else None

    exit_code = None
    if state.get("Running") is False:
        candidate = state.get("ExitCode")
        if (
            isinstance(candidate, int)
            and not isinstance(candidate, bool)
            and -1024 <= candidate <= 1024
        ):
            exit_code = candidate
    updated["exit_code"] = exit_code

    return updated


def _last_non_null(rows: list[dict[str, Any]], key: str) -> Any:
    value = None
    for row in rows:
        candidate = row.get(key)
        if candidate is not None:
            value = candidate
    return value


def _max_non_null(rows: list[dict[str, Any]], key: str) -> Any:
    values = [
        row[key]
        for row in rows
        if isinstance(row.get(key), (int, float)) and not isinstance(row.get(key), bool)
    ]
    return max(values) if values else None


def rollup(rows: Iterable[dict[str, Any]], *, attempt_id: Any) -> dict[str, Any] | None:
    """Roll one attempt's container samples into an attempt_rollup row."""
    selected = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("attempt_id") == attempt_id
    ]
    if not selected:
        return None

    selected.sort(key=lambda row: row.get("ts") or "")
    last = selected[-1]

    cpu_values = [
        row["cpu_ratio"]
        for row in selected
        if isinstance(row.get("cpu_ratio"), (int, float))
        and not isinstance(row.get("cpu_ratio"), bool)
        and row["cpu_ratio"] >= 0
    ]

    oom_values = [row.get("oom_killed") for row in selected]
    if any(value is True for value in oom_values):
        oom_killed: bool | None = True
    elif any(value is False for value in oom_values):
        oom_killed = False
    else:
        oom_killed = None

    source = last.get("source")
    if source not in _SOURCES:
        source = "unknown"

    return {
        "schema": "resource.sample/v1",
        "schema_version": "v1",
        "ts": _valid_timestamp(last.get("ts")),
        "scope": "attempt_rollup",
        "source": source,
        "host_alias": _validate_alias(_last_non_null(selected, "host_alias")),
        "attempt_id": _id_or_none(attempt_id),
        "project": _id_or_none(_last_non_null(selected, "project")),
        "mem_peak_bytes": _max_non_null(selected, "mem_bytes"),
        "mem_limit_bytes": _last_non_null(selected, "mem_limit_bytes"),
        "cpu_ratio": (sum(cpu_values) / len(cpu_values)) if cpu_values else None,
        "pids": _max_non_null(selected, "pids"),
        "oom_killed": oom_killed,
        "exit_code": _last_non_null(selected, "exit_code"),
        "net_rx_bytes": last.get("net_rx_bytes"),
        "net_tx_bytes": last.get("net_tx_bytes"),
        "samples": len(selected),
    }
