"""Harvest bounded inference and tool-call metrics from OpenCode messages."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import math
import re


KNOWN_TOOLS = frozenset({
    "bash",
    "edit",
    "glob",
    "grep",
    "list",
    "multiedit",
    "patch",
    "question",
    "read",
    "skill",
    "task",
    "todoread",
    "todowrite",
    "webfetch",
    "websearch",
    "write",
})

_JOIN_KEYS = (
    "attempt_id",
    "card_id",
    "run_id",
    "project",
    "role",
    "task_id",
    "job_id",
    "trace_id",
)
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_LABEL_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_MODEL_RE = re.compile(r"(?!.*//)[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_MCP_SERVER_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,59}")


def _fullmatch(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _label(value):
    if not isinstance(value, str):
        return None
    value = value.lower()
    return value if _LABEL_RE.fullmatch(value) else None


def _model(value):
    return value if _fullmatch(_MODEL_RE, value) else None


def _nullable_id(value):
    return value if _fullmatch(_ID_RE, value) else None


def _join_values(join):
    try:
        source = join if isinstance(join, Mapping) else {}
        values = {}
        for key in _JOIN_KEYS:
            value = source.get(key)
            if key == "role":
                values[key] = value if value in ("work", "verify", "coordinator") else None
            else:
                values[key] = _nullable_id(value)
        return values
    except Exception:
        return {key: None for key in _JOIN_KEYS}


def tool_label(name):
    """Return a closed tool vocabulary label or a bounded MCP server label."""
    if not isinstance(name, str):
        return "other"
    normalized = name.strip().lower()
    if normalized in KNOWN_TOOLS:
        return normalized
    server = None
    if normalized.startswith("mcp:"):
        server = normalized[4:].split(":", 1)[0]
    elif normalized.startswith("mcp__"):
        server = normalized[5:].split("__", 1)[0]
    if server is not None and _MCP_SERVER_RE.fullmatch(server):
        return "mcp:" + server
    return "other"


def _epoch_ms(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (OverflowError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _utc_timestamp(epoch_ms):
    epoch_ms = _epoch_ms(epoch_ms)
    if epoch_ms is None:
        return None
    try:
        seconds = math.floor(epoch_ms / 1000.0)
        return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def _elapsed_s(end, start):
    end = _epoch_ms(end)
    start = _epoch_ms(start)
    if end is None or start is None or end < start:
        return None
    elapsed = (end - start) / 1000.0
    return elapsed if math.isfinite(elapsed) else None


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _time(mapping):
    if not isinstance(mapping, Mapping):
        return {}
    value = mapping.get("time")
    return value if isinstance(value, Mapping) else {}


def _parts(value):
    return value if isinstance(value, (list, tuple)) else []


def _assistant(message):
    if not isinstance(message, Mapping):
        return None
    info = message.get("info")
    if isinstance(info, Mapping) and info.get("role") == "assistant":
        return info, _parts(message.get("parts")), False
    if message.get("type") == "assistant":
        data = message.get("data")
        if isinstance(data, Mapping):
            return data, _parts(data.get("content")), True
    return None


def _first_chunk(parts, times, is_v2):
    if is_v2:
        streamed = _epoch_ms(times.get("streamed"))
        created = _epoch_ms(times.get("created"))
        if streamed is not None and created is not None and streamed >= created:
            return streamed if _utc_timestamp(streamed) is not None else None
        return None
    created = _epoch_ms(times.get("created"))
    if created is None:
        return None
    for part in parts:
        if not isinstance(part, Mapping) or part.get("type") not in ("text", "reasoning"):
            continue
        start = _epoch_ms(_time(part).get("start"))
        if start is not None and start >= created and _utc_timestamp(start) is not None:
            return start
    return None


def _cost(info):
    value = info.get("cost")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "unknown"
    try:
        if math.isfinite(value) and value > 0:
            return value, "actual"
    except (TypeError, ValueError):
        pass
    return None, "unknown"


def _error_fields(info):
    error = info.get("error")
    if not isinstance(error, Mapping):
        return None, None
    status = error.get("status", error.get("statusCode"))
    if type(status) is not int or not 100 <= status <= 599:
        status = None
    return "harness_error", status


def _inference_row(info, parts, is_v2, seq, joined, route, requested_model, harness, context_limit):
    times = _time(info)
    created = _epoch_ms(times.get("created"))
    completed = _epoch_ms(times.get("completed"))
    first_chunk = _first_chunk(parts, times, is_v2)
    ts_end = _utc_timestamp(completed)
    duration_s = _elapsed_s(completed, created) if ts_end is not None else None
    ttft_s = _elapsed_s(first_chunk, created) if first_chunk is not None else None

    tokens = info.get("tokens")
    has_usage = isinstance(tokens, Mapping)
    if has_usage:
        cache = tokens.get("cache")
        cache = cache if isinstance(cache, Mapping) else {}
        counts = {
            "input_tokens": _count(tokens.get("input")),
            "output_tokens": _count(tokens.get("output")),
            "reasoning_tokens": _count(tokens.get("reasoning")),
            "cache_read_tokens": _count(cache.get("read")),
            "cache_write_tokens": _count(cache.get("write")),
        }
        usage_status = "known" if all(value is not None for value in counts.values()) else "partial"
    else:
        counts = {
            "input_tokens": None,
            "output_tokens": None,
            "reasoning_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
        }
        usage_status = "unknown"

    output_tps = None
    output_tokens = counts["output_tokens"]
    if output_tokens is not None and first_chunk is not None and ts_end is not None:
        generation_s = _elapsed_s(completed, first_chunk)
        if generation_s is not None and generation_s > 0:
            output_tps = output_tokens / generation_s

    context_fill_ratio = None
    context_counts = (counts["input_tokens"], counts["cache_read_tokens"], counts["cache_write_tokens"])
    if context_limit is not None and context_limit > 0 and all(value is not None for value in context_counts):
        ratio = sum(context_counts) / context_limit
        if math.isfinite(ratio) and 0 <= ratio <= 2:
            context_fill_ratio = ratio

    model = info.get("model")
    if not isinstance(model, Mapping):
        model = {}
    provider = _label(info.get("providerID")) or _label(model.get("providerID")) or route or "unknown"
    served_model = _model(info.get("modelID")) or _model(model.get("id"))
    cost_usd_list, cost_status = _cost(info)
    error_type, http_status = _error_fields(info)

    row = {
        "schema": "gen_ai.inference/v1",
        "schema_version": "v1",
        **joined,
        "seq": seq,
        "ts_start": _utc_timestamp(created),
        "ts_first_chunk": _utc_timestamp(first_chunk),
        "ts_end": ts_end if duration_s is not None else None,
        "duration_s": duration_s,
        "ttft_s": ttft_s,
        "ttft_source": "harness_part_timing" if ttft_s is not None else "unknown",
        "output_tps": output_tps,
        "requested_model": requested_model,
        "served_model": served_model,
        "provider": provider,
        "route": route,
        "harness": harness,
        "fallback_hop": 0,
        **counts,
        "usage_status": usage_status,
        "finish_reason": _label(info.get("finish")),
        "error_type": error_type,
        "http_status": http_status,
        "cost_usd_list": cost_usd_list,
        "cost_status": cost_status,
        "price_table_version": None,
        "context_limit": context_limit,
        "context_fill_ratio": context_fill_ratio,
    }
    return row


def _tool_status(state):
    status = state.get("status")
    if status == "completed":
        return "ok"
    if status in ("error", "failed", "failure"):
        return "error"
    if status in ("denied", "permission_denied"):
        return "denied"
    if status in ("cancelled", "canceled", "aborted"):
        return "cancelled"
    return "unknown"


def _output_bytes(value):
    if value is None:
        return None
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return len(encoded.encode("utf-8"))
    except Exception:
        return None


def _tool_row(part, is_v2, step_seq, call_seq, joined, assistant_created):
    state = part.get("state")
    state = state if isinstance(state, Mapping) else {}
    part_time = _time(part)
    state_time = _time(state)
    if is_v2:
        created = _epoch_ms(part_time.get("created"))
        ran = _epoch_ms(part_time.get("ran"))
        started = ran if ran is not None else created
        completed = _epoch_ms(part_time.get("completed"))
        raw_name = part.get("name")
    else:
        started = _epoch_ms(state_time.get("start"))
        completed = _epoch_ms(state_time.get("end"))
        raw_name = part.get("tool")
    if started is None or _utc_timestamp(started) is None:
        started = assistant_created
    ts_start = _utc_timestamp(started)
    if ts_start is None:
        return None
    ts_end = _utc_timestamp(completed)
    duration_s = _elapsed_s(completed, started) if ts_end is not None else None
    status = _tool_status(state)
    name = tool_label(raw_name)
    output = state.get("output") if "output" in state else state.get("content")
    error_class = {
        "error": "tool_error",
        "denied": "permission_denied",
        "cancelled": "cancelled",
    }.get(status)
    return {
        "schema": "gen_ai.tool_call/v1",
        "schema_version": "v1",
        **joined,
        "step_seq": step_seq,
        "call_seq": call_seq,
        "tool_name": name,
        "tool_type": "extension" if name.startswith("mcp:") else "unknown" if name == "other" else "function",
        "ts_start": ts_start,
        "ts_end": ts_end if duration_s is not None else None,
        "duration_s": duration_s,
        "status": status,
        "error_class": error_class,
        "output_bytes": _output_bytes(output),
    }


def harvest(messages, *, join, route, requested_model, harness, context_limit=None):
    """Convert one attempt's session messages into schema-versioned metric rows."""
    inference = []
    tools = []
    joined = _join_values(join)
    route = _label(route)
    requested_model = _model(requested_model)
    harness = _label(harness)
    context_limit = context_limit if type(context_limit) is int and context_limit >= 0 else None

    try:
        iterator = iter(messages)
    except (TypeError, ValueError):
        return {"inference": inference, "tools": tools}

    for message in iterator:
        try:
            parsed = _assistant(message)
            if parsed is None:
                continue
            info, parts, is_v2 = parsed
            created = _epoch_ms(_time(info).get("created"))
            if created is None or _utc_timestamp(created) is None:
                continue
            step_seq = len(inference) + 1
            row = _inference_row(
                info,
                parts,
                is_v2,
                step_seq,
                joined,
                route,
                requested_model,
                harness,
                context_limit,
            )
            inference.append(row)
        except Exception:
            continue

        for part in parts:
            try:
                if not isinstance(part, Mapping) or part.get("type") != "tool":
                    continue
                tool = _tool_row(part, is_v2, step_seq, len(tools) + 1, joined, created)
                if tool is not None:
                    tools.append(tool)
            except Exception:
                continue

    return {"inference": inference, "tools": tools}
