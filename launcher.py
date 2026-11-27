#!/usr/bin/env python3
"""hyvv worker-host runtime (runs on a worker PC or SSH host, never on the control surface).

Owns every Cua call, holds the provider key and never returns it, enforces one
execution controller per attempt, journals attempts locally, and exposes a
narrow token-scoped HTTP API on loopback or a Tailscale address only.

  uv run --with cua==0.5.0 launcher.py serve  --config config.json
  uv run --with cua==0.5.0 launcher.py doctor --config config.json
"""
from __future__ import annotations
import argparse, asyncio, bisect, platform, hashlib, hmac, ipaddress, json, os, re, shlex, socket, sys, threading, time, uuid
from contextlib import contextmanager
from datetime import datetime, timezone

try:
    import provisioning as _provisioning
except ImportError:  # pragma: no cover - the module ships beside launcher.py
    _provisioning = None
try:
    import landing_export as _landing_export
except ImportError:  # pragma: no cover - the module ships beside launcher.py
    _landing_export = None
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

try:
    from hyvv import rizzstack_validators as _rizzstack_validators
except ImportError:
    _rizzstack_validators = None

if _landing_export is None:  # pragma: no cover - loaded from a sibling copy
    try:
        import importlib.util as _importlib_util
        _export_spec = _importlib_util.spec_from_file_location("landing_export", Path(__file__).with_name("landing_export.py"))
        _landing_export = _importlib_util.module_from_spec(_export_spec)
        sys.modules.setdefault("landing_export", _landing_export)
        _export_spec.loader.exec_module(_landing_export)
    except Exception:
        _landing_export = None

VERSION = "0.1.19"
HEARTBEAT_S = 60  # controller emits a heartbeat event after this many quiet seconds
MAX_WORKERS = 14  # hard ceiling for max_concurrent (owner directive: final ceiling, supersedes the interim cap-12)
TERMINAL = {"succeeded", "failed", "cancelled"}

# A resumable rate-limited attempt keeps its sandbox and run; the retry sends this short
# continue turn instead of the full brief.
RESUME_PROMPT = ("Continue the same task from where the previous turn stopped; do not restart "
                 "from scratch. The prior turn was interrupted by a provider rate limit.")

JOURNAL_ACTIVE = ("starting", "running", "cancel_requested")
SAVE_REPLACE_TRIES = 10
SAVE_REPLACE_BUDGET_S = 2.0  # total backoff across retries of os.replace (about 2 seconds)
GO_BASE_URL = "https://opencode.ai/inference/go/openai/v1"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"  # official OpenAI-compatible endpoint
ZOO_BASE_URL = "https://www.zoocode.dev/api/gateway/v1"  # Zoo gateway, OpenAI-compatible
NOUS_BASE_URL = "https://inference-api.nousresearch.com/v1"  # Nous Portal inference API, OpenAI-compatible
# Workers must not fan out: deny the OpenCode `task` (subagent) tool.
WORKER_OPENCODE_CONFIG = {"$schema": "https://opencode.ai/config.json", "permission": {"task": "deny"}}
# Allow-listed per-run models.
# "key": "deepseek" sends DEEPSEEK_API_KEY; "go" sends the OpenCode Go key; "none" sends no key.
# The free model only works from inside OpenCode, so it uses OpenCode's built-in provider
# (opencode/<id>, no base_url) rather than an OpenAI-compatible endpoint.
def _free(model_id, ctx_tokens):
    # Prompt cap: leave ~80k tokens for OpenCode's own system prompt/tools (a probe used ~43k) and output;
    # ~3 chars per token; never above 300k chars (~100k tokens) for the free tier.
    # fallback aliases to Nous DeepSeek v4.1 Flash 1017 (see MODELS).
    return {"model": f"opencode/{model_id}", "base_url": None, "wire": None, "key": "none", "free": True,
            "fallback": "nous:deepseek-v4.1-flash-1017", "context_tokens": ctx_tokens,
            "max_prompt_chars": max(60_000, min(300_000, (ctx_tokens - 80_000) * 3)), "backoff_s": [20, 60]}

def _go_free(model_id, ctx_tokens):
    # OpenCode Go also serves some *-free ids (docs: opencode.ai/docs/go, Oct 21 2026). Route them through Go's
    # OpenAI-compatible endpoint with the Go key, instead of Zen's built-in provider.
    return {**_free(model_id, ctx_tokens), "model": model_id, "base_url": GO_BASE_URL, "wire": "openai-chat", "key": "go", "via": "go"}

# Routing (0.1.15, precedence owner-decided 2026-11-15): a model with no prefix, or a `go:`
# prefix (including bare `deepseek-v4.1-flash`), goes to OpenCode Go. `deepseek:<model>` (e.g.
# `deepseek:deepseek-flash`) goes to DeepSeek official. An explicitly named model or tier is
# authoritative; priority: high reroutes only a brief that names no model.
# A Go monthly-limit 429 retries once on Nous DeepSeek 1017 and records fallback="ds1017n".
# Free models only work from inside OpenCode, so they use OpenCode's built-in provider (opencode/<id>, no base_url).
# Every free id below answered `opencode run -m opencode/<id>` on Oct 20, 2026 (jev-1.13-free and space-bunny-free were "Model unavailable").
# Context sizes from models.dev (input limit where it is lower).
_DEEPSEEK_FLASH = {"model": "deepseek-flash", "base_url": DEEPSEEK_BASE_URL, "wire": "openai-chat", "key": "deepseek"}
_GO_FLASH = {"model": "deepseek-v4.1-flash", "base_url": GO_BASE_URL, "wire": "openai-chat", "key": "go"}
# Zoo gateway, free GLM 5.3 Flash pass. `zoo/glm-5.3-flash` is the free model; `zai/glm-5.3-flash`
# is the paid one, so the id matters. 16 concurrent runs are clean and 20 is the edge, which the
# launcher's MAX_WORKERS=14 ceiling sits well under. Thinking is always on and spends the
# output budget FIRST, so callers must allow at least 4096 output tokens (8192 for coding); any
# max_tokens below 1024 is silently raised to 1024 by the gateway. Streaming usage is always
# zeros, so metrics must come from non-stream usage or an estimate. Nous DeepSeek 1017 is the
# fallback once Zoo rate limits persist (or after the free pass ends).
_ZOO_GLM = {"model": "zoo/glm-5.3-flash", "base_url": ZOO_BASE_URL, "wire": "openai-chat", "key": "zoo",
            "fallback": "nous:deepseek-v4.1-flash-1017", "backoff_s": [15, 30, 60]}
# Nous Portal, Tencent Hy4 Flash (owner request 2026-11-24). The key allows 180 requests/min and
# 7,560/hour shared across every model, so nous attempts are paced: at most `max_active` run at
# once and starts are spaced by `start_spacing_s` (both overridable per host in cfg.models).
# A 429 retries on `backoff_s` then falls back to OpenCode Go DeepSeek flash; an auth or 5xx
# provider failure, or a missing nous key, falls back once instead of failing the attempt.
# Benchmarked 2026-11-24: never send temperature 0 (the model loops in its thinking).
# Owner t1665u: also the discounted DeepSeek flash (1017: 20/20 in the Nov 24 benchmark, about
# half Hy4's cost and faster) and the $0 Nous models that answer API traffic. Low-risk bees run
# 1017 first, then Hy4 Flash, then OpenCode Go DeepSeek flash (a chain of MAX_FALLBACK_HOPS).
# Every nous:* entry carries the same max_active, and pacing counts attempts per key, so the
# cap holds across all Nous models combined. Never a `:batch` SKU (async, not for agents).
_NOUS = {"base_url": NOUS_BASE_URL, "wire": "openai-chat", "key": "nous", "backoff_s": [10, 30],
         "fallback_on_provider_error": True, "fallback_on_missing_key": True, "fallback_chain": True,
         "max_active": 8, "start_spacing_s": 2}
_NOUS_HY4 = {**_NOUS, "model": "tencent/hy4-flash", "fallback": "go:deepseek-v4.1-flash"}
_NOUS_DS1017 = {**_NOUS, "model": "deepseek/deepseek-v4.1-flash-1017", "fallback": "nous:hy4-flash"}
_NOUS_HY4_FREE = {**_NOUS, "model": "tencent/hy4-flash:free", "fallback": "nous:hy4-flash"}
_NOUS_LONGCAT_FREE = {**_NOUS, "model": "meituan/longcat-2.5:free", "fallback": "nous:hy4-flash"}
MAX_FALLBACK_HOPS = 2  # opt-in chains (fallback_chain) hop at most this many times per attempt
MODELS = {
    "deepseek-flash": dict(_GO_FLASH),  # unprefixed -> Go (0.1.15)
    "deepseek-v4.1-flash": dict(_GO_FLASH),  # unprefixed / default briefs -> Go
    "go:deepseek-v4.1-flash": dict(_GO_FLASH),
    "zoo:glm-5.3-flash": dict(_ZOO_GLM),  # free Zoo pass; Nous 1017 fallback when the pass ends
    "deepseek:deepseek-flash": dict(_DEEPSEEK_FLASH),
    "deepseek:deepseek-v4.1-flash": dict(_DEEPSEEK_FLASH),
    "nous:hy4-flash": dict(_NOUS_HY4),  # Nous Portal; paced, falls back to Go DeepSeek flash
    "nous:deepseek-v4.1-flash-1017": dict(_NOUS_DS1017),  # discounted DeepSeek flash -> hy4 -> go
    "nous:hy4-flash-free": dict(_NOUS_HY4_FREE),
    "nous:longcat-2.5-free": dict(_NOUS_LONGCAT_FREE),
    "deepseek-v4.1-flash-free": _free("deepseek-v4.1-flash-free", 128_000),
    "beam-free": _go_free("beam-free", 1_048_576),
    "big-pickle": _free("big-pickle", 160_000),
    "minimax-m3.1-flash-free": _go_free("minimax-m3.1-flash-free", 1_000_000),
    "nemotron-3.5-super-vl-free": _free("nemotron-3.5-super-vl-free", 1_048_576),
    "nemotron-3.5-lightning-free": _free("nemotron-3.5-lightning-free", 262_144),
    "glm-5.3-flash-free": _free("glm-5.3-flash-free", 200_000),
    "mimo-v2.6-flash-free": _free("mimo-v2.6-flash-free", 200_000),
    "longcat-2.5-preview-free": _free("longcat-2.5-preview-free", 1_000_000),
}
FREE_POOL = [m for m, v in MODELS.items() if v.get("free")]  # deepseek first: preferred when all are equally fresh
# Owner routing change (2026-11-17, MR.INDIA with the owner's OK): Zoo's free pass refuses
# non-IDE API traffic, so the "standard" tier must never resolve to it. OpenCode Go's
# deepseek-v4.1-flash is the host default now; naming it explicitly is the supported route.
TIERS = {"simple": "pool:free", "standard": "go:deepseek-v4.1-flash"}
FREE_TIER_DENIED = re.compile(r"FreeTierError|free tier can only be used", re.I)
RATE_LIMIT = re.compile(r"\b429\b|rate.?limit|too many requests|quota", re.I)
# Zoo answers 429 "Too many concurrent requests" with a Retry-After of 5-7s, and a 402
# "Insufficient credits" that is really an admission/concurrency error rather than a permanent
# failure. Both are retried; the 402 is matched only for a zoo-keyed model.
RETRY_AFTER = re.compile(r"retry[\s_-]*after\W{0,3}(\d{1,4})", re.I)
ZOO_ADMISSION = re.compile(r"\b402\b|insufficient credits", re.I)
# A provider can stop serving a model to API traffic outright. Zoo's free pass did exactly
# that mid-campaign ("Zoo Pass Pilot models are available in the Zoo Code IDE only."), which
# arrives as a bare error event - no 402, no 429 - so it needs its own fallback branch; a
# retry backoff would just repeat the refusal until the attempt died.
MODEL_UNAVAILABLE = re.compile(
    r"available in the .{0,40}IDE only|model[_ ]unavailable|model .{0,60} is not available|no longer available",
    re.I,
)
# Provider-side failures (auth, forbidden, 5xx). Only models that opt in with
# "fallback_on_provider_error" roll their fallback on these; every other model keeps its path.
PROVIDER_FAILURE = re.compile(
    r"\b(?:401|403|500|502|503|504)\b|unauthori[sz]ed|invalid[ _-]?api[ _-]?key|authentication|"
    r"forbidden|service unavailable|bad gateway|gateway timeout|overloaded",
    re.I,
)
ZOO_RETRY_AFTER_MAX = 300
MONTHLY_LIMIT = re.compile(r"GoUsageLimitError|usage limit reached|UsageLimit", re.I)
MONTHLY_LIMIT_HINT = re.compile(r"monthly|limitName", re.I)
# ---- task kinds. "opencode" (default) = Cua agents harness in a sandbox; "agy-cua" = Antigravity CLI
# on the PC host whose ONLY tools are a cua MCP server bound to that attempt's own sandbox.
# "check" = a check-run job: no model; run brief.commands in a container and report exit codes + logs.
KINDS = ("opencode", "agy-cua", "check")
# ids from `agy models`, Oct 21 2026 ("gemini-4-argon" is accepted as an alias; status SUCCESS in the smoke test)
AGY_MODELS = {"gemini-4-argon", "gemini-4-argon-low", "gemini-4-argon-medium", "gemini-4-argon-high",
              "gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high",
              "claude-sonnet-5-5-low", "claude-sonnet-5-5-medium", "claude-sonnet-5-5-high",
              "claude-opus-5-5-low", "claude-opus-5-5-medium", "claude-opus-5-5-high"}
AGY_TIERS = {"simple": "gemini-3.8-flash-medium", "complex": "gemini-4-argon",
             "sonnet": "claude-sonnet-5-5-medium", "sonnet-high": "claude-sonnet-5-5-high",
             "opus": "claude-opus-5-5-medium", "opus-high": "claude-opus-5-5-high"}  # Flash/Argon/Claude pools
AGY_DEFAULT_TIER = "simple"
AGY_DEFAULT_MODEL = AGY_TIERS[AGY_DEFAULT_TIER]
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "thinking_tokens", "total_tokens")
AGY_MCP_NAME = "orbit-cua"  # registered once in ~/.gemini/config/mcp_config.json (scripts/setup-agy-cua.ps1)
AGY_ALLOW_RULE = f"mcp({AGY_MCP_NAME}/*)"
DEFAULT_ATTEMPT_TIMEOUT_S = 3600
AGY_TIMEOUT_S = (60, 600, 3600)  # min, default, max for brief.budget.timeout_s
# budget.timeout_s is also an ATTEMPT wall clock, not just a value handed to a worker: a controller
# watchdog force-finishes an attempt this many seconds after the budget expires even when the
# worker (or a backend call) never returns. The grace covers stopping the worker; the terminal
# journal write happens inside it, so the attempt slot always frees.
BUDGET_STOP_GRACE_S = 15
BUDGET_STOP_DRAIN_S = 2.0  # bounded final event drain at the budget stop: the stop decision must
# not preempt the event drain, or a run that finished INSIDE its budget is force-failed with a false
# outlived detail and its result is lost. The SDK pages events, so the drain repeats across pages
# until the stream is exhausted or this window elapses; a backend that cannot answer in time just
# gets force-failed.
CHECK_ATTEMPT_OVERHEAD_S = 300  # launcher-side slack (container create/seed/cleanup) atop a check job's per-command clocks
AGY_MODES = ("ui", "code")  # code: repo seeded into the attempt's sandbox; agy edits/runs ONLY there via orbit-cua shell+file
AGY_CODE_PERMS = "computer:shell,computer:file,computer:screenshot,computer:click,computer:type,computer:key,computer:scroll"
# ---- sandbox naming: <model abbr>-<slug>-<short attempt id>, e.g. ds41-fix-login-bug-89abcdef01
MODEL_ABBR = {
    "deepseek-flash": "dsflash", "deepseek-v4.1-flash": "ds41", "go:deepseek-v4.1-flash": "ds41go",
    "deepseek:deepseek-flash": "dsflash", "deepseek:deepseek-v4.1-flash": "dsflash",
    "zoo:glm-5.3-flash": "zoo53", "nous:hy4-flash": "hy4nous",
    "nous:deepseek-v4.1-flash-1017": "ds1017n", "nous:hy4-flash-free": "hy4nfr", "nous:longcat-2.5-free": "lc25nfr",
    "deepseek-v4.1-flash-free": "ds41fr", "beam-free": "beamfr", "big-pickle": "bpkl",
    "minimax-m3.1-flash-free": "mm31fr", "nemotron-3.5-super-vl-free": "nm35svfr", "nemotron-3.5-lightning-free": "nm35lfr",
    "glm-5.3-flash-free": "glm53fr", "mimo-v2.6-flash-free": "mimo26fr", "longcat-2.5-preview-free": "lc25fr",
    "gemini-4-argon": "ga4", "gemini-4-argon-low": "ga4l", "gemini-4-argon-medium": "ga4m", "gemini-4-argon-high": "ga4h",
    "gemini-3.8-flash-low": "gf38l", "gemini-3.8-flash-medium": "gf38m", "gemini-3.8-flash-high": "gf38h",
    "claude-sonnet-5-5-low": "cs55l", "claude-sonnet-5-5-medium": "cs55m", "claude-sonnet-5-5-high": "cs55h",
    "claude-opus-5-5-low": "co55l", "claude-opus-5-5-medium": "co55m", "claude-opus-5-5-high": "co55h",
}
SANDBOX_NAME = re.compile(r"(orbit|orbitagy)-[A-Za-z0-9_\-]+|[a-z0-9]{2,10}-([a-z0-9]+(-[a-z0-9]+)*-)?[a-z0-9]{10}")

def model_abbr(model):
    return MODEL_ABBR.get(model or "") or (re.sub(r"[^a-z0-9]", "", (model or "x").lower().split("/")[-1])[:8] or "x")

def slugify(text, n=24):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:n].strip("-")
    return s

def sandbox_name(a):
    """<model abbr>-<slug>-<short attempt id>. Slug from brief.label, then brief.title, then the objective."""
    b = a.get("brief") or {}
    slug = slugify(b.get("label") or b.get("title") or " ".join(str(b.get("objective", "")).split()[:5]))
    short = re.sub(r"[^a-z0-9]", "", a["attempt_id"].lower())[-10:].rjust(10, "0")
    return "-".join(x for x in (model_abbr(a.get("model")), slug, short) if x)

SECRETISH = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+\S+|[A-Za-z0-9_\-]{32,})")

# Canonical credential shapes aligned with the canonical validator's refusals.
# Every fragment is assembled at runtime so this source never contains a
# credential-shaped literal of its own.
_SECRETISH_EXTRA = re.compile("|".join((
    "AK" + "IA" + "[0-9A-Z]{16}",
    "gh" + "[pousr]_" + "[A-Za-z0-9]{20,}",
    "github" + "_pat_" + "[A-Za-z0-9_]{22,}",
    "x" + "ox[baprs]-" + "[A-Za-z0-9-]{10,}",
    "ey" + "J[A-Za-z0-9_=-]*" + r"\." + "[A-Za-z0-9_-]+" + r"\." + "[A-Za-z0-9_-]+",
)))

def redact(s):
    if not isinstance(s, str):
        return s
    s = SECRETISH.sub("<redacted>", s)
    return _SECRETISH_EXTRA.sub("<redacted>", s)

# ---------------------------------------------------------------- bee landed events
# A terminal attempt produces exactly one durable CloudEvent. The event is written to
# <data_dir>/events/outbox/*.json with temp-file + fsync + rename, so a crash
# can leave a complete event or no event, never a torn one. Delivery is a local signal
# (file or Unix socket); the outbox file itself is the replayable source of truth.
LANDED_EVENT_TYPE = "rizzstack.bee.landed.v1"
LANDED_EVENT_SCHEMA = "https://github.com/mojomast/workflow/schemas/bee-landed.schema.json"
LANDED_DATA_SCHEMA = "rizzstack.bee-landed/1"
LANDED_EVENT_ID_PREFIX = "urn:rizzstack:bee-landed:"
_LANDED_EVIDENCE_KEYS = ("schema", "feature_id", "job_id", "task_id", "attempt_id",
                         "coordinator", "backend", "state", "source_rev", "lease", "result",
                         "correlation_id", "causation_id")
# The receipt's usage block rides the envelope as an optional CloudEvents extension
# attribute, not inside `data`: the frozen rizzstack.bee-landed/1 payload keeps its
# closed field set (docs/bee-landed.md), and an envelope-root extension is the additive,
# backward-compatible evolution path (old consumers ignore unknown attributes, so no
# schema version bump or repin). Contents are scrubbed to numeric token counts plus
# tool_calls, stop_reason and the model/route identity.
LANDED_USAGE_ATTR = "rizzstackusage"
# Canonical count keys emitted under `rizzstackusage`. The emitted names carry no
# credential-heuristic substring anywhere in the string: validating consumers
# refuse attribute sub-keys whose names look like credential words, so the bare
# count names are emitted and every legacy spelling stays input-only (mapped
# through _USAGE_TOKEN_ALIASES, never emitted).
_USAGE_TOKEN_KEYS = ("input", "cache_read", "cache_write", "output", "reasoning", "thinking", "total")
_USAGE_TOKEN_ALIASES = {
    "inputtokens": "input", "prompttokens": "input",
    "cacheread": "cache_read", "cachereadtokens": "cache_read",
    "cachedtokens": "cache_read", "cachereadinputtokens": "cache_read",
    "cachewrite": "cache_write", "cachewritetokens": "cache_write",
    "cachecreationtokens": "cache_write", "cachecreationinputtokens": "cache_write",
    "outputtokens": "output", "completiontokens": "output",
    "reasoningtokens": "reasoning",
    "thinkingtokens": "thinking",
    "totaltokens": "total",
}
# A sane per-run magnitude bound for every numeric count in the attribute: no
# single run consumes a billion tokens or issues a billion tool calls, so a
# count at or above this bound is a corrupted or hostile value. It is refused
# (the count stays absent), never clamped into a fabricated number.
_USAGE_COUNT_LIMIT = 1_000_000_000


class LandedEventConflict(ValueError):
    """A second, contradictory terminal observation for the same attempt was refused."""


def _iso_utc(ts=None):
    ts = time.time() if ts is None else ts
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_bytes(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _text_id(value):
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("id", "name", "ref", "coordinator_id", "session_id", "sha256"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _canonical_id(value, fallback):
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9._-]", "-", text)[:128]
    if not text or not re.match(r"[A-Za-z0-9]", text[0]):
        return fallback
    return text


def _bounded_text(value, maximum):
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:maximum]


def _brief_id(brief, name):
    for key in (name + "_id", name):
        value = brief.get(key)
        found = value.strip() if isinstance(value, str) and value.strip() else _text_id(value)
        if found:
            return found
    return None


def landed_source(cfg):
    """A stable, non-secret CloudEvents source for this launcher."""
    cfg = cfg or {}
    ident = (cfg.get("launcher_id") or cfg.get("worker_id") or cfg.get("source") or
             cfg.get("host_id") or platform.node() or "launcher")
    ident = str(ident).strip() or "launcher"
    if "://" in ident:
        return ident
    safe = re.sub(r"[^A-Za-z0-9._~:-]", "-", ident) or "launcher"
    return "hyvv://" + safe


def landed_event_id(attempt_id, state, transition=None):
    """Opaque URN id, deterministic for one (attempt_id, state, transition) observation."""
    transition = str(transition).strip() if isinstance(transition, str) and transition.strip() else str(state)
    material = f"{attempt_id}:{state}:{transition}"
    return LANDED_EVENT_ID_PREFIX + _sha256_bytes(material.encode("utf-8"))[:32]


def _coordinator_parts(a):
    brief = a.get("brief") or {}
    raw = brief.get("coordinator") if brief.get("coordinator") is not None else a.get("coordinator")
    cid = _text_id(raw) or _text_id(brief.get("coordinator_id")) or _text_id(a.get("coordinator_id"))
    session = brief.get("coordinator_session") or brief.get("session_id") or brief.get("session")
    if isinstance(raw, dict):
        session = session or raw.get("session") or raw.get("session_id")
    if isinstance(session, dict):
        session = _text_id(session)
    session = session.strip() if isinstance(session, str) and session.strip() else None
    return cid, session


def _source_rev(a, brief, attempt_id, state):
    value = brief.get("source_rev") if brief.get("source_rev") is not None else a.get("source_rev")
    if isinstance(value, str):
        value = value.strip().lower()
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
            return value
    # Keep the wire event canonical even when a legacy brief omitted source_rev.
    return _sha256_bytes(f"{attempt_id}:{state}".encode("utf-8"))


def _assigned_files(a, brief):
    raw = brief.get("assigned_files") if brief.get("assigned_files") is not None else a.get("assigned_files")
    lease = brief.get("lease") if brief.get("lease") is not None else a.get("lease")
    if raw is None and isinstance(lease, dict):
        raw = lease.get("assigned_files")
    files = []
    for item in raw or []:
        if not isinstance(item, str):
            continue
        path = item.strip()
        if not path or path in files or path.startswith("/") or "\\" in path:
            continue
        if not re.fullmatch(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*", path):
            continue
        if any(part in ("", ".", "..") for part in path.split("/")):
            continue
        files.append(path)
    return files


def _lease_sha256(a, source_rev):
    brief = a.get("brief") or {}
    for key in ("lease_sha256", "lease_sha"):
        value = brief.get(key) if brief.get(key) is not None else a.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value.strip()):
            return value.strip().lower()
    lease = brief.get("lease") if brief.get("lease") is not None else a.get("lease")
    if isinstance(lease, dict):
        nested = lease.get("sha256") or lease.get("sha256_hex")
        if isinstance(nested, str) and re.fullmatch(r"[0-9a-fA-F]{64}", nested.strip()):
            return nested.strip().lower()
    elif isinstance(lease, str) and lease.strip():
        value = lease.strip()
        if re.fullmatch(r"[0-9a-fA-F]{64}", value):
            return value.lower()
    material = {"assigned_files": sorted(_assigned_files(a, brief)), "source_rev": source_rev}
    return _sha256_bytes(_canonical_bytes(material))


def _landed_result(a, result_path, attempt_id=None):
    aid = str(attempt_id or a.get("attempt_id") or (a.get("brief") or {}).get("attempt_id") or "")
    info = {"available": False}
    if result_path is None:
        return info
    path = Path(result_path)
    try:
        if not path.is_file():
            return info
        raw = path.read_bytes()
        info.update(available=True, artifact_sha256=_sha256_bytes(raw),
                    reference=f"hyvv://attempts/{aid}/result")
    except OSError:
        pass
    return info


def _usage_count(value):
    """A non-negative integer count under the magnitude bound, or None when the source has none.

    Zeros are kept only when the provider itself reported 0; a missing value never
    becomes 0 as a stand-in. A count at or above _USAGE_COUNT_LIMIT is an absurd
    magnitude and is refused, never clamped."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value < _USAGE_COUNT_LIMIT:
        return value
    if isinstance(value, float) and 0 <= value < _USAGE_COUNT_LIMIT and value.is_integer():
        return int(value)
    return None


# Free-text refusal shapes for the usage attribute: a URL (any scheme), a
# filesystem path, an IPv4 address, a drive path, a host:port, an IPv6 literal
# (bare or bracketed, with or without a port) or a dotted host must never reach
# the event as free text. Used for non-enum stop reasons.
_USAGE_FREEFORM_RE = re.compile(
    r"(?i)(?:[a-z][a-z0-9+.\-]*://"  # URL scheme
    r"|[/\\]"                        # path separator
    r"|\b\d{1,3}(?:\.\d{1,3}){3}\b"  # IPv4 address
    r"|\[[0-9a-f:.]+\]"              # bracketed IPv6 literal (with or without a port)
    r"|(?<![0-9a-f:])(?:[0-9a-f]{0,4}:){2,}[0-9a-f]{0,4}(?![0-9a-f:])"  # bare IPv6 literal
    r"|\b[a-z]:[\\/]"                # drive path
    r"|[a-z0-9_\-]+:\d"              # host:port
    r"|[a-z0-9_\-]+\.[a-z0-9_\-]+)"  # dotted host or domain-ish run
)

# Model refusal shapes: narrower than the free-text rule because model ids
# legitimately carry dotted version segments (deepseek-v4.1-flash). A URL
# scheme, a path separator, an IPv4 or IPv6 address, a drive path or a
# host:port is refused; a dotted version segment is kept.
_USAGE_MODEL_RE = re.compile(
    r"(?i)(?:[a-z][a-z0-9+.\-]*://"
    r"|[/\\]"
    r"|\b\d{1,3}(?:\.\d{1,3}){3}\b"
    r"|\[[0-9a-f:.]+\]"
    r"|(?<![0-9a-f:])(?:[0-9a-f]{0,4}:){2,}[0-9a-f]{0,4}(?![0-9a-f:])"
    r"|\b[a-z]:[\\/]"
    r"|[a-z0-9_\-]+:\d"
    r")")

# Host-like dotted shapes for the model gate: a dotted run whose final segment
# is a private-network suffix or an alpha-only top-level domain of two or more
# letters is a hostname, not a model id. Dotted version identifiers
# (deepseek-v4.1-flash, v4.1) end in a numeric or hyphenated segment and stay.
_USAGE_MODEL_HOSTISH_RE = re.compile(
    r"(?i)(?:\.(?:internal|local|lan|corp|home\.arpa)$"  # private-network suffix
    r"|\.[a-z]{2,}$)")                                   # alpha-only TLD, length >= 2

# The launcher's known stop reasons. A receipt stop reason matching one of these
# (case-insensitively) is emitted lowercased; any other free text must survive
# strict scrubbing or the field stays absent.
_USAGE_STOP_REASONS = frozenset((
    "end_turn", "max_tokens", "stop_sequence", "tool_use", "tool_calls",
    "content_filter", "length", "stop", "error", "timeout", "aborted",
    "cancelled", "canceled", "interrupted", "refusal", "unknown", "other",
))

# Keep optional metadata within the vocabulary accepted by the coordinator.
# A provider's prose explanation belongs in its receipt; it must not prevent
# an otherwise valid terminal event (and its usage counts) from being consumed.
_USAGE_REASON_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_-]*|turn ended")
_USAGE_MODEL_LABEL = re.compile(
    r"(?:(?:zoo|go|deepseek):(?=[A-Za-z]))?[A-Za-z0-9][A-Za-z0-9_+-]*(?:\.[0-9][A-Za-z0-9_+-]*)*"
)


def _usage_stop_reason(value):
    """A stop reason that may cross the event, or None when it must not.

    Known launcher stop reasons pass lowercased. Other labels must fit the
    coordinator's code format and pass scrubbing; raw receipt prose stays local."""
    text = _bounded_text(value, 64)
    if not text:
        return None
    low = text.lower()
    if low in _USAGE_STOP_REASONS:
        return low
    if (redact(text) != text or _USAGE_FREEFORM_RE.search(text)
            or not _USAGE_REASON_LABEL.fullmatch(text) or low == "localhost"):
        return None
    return text


def _usage_model(value):
    """An identifier-only model name that may cross the event, or None.

    The value passes through the redactor first: a secret-shaped run is refused,
    so the redaction marker never masquerades as a model id. URL, host/IP
    (IPv4 or IPv6), host:port and filesystem-path shapes are refused the same
    way, as is a dotted host shape (private-network suffix or alpha-only TLD);
    dotted version segments (deepseek-v4.1-flash) stay fine."""
    text = _bounded_text(value, 128)
    if not text:
        return None
    if redact(text) != text or _USAGE_MODEL_RE.search(text):
        return None
    if (_USAGE_MODEL_HOSTISH_RE.search(text) or not _USAGE_MODEL_LABEL.fullmatch(text)
            or text.rpartition(":")[2].lower() == "localhost"):
        return None
    return text


def _usage_route(kind, model):
    """Coarse route identity for the usage attribute: which provider path served the run."""
    if kind == "agy-cua":
        return "agy"
    text = str(model or "").strip()
    prefix, _, rest = text.partition(":")
    if prefix in ("zoo", "go", "deepseek") and rest:
        return prefix
    key = (MODELS.get(text) or {}).get("key")
    if key == "none":
        return "free"
    return key if key in ("go", "deepseek", "zoo", "kimi", "nous") else None


def _usage_tokens(value):
    """Allowlist projection of a provider usage object onto canonical count keys.

    Accepts a dict or a JSON string (the SDK reports usage_json). Unknown keys are
    dropped, never copied through: only numeric counts survive, emitted under the
    canonical names in _USAGE_TOKEN_KEYS. Legacy and alias spellings are accepted
    as input (mapped through _USAGE_TOKEN_ALIASES) and never emitted; a canonical
    name wins over an alias of the same count, whatever the order."""
    if isinstance(value, str) and value.strip():
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    if not isinstance(value, dict):
        return {}
    out = {}
    # canonical snake_case spellings win over aliases, whatever the key order
    for key, raw in value.items():
        if isinstance(key, str) and key.lower().replace("-", "_") in _USAGE_TOKEN_KEYS:
            count = _usage_count(raw)
            if count is not None:
                out[key.lower().replace("-", "_")] = count
    for key, raw in value.items():
        if not isinstance(key, str):
            continue
        name = _USAGE_TOKEN_ALIASES.get(re.sub(r"[^a-z0-9]", "", key.lower()))
        if name and name not in out:
            count = _usage_count(raw)
            if count is not None:
                out[name] = count
    return out


def _landed_usage(a, result_path):
    """The receipt's scrubbed usage block for the landed event, or None when there is none.

    Reads the persisted receipt (orbit.local-worker.result/v1) and projects
    observed.usage onto numeric counts under the canonical count names, plus
    observed tool_calls, a gated stop_reason and the gated model/route identity.
    Nothing else crosses: no prompts, file contents, provider credentials, quota
    state or local paths. Without at least one count the attribute stays absent
    entirely, so receipts without usage produce events byte-identical to the
    pre-usage shape (never zero-filled). The attempt keeps the event identity it
    already has, since the attribute lives outside the conflict evidence."""
    if result_path is None:
        return None
    try:
        receipt = json.loads(Path(result_path).read_text("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(receipt, dict):
        return None
    observed = receipt.get("observed")
    observed = observed if isinstance(observed, dict) else {}
    block = _usage_tokens(observed.get("usage"))
    if not block:
        return None
    tool_calls = _usage_count(observed.get("tool_calls"))
    if tool_calls is not None:
        block["tool_calls"] = tool_calls
    stop_reason = _usage_stop_reason(observed.get("stop_reason"))
    if stop_reason:
        block["stop_reason"] = stop_reason
    source = receipt.get("source") if isinstance(receipt.get("source"), dict) else {}
    brief = a.get("brief") or {}
    kind = a.get("kind") or source.get("kind") or brief.get("kind") or "opencode"
    model = _usage_model(a.get("model") or source.get("model") or brief.get("model"))
    if model:
        block["model"] = model
        route = _usage_route(kind, model)
        if route:
            block["route"] = route
    return block


def build_landed_event(a, cfg=None, result_path=None):
    """Build the canonical CloudEvents envelope for a persisted terminal attempt."""
    state = a.get("state")
    if state not in TERMINAL:
        raise ValueError(f"attempt is not terminal (state {state!r})")
    aid = str(a.get("attempt_id") or (a.get("brief") or {}).get("attempt_id") or "").strip()
    if not aid:
        raise ValueError("attempt_id is required")
    brief = a.get("brief") or {}
    cfg = cfg or {}
    kind = a.get("kind") or brief.get("kind") or "opencode"
    harness = a.get("harness") or ("agy" if kind == "agy-cua" else ("check" if kind == "check" else "opencode"))
    coordinator_id, coordinator_session = _coordinator_parts(a)
    if not coordinator_id:
        coordinator_id = str(cfg.get("launcher_id") or cfg.get("worker_id") or cfg.get("source") or "launcher").strip()
        coordinator_id = coordinator_id or "launcher"
    coordinator = {"identity": _bounded_text(coordinator_id, 256) or "launcher"}
    if coordinator_session:
        coordinator["session"] = _bounded_text(coordinator_session, 256)
        if not coordinator["session"]:
            coordinator.pop("session")
    backend = {"harness": _bounded_text(harness, 128) or "opencode"}
    model = a.get("model") or brief.get("model") or cfg.get("model")
    model = _bounded_text(model, 128)
    if model:
        backend["model"] = model
    source_rev = _source_rev(a, brief, aid, state)
    transition = (brief.get("terminal_transition_id") or a.get("terminal_transition_id") or
                  brief.get("transition_id") or a.get("transition_id") or
                  brief.get("transition") or a.get("transition") or state)
    event_id = landed_event_id(aid, state, transition)
    data = {
        "schema": LANDED_DATA_SCHEMA,
        "feature_id": _canonical_id(_brief_id(brief, "feature"), aid),
        "job_id": _canonical_id(_brief_id(brief, "job"), aid),
        "task_id": _canonical_id(_brief_id(brief, "task"), aid),
        "attempt_id": aid,
        "coordinator": coordinator,
        "backend": backend,
        "state": state,
        "source_rev": source_rev,
        "lease": {"sha256": _lease_sha256(a, source_rev), "assigned_files": _assigned_files(a, brief)},
        "result": _landed_result(a, result_path, aid),
    }
    for key in ("correlation_id", "causation_id"):
        value = _bounded_text(brief.get(key) if brief.get(key) is not None else a.get(key), 256)
        if value:
            data[key] = value
    event = {
        "specversion": "1.0",
        "id": event_id,
        "source": landed_source(cfg),
        "type": LANDED_EVENT_TYPE,
        "time": _iso_utc(a.get("updated_at") or a.get("created_at")),
        "subject": aid,
        "datacontenttype": "application/json",
        "dataschema": LANDED_EVENT_SCHEMA,
        "data": data,
    }
    usage = _landed_usage(a, result_path)
    if usage:
        event[LANDED_USAGE_ATTR] = usage
    return event


def _landing_evidence(data):
    data = data or {}
    core = {key: data[key] for key in _LANDED_EVIDENCE_KEYS if key in data}
    return _sha256_bytes(_canonical_bytes(core))


def _landing_conflicts(existing, event):
    old = existing.get("data") if isinstance(existing, dict) else None
    new = event.get("data") or {}
    if not isinstance(old, dict):
        return True
    if old.get("attempt_id") != new.get("attempt_id") or old.get("state") != new.get("state"):
        return True
    return _landing_evidence(old) != _landing_evidence(new)


def _landed_outbox(data_dir):
    return Path(data_dir) / "events" / "outbox"


def _find_landed_events(outbox, attempt_id):
    found = []
    # Caller owns the publication lock and has created the directory. A
    # failed listing/read is not evidence that this attempt has never landed.
    with os.scandir(outbox) as entries:
        paths = sorted(Path(e.path) for e in entries if e.name.endswith(".json") and e.is_file(follow_symlinks=False))
    for path in paths:
        try:
            event = json.loads(path.read_text("utf-8"))
        except (FileNotFoundError, ValueError):
            continue
        data = event.get("data") if isinstance(event, dict) else None
        if isinstance(data, dict) and data.get("attempt_id") == attempt_id:
            found.append((path, event))
    return found


def _fsync_dir(path, *, required=False):
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        if required:
            raise
        return
    try:
        os.fsync(fd)
    except OSError:
        if required:
            raise
    finally:
        os.close(fd)


def _register_landed_file(outbox, *, expected_event=None):
    """A registration failure leaves the file for recovery, but cannot authorize delivery."""
    if _landing_export is None:
        return False
    try:
        _landing_export.register_landed_file(outbox, expected_event=expected_event)
        return True
    except _landing_export.LandingContentConflict as ex:
        raise LandedEventConflict(str(ex)) from ex
    except Exception:
        # The next export scans the outbox and registers an unindexed file.
        return False


_WINDOWS_LANDED_NAMES = os.name == "nt"
_LANDED_PUBLISH_LOCK = threading.Lock()


@contextmanager
def _landed_publication(outbox):
    # Serialize the attempt-level conflict check *and* publication, even when
    # two terminal states have different IDs. Closing releases the OS lock on
    # both success and process death. Registration and notification run outside.
    outbox.mkdir(parents=True, exist_ok=True)
    with _LANDED_PUBLISH_LOCK, (outbox / ".publish.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def _landed_names(outbox, event_id):
    """Preferred basename, followed by any compatible pre-existing spelling."""
    plain = event_id + ".json"
    # '~' is outside the accepted ID alphabet. Hash the *case-sensitive* ID,
    # not a sanitized spelling: e.g. A:B, A_B and a:b must stay distinct.
    portable = "landed~" + hashlib.sha256(event_id.encode("ascii")).hexdigest() + ".json"
    if not _WINDOWS_LANDED_NAMES:
        return (plain, portable)  # also recognize an outbox copied from Windows
    # An older Windows writer could persist ordinary names. Never open an ADS
    # or DOS device. Only reuse the exact legacy spelling, not a folded alias.
    device = re.match(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])\.", plain, re.I)
    if ":" not in plain and not device and any(p.name == plain for p in outbox.iterdir()):
        return (portable, plain)
    return (portable,)


def _persist_landed_event(outbox, event, event_id):
    """Called under _landed_publication. Return (path, stored event, created)."""
    names = _landed_names(outbox, event_id)
    for candidate in names:
        path = outbox / candidate
        try:
            existing = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as ex:
            raise LandedEventConflict(f"existing landed event {event_id} is unreadable") from ex
        if (_landing_conflicts(existing, event) or
                existing.get("id", path.stem) != event.get("id")):
            raise LandedEventConflict(f"conflicting landed event at {candidate}")
        return path, existing, False
    path = outbox / names[0]
    payload = json.dumps(event, sort_keys=True, indent=1, ensure_ascii=False).encode("utf-8")
    nonce = uuid.uuid4().hex
    tmp = outbox / (f".{nonce}.tmp" if _WINDOWS_LANDED_NAMES else f".{event_id}.{os.getpid()}.{nonce}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(outbox)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
    return path, event, True


def write_landed_event(outbox, event):
    """Atomically persist one event; never overwrite conflicting evidence."""
    outbox = Path(outbox)
    event_id = str(event.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", event_id):
        raise ValueError("invalid landed event id")
    with _landed_publication(outbox):
        path, _, _ = _persist_landed_event(outbox, event, event_id)
    _register_landed_file(outbox)
    return path


def _landed_notifier(cfg):
    cfg = cfg or {}
    for key in ("notify_socket", "landed_socket", "landing_socket"):
        if cfg.get(key):
            return {"type": "unix", "path": cfg[key]}
    for key in ("notify_file", "notify_path", "landed_file", "landing_file"):
        if cfg.get(key):
            return {"type": "file", "path": cfg[key]}
    for key in ("landed_notifier", "landing_notifier", "bee_landed_notifier", "event_notifier",
                "outbox_notifier", "landed_notify", "notifier"):
        if cfg.get(key) is not None:
            return cfg[key]
    for section in ("landed", "landing", "bee_landed", "events"):
        block = cfg.get(section)
        if isinstance(block, dict) and block.get("notifier") is not None:
            return block["notifier"]
    return None


def _notifier_target(spec):
    options = {}
    if isinstance(spec, str):
        text = spec.strip()
        if text.startswith("unix://"):
            return "unix", text[7:], options
        if text.startswith("unix:"):
            return "unix", text[5:], options
        if text.startswith("file://"):
            return "file", text[7:], options
        if text.startswith("file:"):
            return "file", text[5:], options
        return "file", text, options
    if not isinstance(spec, dict):
        raise ValueError("notifier must be a path, URI, or object")
    options = spec
    kind = str(spec.get("type") or spec.get("kind") or spec.get("transport") or "file").lower()
    path = spec.get("path") or spec.get("file") or spec.get("socket") or spec.get("sock")
    if not isinstance(path, str) or not path:
        raise ValueError("notifier path is required")
    if kind in ("unix", "unix-socket", "unix_socket", "socket", "sock"):
        kind = "unix"
    else:
        kind = "file"
    return kind, path, options


def notify_landed_event(event, cfg=None, notifier=None):
    """Best-effort local delivery signal. A failure never removes or damages the outbox event."""
    spec = _landed_notifier(cfg) if notifier is None else notifier
    if spec is None:
        return False
    payload = (json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    try:
        kind, target, options = _notifier_target(spec)
        if kind == "unix":
            mode = str(options.get("mode") or options.get("socket_type") or "stream").lower()
            sock_type = socket.SOCK_DGRAM if mode in ("dgram", "datagram") else socket.SOCK_STREAM
            sock = socket.socket(socket.AF_UNIX, sock_type)
            try:
                sock.settimeout(float(options.get("timeout_s") or 2.0))
                sock.connect(target)
                if sock_type == socket.SOCK_DGRAM:
                    sock.send(payload)
                else:
                    sock.sendall(payload)
            finally:
                sock.close()
        else:
            path = Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
        return True
    except Exception:
        return False


def emit_landed_record(a, data_dir, cfg=None, result_path=None):
    """Write one idempotent landed event and signal delivery. Returns (path, event, created)."""
    cfg = cfg or {}
    event = build_landed_event(a, cfg, result_path)
    outbox = _landed_outbox(data_dir)
    with _landed_publication(outbox):
        for path, existing in _find_landed_events(outbox, event["data"]["attempt_id"]):
            if _landing_conflicts(existing, event):
                raise LandedEventConflict(f"conflicting terminal evidence for attempt {event['data']['attempt_id']}")
            stored, created = existing, False
            break
        else:
            path, stored, created = _persist_landed_event(outbox, event, event["id"])
    registered = _register_landed_file(outbox, expected_event=stored if created else None)
    if created and registered:
        notify_landed_event(stored, cfg)
    return path, stored, created


def replay_landed_events(data_dir, cfg=None, notifier=None):
    """Re-signal retained, pinned events in position order. Returns (delivered, total)."""
    if _landing_export is None:
        raise OSError("notifier replay requires the landing index")
    after = None
    delivered = total = 0
    while True:
        try:
            page = _landing_export.export_events(data_dir, after=after, registered_only=True)
        except _landing_export.LandingContentConflict as ex:
            raise LandedEventConflict(str(ex)) from ex
        for row in page["events"]:
            # Send exactly the verified object; never reopen a filename after
            # the exporter releases the index lock.
            total += 1
            delivered += 1 if notify_landed_event(row["event"], cfg, notifier) else 0
        if not page["has_more"]:
            return delivered, total
        after = page["next_cursor"]


# ---------------------------------------------------------------- deployment receipt (/v1/health)
# The admin apply-update records what it deployed in hyvv-deployed.json. /v1/health republishes only
# the validated identity fields so a coordinator can confirm the running build from deployment
# evidence instead of a bare version string. No other receipt content and no path is ever exposed.
DEPLOYMENT_RECEIPT_NAME = "hyvv-deployed.json"
_RECEIPT_MAX_BYTES = 64 * 1024
_RECEIPT_AT_KEYS = ("deployed_at", "applied_at", "installed_at_utc")
# The PC apply-update writes its receipt into the launcher's install directory (the folder holding
# the running launcher.py), with keys repo/sha/version/installed_at_utc/files.
LAUNCHER_DIR = Path(__file__).resolve().parent
_RECEIPT_REV_KEYS = ("rev", "revision", "source_rev", "sha", "commit")
_RECEIPT_VERSION_RE = re.compile(r"\d+(?:\.\d+){1,3}")
_RECEIPT_REV_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _receipt_paths(cfg):
    explicit = cfg.get("deployment_receipt_path")
    if explicit:  # a configured location never falls back to a default one
        return [Path(explicit).expanduser()], True
    paths = []
    if cfg.get("_config_path"):
        paths.append(Path(cfg["_config_path"]).expanduser().parent / DEPLOYMENT_RECEIPT_NAME)
    if cfg.get("data_dir"):
        paths.append(Path(cfg["data_dir"]).expanduser() / DEPLOYMENT_RECEIPT_NAME)
    install = LAUNCHER_DIR / DEPLOYMENT_RECEIPT_NAME
    if install not in paths:
        paths.append(install)
    return paths, False


def deployment_receipt(cfg):
    """``{"deployed_at", "version", "rev"}`` from the apply-update receipt, an invalid marker, or None.

    None means no receipt exists at the default locations (the key is then omitted from /v1/health).
    A configured path that is missing, an unreadable/oversize file, or a receipt without a zoned
    ``deployed_at`` timestamp and a well-formed identity (dotted version and/or 40/64-hex rev) yields
    ``{"status": "invalid", "reason": ...}`` -- never a repaired or partial receipt.
    """
    paths, explicit = _receipt_paths(cfg)
    path = next((p for p in paths if p.is_file()), None)
    if path is None:
        return {"status": "invalid", "reason": "receipt_unreadable"} if explicit else None
    invalid = lambda reason: {"status": "invalid", "reason": reason}
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_RECEIPT_MAX_BYTES + 1)
        if len(raw) > _RECEIPT_MAX_BYTES:
            return invalid("receipt_unreadable")
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeDecodeError):
        return invalid("receipt_unreadable")
    if not isinstance(data, dict):
        return invalid("receipt_malformed")
    at = next((data[k] for k in _RECEIPT_AT_KEYS if isinstance(data.get(k), str) and data[k].strip()), None)
    try:
        when = datetime.fromisoformat(at.strip().replace("Z", "+00:00")) if at else None
    except ValueError:
        when = None
    if when is None or when.tzinfo is None:
        return invalid("receipt_malformed")
    out = {"deployed_at": when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    version = data.get("version")
    if version is not None:
        if not (isinstance(version, str) and _RECEIPT_VERSION_RE.fullmatch(version.strip())):
            return invalid("receipt_malformed")
        out["version"] = version.strip()
    rev = next((data[k] for k in _RECEIPT_REV_KEYS if data.get(k) is not None), None)
    if rev is not None:
        if not (isinstance(rev, str) and _RECEIPT_REV_RE.fullmatch(rev.strip().lower())):
            return invalid("receipt_malformed")
        out["rev"] = rev.strip().lower()
    if "version" not in out and "rev" not in out:
        return invalid("receipt_malformed")
    return out


def load_config(path):
    cfg = json.loads(Path(path).read_text("utf-8-sig"))
    cfg.setdefault("port", 47821)
    cfg["max_concurrent"] = max(1, min(MAX_WORKERS, int(cfg.get("max_concurrent", 1))))
    cfg.setdefault("model", "deepseek-flash")
    cfg.setdefault("base_url", GO_BASE_URL)
    cfg.setdefault("wire", "openai-chat")
    cfg.setdefault("image", "ghcr.io/trycua/linux@sha256:fd00735ceb6c65ab628d962640068c9f63076a49a62d86f6b6eb5ac4bbcbbf6b")
    cfg.setdefault("runtime", "runc")
    cfg.setdefault("memory_mb", 6144)
    cfg.setdefault("cpus", 2)
    cfg.setdefault("backoff_s", [30, 60, 120, 240])
    try:
        default_timeout_s = int(cfg.get("default_timeout_s", DEFAULT_ATTEMPT_TIMEOUT_S))
    except (TypeError, ValueError, OverflowError):
        default_timeout_s = DEFAULT_ATTEMPT_TIMEOUT_S
    cfg["default_timeout_s"] = max(60, min(86400, default_timeout_s))
    cfg.setdefault("require_budget", False)
    cfg.setdefault("data_dir", str(Path.home() / ".orbit-launcher"))
    cfg.setdefault("projects", {})  # name -> {"repo": url} (authorized projects)
    cfg.setdefault("provisioning", {})  # {"policy": path, "journal": path, "enabled": bool}
    cfg.setdefault("extra_binds", ["127.0.0.1"])  # loopback listener; memory only, never written back
    cfg.setdefault("agy_exe", str(Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.exe"))
    cfg.setdefault("agy_permissions", None)  # None = agy_cua_mcp.DEFAULT_PERMS (UI only: no computer:shell/file)
    cfg.setdefault("security_profiles", {})  # name -> profile for check jobs (see validate_security_profiles)
    cfg.setdefault("check_security_profile", "default")
    validate_security_profiles(cfg)
    return cfg

# ---------------------------------------------------------------- security profiles (check jobs)
# Coding bees (opencode, agy-cua) always run on today's profile: the Cua sandbox with Docker's default
# seccomp. Cua 0.5.0's SandboxCreateOptions has no security_opt / seccomp / cap field, so a check job
# with a non-default profile is launched by `docker run` directly (DockerCheckRunner), never through Cua.
SECURITY_PROFILE_KEYS = {"image", "seccomp", "network", "memory_mb", "cpus", "pids_limit", "description"}
SECURITY_PROFILE_NETWORKS = ("none", "bridge")
_FORBIDDEN_ARGV = ("--privileged", "--cap-add", "--userns", "--pid", "--ipc", "--uts", "--device", "--volume", "-v", "--mount")
_IMAGE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}")

def validate_security_profiles(cfg):
    """Profiles may only name an image, a seccomp JSON file, a network (none|bridge) and limits.
    Anything that would widen privilege (privileged, cap_add, unconfined, apparmor, host network) is refused."""
    profs = cfg.get("security_profiles") or {}
    if not isinstance(profs, dict): raise ValueError("security_profiles must be an object")
    for name, p in profs.items():
        if name == "default": raise ValueError("security_profiles.default is built in and cannot be redefined")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", str(name)): raise ValueError(f"bad security profile name: {name}")
        if not isinstance(p, dict): raise ValueError(f"security_profiles.{name} must be an object")
        extra = set(p) - SECURITY_PROFILE_KEYS
        if extra: raise ValueError(f"security_profiles.{name}: unsupported keys {sorted(extra)}")
        if not isinstance(p.get("image"), str) or not _IMAGE_REF.fullmatch(p["image"]): raise ValueError(f"security_profiles.{name}.image must be an image reference")
        sc = p.get("seccomp")
        if sc is not None:
            if not isinstance(sc, str) or not sc.lower().endswith(".json") or "unconfined" in sc.lower() or "=" in sc or sc.startswith("-"):
                raise ValueError(f"security_profiles.{name}.seccomp must be a path to a seccomp .json file")
        if p.get("network", "none") not in SECURITY_PROFILE_NETWORKS:
            raise ValueError(f"security_profiles.{name}.network must be one of {SECURITY_PROFILE_NETWORKS}")
        for k in ("memory_mb", "cpus", "pids_limit"):
            if k in p and not (isinstance(p[k], int) and p[k] > 0): raise ValueError(f"security_profiles.{name}.{k} must be a positive integer")
        if any(x in json.dumps(p).lower() for x in ("privileged", "unconfined", "sys_admin")):
            raise ValueError(f"security_profiles.{name} must not widen privileges")
    csp = cfg.get("check_security_profile") or "default"
    if csp != "default" and csp not in profs: raise ValueError(f"check_security_profile {csp} is not defined")

def resolve_security_profile(cfg, kind, brief):
    """Pick the security profile for one job. Coding bees can only use "default"; check jobs may name any
    profile defined in launcher config (brief.security_profile, else cfg.check_security_profile)."""
    want = (brief or {}).get("security_profile")
    if want is not None and not isinstance(want, str): raise ValueError("brief.security_profile must be a string")
    if kind != "check":
        if want not in (None, "default"):
            raise ValueError("security_profile applies to check jobs only; coding bees use the default profile")
        return "default"
    name = want or cfg.get("check_security_profile") or "default"
    if name != "default" and name not in (cfg.get("security_profiles") or {}):
        raise ValueError(f"unknown security_profile: {name} (configured: {', '.join(['default'] + sorted(cfg.get('security_profiles') or {}))})")
    return name

def security_profile(cfg, name):
    if name == "default":
        return {"image": cfg["image"], "seccomp": None, "network": "none"}
    return dict(cfg["security_profiles"][name])

def docker_check_argv(cfg, profile_name, name):
    """`docker run` argv for a check-job container. Only adds limits and, for a non-default profile, its seccomp
    file; never privileged, never extra caps, never host namespaces or bind mounts."""
    p = security_profile(cfg, profile_name)
    argv = ["docker", "run", "-d", "--name", name, "--label", "io.hyvv.role=check", "--label", f"io.hyvv.security-profile={profile_name}",
            "--memory", f"{int(p.get('memory_mb') or cfg.get('memory_mb') or 4096)}m", "--cpus", str(int(p.get("cpus") or cfg.get("cpus") or 2)),
            "--pids-limit", str(int(p.get("pids_limit") or 4096)), "--network", p.get("network") or "none"]
    if p.get("seccomp"): argv += ["--security-opt", "seccomp=" + str(p["seccomp"])]
    argv += ["--entrypoint", "/bin/sh", p["image"], "-c", "sleep infinity"]
    for a in (x.lower() for x in argv):
        if "unconfined" in a or "apparmor" in a or any(a == f or a.startswith(f + "=") for f in _FORBIDDEN_ARGV):
            raise ValueError(f"refusing check container argv containing {a}")
    return argv

def _proc_run(argv, input=None, timeout=None):
    import subprocess
    r = subprocess.run(argv, input=input, capture_output=True, timeout=timeout)
    dec = lambda v: (v or b"").decode("utf-8", "replace")
    return r.returncode, dec(r.stdout), dec(r.stderr)

CHECK_TAIL = 16_000  # chars of stdout/stderr kept per check in the result (full log on disk)
CHECK_TIMEOUT_S = (10, 1800, 7200)  # min, default, max for brief.budget.timeout_s per command

def check_timeout_s(b):
    """brief.budget.timeout_s for ONE check command, clamped to CHECK_TIMEOUT_S (min, default, max)."""
    t = int(((b or {}).get("budget") or {}).get("timeout_s") or CHECK_TIMEOUT_S[1])
    return max(CHECK_TIMEOUT_S[0], min(CHECK_TIMEOUT_S[2], t))

class DockerCheckRunner:
    """Runs a check job: one throwaway container per attempt (security profile applied at `docker run`), the project
    seeded at /work/<project> as a fresh git repo, then each brief.commands entry via `docker exec`. The result is
    exit codes + logs, not a diff. Cleanup removes only this attempt's own container."""
    def __init__(self, cfg, run=None):
        self.cfg, self._run = cfg, run or _proc_run

    async def _call(self, argv, input=None, timeout=None):
        return await asyncio.to_thread(self._run, argv, input, timeout)

    async def start(self, a):
        cfg, b = self.cfg, a["brief"]
        prof = a.get("security_profile") or resolve_security_profile(cfg, "check", b)
        name = a.get("sandbox") or ("hyvv-check-" + re.sub(r"[^A-Za-z0-9_-]", "", a["attempt_id"])[:48])
        a["sandbox"] = name
        c, _, e = await self._call(docker_check_argv(cfg, prof, name), timeout=300)
        if c != 0: raise RuntimeError(f"docker run failed: {redact(e)[-300:]}")
        wd, rev, seed = None, None, None
        proj = cfg["projects"].get(b.get("project", ""), {})
        if proj.get("path"):
            rev, tar = export_project(proj["path"], sync_branch=proj.get("sync_branch"), source_rev=b.get("source_rev"), branch=b.get("branch"))
            wd = "/work/" + re.sub(r"[^A-Za-z0-9._-]", "_", b["project"])
            c, _, e = await self._call(["docker", "exec", "-i", name, "sh", "-c", f"mkdir -p '{wd}' && tar -xf - -C '{wd}'"], input=tar, timeout=300)
            if c != 0: raise RuntimeError(f"seeding check container failed: {redact(e)[-300:]}")
            c, o, e = await self._call(["docker", "exec", "-w", wd, name, "sh", "-c",
                                        f"git init -q && git add -A && git -c user.name=orbit -c user.email=orbit@local commit -qm 'base {rev}' && git update-ref refs/hyvv/seed HEAD && git rev-parse HEAD"], timeout=300)
            if c != 0: raise RuntimeError(f"seeding check container failed: {redact(e)[-300:]}")
            seed = (o.strip().splitlines() or [""])[-1][:64]
        return {"sandbox": name, "sandbox_id": name, "run_id": "check-" + uuid.uuid4().hex[:8], "workdir": wd,
                "local_base_rev": rev, "seed_commit": seed, "security_profile": prof}

    async def run_checks(self, a):
        b = a["brief"]; out = []
        t = check_timeout_s(b)
        logs = Path(self.cfg["data_dir"]) / "attempts"
        for i, cmd in enumerate(b.get("commands") or []):
            if a.get("cancel"): break
            t0 = time.time()
            argv = ["docker", "exec"] + (["-w", a["workdir"]] if a.get("workdir") else []) + [a["sandbox"], "bash", "-lc", cmd]
            try: c, o, e = await self._call(argv, timeout=t)
            except Exception as ex: c, o, e = None, "", f"{type(ex).__name__}: timed out or failed to run"
            o, e = redact(o), redact(e)
            log = logs / f"{a['attempt_id']}.check-{i + 1}.log"
            try: log.write_text(f"$ {cmd}\n--- stdout\n{o}\n--- stderr\n{e}\n--- exit {c}\n", "utf-8")
            except OSError: log = None
            out.append({"name": f"check-{i + 1}", "command": cmd, "exit_code": c, "duration_s": round(time.time() - t0, 3),
                        "stdout_tail": o[-CHECK_TAIL:], "stderr_tail": e[-CHECK_TAIL:], "log_file": str(log) if log else None})
        return out

    async def cleanup(self, a):
        name = a.get("sandbox")
        if not name or not name.startswith("hyvv-check-"): return
        try: await self._call(["docker", "rm", "-f", name], timeout=120)
        except Exception: pass

def check_bind(host):
    ip = ipaddress.ip_address(host)
    if ip.is_unspecified:
        raise SystemExit("refusing to bind 0.0.0.0/:: - set bind to the PC's Tailscale IP (100.x)")
    if not (ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10") or ip in ipaddress.ip_network("fd7a:115c:a1e0::/48")):
        raise SystemExit(f"refusing to bind {host}: only loopback or a Tailscale address is allowed")

_KEY_ENV = {"go": "ORBIT_WORKER_GO_KEY", "deepseek": "DEEPSEEK_API_KEY", "zoo": "ORBIT_WORKER_ZOO_KEY", "kimi": "ORBIT_WORKER_KIMI_KEY", "nous": "ORBIT_WORKER_NOUS_KEY"}
_KEY_FILE_CFG = {"go": "go_key_file", "deepseek": "deepseek_key_file", "zoo": "zoo_key_file", "kimi": "kimi_key_file", "nous": "nous_key_file"}
_KEY_FILE_NAME = {"go": "go.key", "deepseek": "deepseek.key", "zoo": "zoo.key", "kimi": "kimi.key", "nous": "nous.key"}

def provider_key(cfg, source):
    """Provider key for a named source (`go`, `deepseek`, `zoo`, `kimi` or `nous`): env first, else a user-only file. Never logged."""
    env_name = _KEY_ENV.get(source)
    if env_name:
        v = os.environ.get(env_name)
        if v: return v.strip()
    cfg_key = _KEY_FILE_CFG.get(source)
    default = _KEY_FILE_NAME.get(source)
    if not default:
        return ""
    p = Path((cfg.get(cfg_key) if cfg_key else None) or (Path(cfg["data_dir"]) / default))
    return p.read_text("utf-8").strip() if p.exists() else ""

def read_secret(cfg, name="ORBIT_WORKER_GO_KEY"):
    """Back-compat wrapper around provider_key. Never logged or returned."""
    if name == "DEEPSEEK_API_KEY":
        return provider_key(cfg, "deepseek")
    return provider_key(cfg, "go")

# ---------------------------------------------------------------- backends
# Every real-SDK call the backend makes, checked against the installed package by
# `launcher.py doctor` / sdk_contract_check() (tests run it against cua==0.5.0).
#   (class in cua._native, method, must_be_async, required parameter names)
SDK_CONTRACT = [
    ("Cua", "sandboxes", False, []),
    ("Sandboxes", "create", True, ["options"]),
    ("Sandboxes", "connect", True, ["name"]),     # name -> Sandbox handle (get() returns SandboxInfo: no agents())
    ("Sandboxes", "delete", True, ["name"]),
    ("Sandbox", "agents", True, []),
    ("Sandbox", "name", False, []),
    ("Sandbox", "spacesd", True, ["probe_timeout_ms"]),
    ("SpacesdClient", "sh", True, ["line", "timeout_ms"]),
    ("SpacesdClient", "upload", True, ["path", "data", "options"]),
    ("Sandbox", "id", False, []),
    ("Agents", "run", True, ["harness", "prompt", "options"]),
    ("Agents", "get", True, ["run_id"]),
    ("AgentRun", "run_id", False, []),
    ("AgentRun", "events", True, ["cursor", "max"]),
    ("AgentRun", "interrupt", True, []),
    ("AgentRun", "stop", True, []),
    ("AgentRun", "result", True, []),
    ("AgentRun", "artifacts", True, []),
]
SDK_TYPES = {  # constructor keyword fields the backend passes or reads
    "SandboxCreateOptions": ["on", "kind", "runtime", "image", "name", "memory_mb", "cpus", "wait_for"],
    "ReadinessProbe": ["service"],
    "AgentRunOptions": ["env", "model", "base_url", "wire", "repo", "branch", "cwd", "exit_when_idle", "label"],
    "AgentEventPage": ["events", "cursor", "caught_up"],
    "AgentEvent": ["seq", "kind", "line", "text", "stop_reason"],
    "AgentRunResult": ["status", "stop_reason", "text", "tool_calls", "usage_json", "error"],
    "AgentArtifact": ["path", "size"],
    "ProcessOutput": ["stdout", "stderr", "pty", "exit"],
}

def sdk_contract_check():
    import inspect
    import cua
    from cua import _native as n
    problems = []
    for name in ("embedded", "SandboxCreateOptions", "AgentRunOptions", "ReadinessProbe", "cua_sdk_version"):
        if not hasattr(cua, name): problems.append(f"cua.{name} missing")
    try:
        if "state_dir" not in inspect.signature(cua.embedded).parameters: problems.append("cua.embedded lacks state_dir (agy-cua needs a per-attempt state dir)")
    except (TypeError, ValueError): problems.append("cua.embedded signature unreadable")
    for cls, meth, is_async, params in SDK_CONTRACT:
        c = getattr(n, cls, None); f = getattr(c, meth, None) if c else None
        if f is None: problems.append(f"{cls}.{meth} missing"); continue
        if inspect.iscoroutinefunction(f) != is_async: problems.append(f"{cls}.{meth} async={not is_async} expected {is_async}")
        got = [x for x in inspect.signature(f).parameters if x != "self"]
        for x in params:
            if x not in got: problems.append(f"{cls}.{meth} lacks parameter {x} (has {got})")
    for typ, fields in SDK_TYPES.items():
        c = getattr(n, typ, None)
        if c is None: problems.append(f"type {typ} missing"); continue
        got = inspect.signature(c.__init__).parameters
        for x in fields:
            if x not in got: problems.append(f"{typ} lacks field {x}")
    return problems

class WorkspaceError(RuntimeError):
    """Measured workspace/collection failure; never an ordinary empty patch."""
    def __init__(self, code, stage, detail, exit_code=None):
        self.code, self.stage, self.exit_code = code, stage, exit_code
        self.detail = redact(str(detail))[-300:]
        super().__init__(f"{code} ({stage}): {self.detail}")

    def to_dict(self):
        return {"code": self.code, "stage": self.stage, "exit_code": self.exit_code, "detail": self.detail}


class CuaBackend:
    """Real backend: cua 0.5.0 SDK, local Docker Desktop sandboxes (runc).
    Handles: client.sandboxes() (sync) -> Sandboxes; await .create(opts)/.connect(name) -> Sandbox;
    await Sandbox.agents() -> Agents; await Agents.run(harness, prompt, opts)/.get(run_id) -> AgentRun."""
    def __init__(self, cfg):
        import cua  # deferred so tests/doctor work without it
        self.cua, self.cfg = cua, cfg
        self.client = cua.embedded()
        self._sb = {}
        self._sdc = {}
        self.agy = AgyCuaRunner(self)
        self.checks = DockerCheckRunner(cfg)  # check jobs: docker directly (Cua cannot set a seccomp profile)

    async def check_start(self, a): return await self.checks.start(a)
    async def run_checks(self, a): return await self.checks.run_checks(a)
    async def check_cleanup(self, a): return await self.checks.cleanup(a)

    async def _sandbox(self, name):
        if name not in self._sb:  # reattach after restart: connect by name, never get() (SandboxInfo)
            self._sb[name] = await self.client.sandboxes().connect(name)
        return self._sb[name]

    async def _sd(self, sb):
        # Container sandboxes: shell/files go through cua-spacesd (guest_sh is Lume/agentless only).
        key = sb.name()
        if key not in self._sdc: self._sdc[key] = await sb.spacesd(None)
        return self._sdc[key]

    async def _sh(self, sb, line, timeout_ms=120000):
        out = await (await self._sd(sb)).sh(line, timeout_ms)
        dec = lambda v: v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v or "")
        code = getattr(out.exit, "code", None)
        return dec(out.stdout) + dec(out.pty), dec(out.stderr), code

    async def _checked_sh(self, sb, line, *, stage, code="workspace_invalid"):
        try:
            out, err, status = await self._sh(sb, line)
        except Exception as ex:
            raise WorkspaceError(code, stage, f"shell transport failed: {type(ex).__name__}") from ex
        # Missing status is not evidence that the sandbox command succeeded.
        if type(status) is not int or status != 0:
            raise WorkspaceError(code, stage, err or "command did not report exit code 0",
                                 status if type(status) is int else None)
        return out

    async def _verify_workspace(self, sb, wd, seed, *, code="workspace_invalid"):
        if not isinstance(wd, str) or not wd.startswith("/"):
            raise WorkspaceError(code, "workspace", "a seeded absolute sandbox workdir is required")
        if not isinstance(seed, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", seed):
            raise WorkspaceError(code, "seed", "a recorded seed commit is required; refusing a HEAD fallback")
        # A nested directory in some other checkout is not this attempt's root.
        # Git itself resolves .git files, so linked worktrees are supported.
        out = await self._checked_sh(sb,
            f"cd {shlex.quote(wd)} && root=$(git rev-parse --show-toplevel) && "
            'test "$root" = "$(pwd -P)" && '
            f"git rev-parse --verify {shlex.quote(seed + '^{commit}')}", stage="workspace", code=code)
        if out.strip() != seed:
            raise WorkspaceError(code, "seed", "the sandbox did not verify the recorded seed commit")

    async def _seed_project(self, sb, proj, name, source_rev=None, branch=None):
        """Copy an authorized local git repo into the sandbox; returns (workdir, base_rev, seed_commit).

        seed_commit is the in-sandbox commit the copy starts from (also kept as refs/hyvv/seed); the patch
        collector diffs against it, so a bee that commits or moves HEAD does not empty the patch.

        Prefer brief source_rev (optionally after fetching branch); otherwise sync_branch / HEAD.
        """
        src = proj["path"]
        rev, tar = export_project(src, sync_branch=proj.get("sync_branch"), source_rev=source_rev, branch=branch)
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name in (".", ".."):
            raise WorkspaceError("workspace_invalid", "seed", "invalid project directory name")
        home = (await self._checked_sh(sb, 'printf %s "$HOME"', stage="home")).strip()
        if not home.startswith("/"):
            raise WorkspaceError("workspace_invalid", "home", "sandbox HOME must be an absolute directory")
        wd = f"{home}/work/{name}"
        await self._checked_sh(sb, f"rm -rf {shlex.quote(wd)} /tmp/orbit-src.tar && mkdir -p {shlex.quote(wd)}", stage="prepare")
        await (await self._sd(sb)).upload("/tmp/orbit-src.tar", tar, None)
        o = await self._checked_sh(sb, f"cd {shlex.quote(wd)} && tar -xf /tmp/orbit-src.tar && git init -q && "
                                     f"printf '__pycache__/\\n*.pyc\\n.pytest_cache/\\nnode_modules/\\n' >> .git/info/exclude && git add -A && "
                                     f"git -c user.name=orbit -c user.email=orbit@local commit --allow-empty -qm 'base {rev}' && git update-ref refs/hyvv/seed HEAD && git rev-parse HEAD",
                                     stage="seed")
        seed = o.strip()
        await self._verify_workspace(sb, wd, seed)
        return wd, rev, seed

    async def environment(self, a):
        if a.get("kind") == "agy-cua": return await self.agy.environment(a)
        sb = await self._sandbox(a["sandbox"])
        o, _, _ = await self._sh(sb, "printf 'host=%s\\nkernel=%s\\n' \"$(hostname)\" \"$(uname -sr)\"; "
                                     "ls -d $HOME/.cua/tools/opencode/* 2>/dev/null | head -1 | sed 's/^/opencode_install=/'; "
                                     "(command -v opencode >/dev/null && opencode --version || ls $HOME/.cua/tools/opencode 2>/dev/null) | head -1 | sed 's/^/opencode_version=/'")
        env = dict(l.split("=", 1) for l in o.splitlines() if "=" in l)
        env.update({"sandbox_name": a["sandbox"], "sandbox_id": a.get("sandbox_id"), "workdir": a.get("workdir"), "cua_run_id": a.get("run_id")})
        return env

    async def patch(self, a):
        if not a.get("workdir"):
            if a["brief"].get("assigned_files") or (a.get("kind") == "agy-cua" and a.get("mode") == "code"):
                raise WorkspaceError("patch_collection_failed", "workspace", "scoped code attempt has no seeded workdir")
            return None
        sb = await self.agy.sandbox(a) if a.get("kind") == "agy-cua" else await self._sandbox(a["sandbox"])
        wd = a["workdir"]
        # Diff against the seed commit recorded at start, not HEAD: a bee that commits (or `git checkout -B`)
        # moves HEAD, and a HEAD diff would then come back empty. Missing baselines must fail explicitly.
        base = a.get("seed_commit")
        await self._verify_workspace(sb, wd, base, code="patch_collection_failed")
        prefix = f"cd {shlex.quote(wd)} && "
        async def collect(command, stage):
            return await self._checked_sh(sb, prefix + command, stage=stage, code="patch_collection_failed")
        lease = a["brief"].get("assigned_files")
        scoped = isinstance(lease, (list, tuple)) and bool(lease)
        untracked = None
        if scoped:
            # Untracked worktree files must be recorded before `git add -A` hides them: a bee's own
            # tooling (lockfiles, caches) creates them, and they must not ride along in the payload.
            untracked = {p for p in (await collect("git ls-files --others --exclude-standard -z", "untracked")).split("\0") if p}
        await collect("git add -A", "stage")
        async def entries():
            rows = await collect(f"git diff --cached --name-status --no-renames -z {base}", "names")
            parts = rows.split("\0") if rows else [""]
            if parts[-1] != "" or (len(parts) - 1) % 2:
                raise WorkspaceError("patch_collection_failed", "names", "invalid Git name-status output")
            out = []
            for st, path in zip(parts[:-1:2], parts[1:-1:2]):
                if st not in ("A", "M", "D", "T") or not path:
                    raise WorkspaceError("patch_collection_failed", "names", "invalid Git change entry")
                out.append((st, path))
            return out
        rows = await entries()
        # Only a brand-new staged file can be an untracked side-effect artifact. A path that exists
        # in the base tree is a real tracked change (M/D/T) and always reaches the verifier, in
        # lease or not; without a lease nothing is filtered.
        dropped = [path for st, path in rows if st == "A" and path in untracked and not in_assigned_scope(path, lease)] if scoped else []
        if dropped:
            await collect("git rm -q --cached -f --ignore-unmatch -- " + " ".join(shlex.quote(p) for p in dropped), "unstage")
            rows = await entries()
        files = []
        for st, path in rows:
            sha = None
            if st != "D":
                # No pipeline: a missing/unreadable file must not look like a successful cut(1).
                out = await collect(f"sha256sum < {shlex.quote(path)}", "hash")
                sha = (out.split() or [""])[0]
                if not re.fullmatch(r"[0-9a-f]{64}", sha):
                    raise WorkspaceError("patch_collection_failed", "hash", "invalid file digest")
            files.append({"path": path, "status": st, "sha256": sha, "change": "deleted" if st.startswith("D") else ("added" if st.startswith("A") else "changed"),
                          "in_assigned_scope": in_assigned_scope(path, lease)})
        diff = await collect(f"git diff --cached --binary {base}", "diff")
        if bool(diff) != bool(files):
            raise WorkspaceError("patch_collection_failed", "diff", "patch and changed-file evidence disagree")
        evidence = {"diff": diff, "files": files, "base": base}
        if dropped:
            evidence["excluded_artifacts"] = [{"path": p, "reason": "untracked_outside_assigned_files"} for p in dropped]
        elif not scoped:
            # No lease to filter against: everything is collected exactly as before, and the evidence says why.
            evidence["scope_note"] = "no assigned_files lease: untracked files collected unfiltered"
        return evidence

    async def _inject_bbs_token(self, sb, attempt):
        """Mint via bbs-beetest/mint-token.ps1 (DPAPI refresh credential stays on this PC), upload only the access
        token into the sandbox, delete the local copy. Failure is non-fatal: the bee then runs stand-in only."""
        import subprocess
        d = Path(self.cfg.get("bbs_beetest_dir") or Path(self.cfg["data_dir"]) / "bbs-beetest"); aid = attempt["attempt_id"]
        out = d / "tokens" / f"{aid}.token"
        try:
            r = await asyncio.to_thread(subprocess.run, ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(d / "mint-token.ps1"),
                                                         "-Bee", aid, "-OutFile", str(out)], capture_output=True, text=True, timeout=360)
            if r.returncode != 0 or not out.exists():
                lines = (r.stdout or "").strip().splitlines()
                attempt["bbs_token"] = "mint failed: " + redact(lines[-1] if lines else "no output")[:200]; return None
            m = re.search(r"expires_at=(\S+)", r.stdout or ""); exp = m.group(1) if m else "unknown"
            sd = await self._sd(sb)
            await sd.upload("/tmp/toh-bbs-test.token", out.read_bytes(), None)
            await sd.upload("/tmp/toh-bbs-test.token.expires_at", exp.encode(), None)
            attempt["bbs_token"] = f"injected, expires_at={exp}"; return "/tmp/toh-bbs-test.token"
        except Exception as ex:
            attempt["bbs_token"] = f"mint error: {type(ex).__name__}"; return None
        finally:
            try: out.unlink()
            except OSError: pass

    async def start(self, attempt, key):
        if attempt.get("kind") == "agy-cua": return await self.agy.start(attempt)
        cua, cfg, b = self.cua, self.cfg, attempt["brief"]
        proj = cfg["projects"].get(b.get("project", ""), {})
        if b.get("assigned_files") and not proj.get("path"):
            raise WorkspaceError("workspace_invalid", "project", "scoped code work requires a registered local-path project")
        name = attempt.get("sandbox") or sandbox_name(attempt)
        attempt["sandbox"] = name  # recorded before create so a failed start still cleans up the right sandbox
        sb = await self.client.sandboxes().create(cua.SandboxCreateOptions(
            on="local", kind="container", runtime=cfg["runtime"], image=cfg["image"], name=name,
            memory_mb=cfg["memory_mb"], cpus=cfg.get("cpus", 2), wait_for=[cua.ReadinessProbe(service="env")]))
        self._sb[name] = sb
        wd, base, seed = (None, None, None)
        if proj.get("path"):
            wd, base, seed = await self._seed_project(sb, proj, b["project"], source_rev=b.get("source_rev"), branch=b.get("branch"))
            attempt.update(workdir=wd, local_base_rev=base, seed_commit=seed)
        spec = model_spec(cfg, attempt.get("model"), attempt.get("brief"))
        env = {"OPENCODE_CONFIG_CONTENT": json.dumps(dict(WORKER_OPENCODE_CONFIG, **({"model": spec["model"]} if spec["key"] == "none" else {})))}
        if spec["key"] in ("go", "deepseek", "zoo", "kimi", "nous"): env["OPENAI_API_KEY"] = key  # OpenAI-compatible: DeepSeek official, OpenCode Go, the Zoo gateway, Kimi direct or Nous Portal
        if b.get("verify_of") and wd:  # verify bee: hand it the finished work bee's patch (never a credential)
            vp = Path(cfg["data_dir"]) / "attempts" / f"{b['verify_of']}.result.json"
            diff = ((json.loads(vp.read_text("utf-8")).get("patch") or {}).get("diff") or "") if vp.exists() else ""
            if not diff.strip(): raise ValueError("verify_of attempt has no patch to verify")
            await (await self._sd(sb)).upload("/tmp/verify-worker.patch", diff.encode("utf-8"), None)
            env["VERIFY_PATCH_FILE"] = "/tmp/verify-worker.patch"
        if b.get("bbs_test_token"):  # coordinator opt-in: one ~15-min toh-beetest access token (never the refresh credential)
            tf = await self._inject_bbs_token(sb, attempt)
            if tf: env["TOH_BBS_TEST_TOKEN_FILE"] = tf
        prompt = render_prompt(b)
        if spec.get("max_prompt_chars") and len(prompt) > spec["max_prompt_chars"]:
            raise ValueError(f"brief too large for {spec['name']} ({len(prompt)} chars > {spec['max_prompt_chars']})")
        opts = cua.AgentRunOptions(
            env=env, model=spec["model"], base_url=spec["base_url"], wire=spec["wire"],
            repo=None if wd else proj.get("repo"), branch=None if wd else b.get("branch"), cwd=wd or b.get("workdir"),
            exit_when_idle=True, label=attempt["attempt_id"])
        agents = await sb.agents()
        run = await agents.run("opencode", prompt, opts)
        return {"sandbox": name, "sandbox_id": sb.id(), "run_id": run.run_id(), "workdir": wd, "local_base_rev": base, "seed_commit": seed}

    async def _run(self, a):
        agents = await (await self._sandbox(a["sandbox"])).agents()
        return await agents.get(a["run_id"])

    async def events(self, a, cursor):
        if a.get("kind") == "agy-cua": return await self.agy.events(a, cursor)
        page = await (await self._run(a)).events(cursor, 200)
        if self.cfg.get('rails_enabled') is True:
            from rails_log_source import sdk_tool_fields
        return [{"seq": e.seq, "kind": e.kind, "line": redact(e.line or ""), "stop_reason": e.stop_reason,
                 "text": redact(e.text) if e.kind in ("message", "error", "turn_ended") else None,
                 **(sdk_tool_fields(e) if self.cfg.get('rails_enabled') is True else {})} for e in page.events], page.cursor

    async def interrupt(self, a):
        if a.get("kind") == "agy-cua": return await self.agy.interrupt(a)
        r = await self._run(a)
        await r.interrupt()
        await r.stop()

    async def result(self, a):
        if a.get("kind") == "agy-cua": return await self.agy.result(a)
        r = await self._run(a)
        res = await r.result()
        arts = [{"path": x.path, "size": x.size} for x in await r.artifacts()]
        return {"status": res.status, "stop_reason": res.stop_reason, "text": redact(res.text),
                "tool_calls": res.tool_calls, "usage": res.usage_json, "error": redact(res.error), "artifacts": arts}

    async def file_sha256(self, a, path):
        if a.get("kind") == "agy-cua": return None
        sb = await self._sandbox(a["sandbox"])
        safe = path.replace("'", "'\\''")
        o, _, _ = await self._sh(sb, f"cd '{a.get('workdir') or '.'}' && sha256sum -- '{safe}' 2>/dev/null | cut -d' ' -f1")
        return o.strip()[:64] or None

    async def cleanup(self, a):
        if a.get("kind") == "agy-cua": return await self.agy.cleanup(a)
        name = a.get("sandbox")
        if not name: return
        try: await self.client.sandboxes().delete(name)
        except Exception: pass
        self._sb.pop(name, None)


def agy_attempt_dir(cfg, aid): return Path(cfg["data_dir"]) / "agy-cua" / aid

def agy_child_env(base, sandbox, state_dir, perms=None, cua_exe=None):
    """Env for the agy process: no launcher secrets, no Cua daemon/host tokens; only this attempt's sandbox refs."""
    env = {k: v for k, v in base.items() if not k.startswith(("CUA_", "ORBIT_"))
           and not re.search(r"(TOKEN|SECRET|API_KEY|PASSWORD)", k, re.I)}
    env.update(ORBIT_AGY_CUA_SANDBOX=sandbox, ORBIT_AGY_CUA_STATE_DIR=str(state_dir))
    if perms: env["ORBIT_AGY_CUA_PERMISSIONS"] = perms
    if cua_exe: env["ORBIT_AGY_CUA_EXE"] = cua_exe
    return env

def agy_argv(cfg, prompt, model, timeout_s):
    """agy print mode. No --dangerously-skip-permissions and no --mode auto: print mode denies every tool
    not on the settings.json allow list, and the only allow rule is mcp(orbit-cua/*)."""
    if model not in AGY_MODELS: raise ValueError(f"agy model not allowed: {model}")
    return [cfg["agy_exe"], "-p", prompt, "--model", model, "--output-format", "json",
            "--print-timeout", f"{int(timeout_s)}s", "--disable-slash-commands"]

def agy_timeout(b, mode="ui"):
    t = (b.get("budget") or {}).get("timeout_s") or (1800 if mode == "code" else AGY_TIMEOUT_S[1])
    return max(AGY_TIMEOUT_S[0], min(AGY_TIMEOUT_S[2], int(t)))

def _attempt_default_timeout(cfg):
    """The host's default attempt wall clock: config `default_timeout_s`, a positive whole number
    of seconds, falling back to DEFAULT_ATTEMPT_TIMEOUT_S when it is missing, malformed or zero."""
    try:
        default = int((cfg or {}).get("default_timeout_s", DEFAULT_ATTEMPT_TIMEOUT_S))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_ATTEMPT_TIMEOUT_S
    return default if default > 0 else DEFAULT_ATTEMPT_TIMEOUT_S

def attempt_budget_s(a, cfg=None):
    """The attempt wall clock in seconds from brief.budget.timeout_s or the host default.

    budget.timeout_s must bound the whole attempt, not only the child it is handed to: whatever a
    worker does with its own flag, the launcher stops the attempt at this deadline plus
    BUDGET_STOP_GRACE_S. Kind semantics preserved: agy-cua keeps the AGY_TIMEOUT_S min/default/max
    clamp (and the code-mode default), check keeps the CHECK_TIMEOUT_S per-command clamp and gets a
    per-command-derived attempt clock (each command stays individually bounded), opencode enforces
    exactly a positive configured value and otherwise uses default_timeout_s.
    """
    b = a.get("brief") or {}
    kind = a.get("kind") or b.get("kind") or "opencode"
    if kind == "agy-cua":
        return agy_timeout(b, a.get("mode") or "ui")
    if kind == "check":
        return check_timeout_s(b) * (len(b.get("commands") or []) + 1) + CHECK_ATTEMPT_OVERHEAD_S
    default = _attempt_default_timeout(cfg)
    budget = b.get("budget")
    raw = budget.get("timeout_s") if isinstance(budget, dict) else None
    try:
        t = int(raw)
    except (TypeError, ValueError, OverflowError):
        return default
    return t if t > 0 else default

def render_agy_prompt(b):
    parts = [f"# Objective\n{b['objective']}"]
    for k, title in [("deliverables", "Deliverables"), ("acceptance", "Acceptance criteria"), ("deps", "Context")]:
        if b.get(k): parts.append(f"# {title}\n" + "\n".join(f"- {x}" for x in b[k]))
    parts.append(f"# Rules\n- You control ONE isolated Linux sandbox desktop, only through the `{AGY_MCP_NAME}` MCP tools. "
                 "Start with a screenshot.\n- Never pass a `sandbox` argument; the default sandbox is the only one you may use.\n"
                 "- Do not try to use or reach the host computer, its files or its shell.\n- Do not start subagents. Do not print secrets.")
    parts.append("# Return format\nEnd with a JSON block: {\"summary\":..., \"observations\":[...], \"checks\":[{\"name\":...,\"passed\":bool}], \"open_issues\":[...]}")
    return "\n\n".join(parts)

def render_agy_code_prompt(b, workdir):
    parts = [f"# Objective\n{b['objective']}"]
    for k, title in [("deliverables", "Deliverables"), ("acceptance", "Acceptance criteria"), ("assigned_files", "Files you may change"),
                     ("commands", "Commands you may run"), ("deps", "Context")]:
        if b.get(k): parts.append(f"# {title}\n" + "\n".join(f"- {x}" for x in b[k]))
    parts.append(f"# Where the code is\nThe repository is checked out INSIDE an isolated Linux sandbox at `{workdir}`. "
                 f"Read, edit and test it ONLY with the `{AGY_MCP_NAME}` MCP tools (computer shell and file tools); always `cd {workdir}` first. "
                 "The host computer is not yours: it has no repo and you have no host tools.")
    parts.append("# Rules\n- Change only files inside the working directory (and only the assigned files, if listed).\n"
                 "- Do NOT commit, push or create branches: the launcher collects your changes as a patch against the base commit.\n"
                 "- Never pass a `sandbox` argument. Do not start subagents. Do not print secrets or environment variables.")
    parts.append("# Return format\nEnd with a JSON block: {\"summary\":..., \"changed_files\":[...], \"checks\":[{\"name\":...,\"command\":...,\"passed\":bool}], \"open_issues\":[...]}")
    return "\n\n".join(parts)

def parse_agy_output(raw):
    """agy --output-format json -> (text, meta). Tolerates leading log lines and unknown shapes."""
    raw = raw or ""; obj = None
    for cand in (raw.strip(), raw[raw.find("{"):] if "{" in raw else ""):
        try: obj = json.loads(cand); break
        except Exception: pass
    if not isinstance(obj, dict): return raw.strip()[-8000:], {"parsed": False}
    text = next((obj[k] for k in ("response", "result", "text", "output", "message") if isinstance(obj.get(k), str)), None)
    # agy 1.4.4 json: conversation_id, status, response, duration_seconds, num_turns, usage{input,output,thinking,cache_read,total}_tokens, denied_actions[]
    meta = {k: obj[k] for k in obj if k not in ("response", "result", "text", "output", "message")}
    meta["parsed"] = True
    return (text if text is not None else json.dumps(obj)[:8000]), meta

def parse_agy_quota(raw):
    """`agy -p /usage --output-format json` -> [{group, id, window, remaining_fraction, reset_time_utc, reset_time_local}]."""
    import datetime
    try: groups = json.loads(raw)["command"]["data"]["groups"]
    except Exception: return None
    out = []
    for g in groups or []:
        for b in g.get("buckets") or []:
            rt = b.get("reset_time"); loc = None
            try: loc = datetime.datetime.fromisoformat(rt.replace("Z", "+00:00")).astimezone().isoformat(timespec="minutes")
            except Exception: pass
            out.append({"group": g.get("name"), "id": b.get("id"), "window": b.get("window"),
                        "remaining_fraction": b.get("remaining_fraction"), "reset_time_utc": rt, "reset_time_local": loc})
    return out

def agy_quota(cfg, cwd):
    """Remaining plan quota (0 tokens: a local slash command). None if agy cannot answer within 45s."""
    import subprocess
    try:
        env = agy_child_env(os.environ, "", ""); [env.pop(k, None) for k in ("ORBIT_AGY_CUA_SANDBOX", "ORBIT_AGY_CUA_STATE_DIR")]
        r = subprocess.run([cfg["agy_exe"], "-p", "/usage", "--output-format", "json"], cwd=str(cwd), env=env, capture_output=True,
                           timeout=45, stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return parse_agy_quota(r.stdout.decode("utf-8", "replace"))
    except Exception: return None

def usage_totals(log, day):
    """Per-model totals for one local date from the append-only agy usage log."""
    tot = {}
    try: lines = Path(log).read_text("utf-8").splitlines()
    except FileNotFoundError: return tot
    for l in lines:
        try: x = json.loads(l)
        except Exception: continue
        if x.get("date") != day: continue
        t = tot.setdefault(x.get("model") or "?", {"runs": 0, **{k: 0 for k in USAGE_FIELDS}, "duration_seconds": 0.0})
        t["runs"] += 1; t["duration_seconds"] = round(t["duration_seconds"] + float(x.get("duration_seconds") or 0), 3)
        for k in USAGE_FIELDS: t[k] += int(x.get(k) or 0)
    return {"date": day, "models": tot}

class AgyCuaRunner:
    """agy-cua attempts: same sandbox lifecycle as opencode attempts (Cua SDK create/delete, one sandbox per
    attempt), but the sandbox lives in a per-attempt Cua state dir, so the `cua mcp --embedded --state-dir`
    server that agy talks to can resolve no other sandbox. agy itself runs on the host as a child process."""
    def __init__(self, be): self.be, self.procs, self.clients = be, {}, {}

    def _client(self, a):
        sd = a["cua_state_dir"]
        if sd not in self.clients: self.clients[sd] = self.be.cua.embedded(state_dir=sd)
        return self.clients[sd]

    async def sandbox(self, a):
        n = a["sandbox"]
        if n not in self.be._sb: self.be._sb[n] = await self._client(a).sandboxes().connect(n)
        return self.be._sb[n]

    async def start(self, a):
        import subprocess
        cua, cfg, b, aid = self.be.cua, self.be.cfg, a["brief"], a["attempt_id"]
        mode = a.get("mode") or "ui"
        d = agy_attempt_dir(cfg, aid); state = d / "cua-state"; ws = d / "workspace"
        for x in (state, ws): x.mkdir(parents=True, exist_ok=True)
        name = sandbox_name(a)
        a.update(cua_state_dir=str(state), sandbox=name)  # recorded before create so a failure still cleans up
        sb = await self._client(a).sandboxes().create(cua.SandboxCreateOptions(
            on="local", kind="container", runtime=cfg["runtime"], image=cfg["image"], name=name,
            memory_mb=cfg["memory_mb"], cpus=cfg.get("cpus", 2), wait_for=[cua.ReadinessProbe(service="env")]))
        self.be._sb[name] = sb
        wd = base = None
        if mode == "code":  # same seeding as opencode bees: source_rev, else sync_branch / HEAD -> git repo in the sandbox
            proj = cfg["projects"].get(b.get("project", ""), {})
            if not proj.get("path"): raise ValueError("agy-cua code mode needs a project with a local path on this PC")
            wd, base, seed = await self.be._seed_project(sb, proj, b["project"], source_rev=b.get("source_rev"), branch=b.get("branch"))
            a.update(workdir=wd, local_base_rev=base, seed_commit=seed)
        timeout = agy_timeout(b, mode)
        prompt = render_agy_code_prompt(b, wd) if mode == "code" else render_agy_prompt(b)
        argv = agy_argv(cfg, prompt, a.get("model") or AGY_DEFAULT_MODEL, timeout)
        perms = cfg.get("agy_code_permissions", AGY_CODE_PERMS) if mode == "code" else cfg.get("agy_permissions")
        env = agy_child_env(os.environ, name, state, perms, self._cua_exe())
        out = open(d / "agy.out.json", "wb"); err = open(d / "agy.err.log", "wb")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.Popen(argv, cwd=str(ws), env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, creationflags=flags)
        out.close(); err.close()
        run_id = f"agy-{p.pid}-{uuid.uuid4().hex[:6]}"
        self.procs[run_id] = {"p": p, "t0": time.time(), "timeout": timeout, "emitted": 0}
        return {"sandbox": name, "sandbox_id": sb.id(), "run_id": run_id, "cua_state_dir": str(state),
                "agy_dir": str(d), "agy_timeout_s": timeout, "workdir": wd, "local_base_rev": base, "seed_commit": seed if mode == "code" and proj.get("path") else None}

    def _cua_exe(self):
        import shutil
        return shutil.which("cua", path=str(Path(sys.executable).parent)) or shutil.which("cua")

    async def events(self, a, cursor):
        r = self.procs.get(a["run_id"])
        if r is None:  # launcher restarted: the agy child is not ours any more; stop it and fail the attempt honestly
            try: os.kill(int(a["run_id"].split("-")[1]), 9)
            except Exception: pass
            self.procs[a["run_id"]] = r = {"p": None, "emitted": 1, "done": ("exited", "launcher restarted during agy run")}
        evs = []
        if r["emitted"] == 0: evs.append({"kind": "turn_started", "line": "agy started"}); r["emitted"] = 1
        p = r["p"]
        if "done" not in r and p is not None:
            if p.poll() is None and time.time() - r["t0"] > r["timeout"] + 60:
                p.kill(); p.wait(30); r["done"] = ("exited", f"agy killed after {r['timeout'] + 60}s")
            elif p.poll() is not None:
                text, meta = parse_agy_output(Path(a["agy_dir"], "agy.out.json").read_bytes().decode("utf-8", "replace"))
                r["text"], r["meta"], r["rc"] = redact(text), meta, p.returncode
                why = (f"agy exit code {p.returncode}" if p.returncode != 0 else "agy reported an error" if meta.get("is_error")
                       else f"agy status {meta.get('status')}" if meta.get("status") not in (None, "SUCCESS")
                       else "agy denied tool calls: " + ", ".join(str(x.get("display_name") or x.get("action")) for x in meta["denied_actions"]) if meta.get("denied_actions")
                       else "agy returned no response" if not (text or "").strip() else None)
                r["done"] = ("exited", why) if why else ("turn_ended", "end_turn")
        if "done" in r and r["emitted"] == 1:
            kind, why = r["done"]; r["emitted"] = 2
            if r.get("text"): evs.append({"kind": "message", "text": r["text"][:4000], "line": ""})
            evs.append({"kind": kind, "stop_reason": "end_turn" if kind == "turn_ended" else None, "line": why, "text": why})
        out = [dict(e, seq=cursor + i + 1, text=e.get("text"), stop_reason=e.get("stop_reason")) for i, e in enumerate(evs)]
        return out, cursor + len(out)

    async def interrupt(self, a):
        r = self.procs.get(a.get("run_id")) or {}
        if r.get("p") and r["p"].poll() is None: r["p"].kill()

    async def result(self, a):
        r = self.procs.get(a["run_id"]) or {}
        err = None if r.get("done", ("",))[0] == "turn_ended" else (r.get("done") or ("", "unknown"))[1]
        if err:
            tail = Path(a["agy_dir"], "agy.err.log")
            if tail.exists(): err += ": " + redact(tail.read_bytes().decode("utf-8", "replace")[-600:])
        usage = self.record_usage(a, r)
        return {"status": "completed" if not err else "failed", "stop_reason": "end_turn" if not err else "error",
                "text": r.get("text"), "tool_calls": (r.get("meta") or {}).get("num_turns"), "usage": usage, "error": err, "artifacts": []}

    def record_usage(self, a, r):
        """Append one line to <data_dir>/agy-usage.jsonl (no prompt, no output, no secrets) and return the usage
        block with today's per-model running totals (local date). Logged once per attempt run."""
        meta = r.get("meta") or {}; u = meta.get("usage") or {}
        if r.get("usage_block"): return r["usage_block"]
        now = time.time(); day = time.strftime("%Y-%m-%d", time.localtime(now))
        row = {"ts": now, "date": day, "attempt_id": a["attempt_id"], "model": a.get("model"), "tier": a.get("tier"),
               **{k: int(u.get(k) or 0) for k in USAGE_FIELDS}, "num_turns": meta.get("num_turns"),
               "duration_seconds": meta.get("duration_seconds"), "agy_status": meta.get("status"),
               "conversation_id": meta.get("conversation_id"), "denied_actions": len(meta.get("denied_actions") or []),
               "outcome": (r.get("done") or ("unknown",))[0]}
        log = Path(self.be.cfg["data_dir"]) / "agy-usage.jsonl"
        quota = agy_quota(self.be.cfg, Path(a.get("agy_dir") or self.be.cfg["data_dir"])) if self.be.cfg.get("agy_quota", True) else None
        row["quota"] = quota
        with open(log, "a", encoding="utf-8") as f: f.write(json.dumps(row) + "\n")
        r["usage_block"] = {"model": row["model"], "tier": row["tier"], **{k: row[k] for k in USAGE_FIELDS},
                            "num_turns": row["num_turns"], "duration_seconds": row["duration_seconds"],
                            "quota": quota, "quota_source": "agy -p /usage --output-format json (after the run)" if quota else "unavailable (agy /usage did not answer)",
                            "daily_totals": usage_totals(log, day), "usage_log": str(log)}
        return r["usage_block"]

    async def environment(self, a):
        return {"sandbox_name": a.get("sandbox"), "sandbox_id": a.get("sandbox_id"), "cua_state_dir": a.get("cua_state_dir"),
                "mode": a.get("mode") or "ui", "workdir": a.get("workdir"), "local_base_rev": a.get("local_base_rev"),
                "agy_exe": self.be.cfg["agy_exe"], "agy_model": a.get("model"), "mcp_server": AGY_MCP_NAME, "agy_run_id": a.get("run_id")}

    async def cleanup(self, a):
        await self.interrupt(a)
        if a.get("sandbox") and a.get("cua_state_dir"):
            try: await self._client(a).sandboxes().delete(a["sandbox"])
            except Exception: pass

class FakeBackend:
    """Scripted backend for tests: brief.fake = {"events":[...], "fail_start":bool, "rate_limit_first":n}."""
    def __init__(self, cfg): self.runs, self.starts = {}, 0
    async def start(self, a, key):
        f = a["brief"].get("fake", {}); self.starts += 1
        if f.get("fail_start"): raise RuntimeError("fake start failure")
        rid = "run-" + uuid.uuid4().hex[:8]
        evs = list(f.get("events") or [{"kind": "turn_started"}, {"kind": "message", "text": "done"}, {"kind": "turn_ended", "stop_reason": "end_turn"}])
        if f.get("monthly_limit_first", 0) > a.get("rate_limited", 0) or (not a.get("fallback") and f.get("monthly_limit") and a.get("model") in f.get("monthly_limit_models", [a.get("model")])):
            evs = [{"kind": "error", "text": f.get("monthly_limit_text") or "HTTP 429 GoUsageLimitError: usage limit reached monthly limitName=monthly"}]
        elif f.get("error_first", 0) > a.get("rate_limited", 0) and f.get("error_text"):
            evs = [{"kind": "error", "text": f["error_text"]}]
        elif f.get("rate_limit_first", 0) > a.get("rate_limited", 0) or a.get("model") in f.get("rate_limit_models", []):
            evs = [{"kind": "error", "text": f.get("rate_limit_text") or "HTTP 429 Too Many Requests"}]
        if a.get("model") in f.get("deny_models", []):
            evs = [{"kind": "error", "text": "FreeTierError: OpenCode's free tier can only be used from within OpenCode."}]
        if a.get("model") in f.get("unavailable_models", []):
            evs = [{"kind": "error", "text": f.get("unavailable_text") or
                    "Internal error: Zoo Pass Pilot models are available in the Zoo Code IDE only."}]
        self.models = getattr(self, "models", []) + [a.get("model")]
        self.runs[rid] = {"events": [dict(e, seq=i + 1) for i, e in enumerate(evs)], "delay": f.get("delay", 0), "t0": time.time(), "cancelled": False}
        return {"sandbox": "fake", "run_id": rid}
    async def events(self, a, cursor):
        r = self.runs[a["run_id"]]
        if r["cancelled"]: return [], cursor
        if time.time() - r["t0"] < r["delay"]: return [], cursor
        out = [e for e in r["events"] if e["seq"] > cursor]
        return out, (out[-1]["seq"] if out else cursor)
    async def interrupt(self, a): self.runs[a["run_id"]]["cancelled"] = True
    async def result(self, a): return {"status": "completed", "text": "done", "artifacts": [{"path": "out.txt", "size": 2}], "tool_calls": 1}
    async def file_sha256(self, a, path): return hashlib.sha256(path.encode()).hexdigest()
    async def cleanup(self, a): pass
    async def check_start(self, a):
        return {"sandbox": "hyvv-check-fake", "sandbox_id": "fake", "run_id": "check-" + uuid.uuid4().hex[:8], "workdir": None,
                "local_base_rev": None, "seed_commit": None, "security_profile": a.get("security_profile")}
    async def run_checks(self, a):
        exits = (a["brief"].get("fake") or {}).get("check_exits") or []
        return [{"name": f"check-{i + 1}", "command": c, "exit_code": exits[i] if i < len(exits) else 0, "duration_s": 0.0,
                 "stdout_tail": "", "stderr_tail": "", "log_file": None} for i, c in enumerate(a["brief"].get("commands") or [])]
    async def check_cleanup(self, a): pass

def _clip_text(value, limit, collapse_ws=False):
    text = str(value).strip()
    if collapse_ws:
        text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"

def _is_scalar(value):
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)

def _finding_scalar_lines(findings):
    lines = []
    for key in ("failure_id", "verdict", "summary"):
        value = findings.get(key)
        if not _is_scalar(value):
            continue
        text = _clip_text(value, 2000 if key == "summary" else 200, collapse_ws=True)
        if text:
            lines.append(f"- {key}: {text}")
    return lines

def _source_refs(sources):
    refs = []
    for source in sources[:20]:
        if not isinstance(source, dict):
            continue
        ref = source.get("ref")
        if not _is_scalar(ref):
            continue
        kind = source.get("kind")
        prefix = f"{kind}: " if isinstance(kind, str) and kind else ""
        refs.append(_clip_text(prefix + str(ref), 300, collapse_ws=True))
    return refs

def _rejected_entries(alternatives):
    entries = [_clip_text(x, 300, collapse_ws=True) for x in alternatives[:20] if _is_scalar(x)]
    return [x for x in entries if x]

def _finding_block(label, entries):
    if not entries:
        return None
    return f"- {label}:\n" + "\n".join(f"  - {entry}" for entry in entries)

def _render_findings(findings):
    if not isinstance(findings, dict):
        return None
    lines = _finding_scalar_lines(findings)
    sources = findings.get("sources")
    if isinstance(sources, list):
        block = _finding_block("sources", _source_refs(sources))
        if block:
            lines.append(block)
    alternatives = findings.get("rejected_alternatives")
    if isinstance(alternatives, list):
        block = _finding_block("rejected alternatives", _rejected_entries(alternatives))
        if block:
            lines.append(block)
    if not lines:
        return None
    return _clip_text("# Research findings\n" + "\n".join(lines), 4000)

def _render_tests(tests):
    if not isinstance(tests, list):
        return None
    names = [_clip_text(x, 500, collapse_ws=True) for x in tests[:50]
             if isinstance(x, str) and x.strip()]
    if not names:
        return None
    section = "# Tests that must pass\n" + "\n".join(f"- {name}" for name in names)
    return _clip_text(section, 8000)

def render_prompt(b):
    parts = [f"# Objective\n{b['objective']}"]
    for k, title in [("deliverables", "Deliverables"), ("acceptance", "Acceptance criteria"), ("assigned_files", "Files you may change"),
                     ("commands", "Tools/commands you may run"), ("deps", "Dependencies/context")]:
        if b.get(k): parts.append(f"# {title}\n" + "\n".join(f"- {x}" for x in b[k]))
    findings = _render_findings(b.get("findings"))
    if findings: parts.append(findings)
    tests = _render_tests(b.get("tests"))
    if tests: parts.append(tests)
    parts.append("# Rules\n- Work only inside the working directory.\n- Do not start subagents.\n- Do not print secrets or environment variables.")
    parts.append("# Return format\nEnd with a JSON block: {\"summary\":..., \"changed_files\":[...], \"checks\":[{\"name\":...,\"command\":...,\"passed\":bool}], \"open_issues\":[...]}")
    return "\n\n".join(parts)

def _string_entries(value):
    """The string entries of a brief list field; anything else is ignored."""
    return [x for x in value or [] if isinstance(x, str)]

def _nonempty_list(value):
    return isinstance(value, list) and bool(value)

def normalize_brief(b):
    """Accept both brief shapes: v0.1.1 (acceptance is a string list) and bundlebees main at c10aa42
    (acceptance may be {criteria, commands, tests}). Flatten a dict to a list of strings. Unknown
    extra fields are left untouched."""
    acc = b.get("acceptance") if isinstance(b, dict) else None
    if isinstance(acc, dict):
        b = dict(b)
        crit = _string_entries(acc.get("criteria"))
        acc_tests = _string_entries(acc.get("tests"))
        b["acceptance"] = crit + [f"Run: {c}" for c in _string_entries(acc.get("commands"))] + \
                          [f"Test passes: {t}" for t in acc_tests]
        if acc_tests and not _nonempty_list(b.get("tests")):
            b["tests"] = list(acc_tests)
    return b

def _budget_requirement_error(cfg, b):
    """The require_budget refusal for a brief without a positive budget.timeout_s, else None."""
    if not (cfg or {}).get("require_budget"):
        return None
    budget = b.get("budget")
    raw = budget.get("timeout_s") if isinstance(budget, dict) else None
    try:
        timeout_s = int(raw)
    except (TypeError, ValueError, OverflowError):
        timeout_s = 0
    return None if timeout_s > 0 else "brief.budget.timeout_s must be a positive number of seconds when require_budget is set"

def validate_brief(b, cfg=None):
    """v0.1.1 requires only objective; main (c10aa42) also sends source_rev, assigned_files and
    acceptance. Unknown extra fields (contract, evidence, etc.) are ignored, not rejected."""
    if not isinstance(b, dict) or not str(b.get("objective", "")).strip(): return "brief.objective is required"
    for k in ("deliverables", "acceptance", "assigned_files", "commands", "deps"):
        if k in b and not (isinstance(b[k], list) and all(isinstance(x, str) for x in b[k])): return f"brief.{k} must be a list of strings"
    return _budget_requirement_error(cfg, b)


def _brief_validation_report(brief):
    if not isinstance(brief, dict) or brief.get("schema") != "rizzstack.brief/1":
        return {"status": "unversioned", "legacy_fields": sorted(brief) if isinstance(brief, dict) else []}
    schema_dir = Path(__file__).resolve().parent / "schemas"
    try:
        schema = json.loads((schema_dir / "rizzstack-brief.schema.json").read_text("utf-8"))
        problems = _rizzstack_validators.validate_brief(brief, schema_dir=schema_dir)
        unknown_fields = sorted(set(brief) - set(schema.get("properties", {})))
    except Exception:
        return {"status": "invalid", "problems": ["brief validation unavailable"], "unknown_fields": []}
    return {"status": "invalid" if problems else "valid", "problems": problems, "unknown_fields": unknown_fields}


# Same text rule as contract/v1/project_state.py SAFE_PATH: relative, no empty,
# "." or ".." segment, no backslash, no trailing slash after normalize, <= 244.
_SCOPE_PATH = re.compile(r"^(?!/)(?!.*(?:^|/)[.]{1,2}(?:/|$))(?!.*//)(?!.*/$)[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")


def _safe_rel_path(value):
    """Normalize a repo-relative POSIX path, or None if it is not safe."""
    if not isinstance(value, str) or "\\" in value:
        return None
    text = value.rstrip("/")
    if not text or len(text) > 244 or not _SCOPE_PATH.match(text):
        return None
    return text


def in_assigned_scope(path, scope):
    """True only when path is inside a non-empty assigned_files list.

    Fail closed: missing, empty, or non-list scope allows nothing. A scope
    entry matches the path exactly, or as a directory prefix followed by '/'.
    Unsafe paths (`..`, absolute, backslash) never match.
    """
    if not isinstance(scope, (list, tuple)) or not scope:
        return False
    file_path = _safe_rel_path(path)
    if file_path is None:
        return False
    for raw in scope:
        entry = _safe_rel_path(raw)
        if entry is None:
            continue
        if file_path == entry or file_path.startswith(entry + "/"):
            return True
    return False


# ---------------------------------------------------------------- launcher core

def replace_retryable(exc):
    """True for the Windows replace failures that a short retry can clear (WinError 5 or 32)."""
    if not isinstance(exc, OSError):
        return False
    if getattr(exc, "winerror", None) in (5, 32):
        return True
    return isinstance(exc, PermissionError)


def _save_replace_delays():
    """n-1 sleeps after the first failure, totalling SAVE_REPLACE_BUDGET_S before the last try."""
    steps = max(1, SAVE_REPLACE_TRIES - 1)
    return [SAVE_REPLACE_BUDGET_S / steps] * steps


def journal_record_valid(obj):
    return isinstance(obj, dict) and isinstance(obj.get("attempt_id"), str) and isinstance(obj.get("state"), str)


# ---------------------------------------------------------------- capacity (/v1/capacity)
# Admission inputs for a coordinator that fills bee slots ("go max"). Every value that cannot be
# read is None -- never a guess -- so the caller fails closed. Nothing here starts, stops or
# changes an attempt, and nothing here carries a token, path, host or provider URL.
CAPACITY_SCHEMA = "hyvv.capacity/1"
CAPACITY_LANES = ("kimi", "nous", "go", "other")
CAPACITY_RATE_LIMIT_WINDOW_S = 600
CAPACITY_DOCKER_TTL_S = 30      # refresh the cached Docker probe at most this often
CAPACITY_DOCKER_STALE_S = 120   # an older probe reads as unknown (ok: None)
CAPACITY_DOCKER_TIMEOUT_S = 10

def capacity_lane(cfg, model):
    """Provider lane of a model id: kimi, nous, go or other. Unknown ids are `other`."""
    if not model:
        return "other"
    try:
        key = model_spec(cfg, model).get("key")
    except Exception:
        key = None
    if key is None:  # classification only (never routing): fall back to the id's prefix
        key = str(model).split(":", 1)[0] if ":" in str(model) else None
    return key if key in ("kimi", "nous", "go") else "other"

def parse_meminfo(text):
    vals = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ("MemTotal:", "MemAvailable:") and parts[1].isdigit():
            vals[parts[0][:-1]] = int(parts[1]) // 1024
    if "MemTotal" not in vals or "MemAvailable" not in vals:
        return None
    return {"mem_total_mb": vals["MemTotal"], "mem_available_mb": vals["MemAvailable"]}

def _host_memory():
    try:
        if sys.platform == "win32":
            import ctypes
            class _MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = _MS(); ms.dwLength = ctypes.sizeof(_MS)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                return None
            return {"mem_total_mb": int(ms.ullTotalPhys // 1048576), "mem_available_mb": int(ms.ullAvailPhys // 1048576)}
        return parse_meminfo(Path("/proc/meminfo").read_text("utf-8"))
    except Exception:
        return None

def _cpu_times():
    """Cumulative (idle, total) CPU time, or None when unreadable."""
    try:
        if sys.platform == "win32":
            import ctypes
            idle, kern, user = (ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong())
            if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
                return None
            return idle.value, kern.value + user.value  # kernel time includes idle time
        f = Path("/proc/stat").read_text("utf-8").splitlines()[0].split()
        nums = [int(x) for x in f[1:]]
        return nums[3] + (nums[4] if len(nums) > 4 else 0), sum(nums)
    except Exception:
        return None

def cpu_busy_pct(prev, cur):
    if not prev or not cur:
        return None
    didle, dtotal = cur[0] - prev[0], cur[1] - prev[1]
    if dtotal <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (1 - didle / dtotal))), 1)

_CPU_LAST = {"sample": None, "at": 0.0, "pct": None}
_CPU_LOCK = threading.Lock()

def host_snapshot():
    mem = _host_memory() or {"mem_total_mb": None, "mem_available_mb": None}
    with _CPU_LOCK:
        cur = _cpu_times()
        prev = _CPU_LAST["sample"]
        if cur and (prev is None or time.time() - _CPU_LAST["at"] > 60):
            time.sleep(0.25)  # a first (or very old) sample needs a short second window
            prev, cur = cur, _cpu_times()
        pct = cpu_busy_pct(prev, cur)
        if pct is None and time.time() - _CPU_LAST["at"] < 10:
            pct = _CPU_LAST["pct"]  # back-to-back reads: reuse the window just measured
        if cur:
            _CPU_LAST.update(sample=cur, at=time.time(), pct=pct)
    return {"mem_total_mb": mem["mem_total_mb"], "mem_available_mb": mem["mem_available_mb"],
            "cpu_count": os.cpu_count(), "cpu_busy_pct": pct}

class DockerProbe:
    """Cached, read-only `docker info` probe. A hung Docker daemon costs one bounded timeout
    in a background thread, never an HTTP handler."""
    ARGV = ["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}} {{.ContainersRunning}} {{.ServerVersion}}"]

    def __init__(self, runner=None):
        self._runner = runner or _proc_run
        self._lock = threading.Lock()
        self._busy = False
        self._at = 0.0
        self._snap = {"ok": None, "ncpu": None, "mem_total_mb": None, "containers_running": None, "detail": "not_probed"}

    def refresh(self):
        try:
            rc, out, _err = self._runner(list(self.ARGV), timeout=CAPACITY_DOCKER_TIMEOUT_S)
            if rc != 0:
                snap = {"ok": False, "ncpu": None, "mem_total_mb": None, "containers_running": None, "detail": f"exit_{rc}"}
            else:
                parts = (out or "").split()
                ncpu, mem, running = (int(parts[0]), int(parts[1]), int(parts[2])) if len(parts) >= 3 else (None, None, None)
                snap = {"ok": True, "ncpu": ncpu, "mem_total_mb": (mem // 1048576) if mem is not None else None,
                        "containers_running": running, "detail": "ok"}
        except Exception as ex:
            import subprocess
            detail = "timeout" if isinstance(ex, subprocess.TimeoutExpired) else ("unparseable" if isinstance(ex, ValueError) else "unreachable")
            snap = {"ok": False, "ncpu": None, "mem_total_mb": None, "containers_running": None, "detail": detail}
        with self._lock:
            self._snap, self._at, self._busy = snap, time.time(), False

    def _kick(self):
        with self._lock:
            if self._busy or (self._at and time.time() - self._at < CAPACITY_DOCKER_TTL_S):
                return
            self._busy = True
        threading.Thread(target=self.refresh, daemon=True).start()

    def snapshot(self, kick=True):
        if kick:
            self._kick()
        with self._lock:
            snap, at = dict(self._snap), self._at
        age = round(time.time() - at, 1) if at else None
        if at and age > CAPACITY_DOCKER_STALE_S:
            snap = {"ok": None, "ncpu": None, "mem_total_mb": None, "containers_running": None, "detail": "stale"}
        snap["age_s"] = age
        return snap


class Launcher:
    def __init__(self, cfg, backend):
        self.cfg, self.backend = cfg, backend
        self.model_health = {m: {"last_limited": 0.0, "last_used": 0.0} for m in FREE_POOL}
        self.dir = Path(cfg["data_dir"]); (self.dir / "attempts").mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.attempts = {}
        self.controllers = {}  # attempt_id -> asyncio task (exactly one per attempt)
        self._budget_finishing = set()  # attempt_ids with a budget teardown in flight (in-memory single-flight)
        self.loop = asyncio.new_event_loop()
        self._ev_lock = threading.Lock(); self._ev_idx = {}  # events_since offset index
        self.started_at = time.time(); self.loop_lag_ms = 0.0
        self.landing_conflicts = []
        self._cleaned_aids = set()
        self.docker_probe = DockerProbe()
        self._rate_limits = []  # (at, lane) of recent provider 429s, for /v1/capacity
        self._cleaned_sandboxes = set()
        self._load()
        self.rails = None
        if cfg.get('rails_enabled') is True:
            from rails_emitter import RailsEmitter
            self.rails = RailsEmitter(self.dir)
            self.rails.recover(self.attempts)
        self.recover_landed_events()
        try:
            self.reconcile()
        except Exception:
            pass  # never crash startup; health will still advertise any leftover split-brain

    # journal: one JSON per attempt (atomic replace) + append-only events log
    def _path(self, aid): return self.dir / "attempts" / f"{aid}.json"
    def _save(self, a):
        p = self._path(a["attempt_id"]); tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(a, indent=1), "utf-8")
        last = None
        delays = _save_replace_delays()
        for i in range(SAVE_REPLACE_TRIES):
            try:
                os.replace(tmp, p)
                try: self.publish_runtime_status()
                except Exception: pass
                return
            except OSError as ex:
                if not replace_retryable(ex):
                    raise
                last = ex
                if i < len(delays):
                    time.sleep(delays[i])
        raise last
    def _append_events(self, a, evs):
        path = self.dir / "attempts" / f"{a['attempt_id']}.events.jsonl"
        try:
            with open(path, "a", encoding="utf-8") as f:
                for e in evs: f.write(json.dumps(e) + "\n")
                f.flush()
                os.fsync(f.fileno())
            # Windows has no portable directory fsync. Health explicitly
            # scopes completeness to the retained bytes, including after restart.
            if os.name != "nt":
                _fsync_dir(path.parent, required=True)
        except OSError:
            if self.rails:
                self.rails.journal_write_failed(a['attempt_id'])
            raise
        if self.rails:
            self.rails.ingest(a['attempt_id'])
    def _load(self):
        for p in (self.dir / "attempts").glob("*.json"):
            if p.name.count(".") != 1: continue  # skip <id>.result.json (and any other sidecar)
            try: a = json.loads(p.read_text("utf-8"))
            except Exception: continue
            if isinstance(a, dict) and "attempt_id" in a and "state" in a: self.attempts[a["attempt_id"]] = a
    def events_since(self, aid, cursor):
        # Offset-indexed (plan C): index each complete line's lseq -> byte offset once, then read only
        # from the first event after `cursor` instead of re-parsing the whole file every poll.
        p = self.dir / "attempts" / f"{aid}.events.jsonl"
        if not p.exists(): return []
        with self._ev_lock:
            idx = self._ev_idx.get(aid)
            size = p.stat().st_size
            if idx is None or size < idx["end"]: idx = self._ev_idx[aid] = {"end": 0, "seqs": [], "offs": []}
            with open(p, "rb") as f:
                if size > idx["end"]:
                    f.seek(idx["end"]); pos = idx["end"]
                    for line in f.read(size - idx["end"]).splitlines(keepends=True):
                        if not line.endswith(b"\n"): break  # partial line still being written
                        try: q = json.loads(line)["lseq"]
                        except Exception: q = None
                        if q is not None and (not idx["seqs"] or q > idx["seqs"][-1]): idx["seqs"].append(q); idx["offs"].append(pos)
                        pos += len(line)
                    idx["end"] = pos
                i = bisect.bisect_right(idx["seqs"], cursor)
                if i >= len(idx["seqs"]): return []
                f.seek(idx["offs"][i]); data = f.read(idx["end"] - idx["offs"][i])
        out = []
        for l in data.decode("utf-8").splitlines():
            try: e = json.loads(l)
            except Exception: continue
            if e.get("lseq", 0) > cursor: out.append(e)
        return out

    def public(self, a):
        keys = ("attempt_id", "kind", "mode", "tier", "state", "detail", "run_id", "sandbox", "created_at", "updated_at", "lseq", "queue_position", "retry_at", "rate_limited", "model", "fallback", "pool", "model_history")
        d = {k: a.get(k) for k in keys}
        if "brief_validation" in a:
            d["brief_validation"] = a["brief_validation"]
        if a.get("workspace_error"):
            d["workspace_error"] = a["workspace_error"]
        if d["state"] == "queued": q = self._queue(); d["queue_position"] = q.index(a["attempt_id"]) + 1 if a["attempt_id"] in q else None  # one snapshot: a second _queue() call could race the .index()
        return d

    def listing(self, a):
        """public() plus the lease fields bundlebees needs for its overlap check (no brief text)."""
        b = a.get("brief") or {}
        d = self.public(a)
        d.update(project=b.get("project"), branch=b.get("branch"), assigned_files=list(b.get("assigned_files") or []),
                 task_id=b.get("task_id"), reviewed=(a.get("review") or {}).get("accepted"))
        return d

    def review(self, aid, accepted, note=None):
        with self.lock:
            a = self.attempts.get(aid)
            if not a: return None, "unknown"
            if a["state"] not in ("succeeded", "failed"): return a, f"only finished attempts can be reviewed (state {a['state']})"
            a["review"] = {"accepted": accepted, "note": redact(str(note or ""))[:500], "at": time.time()}
            self._save(a)
            return a, None

    def _queue(self):
        return sorted([x["attempt_id"] for x in self.attempts.values() if x["state"] in ("queued", "backoff")], key=lambda i: self.attempts[i]["created_at"])

    def _check_verify_of(self, brief, kind):
        """A verify bee re-runs a succeeded work bee's acceptance on its patch: same project and source_rev, opencode only."""
        vid = str(brief.get("verify_of"))
        if kind != "opencode" or not re.fullmatch(r"att_[0-9a-f]{8,32}", vid): raise ValueError("verify_of must name an opencode attempt id")
        parent = self.attempts.get(vid)
        if not parent: raise ValueError("verify_of attempt not found")
        if parent["state"] != "succeeded": raise ValueError("verify_of attempt has not succeeded")
        pb = parent.get("brief") or {}
        if pb.get("project") != brief.get("project") or pb.get("source_rev") != brief.get("source_rev"):
            raise ValueError("verify_of attempt has a different project or source_rev")
        if not (self.dir / "attempts" / f"{vid}.result.json").exists(): raise ValueError("verify_of attempt has no result")

    def submit(self, aid, brief, model=None, kind=None, mode=None, validation=None):
        with self.lock:
            if aid in self.attempts: return self.attempts[aid], False  # idempotent: never a second controller
            if validation is None: validation = _brief_validation_report(brief)
            kind = kind or brief.get("kind") or "opencode"
            if kind not in KINDS: raise ValueError(f"unknown kind: {kind} (allowed: {', '.join(KINDS)})")
            prof = resolve_security_profile(self.cfg, kind, brief)  # ValueError -> 400; coding bees: "default" only
            if kind == "check":
                if not brief.get("commands"): raise ValueError("check jobs need brief.commands (a non-empty list of strings)")
                if brief.get("verify_of") is not None: raise ValueError("verify_of applies to opencode attempts only")
                now = time.time()
                a = {"attempt_id": aid, "kind": kind, "brief": brief, "brief_validation": validation, "state": "queued", "detail": "waiting for a worker slot", "created_at": now,
                     "updated_at": now, "lseq": 0, "cursor": 0, "rate_limited": 0, "model": None, "security_profile": prof}
                self.attempts[aid] = a; self._save(a)
                self.kick(); return a, True
            if kind == "agy-cua":
                sel = model or brief.get("tier") or brief.get("model") or AGY_DEFAULT_TIER
                m = AGY_TIERS.get(sel, sel)
                if m not in AGY_MODELS: raise ValueError(f"agy tier/model not allowed: {sel} (tiers: {', '.join(AGY_TIERS)})")
                spec = {"name": m, "tier": next((t for t, v in AGY_TIERS.items() if v == m and sel in (t, m)), None)}
            else:
                spec = model_spec(self.cfg, model or brief.get("model") or brief.get("tier"), brief)  # ValueError -> 400
            if brief.get("verify_of") is not None: self._check_verify_of(brief, kind)
            now = time.time()
            a = {"attempt_id": aid, "kind": kind, "brief": brief, "brief_validation": validation, "state": "queued", "detail": "waiting for a worker slot", "created_at": now,
                 "updated_at": now, "lseq": 0, "cursor": 0, "rate_limited": 0, "model": None if spec.get("pool") else spec["name"]}
            if spec.get("pool"): a.update(pool=spec["pool"], tried=[], model_history=[])
            if kind == "agy-cua":
                a["tier"] = spec.get("tier"); a["mode"] = mode or brief.get("mode") or "ui"
                if a["mode"] not in AGY_MODES: raise ValueError(f"agy-cua mode must be one of {AGY_MODES}")
                if a["mode"] == "code" and not self.cfg["projects"].get(brief.get("project", ""), {}).get("path"):
                    raise ValueError("agy-cua code mode needs brief.project naming a local-path project on this PC")
                if a["mode"] == "ui" and brief.get("project"): raise ValueError("agy-cua ui mode takes no project")
            self.attempts[aid] = a; self._save(a)
        self.kick(); return a, True

    def cancel(self, aid):
        prestart_sandbox = False
        with self.lock:
            a = self.attempts.get(aid)
            if not a: return None
            if a["state"] in TERMINAL: return a
            if a["state"] in ("queued", "backoff"):
                self._set(a, "cancelled", "cancelled before start")
                prestart_sandbox = bool(a.get("sandbox"))
            else: a["cancel"] = True; self._set(a, "cancel_requested", "interrupt sent")
        if prestart_sandbox:
            # A resumable rate-limited attempt still owns its sandbox while it is parked in
            # backoff; cancelling before the retry starts must not leak it.
            try: asyncio.run_coroutine_threadsafe(self._cleanup_once(a), self.loop)
            except RuntimeError: pass  # loop closed: same no-op the pre-start cancel had
        self.kick(); return a

    def landed_result_path(self, a):
        aid = a.get("attempt_id") or (a.get("brief") or {}).get("attempt_id")
        return self.dir / "attempts" / f"{aid}.result.json"

    def build_landed_event(self, a, result_path=None):
        return build_landed_event(a, self.cfg, self.landed_result_path(a) if result_path is None else result_path)

    def emit_landed_event(self, a, result_path=None):
        """Persist exactly one event for this terminal attempt; refuse contradictory evidence."""
        if result_path is None:
            result_path = self.landed_result_path(a)
        with self.lock:
            _, event, _ = emit_landed_record(a, self.dir, self.cfg, result_path)
            if self.rails:
                self.rails.receipt(a['attempt_id'], event.get(LANDED_USAGE_ATTR), MODELS.keys() | AGY_MODELS,
                                   agy=a.get('kind') == 'agy-cua')
            return event

    def replay_landed_events(self, notifier=None):
        """Re-send durable outbox events to the configured notifier (at-least-once)."""
        return replay_landed_events(self.dir, self.cfg, notifier)

    def recover_landed_events(self):
        """Emit events missed by a crash after the terminal journal write; never crashes startup."""
        recovered = []
        for a in list(self.attempts.values()):
            if a.get("state") not in TERMINAL:
                continue
            try:
                recovered.append(self.emit_landed_event(a))
            except LandedEventConflict as ex:
                self.landing_conflicts.append({"attempt_id": a.get("attempt_id"), "error": str(ex)})
            except Exception as ex:  # disk errors must not stop the launcher; the journal still has the state
                self.landing_conflicts.append({"attempt_id": a.get("attempt_id"), "error": type(ex).__name__})
        return recovered

    def _set(self, a, state, detail, ev=None):
        a["state"], a["detail"], a["updated_at"] = state, detail, time.time()
        a["lseq"] += 1
        e = {"lseq": a["lseq"], "ts": a["updated_at"], "kind": "state", "state": state, "detail": detail}
        if ev: e.update(ev)
        self._append_events(a, [e]); self._save(a)
        if state in TERMINAL:
            try:
                self.emit_landed_event(a)
            except LandedEventConflict:
                raise
            except Exception as ex:  # keep the durable journal transition; recovery on restart will retry
                self.landing_conflicts.append({"attempt_id": a.get("attempt_id"), "error": type(ex).__name__})

    def _active(self): return [x for x in self.attempts.values() if x["state"] in ("starting", "running", "cancel_requested")
                               and not x.get("budget_stop")]  # budget_stop: the watchdog owns the teardown

    def _controller_live(self, aid):
        task = self.controllers.get(aid)
        if task is None:
            return False
        done = getattr(task, "done", None)
        if callable(done):
            try:
                return not done()
            except Exception:
                return False
        return True

    async def _cleanup_once(self, a, timeout=None):
        """Idempotent teardown of only this attempt's recorded sandbox. Safe to call from finally.

        The ledger is keyed by sandbox, not attempt id: a retry keeps the same attempt id but
        gets a new sandbox, and that second sandbox must also be torn down exactly once.
        """
        aid = a.get("attempt_id")
        if not aid:
            return
        sandbox = a.get("sandbox")
        if sandbox:
            if sandbox in self._cleaned_sandboxes:
                return
            self._cleaned_sandboxes.add(sandbox)
        elif aid in self._cleaned_aids:
            return
        self._cleaned_aids.add(aid)
        try:
            coro = self.backend.cleanup(a)
            if timeout is not None:
                await asyncio.wait_for(coro, timeout)
            else:
                await coro
        except Exception:
            pass

    def _sync_cleanup(self, a):
        try:
            if self.loop.is_running():
                asyncio.run_coroutine_threadsafe(self._cleanup_once(a), self.loop).result(timeout=60)
            else:
                self.loop.run_until_complete(self._cleanup_once(a))
        except Exception:
            pass

    def _read_journal_file(self, path):
        try:
            obj = json.loads(Path(path).read_text("utf-8"))
        except Exception:
            return None
        return obj if journal_record_valid(obj) else None

    def journal_counts(self):
        """Disk-journal counters for health and the token-free runtime status file."""
        d = self.dir / "attempts"
        active = 0
        tmp = 0
        if d.is_dir():
            for p in d.glob("*.tmp"):
                if p.is_file():
                    tmp += 1
            for p in d.glob("*.json"):
                if p.name.count(".") != 1:
                    continue
                rec = self._read_journal_file(p)
                if rec and rec.get("state") in JOURNAL_ACTIVE:
                    active += 1
        return active, tmp

    def note_rate_limit(self, model, at=None):
        """Record one provider rate limit for /v1/capacity (lane only, never the error text)."""
        lane = capacity_lane(self.cfg, model)
        now = time.time()
        with self.lock:
            self._rate_limits.append((float(at if at is not None else now), lane))
            cutoff = now - CAPACITY_RATE_LIMIT_WINDOW_S
            self._rate_limits = [r for r in self._rate_limits if r[0] >= cutoff][-1000:]

    def capacity_status(self):
        now = time.time()
        with self.lock:
            active = self._active()
            queued = [self.attempts[i] for i in self._queue()]
            by_active = {k: 0 for k in CAPACITY_LANES}
            by_queued = {k: 0 for k in CAPACITY_LANES}
            for a in active:
                by_active[capacity_lane(self.cfg, a.get("model"))] += 1
            for a in queued:
                by_queued[capacity_lane(self.cfg, a.get("model"))] += 1
            cutoff = now - CAPACITY_RATE_LIMIT_WINDOW_S
            limits = {k: {"count": 0, "last_at": None} for k in CAPACITY_LANES}
            for at, lane in self._rate_limits:
                if at >= cutoff:
                    limits[lane]["count"] += 1
                    limits[lane]["last_at"] = max(limits[lane]["last_at"] or 0.0, at)
            max_c = self.cfg["max_concurrent"]
        try:
            jact, jtmp = self.journal_counts()
            split = (len(active) != jact) or (jtmp > 0)
        except Exception:
            split = True  # unreadable journal: report the worst case
        return {"schema": CAPACITY_SCHEMA, "observed_at": round(now, 3), "max_concurrent": max_c,
                "active": len(active), "queued": len(queued), "split_brain": split,
                "active_by_lane": by_active, "queued_by_lane": by_queued,
                "host": host_snapshot(), "docker": self.docker_probe.snapshot(),
                "rate_limits": {"window_s": CAPACITY_RATE_LIMIT_WINDOW_S, "by_lane": limits}}

    def publish_runtime_status(self):
        live = len(self._active())
        journal_active, journal_tmp = self.journal_counts()
        body = {"active": live, "journal_active": journal_active, "journal_tmp": journal_tmp,
                "split_brain": (live != journal_active) or (journal_tmp > 0),
                "version": VERSION, "updated_at": time.time()}
        path = self.dir / "runtime_status.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=1), "utf-8")
        try:
            os.replace(tmp, path)
        except OSError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise
        return body

    def _repair_tmp_journals(self):
        d = self.dir / "attempts"
        if not d.is_dir():
            return
        for tmp in list(d.glob("*.tmp")):
            if not tmp.is_file():
                continue
            # _save writes <attempt_id>.tmp next to <attempt_id>.json
            jp = tmp.with_suffix(".json")
            rec_tmp, rec_json = self._read_journal_file(tmp), self._read_journal_file(jp) if jp.exists() else None
            tmp_mtime = tmp.stat().st_mtime if tmp.exists() else 0
            json_mtime = jp.stat().st_mtime if jp.exists() else 0
            chosen = None
            if rec_tmp and rec_json:
                chosen = rec_tmp if tmp_mtime >= json_mtime else rec_json
            elif rec_tmp:
                chosen = rec_tmp
            elif rec_json:
                chosen = rec_json
            if chosen is not None:
                jp.write_text(json.dumps(chosen, indent=1), "utf-8")
                self.attempts[chosen["attempt_id"]] = chosen
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    def _reap_stale_and_expired(self):
        """Land attempts that can no longer have a worker; never touch a healthy in-budget controller."""
        for a in list(self.attempts.values()):
            aid = a.get("attempt_id")
            if not aid:
                continue
            live = self._controller_live(aid)
            # Memory already terminal, disk still active, no live controller: Friday split-brain.
            disk_path = self._path(aid)
            disk = self._read_journal_file(disk_path) if disk_path.exists() else None
            if a.get("state") in TERMINAL and disk and disk.get("state") in JOURNAL_ACTIVE and not live:
                try:
                    self._save(a)
                except OSError:
                    pass
                self._sync_cleanup(a)
                continue
            if a.get("state") not in JOURNAL_ACTIVE:
                continue
            if live:
                continue
            # budget_stop: _schedule/_budget_stop_recover owns the crash-window
            # (may restore an already-collected result). Do not pre-empt it.
            if a.get("budget_stop"):
                continue
            budget = attempt_budget_s(a, self.cfg)
            started = a.get("budget_started_at")
            if budget and started and time.time() >= started + budget:
                try:
                    self._set(a, "failed",
                              f"budget timeout: brief.budget.timeout_s={int(budget)}s reached; "
                              "no live controller (reconcile)")
                except OSError:
                    a["state"] = "failed"
                    a["detail"] = (f"budget timeout: brief.budget.timeout_s={int(budget)}s reached; "
                                   "no live controller (reconcile)")
                    a["updated_at"] = time.time()
                self._sync_cleanup(a)

    def reconcile(self):
        """Startup and periodic repair of leftover journals, expired attempts, and split-brain."""
        with self.lock:
            self._repair_tmp_journals()
            self._reap_stale_and_expired()
            try:
                self.publish_runtime_status()
            except Exception:
                pass

    def kick(self): asyncio.run_coroutine_threadsafe(self._schedule(), self.loop)

    def _spec_of(self, a):
        if a.get("kind") not in (None, "opencode") or (a.get("pool") and not a.get("model")):
            return {}
        try:
            return model_spec(self.cfg, a.get("model"), a.get("brief"))
        except Exception:
            return {}

    @staticmethod
    def _fallback_hops(a):
        return a.get("fallback_hops") or (1 if a.get("fallback") else 0)

    def _may_fallback(self, a, spec):
        """One fallback per attempt, or up to MAX_FALLBACK_HOPS along an opt-in chain
        (spec fallback_chain); never back to a model the attempt already ran on."""
        fb = spec.get("fallback")
        if not fb:
            return False
        hops = self._fallback_hops(a)
        if hops == 0:
            return True
        tried = {h.get("model") for h in a.get("model_history") or []} | {a.get("model")}
        return bool(spec.get("fallback_chain")) and hops < MAX_FALLBACK_HOPS and fb not in tried

    def _fallback_on_provider_error(self, a):
        return bool(self._spec_of(a).get("fallback_on_provider_error"))

    def _paced(self, a):
        """True when starting `a` now would break its provider's pacing (spec max_active /
        start_spacing_s). Called under self.lock from _schedule; records the start time when
        the attempt may go. Models without pacing fields are never held."""
        spec = self._spec_of(a)
        src = spec.get("key")
        if not src or not (spec.get("max_active") or spec.get("start_spacing_s")):
            return False
        if self._over_cap(src, spec.get("max_active")):
            return True  # a finishing attempt's controller kicks the scheduler again
        return self._spacing_hold(src, spec.get("start_spacing_s") or 0)

    def _over_cap(self, src, cap):
        return bool(cap) and sum(1 for x in self._active() if self._spec_of(x).get("key") == src) >= cap

    def _spacing_hold(self, src, spacing):
        last = getattr(self, "_pace_last", {}).get(src, 0.0)
        wait = spacing - (time.time() - last)
        if wait > 0:
            self.loop.call_later(wait + 0.05, self.kick)
            return True
        if not hasattr(self, "_pace_last"): self._pace_last = {}
        self._pace_last[src] = time.time()
        return False

    async def _schedule(self):
        try:
            self.reconcile()
        except Exception:
            pass
        with self.lock:
            # resume controllers for attempts that were live before a restart (reattach, no re-dispatch)
            for a in self._active():
                if a["attempt_id"] not in self.controllers and a.get("run_id"):
                    self.controllers[a["attempt_id"]] = asyncio.ensure_future(self._control(a))
                elif a["attempt_id"] not in self.controllers and a["state"] == "starting":
                    self._set(a, "failed", "launcher restarted during start; no run id recorded (retry from Orbit)")
            # a crash between the budget_stop mark and the terminal write leaves the attempt
            # non-terminal forever: it is not in _active(), so no restart path would ever finish
            # it, and the nonterminal attempt keeps apply-update blocked. Complete the stop honestly.
            # A teardown that is already in flight lands the attempt itself; skipping it here keeps
            # every kick from spawning a no-op recovery that kicks again.
            for a in [x for x in self.attempts.values()
                      if x.get("budget_stop") and x["state"] in ("starting", "running", "cancel_requested")
                      and x["attempt_id"] not in self.controllers
                      and x["attempt_id"] not in self._budget_finishing]:
                self.controllers[a["attempt_id"]] = asyncio.ensure_future(self._budget_stop_recover(a))
            free = self.cfg["max_concurrent"] - len(self._active())
            for aid in self._queue():
                if free <= 0: break
                a = self.attempts[aid]
                if a["state"] == "backoff" and time.time() < a.get("retry_at", 0): continue
                if self._paced(a): continue  # provider pacing (nous): skip, later queued attempts may still start
                free -= 1; self._set(a, "starting", "creating sandbox")
                self.controllers[aid] = asyncio.ensure_future(self._control(a))

    async def _control(self, a):
        aid = a["attempt_id"]
        budget = deadline = started = None
        watchdog = None
        try:
            # budget.timeout_s as an attempt wall clock: anchored at dispatch, kept across a
            # restart-reattach, enforced by the loop's fast path below and by the watchdog even
            # when this controller is itself stuck inside a backend call.
            budget = attempt_budget_s(a, self.cfg)
            if budget:
                with self.lock:
                    started = a.get("budget_started_at") if a.get("run_id") else None  # resumed: keep the original clock
                    if not started:
                        started = time.time(); a["budget_started_at"] = started; self._save(a)
                deadline = started + budget
                watchdog = asyncio.ensure_future(self._budget_watchdog(a, asyncio.current_task(), deadline, budget, started))
            if a.get("kind") == "check": return await self._control_check(a)
            if a.get("run_id") and a.get("resume_pending"):
                a.pop("resume_pending", None)
                try:
                    ids = await self.backend.resume(a, RESUME_PROMPT)
                    if isinstance(ids, dict):
                        a.update({k: v for k, v in ids.items() if v is not None})
                    self._set(a, "running", "run resumed after a rate limit")
                except Exception:
                    # Resuming is best-effort: if the backend cannot continue this run, tear the
                    # old sandbox down and let this controller start a fresh one below.
                    await self._cleanup_once(a)
                    for k in ("run_id", "sandbox", "sandbox_id", "workdir", "local_base_rev", "seed_commit"):
                        a.pop(k, None)
                    a["cursor"] = 0
            if not a.get("run_id"):
                agy = a.get("kind") == "agy-cua"
                if a.get("pool") and not a.get("model"):
                    a["model"] = self._pick_free(a) or MODELS["deepseek-v4.1-flash-free"]["fallback"]; a["model_history"].append({"model": a["model"], "at": time.time()}); self._save(a)
                # Pick the key after the model is known so free-pool / fallback / aliases use the right source.
                spec = model_spec(self.cfg, a.get("model"), a.get("brief"))
                src = spec.get("key") or "none"
                key = "" if agy or src == "none" else provider_key(self.cfg, src)  # agy uses the host's own agy sign-in; no key passed
                if not agy and src != "none" and not key:
                    fb = spec.get("fallback")
                    if spec.get("fallback_on_missing_key") and self._may_fallback(a, spec):
                        # Opt-in models (nous) roll their fallback instead of failing before start.
                        a["fallback_hops"] = self._fallback_hops(a) + 1
                        a["fallback"] = {"from": spec["name"], "to": fb, "reason": f"{src} key not configured on this PC", "at": time.time()}
                        a["model"] = fb
                        a.setdefault("model_history", []).append({"model": fb, "fallback": f"{spec['name']}->{fb}", "after": "provider key not configured", "at": time.time()})
                        a["rate_limited"], a["cursor"] = 0, 0
                        self._set(a, "queued", f"{src} key not configured on this PC; falling back to {fb}"); return
                    self._set(a, "failed", "provider key not configured on this PC"); return
                # A restarted attempt may reuse the deterministic sandbox name its previous
                # incarnation was torn down under; a fresh create is a new sandbox to clean.
                for name in (a.get("sandbox"), sandbox_name(a) if a.get("model") else None):
                    if name: self._cleaned_sandboxes.discard(name)
                ids = await self.backend.start(a, key); del key
                a.update(ids)
                if ids.get("sandbox"): self._cleaned_sandboxes.discard(ids["sandbox"])
                self._set(a, "running", ("agy" if agy else "opencode") + " run started", {"run_id": ids["run_id"]})
            last_hb = last_ev = time.time()
            while True:
                if a.get("budget_stop") or (deadline and time.time() >= deadline):
                    # Watchdog already owns the stop: do not land a racing worker "exited"
                    # as a normal turn (macOS CI hit "turn ended: exited" instead of timeout).
                    return await asyncio.shield(self._budget_finish(a, budget, started))
                if a.get("cancel") and not a.get("interrupted"):
                    await self.backend.interrupt(a); a["interrupted"] = True
                    self._set(a, "cancelled", "interrupt confirmed; run stopped"); await self._cleanup_once(a); return
                evs, cur = await self.backend.events(a, a["cursor"])
                with self.lock:
                    out = []
                    for e in evs:
                        a["lseq"] += 1; out.append(dict(e, lseq=a["lseq"], kind="worker:" + e["kind"], ts=e.get("ts") or time.time()))
                    a["cursor"] = cur
                    if out: self._append_events(a, out); a["updated_at"] = time.time(); self._save(a); last_ev = time.time()
                    elif time.time() - last_hb >= HEARTBEAT_S:
                        # liveness only: proves the controller loop runs; idle_s = seconds since the last worker event
                        a["lseq"] += 1; self._append_events(a, [{"lseq": a["lseq"], "ts": time.time(), "kind": "heartbeat", "idle_s": round(time.time() - last_ev)}])
                        self._save(a)
                    if out or time.time() - last_hb >= HEARTBEAT_S: last_hb = time.time()
                for e in evs:
                    txt = e.get("text") or e.get("line") or ""
                    if e["kind"] == "error" and a.get("kind") == "agy-cua": continue  # agy has no backoff/fallback path
                    if e["kind"] == "error" and FREE_TIER_DENIED.search(txt):
                        return await self._backoff(a, exhausted=True, reason="free tier refused")
                    if e["kind"] == "error" and MODEL_UNAVAILABLE.search(txt):
                        # The provider refuses this model for API traffic entirely: roll the
                        # configured fallback once instead of finishing the attempt as failed.
                        return await self._fallback_unavailable(a, reason=redact(txt)[:160])
                    if e["kind"] == "error" and ZOO_ADMISSION.search(txt) and is_zoo_model(self.cfg, a.get("model"), a.get("brief")):
                        # Zoo: 402 "Insufficient credits" is an admission/concurrency refusal, not a
                        # permanent failure -- retry it on the same model, honouring Retry-After if sent.
                        return await self._backoff(a, reason="zoo admission 402", retry_after=retry_after_s(txt))
                    if e["kind"] == "error" and RATE_LIMIT.search(txt):
                        if is_monthly_limit(txt) and is_go_model(self.cfg, a.get("model"), a.get("brief")) and not a.get("fallback"):
                            return await self._fallback_to_deepseek(a, reason="go monthly-limit 429")
                        return await self._backoff(a, retry_after=retry_after_s(txt))
                    if e["kind"] == "error" and PROVIDER_FAILURE.search(txt) and self._fallback_on_provider_error(a):
                        return await self._fallback_unavailable(a, reason="provider failure: " + redact(txt)[:120])
                    if e["kind"] in ("turn_ended", "exited"):
                        if a.get("budget_stop") or (deadline and time.time() >= deadline):
                            return await asyncio.shield(self._budget_finish(a, budget, started))
                        return await self._finish(a, e)
                await asyncio.sleep(1.0)
        except Exception as ex:  # noqa
            try:
                if isinstance(ex, WorkspaceError):
                    a["workspace_error"] = ex.to_dict()
                self._set(a, "failed", f"controller error: {type(ex).__name__}: {redact(str(ex))[:300]}")
                if not a.get("sandbox") and a.get("model"): a["sandbox"] = sandbox_name(a)  # created but not recorded yet
            except Exception:
                if not a.get("sandbox") and a.get("model"): a["sandbox"] = sandbox_name(a)
        finally:
            # Check jobs have their own finally (check_cleanup). Do not also run the
            # coding-bee sandbox teardown against a check container.
            # When the budget watchdog already owns teardown, skip cleanup here:
            # killing the child first makes _budget_finish drain an "exited" event
            # and land "turn ended: exited" instead of the timeout detail.
            if a.get("kind") != "check":
                with self.lock:
                    budget_owned = aid in self._budget_finishing or (
                        a.get("budget_stop") and a.get("state") not in TERMINAL)
                    resuming = bool(a.get("resume_pending")) and a.get("state") not in TERMINAL
                if not budget_owned and not resuming:
                    try:
                        await self._cleanup_once(a)
                    except Exception:
                        pass
            if watchdog is not None and not watchdog.done(): watchdog.cancel()
            self.controllers.pop(aid, None); self.kick()

    async def _budget_watchdog(self, a, controller, deadline, budget, started):
        """One-shot wall-clock enforcer for one attempt's budget.timeout_s.

        Runs beside the controller so the budget holds regardless of worker behavior: at the
        deadline it stops the controller (whose polling loop - or a backend call inside it - may
        itself be stuck) and drives the attempt through _budget_finish to a terminal state."""
        await asyncio.sleep(max(0.0, deadline - time.time()))
        await asyncio.shield(self._budget_finish(a, budget, started, controller))

    async def _drain_terminal_event(self, a):
        """Bounded final drain at the budget stop: persist whatever the worker already produced and
        return its terminal event (turn_ended/exited), or None when none is pending.

        The deadline decision must not preempt the event drain: a poll loop that wakes at/after the
        deadline would otherwise force-fail a run that finished INSIDE its budget with a false
        'worker outlived' detail, and its collected result would be lost. The backend pages events
        (CuaBackend requests 200 per call), so the drain repeats until the stream returns an empty
        page or BUDGET_STOP_DRAIN_S elapses - a terminal event sitting past the first page must not
        flip the verdict. The controller is already stopped when this runs, so the cursor cannot be
        advanced concurrently."""
        terminal = None
        end = time.monotonic() + BUDGET_STOP_DRAIN_S
        while True:
            try:
                evs, cur = await asyncio.wait_for(self.backend.events(a, a.get("cursor", 0)),
                                                  max(0.0, end - time.monotonic()))
            except Exception:  # no run, dead backend call, drain timeout: fall through to the honest stop
                return terminal
            with self.lock:
                out = []
                for e in evs:
                    a["lseq"] += 1; out.append(dict(e, lseq=a["lseq"], kind="worker:" + e["kind"], ts=e.get("ts") or time.time()))
                a["cursor"] = cur
                if out: self._append_events(a, out); a["updated_at"] = time.time(); self._save(a)
            for e in evs:
                if e.get("kind") in ("turn_ended", "exited"): terminal = e
            if not evs: break  # stream exhausted: every page has been drained
        return terminal

    def _recovered_manifest(self, a):
        """The attempt's already-collected result, when a stop that crashed before the terminal
        write left one on disk (result manifest written by _finish before the state could land)."""
        try:
            m = json.loads((self.dir / "attempts" / f"{a['attempt_id']}.result.json").read_text("utf-8"))
        except Exception:
            return None
        return m if isinstance(m, dict) and m.get("schema") == "orbit.local-worker.result/v1" \
            and m.get("attempt_id") == a["attempt_id"] else None

    async def _budget_stop_recover(self, a):
        """Finish a budget stop that a crash interrupted between the budget_stop mark and the
        terminal write (started by _schedule when a restart finds the attempt in that state)."""
        aid = a["attempt_id"]
        try:
            budget = attempt_budget_s(a, self.cfg)
            if budget:
                await self._budget_finish(a, budget, a.get("budget_started_at"), recover=True)
            else:
                with self.lock:
                    if a["state"] not in TERMINAL:
                        self._set(a, "failed", "budget stop was interrupted by a restart before the outcome was recorded")
                try: await self._cleanup_once(a, timeout=2 * BUDGET_STOP_GRACE_S)
                except Exception: pass
        finally:
            self.controllers.pop(aid, None); self.kick()

    async def _budget_finish(self, a, budget, started, controller=None, recover=False):
        """Force one attempt past its budget wall clock to an honest terminal state (idempotent).

        Whoever arrives first - the controller's own deadline check, or this watchdog when the
        controller is stuck - marks the attempt budget_stop (so _schedule cannot resurrect a
        controller for it), stops the worker, and lands failed with a detail naming the budget.
        The terminal write happens before cleanup, so the active-attempt slot frees immediately
        and an admin apply-update is no longer blocked by a runaway. Shielded by callers: once
        started, the teardown survives the controller's finally cancelling the watchdog.

        Before the verdict the stop drains the worker's pending events across SDK pages (a run that
        finished inside its budget lands succeeded with its result, never a false outlived detail)
        and restores an already-collected result when a crash left one behind, with the same verdict
        the terminal write would land for that shape; with recover=True it completes a stop a
        previous process died in the middle of, with a detail that does not claim what it cannot
        know about the worker's fate. Every stop step is bounded, collection included: a backend
        that never answers lands the attempt failed honestly instead of wedging the teardown."""
        aid = a["attempt_id"]
        cleaned = False
        with self.lock:
            if a["state"] in TERMINAL or a["state"] not in ("starting", "running", "cancel_requested"):
                return
            if aid in self._budget_finishing: return  # one teardown per attempt; the runner lands it
            if a.get("budget_stop") and not recover: return
            self._budget_finishing.add(aid)
            if not a.get("budget_stop"):
                a["budget_stop"] = True; self._save(a)
        try:
            if controller is not None and not controller.done():
                controller.cancel()
                try: await asyncio.wait_for(controller, BUDGET_STOP_GRACE_S)
                except (asyncio.CancelledError, Exception): pass
            done = await self._drain_terminal_event(a)
            if done is not None:  # the run already finished: land it with its collected result
                try:
                    # bounded like every other stop step: a backend.result() that never returns
                    # must not leave the attempt budget_stop-marked and non-terminal forever.
                    await asyncio.wait_for(self._finish(a, done), BUDGET_STOP_GRACE_S)
                    cleaned = True  # _finish tore the worker down itself; the finally must not repeat it
                except Exception as ex:
                    with self.lock:
                        if a["state"] not in TERMINAL:
                            self._set(a, "failed", f"budget timeout: brief.budget.timeout_s={int(budget)}s reached; "
                                                   f"the run finished but collecting its result failed ({type(ex).__name__})")
            elif (man := self._recovered_manifest(a)) is not None:
                obs = man.get("observed") or {}
                # same verdict the terminal write lands for this manifest shape (status completed,
                # no error, and a stop reason the live path accepts: end_turn or unknown)
                ok = obs.get("status") == "completed" and not obs.get("error") and obs.get("stop_reason") in ("end_turn", None)
                self._set(a, "succeeded" if ok else "failed",
                          f"collected result restored after the budget stop was interrupted "
                          f"(brief.budget.timeout_s={int(budget)}s)")
            else:
                try: await asyncio.wait_for(self.backend.interrupt(a), BUDGET_STOP_GRACE_S)
                except Exception: pass
                elapsed = max(0.0, time.time() - (started or time.time()))
                with self.lock:
                    if a["state"] not in TERMINAL:
                        if recover:
                            self._set(a, "failed", f"budget timeout: brief.budget.timeout_s={int(budget)}s reached; "
                                                   "the stop was interrupted by a restart before the outcome was recorded")
                        else:
                            self._set(a, "failed", f"budget timeout: worker outlived brief.budget.timeout_s={int(budget)}s "
                                                   f"(launcher stopped the run after {elapsed:.0f}s)")
        finally:
            with self.lock: self._budget_finishing.discard(aid)
            if not cleaned:  # the success path already cleaned up inside _finish: exactly one teardown
                try: await self._cleanup_once(a, timeout=2 * BUDGET_STOP_GRACE_S)
                except Exception: pass
            self.kick()

    async def _control_check(self, a):
        """Check-run job: start the container, run brief.commands, record exit codes + logs. The result never
        depends on a diff, so `git checkout -B` inside a check cannot hide anything."""
        if a.get("run_id"):  # was running when the launcher stopped: the docker exec stream is gone
            self._set(a, "failed", "launcher restarted during a check run (retry from Orbit)")
            await self.backend.check_cleanup(a); return
        try:
            ids = await self.backend.check_start(a)
            a.update(ids); self._set(a, "running", "check run started", {"run_id": ids["run_id"]})
            checks = await self.backend.run_checks(a)
            if a.get("cancel"):
                self._set(a, "cancelled", "cancelled between checks"); return
            bad = [c["name"] for c in checks if c.get("exit_code") != 0]
            conclusion = "failure" if bad or not checks else "success"
            prof = security_profile(self.cfg, a.get("security_profile") or "default")
            b = a["brief"]
            manifest = {"schema": "orbit.local-worker.check-result/v1", "attempt_id": a["attempt_id"],
                        "source": {"project": b.get("project"), "repo_rev": b.get("source_rev"), "image": prof.get("image"), "kind": "check",
                                   "security_profile": a.get("security_profile"), "launcher": VERSION},
                        "observed": {"node": platform.node(), "run_id": a.get("run_id"), "sandbox_id": a.get("sandbox_id"), "workdir": a.get("workdir"),
                                     "local_base_rev": a.get("local_base_rev"), "seed_commit": a.get("seed_commit"),
                                     "result_file": str(self.dir / "attempts" / f"{a['attempt_id']}.result.json")},
                        "check_run": {"security_profile": a.get("security_profile"), "conclusion": conclusion, "checks": checks},
                        "patch": None, "note": "check_run is measured by the launcher (exit codes and logs); there is no model and no diff"}
            (self.dir / "attempts" / f"{a['attempt_id']}.result.json").write_text(json.dumps(manifest, indent=1), "utf-8")
            self._set(a, "succeeded" if conclusion == "success" else "failed",
                      "checks passed" if conclusion == "success" else ("checks failed: " + ", ".join(bad) if bad else "no checks ran"))
        finally:
            await self.backend.check_cleanup(a)

    def _pick_free(self, a):
        """Least-recently-rate-limited free model not yet tried by this attempt (ties: least recently used)."""
        with self.lock:
            n = len(render_prompt(a["brief"]))  # respect each model's context: skip models the brief does not fit
            cand = [m for m in FREE_POOL if m not in a.get("tried", []) and m in self.model_health and MODELS[m]["max_prompt_chars"] >= n]
            if not cand: return None
            m = min(cand, key=lambda x: (self.model_health[x]["last_limited"], self.model_health[x]["last_used"], FREE_POOL.index(x)))
            self.model_health[m]["last_used"] = time.time(); return m

    async def _fallback_unavailable(self, a, reason="model unavailable"):
        """Roll to the model spec's fallback after a provider refuses the model outright.

        Unlike a 429/402 this never retries the refused model: one switch, then the queue
        runs the attempt on the fallback. A second refusal (or a model with no fallback)
        is terminal - the attempt must not bounce between models forever.
        """
        spec = model_spec(self.cfg, a.get("model"), a.get("brief"))
        fb = spec.get("fallback")
        await self._cleanup_once(a)
        if self._may_fallback(a, spec):
            a["fallback_hops"] = self._fallback_hops(a) + 1
            a["fallback"] = {"from": spec["name"], "to": fb, "reason": f"{reason} (model unavailable)", "at": time.time()}
            a["model"] = fb
            a.setdefault("model_history", []).append(
                {"model": fb, "fallback": f"{spec['name']}->{fb}", "after": reason, "at": time.time()}
            )
            a["rate_limited"], a["cursor"] = 0, 0
            for k in ("run_id", "sandbox"):
                a.pop(k, None)
            self._set(a, "queued", f"{reason}; model {spec['name']} unavailable, falling back to {fb}")
            return
        self._set(a, "failed", f"{reason}; model unavailable and no remaining fallback")

    async def _fallback_to_deepseek(self, a, reason="go monthly-limit 429"):
        # Retry once on Nous DeepSeek 1017 after a Go monthly-limit 429. Records fallback="ds1017n".
        self.note_rate_limit(a.get("model"))
        await self._cleanup_once(a)
        nxt = "nous:deepseek-v4.1-flash-1017"
        a["fallback"] = "ds1017n"
        a["model"] = nxt
        a.setdefault("model_history", []).append({"model": nxt, "fallback": "ds1017n", "after": reason, "at": time.time()})
        a["rate_limited"], a["cursor"] = 0, 0
        for k in ("run_id", "sandbox"): a.pop(k, None)
        self._set(a, "queued", f"{reason}; fallback=ds1017n")

    async def _backoff(self, a, exhausted=False, reason="provider rate limit", retry_after=None):
        self.note_rate_limit(a.get("model"))
        if a.get("pool") and a.get("model") in FREE_POOL:
            await self._cleanup_once(a)
            bad = a["model"]; self.model_health[bad]["last_limited"] = time.time(); a["tried"].append(bad)
            nxt = self._pick_free(a)
            if not nxt:
                nxt = MODELS[bad]["fallback"]
                a["fallback"] = {"from": "pool:free", "to": nxt, "reason": f"{reason} on every free model ({', '.join(a['tried'])})", "at": time.time()}
            a["model"] = nxt; a["model_history"].append({"model": nxt, "after": f"{reason} on {bad}", "at": time.time()})
            a["rate_limited"], a["cursor"] = 0, 0
            for k in ("run_id", "sandbox"): a.pop(k, None)
            self._set(a, "queued", f"{reason} on {bad}; switching to {nxt}"); return
        spec = model_spec(self.cfg, a.get("model"), a.get("brief"))
        n = a["rate_limited"]; delays = spec.get("backoff_s") or self.cfg["backoff_s"]
        if exhausted or n >= len(delays):
            await self._cleanup_once(a)
            fb = spec.get("fallback")
            if self._may_fallback(a, spec):
                a["fallback_hops"] = self._fallback_hops(a) + 1
                a["fallback"] = {"from": spec["name"], "to": fb, "reason": f"{reason} after {n} retr{'y' if n == 1 else 'ies'}", "at": time.time()}
                a["model"], a["rate_limited"], a["cursor"] = fb, 0, 0
                for k in ("run_id", "sandbox"): a.pop(k, None)
                self._set(a, "queued", f"{reason} on {spec['name']}; falling back to {fb}"); return
            self._set(a, "failed", f"{reason} persisted after retries"); return
        resumable = bool(a.get("run_id") and a.get("sandbox")
                         and callable(getattr(self.backend, "resume", None)))
        a["rate_limited"] = n + 1
        delay = delays[n]
        if retry_after is not None:
            # The provider told us when to come back; waiting the fixed schedule instead either
            # retries too early (another 429) or wastes the pass. Record what was honoured.
            delay = retry_after
        a["retry_at"] = time.time() + delay
        a["retry_after_s"] = retry_after
        if resumable:
            # Same model, and the backend can continue the run: keep the sandbox, run id and
            # cursor; the retry sends a short continue turn instead of a fresh full brief.
            a["resume_pending"] = True
        else:
            # A model switch (or a backend without resume) restarts from a fresh sandbox.
            await self._cleanup_once(a)
            for k in ("run_id", "sandbox"): a.pop(k, None)
            a["cursor"] = 0
        hint = " (provider Retry-After)" if retry_after is not None else " (queued)"
        self._set(a, "backoff", f"{reason}; retry {n + 1}/{len(delays)} in {delay:g}s{hint}")
        self.loop.call_later(delay + 0.05, self.kick)

    async def _finish(self, a, e):
        res = await self.backend.result(a)
        manifest = await self._manifest(a, res)
        (self.dir / "attempts" / f"{a['attempt_id']}.result.json").write_text(json.dumps(manifest, indent=1), "utf-8")
        ok = e.get("stop_reason") in ("end_turn", None) and not res.get("error") and e["kind"] == "turn_ended"
        # Tear the sandbox down before publishing the terminal state: a reader that observes
        # "succeeded"/"failed" must never see the recorded sandbox still alive. _set's terminal
        # write (journal save + landed-event publication) can stall on a contended runner, and
        # the retry-resume tests synchronise on the terminal state - with the old order they
        # could observe a finished attempt whose teardown had not run yet (the 3.13 red run
        # saw cleaned == [] instead of ["sb-1"]). Teardown is idempotent, so the controller's
        # finally call stays a no-op.
        await self._cleanup_once(a)
        self._set(a, "succeeded" if ok else "failed", f"turn ended: {e.get('stop_reason') or e['kind']}")

    async def _manifest(self, a, res):
        b = a["brief"]; files = []
        env = patch = None
        if hasattr(self.backend, "patch"):
            try: env = await self.backend.environment(a)
            except Exception as ex: env = {"error": type(ex).__name__}
            patch = await self.backend.patch(a)
        for art in res.get("artifacts", []):
            files.append({"path": art["path"], "size": art["size"], "sha256": await self.backend.file_sha256(a, art["path"]),
                          "change": "deleted" if art.get("size") is None else "changed",
                          "in_assigned_scope": in_assigned_scope(art["path"], b.get("assigned_files"))})
        claims = {}
        m = re.findall(r"\{[\s\S]*\}", res.get("text") or "")
        if m:
            try: claims = json.loads(m[-1])
            except Exception: claims = {"unparsed": True}
        return {"schema": "orbit.local-worker.result/v1", "attempt_id": a["attempt_id"],
                "source": {"project": b.get("project"), "repo_rev": b.get("source_rev"), "input_manifest_sha256": b.get("input_manifest_sha256"),
                           "image": self.cfg["image"], "kind": a.get("kind") or "opencode", "mode": a.get("mode"), "tier": a.get("tier"), "harness": "agy" if a.get("kind") == "agy-cua" else "opencode", "model": a.get("model") or self.cfg["model"], "fallback": a.get("fallback"), "tier_pool": a.get("pool"), "model_history": a.get("model_history"), "launcher": VERSION},
                "observed": {"node": platform.node(), "run_id": a.get("run_id"), "sandbox_id": a.get("sandbox_id"), "workdir": a.get("workdir"), "local_base_rev": a.get("local_base_rev"), "seed_commit": a.get("seed_commit"),
                             "result_file": str(self.dir / "attempts" / f"{a['attempt_id']}.result.json"), "status": res.get("status"), "stop_reason": res.get("stop_reason"),
                             "tool_calls": res.get("tool_calls"), "usage": res.get("usage"), "files": (patch or {}).get("files") or files, "artifacts": files, "error": res.get("error")},
                "environment": env, "patch": patch,
                "worker_claims": claims, "checks": claims.get("checks", []) if isinstance(claims, dict) else [],
                "note": "worker_claims are self-reported by the model; only 'observed' was measured by the launcher"}

# ---------------------------------------------------------------- HTTP API

def active_attempts_for(attempts, project):
    """Nonterminal attempts for a project, by the field the launcher actually stores.

    Attempts keep their project inside the brief (``attempt["brief"]["project"]``),
    so checking ``attempt["project"]`` would silently allow cleanup under a
    running bee.
    """
    count = 0
    for attempt in attempts.values():
        brief = attempt.get("brief") or {}
        if brief.get("project") == project and attempt.get("state") not in TERMINAL:
            count += 1
    return count


def build_provisioner(cfg):
    """Construct the provisioning service from config, or return None."""
    if _provisioning is None:
        return None
    block = cfg.get("provisioning") or {}
    if not block.get("enabled"):
        return None
    config_path = Path(cfg.get("_config_path") or "config.json")
    policy_path = Path(block.get("policy") or config_path.with_name("provisioning-policy.json"))
    journal_dir = Path(block.get("journal") or (Path(cfg["data_dir"]) / "provisioning"))
    policy = _provisioning.load_policy(policy_path)
    registry = _provisioning.RegistryStore(config_path, cache=cfg.setdefault("projects", {}))

    def credential(repo):
        # Authentication is delegated to the host's git credential helper. Only the
        # declared capability is consulted; no secret passes through the launcher.
        if repo in (block.get("credential_ready_repos") or []):
            return "host-credential-helper"
        return None

    return _provisioning.Provisioner(
        policy=policy,
        journal=_provisioning.Journal(journal_dir),
        registry=registry,
        credential=credential,
    )


# Routing-relevant keys a POST /v1/config applies to the RUNNING launcher's in-memory cfg and
# persists to the config file. max_concurrent is accepted too but stays runtime-only (a restart
# reverts it), exactly as before hot apply existed.
CONFIG_HOT_KEYS = ("model", "models", "prefer_zoo", "projects")


def config_backup_path(path):
    """Timestamped backup name beside `path` (UTC; Windows-safe, no colons in the stamp)."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    cand = path.with_name(path.name + ".bak-" + stamp)
    n = 1
    while cand.exists():  # two applies within one second must not overwrite each other's backup
        cand = path.with_name(path.name + ".bak-" + stamp + "-" + str(n))
        n += 1
    return cand


def write_config_file(path, raw):
    """Persist a config dict with the admin path's atomic write plus a timestamped backup.

    The previous file is copied to `<name>.bak-<UTC stamp>` first, then the new content is
    written to a temp file in the same directory, fsynced, and os.replace()d over the target, so
    a crash leaves either the old or the new file - never a partial one. Returns the backup path,
    or None when there was no previous file to back up.
    """
    path = Path(path)
    bak = None
    if path.exists():
        bak = config_backup_path(path)
        data = path.read_bytes()
        fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)
    return bak


def apply_config_patch(L, b):
    """Validate and apply one POST /v1/config body to the running launcher.

    Returns (status, payload) without touching the socket, so the handler can send after the lock
    is released. Runs under L.lock: validation, the persisted write and the in-memory apply are
    one step, so concurrent POSTs cannot lose updates and /v1/health never observes a
    half-applied patch. The persisted write happens BEFORE the in-memory apply, so a failed disk
    write leaves the running cfg exactly as it was.

    `models` and `projects` are entry-level upserts (a POST that pins one project must not drop
    the others); `model` and `prefer_zoo` are scalars. Model names - the host default and every
    project override - are validated against the allow-list as the patch would leave it, so a bad
    name fails here instead of turning into 400s at dispatch time.
    """
    if not isinstance(b, dict):
        return 400, {"error": "config body must be a JSON object"}
    allowed = ("max_concurrent",) + CONFIG_HOT_KEYS
    unknown = sorted(set(b) - set(allowed))
    if unknown:
        return 400, {"error": "unsupported config key(s): " + ", ".join(unknown) + " (allowed: " + ", ".join(allowed) + ")"}
    if not b:
        return 400, {"error": "nothing to apply (allowed: " + ", ".join(allowed) + ")"}
    patch = {}
    if "max_concurrent" in b:
        try: n = int(b["max_concurrent"])
        except (TypeError, ValueError): return 400, {"error": "max_concurrent must be an integer"}
        if not 1 <= n <= MAX_WORKERS: return 400, {"error": f"max_concurrent must be 1..{MAX_WORKERS}"}
        patch["max_concurrent"] = n
    if "prefer_zoo" in b:
        if not isinstance(b["prefer_zoo"], bool): return 400, {"error": "prefer_zoo must be true or false"}
        patch["prefer_zoo"] = b["prefer_zoo"]
    if "models" in b:
        ms = b["models"]
        if not isinstance(ms, dict): return 400, {"error": "models must be an object of per-model overrides"}
        for k2, v2 in ms.items():
            if not isinstance(v2, dict): return 400, {"error": "models." + k2 + " must be an object"}
        patch["models"] = {**(L.cfg.get("models") or {}), **ms}
    if "projects" in b:
        pr = b["projects"]
        if not isinstance(pr, dict): return 400, {"error": "projects must be an object of per-project entries"}
        for k2, v2 in pr.items():
            if not isinstance(v2, dict): return 400, {"error": "projects." + k2 + " must be an object"}
            pm = v2.get("model")
            if pm is not None and (not isinstance(pm, str) or not pm.strip()):
                return 400, {"error": "projects." + k2 + ".model must be a non-empty string"}
        patch["projects"] = {**(L.cfg.get("projects") or {}), **pr}
    if "model" in b:
        m = b["model"]
        if not isinstance(m, str) or not m.strip(): return 400, {"error": "model must be a non-empty string"}
        patch["model"] = m.strip()
    probe = dict(L.cfg)
    if "models" in patch: probe["models"] = patch["models"]
    if "model" in patch: probe["model"] = patch["model"]
    if "model" in patch:
        try: model_spec(probe, patch["model"])
        except ValueError as ex: return 400, {"error": str(ex)}
    for k2, v2 in (patch.get("projects") or {}).items():
        pm = (v2 or {}).get("model")
        if pm is not None:
            try: model_spec(probe, pm.strip())
            except ValueError as ex: return 400, {"error": "projects." + k2 + ".model: " + str(ex)}
    persisted = [k for k in CONFIG_HOT_KEYS if k in patch]
    if persisted:
        cfg_path = Path(L.cfg.get("_config_path") or "config.json")
        try:
            raw = json.loads(cfg_path.read_text("utf-8-sig")) if cfg_path.exists() else None
        except ValueError:
            return 400, {"error": str(cfg_path) + " is not valid JSON; repair it before applying config"}
        except OSError as ex:
            return 500, {"error": "config read failed: " + type(ex).__name__}
        if raw is not None and not isinstance(raw, dict):
            return 400, {"error": str(cfg_path) + " must contain a JSON object"}
        if raw is None:  # no file (or unreadable): rebuild one from the running cfg, minus memory-only keys
            raw = {k: v for k, v in L.cfg.items() if k not in ("_config_path", "extra_binds")}
        for k in persisted: raw[k] = patch[k]
        try: write_config_file(cfg_path, raw)
        except OSError as ex:
            return 500, {"error": "config write failed: " + type(ex).__name__ + ": " + redact(str(ex))[:200]}
    # Mutate the maps IN PLACE (clear + update): the provisioning RegistryStore holds the same
    # projects dict object as its cache, so rebinding would desync it.
    if "models" in patch:
        d = L.cfg.setdefault("models", {}); d.clear(); d.update(patch["models"])
    if "projects" in patch:
        d = L.cfg.setdefault("projects", {}); d.clear(); d.update(patch["projects"])
    for k in ("model", "prefer_zoo", "max_concurrent"):
        if k in patch: L.cfg[k] = patch[k]
    return 200, {"ok": True, "applied": sorted(patch), "persisted": persisted,
                 "max_concurrent": L.cfg["max_concurrent"], "model": L.cfg["model"]}


def make_handler(L, token):
    rails_access = None
    if getattr(L, 'cfg', {}).get('rails_enabled') is True:
        from rails_export import ReadAccess, handle_read
        rails_access = ReadAccess(L.cfg, token)
    class H(BaseHTTPRequestHandler):
        server_version = "orbit-launcher/" + VERSION
        protocol_version = "HTTP/1.1"  # keep-alive (plan C); every reply sets Content-Length
        timeout = 120  # idle keep-alive sockets close after 2 min (the box client drops them after 60 s)
        def log_message(self, *a): pass
        def _send(self, code, obj, *, rails=False):
            body = (json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=True) if rails else json.dumps(obj)).encode()
            self.send_response(code)
            if rails: self.send_header('Cache-Control', 'no-store')
            self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body)))
            if self.close_connection: self.send_header("Connection", "close")
            self.end_headers(); self.wfile.write(body)
        def _auth(self):
            h = self.headers.get("Authorization", "")
            if not (h.startswith("Bearer ") and hmac.compare_digest(h[7:].encode(), token.encode())):
                self.close_connection = True  # request body may be unread; don't reuse this socket
                self._send(401, {"error": "unauthorized"}); return False
            return True
        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 256_000: raise ValueError("body too large")
            return json.loads(self.rfile.read(n) or b"{}")
        def do_GET(self):
            if rails_access is not None and self.path.startswith('/v1/rails/'):
                code, obj = handle_read(self, L, rails_access, MODELS.keys() | AGY_MODELS)
                return self._send(code, obj, rails=True)
            if not self._auth(): return
            p = self.path.split("?")[0].strip("/").split("/"); q = dict(x.split("=", 1) for x in self.path.partition("?")[2].split("&") if "=" in x)
            if p == ["v1", "attempts"]:  # list (0.1.11, bundlebees): public fields + lease info only, newest 200
                rows = sorted(L.attempts.values(), key=lambda x: x.get("created_at") or 0, reverse=True)
                if q.get("active") in ("1", "true"): rows = [x for x in rows if x["state"] not in TERMINAL]
                return self._send(200, {"attempts": [L.listing(x) for x in rows[:200]]})
            if p == ["v1", "provision", "capabilities"]:
                svc = getattr(L, "provisioner", None)
                if svc is None:
                    cfg_block = L.cfg.get("provisioning") or {}
                    return self._send(200, {"schema": "rizzstack.provisioning/1", "backend": "hyvv",
                                            "enabled": False, "profiles": [], "states": [],
                                            "supports_cleanup": False, "supports_receipt_lookup": False,
                                            "reason": "provisioning is not enabled on this host" if not cfg_block.get("enabled") else "provisioning failed to initialise"})
                return self._send(200, svc.capabilities())
            if len(p) == 4 and p[:3] == ["v1", "provision", "receipts"]:
                svc = getattr(L, "provisioner", None)
                if svc is None: return self._send(501, {"error": "provisioning is not enabled on this host"})
                try: return self._send(200, svc.lookup_receipt(p[3]))
                except _provisioning.ProvisionError as ex:
                    return self._send(404 if ex.code == "receipt_unknown" else 409, ex.to_dict())
            if p == ["v1", "provision", "receipts"]:
                svc = getattr(L, "provisioner", None)
                if svc is None: return self._send(501, {"error": "provisioning is not enabled on this host"})
                return self._send(200, {"receipts": [{"idempotency_key": r.get("idempotency_key"), "project": r.get("project"), "state": r.get("state")} for r in svc.journal.all()]})
            if p == ["v1", "projects"]:  # names only: no paths, repos or branches
                return self._send(200, {"projects": [{"name": k} for k in sorted(L.cfg["projects"])]})
            if p == ["v1", "containers"]:  # the sandbox image profile, not live containers
                return self._send(200, {"containers": [{"image": L.cfg.get("image"), "runtime": L.cfg.get("runtime"), "memory_mb": L.cfg.get("memory_mb")}],
                                        "security_profiles": ["default"] + sorted(L.cfg.get("security_profiles") or {})})
            if p == ["v1", "version"]:
                return self._send(200, {"api_version": "v1", "version": VERSION,
                                        "capabilities": ["priority_routing", "go_fallback"]})
            if p == ["v1", "health"]:
                # `models` is the dispatchable allow-list (built-ins plus this host's config entries),
                # so a coordinator can validate a brief's model before it dispatches; `model` stays
                # the host default and is always a member. configured_model repeats it explicitly and
                # effective_model is what an unnamed brief resolves to on the wire (the Zoo
                # preference applies while that credential is readable).
                allow = sorted({L.cfg["model"], *MODELS, *((L.cfg.get("models") or {}).keys())})
                try: eff_model = model_spec(L.cfg, default_model(L.cfg)).get("model") or L.cfg["model"]
                except ValueError: eff_model = L.cfg["model"]
                try:
                    st = L.publish_runtime_status()
                except Exception:
                    live = len(L._active()); jact, jtmp = L.journal_counts()
                    st = {"active": live, "journal_active": jact, "journal_tmp": jtmp,
                          "split_brain": (live != jact) or (jtmp > 0)}
                return self._send(200, {"ok": True, "version": VERSION, "max_concurrent": L.cfg["max_concurrent"], "active": st["active"], "journal_active": st["journal_active"], "journal_tmp": st["journal_tmp"], "split_brain": st["split_brain"], "queued": len(L._queue()), "model": L.cfg["model"],
                                       "configured_model": L.cfg["model"], "effective_model": eff_model, "models": allow, "uptime_s": round(time.time() - L.started_at), "loop_lag_ms": round(L.loop_lag_ms, 1),
                                       **({"deployment_receipt": rc} if (rc := deployment_receipt(L.cfg)) is not None else {})})
            if p == ["v1", "capacity"]:  # admission inputs for go-max fillers (hyvv.capacity/1)
                return self._send(200, L.capacity_status())
            if p == ["v1", "landing", "events"]:  # authenticated export of the durable canonical outbox
                if _landing_export is None:
                    return self._send(501, {"error": "landing export is unavailable"})
                try:
                    page = _landing_export.export_events(
                        L.dir,
                        after=unquote(q["after"]) if "after" in q else None,
                        limit=q.get("limit"),
                    )
                except _landing_export.LandingContentConflict as ex:
                    return self._send(409, {"code": ex.code, "error": str(ex)})
                except _landing_export.LandingExportError as ex:
                    body = {"error": str(ex)}
                    code = getattr(ex, "code", None)
                    if code:
                        body["code"] = code
                    return self._send(400, body)
                except OSError:
                    return self._send(503, {"code": "landing_storage_unavailable",
                                            "error": "landing export storage unavailable; retry with the same cursor"})
                return self._send(200, page)
            if len(p) >= 3 and p[:2] == ["v1", "attempts"]:
                a = L.attempts.get(p[2])
                if not a: return self._send(404, {"error": "unknown attempt"})
                if len(p) == 3: return self._send(200, L.public(a))
                if p[3] == "events": return self._send(200, {"events": L.events_since(p[2], int(q.get("cursor", 0))), "state": a["state"], "cursor": a["lseq"]})
                if p[3] == "result":
                    f = L.dir / "attempts" / f"{p[2]}.result.json"
                    return self._send(200, json.loads(f.read_text("utf-8"))) if f.exists() else self._send(409, {"error": "no result yet", "state": a["state"]})
            self._send(404, {"error": "not found"})
        def do_POST(self):
            if not self._auth(): return
            p = self.path.strip("/").split("/")
            try: b = self._body()
            except Exception: self.close_connection = True; return self._send(400, {"error": "bad json"})
            if p == ["v1", "attempts"]:
                aid = str(b.get("attempt_id") or "").strip()
                if not aid:
                    aid = "att_" + uuid.uuid4().hex[:16]  # 0.1.13: mint when omitted (bundlebees client does not send one)
                elif not re.fullmatch(r"[A-Za-z0-9_\-]{8,64}", aid):
                    return self._send(400, {"error": "attempt_id required"})
                validation = _brief_validation_report(b.get("brief"))
                if isinstance(b.get("brief"), dict): b["brief"] = normalize_brief(b["brief"])
                err = validate_brief(b.get("brief"), L.cfg)
                if err: return self._send(400, {"error": err})
                if b["brief"].get("project") and b["brief"]["project"] not in L.cfg["projects"]: return self._send(403, {"error": "project not authorized on this PC"})
                try: a, created = L.submit(aid, b["brief"], b.get("model") or b.get("tier"), b.get("kind"), b.get("mode"), validation=validation)
                except ValueError as ex: return self._send(400, {"error": str(ex)})
                return self._send(201 if created else 200, L.public(a))
            if p == ["v1", "route"]:  # preview: what this request would resolve to; no run is started
                brief = b.get("brief") if isinstance(b.get("brief"), dict) else {}
                proj = b.get("project")
                # A top-level project stands in for the brief's own when the brief names none, so a
                # coordinator can preflight a project's default; dispatch reads brief.project, and
                # the brief wins here too, so preview and dispatch cannot disagree.
                if isinstance(proj, str) and proj.strip() and not brief.get("project"):
                    brief = dict(brief, project=proj.strip())
                try: return self._send(200, route_preview(L.cfg, b.get("model") or b.get("tier"), brief, b.get("kind")))
                except ValueError as ex: return self._send(400, {"error": str(ex)})
            if p == ["v1", "provision", "project"]:
                svc = getattr(L, "provisioner", None)
                if svc is None: return self._send(501, {"error": "provisioning is not enabled on this host"})
                try: receipt = svc.provision(b)
                except _provisioning.ProvisionError as ex: return self._send(409 if ex.code in ("journal_conflict", "project_conflict", "provision_in_progress", "project_retired", "stale_receipt", "quota_exhausted", "provision_lock_contention", "registry_busy") else 400, ex.to_dict())
                return self._send(200, receipt)
            if p == ["v1", "provision", "cleanup"]:
                svc = getattr(L, "provisioner", None)
                if svc is None: return self._send(501, {"error": "provisioning is not enabled on this host"})
                project = str(b.get("project") or "")
                try: result = svc.cleanup(project, active_attempts=lambda name: active_attempts_for(L.attempts, name))
                except _provisioning.ProvisionError as ex: return self._send(409, ex.to_dict())
                return self._send(200, result)
            if p == ["v1", "config"]:
                # max_concurrent stays runtime-only (1..MAX_WORKERS; a restart reverts it). The
                # routing-relevant keys (CONFIG_HOT_KEYS) are applied to the RUNNING launcher's
                # in-memory cfg AND persisted to the config file with the admin path's atomic
                # write plus a timestamped backup, so the next dispatch and /v1/health see the
                # change without a restart.
                with L.lock: code, out = apply_config_patch(L, b)
                if code == 200: L.kick()
                return self._send(code, out)
            if len(p) == 4 and p[:2] == ["v1", "attempts"] and p[3] == "review":  # 0.1.11: record a coordinator verdict
                if not isinstance(b.get("accepted"), bool): return self._send(400, {"error": "accepted must be true or false"})
                a, err = L.review(p[2], b["accepted"], b.get("note"))
                if err == "unknown": return self._send(404, {"error": "unknown attempt"})
                if err: return self._send(409, {"error": err})
                return self._send(200, dict(L.public(a), accepted=a["review"]["accepted"]))
            if len(p) == 4 and p[:2] == ["v1", "attempts"] and p[3] == "cancel":
                a = L.cancel(p[2]); return self._send(202, L.public(a)) if a else self._send(404, {"error": "unknown attempt"})
            self._send(404, {"error": "not found"})
    return H

def is_monthly_limit(text):
    """True when the provider error is a monthly usage-limit 429 (not a generic rate limit)."""
    t = text or ""
    return bool(RATE_LIMIT.search(t) and MONTHLY_LIMIT.search(t) and MONTHLY_LIMIT_HINT.search(t))

def is_high_priority(brief):
    return str((brief or {}).get("priority") or "").strip().lower() == "high"

def is_go_model(cfg, name, brief=None):
    try:
        return model_spec(cfg, name, brief).get("key") == "go"
    except ValueError:
        return False

def is_zoo_model(cfg, name, brief=None):
    """True when the attempt's model is served through the Zoo gateway key."""
    try:
        return model_spec(cfg, name, brief).get("key") == "zoo"
    except ValueError:
        return False

def retry_after_s(text, cap=ZOO_RETRY_AFTER_MAX):
    """Seconds the provider asked us to wait (`Retry-After: 6`), or None.

    Only the delta-seconds form is honoured; an HTTP-date is not guessed at (it would depend on the
    provider's clock), and an absent or non-numeric hint falls back to the configured backoff schedule.
    """
    m = RETRY_AFTER.search(text or "")
    if not m:
        return None
    try:
        n = int(m.group(1))
    except (TypeError, ValueError):
        return None
    return max(0, min(int(cap), n))

PREFERRED_MODEL = "zoo:glm-5.3-flash"  # free Zoo pass; DeepSeek official flash is its fallback
# Opt-in since 2026-11-19: Zoo's free pass refuses programmatic (non-IDE) use and ends ~Nov 29, so a
# host only prefers it when config.json sets "prefer_zoo": true.
PREFER_ZOO_DEFAULT = False


def project_model(cfg, brief):
    """The model override one project entry carries (`projects.<name>.model` in config.json), or None.

    Owner decision (2026-11-15): a brief that names no model and is not priority high resolves to
    its project's override before the host default. The override uses the same allow-listed names
    (and tiers) as a named model, so an unknown one fails dispatch the same way a named one would.
    """
    projects = cfg.get("projects") or {}
    if not isinstance(projects, dict):
        return None
    entry = projects.get(str((brief or {}).get("project") or ""))
    if not isinstance(entry, dict):
        return None
    m = entry.get("model")
    if isinstance(m, str) and m.strip():
        return m.strip()
    return None


def default_model(cfg):
    """Model for a bee brief that names none.

    The host's configured `model`, unless config.json opts in with `"prefer_zoo": true` and the host
    can authenticate to Zoo, in which case Zoo's free GLM 5.3 Flash pass is used. The preference is
    off by default: the free pass refuses programmatic (non-IDE) use and ends ~Nov 29. A named model,
    an explicit tier and a `priority: high` brief are never affected.
    """
    if cfg.get("prefer_zoo", PREFER_ZOO_DEFAULT):
        try:
            if provider_key(cfg, "zoo"):
                return PREFERRED_MODEL
        except KeyError:
            # A cfg without `data_dir` (partial cfgs in unit tests) cannot resolve a key file; the
            # env var was already checked, so treat it as "no zoo key" rather than raising here.
            pass
    return cfg["model"]


def model_spec(cfg, name, brief=None):
    """Resolve an allow-listed model name (or tier) to its run spec.

    0.1.17 routing, owner precedence (2026-11-15): an explicitly named model or tier is
    authoritative - priority: high routes only UNNAMED briefs to DeepSeek official. A brief that
    names no model gets its project's model override when the project sets one, else
    `default_model(cfg)`: Zoo's free GLM 5.3 Flash pass while this host can authenticate to it,
    else the host default. Named models: no prefix or `go:` -> OpenCode Go (including bare
    deepseek-v4.1-flash); `deepseek:<model>` -> DeepSeek official. Free-pool ids are unchanged.
    """
    # Owner precedence (2026-11-15): an explicitly named model or tier is authoritative -
    # priority: high routes only UNNAMED briefs to DeepSeek official. Whatever the brief names
    # is what runs; high is a default-level signal, not an override of an explicit choice.
    named = bool(name)
    if not name:
        # Unnamed and not high: the brief's project model override wins before the host default
        # (priority high already beats the project override via the `high` branch below).
        if not is_high_priority(brief):
            name = project_model(cfg, brief) or ""
    name = TIERS.get(name, name) if name else default_model(cfg)
    if name == "pool:free": return {"name": "pool:free", "pool": "free"}
    high = is_high_priority(brief) and not named
    prefix, _, rest = name.partition(":")
    if prefix in ("deepseek", "go") and rest:
        inner = rest
    else:
        prefix, inner = "", name
    # Free / non-DeepSeek allow-list entries keep their existing specs unless explicitly prefixed.
    if not high and prefix != "deepseek" and name in MODELS and MODELS[name].get("free"):
        spec = {**MODELS[name], **cfg.get("models", {}).get(name, {}), "name": name}
        return spec
    if high or prefix == "deepseek":
        if inner in ("deepseek-v4.1-flash", "deepseek-flash", ""):
            official = "deepseek:deepseek-flash"
        elif f"deepseek:{inner}" in MODELS:
            official = f"deepseek:{inner}"
        elif inner in MODELS and MODELS[inner].get("key") == "deepseek":
            official = inner
        else:
            official = "deepseek:deepseek-flash"
        base = {**_DEEPSEEK_FLASH, **MODELS.get(official, {}), **cfg.get("models", {}).get(official, {})}
        return {**base, "name": official if high or prefix == "deepseek" else name, "key": "deepseek",
                "model": "deepseek-flash", "base_url": DEEPSEEK_BASE_URL, "wire": "openai-chat"}
    if name not in MODELS and name not in cfg.get("models", {}):
        # go:deepseek-v4.1-flash already in MODELS; other go: aliases of flash
        if prefix == "go" and inner in ("deepseek-v4.1-flash", "deepseek-flash"):
            return {**_GO_FLASH, "name": name}
        raise ValueError(f"model not allowed on this PC: {name}")
    spec = {**MODELS.get(name, {}), **cfg.get("models", {}).get(name, {}), "name": name}
    if name == cfg["model"] and "base_url" not in cfg.get("models", {}).get(name, {}) and spec.get("key") == "go":
        # Only apply cfg.base_url when it is the Go endpoint; never override a DeepSeek official spec.
        if cfg.get("base_url") in (None, GO_BASE_URL) or (cfg.get("base_url") or "").startswith("https://opencode.ai/"):
            spec.update(base_url=cfg["base_url"], wire=cfg.get("wire") or "openai-chat")
    return spec

def route_preview(cfg, model=None, brief=None, kind=None):
    """What a dispatch of this request would resolve to, without starting one.

    Mirrors submit(): the request model field, then the brief's own model, then its tier, then -
    when the brief names none and priority is not high - the brief's project's model override,
    then default_model(cfg). The reply names each provider's credential source (zoo, deepseek, go
    or none) and never carries a credential value or a provider endpoint, so the payload is safe
    to log and to hand back to a coordinator.

    Only kind=opencode is previewable: submit() resolves agy-cua through AGY_TIERS/AGY_MODELS and
    runs check briefs without a model, so any answer for those kinds would describe a route the
    dispatch never takes (found by the independent review of the first version of this endpoint).
    The kind is read the way dispatch reads it: the request's top-level field first, then the
    brief's own, then the opencode default. The preview covers model resolution only - it does not
    run the brief validation dispatch applies (security profile, check commands, project
    authorization), so a 200 means "this is the model a dispatch would pick", not "dispatch would
    accept this brief". A top-level `project` on the request stands in for the brief's own project
    when the brief names none, so a coordinator can preflight a project's default; a dispatch of
    the same brief reads the project the same way.
    """
    brief = brief if isinstance(brief, dict) else {}
    kind = kind or brief.get("kind") or "opencode"
    if kind != "opencode":
        raise ValueError(
            "route preview supports only kind=opencode; dispatch would resolve kind="
            + str(kind) + " outside the model resolver"
        )
    requested = model or brief.get("model") or brief.get("tier") or None
    spec = model_spec(cfg, requested, brief)  # ValueError -> the handler answers 400
    src = spec.get("key") or "none"
    eff = dict([("name", spec.get("name")), ("model", spec.get("model")), ("key", src)])
    creds = {}
    for source in ("zoo", "deepseek", "go"):
        try: creds[source] = bool(provider_key(cfg, source))
        except KeyError: creds[source] = False  # a partial cfg (unit tests) has no data_dir to read from
    pm = project_model(cfg, brief)
    if spec.get("pool"):
        why = "the free pool is used, so the launcher picks one free model per attempt"
    elif requested and requested in TIERS:
        why = "tier " + requested + " maps to " + TIERS[requested]
    elif requested:
        why = "a model was named, so its allow-listed spec is used"
    elif is_high_priority(brief):
        why = "no model was named and brief priority is high, so DeepSeek official flash is used"
    elif pm:
        why = ("no model was named and project " + str(brief.get("project") or "")
               + " sets model " + pm + ", so the project default is used")
    elif creds["zoo"] and cfg.get("prefer_zoo", PREFER_ZOO_DEFAULT):
        why = "no model was named and this host can authenticate to Zoo, so the preferred free pass is used"
    elif not cfg.get("prefer_zoo", PREFER_ZOO_DEFAULT):
        why = "no model was named and the Zoo preference is off on this host (the default), so the host default is used"
    else:
        why = "no model was named and there is no Zoo credential, so the host default is used"
    out = {}
    out["requested"] = requested
    out["effective"] = eff
    out["why"] = why
    out["fallback"] = spec.get("fallback")
    out["credentials"] = creds
    out["configured_model"] = cfg["model"]
    out["effective_model"] = spec.get("model")
    return out

def export_project(src, sync_branch=None, source_rev=None, branch=None):
    """Tree of an authorized repo as (rev, tar bytes), byte-exact as committed (LF).

    If source_rev is set, archive that exact local commit. Fetch from origin only
    when the rev is not already present and an origin remote exists. A brief's
    work branch is best-effort and must not fail the export if it is not on the
    remote yet. Otherwise keep the existing sync_branch / HEAD behaviour. The
    local checkout is never reset.

    core.autocrlf is forced off: on Windows clones it is usually true, which makes
    `git archive` emit CRLF, so worker patches and sha256s stop matching the repo."""
    import subprocess
    git = ["git", "-C", str(src), "-c", "core.autocrlf=false", "-c", "core.eol=lf"]

    def _has_origin():
        r = subprocess.run(git + ["remote"], capture_output=True, text=True)
        return "origin" in {x.strip() for x in (r.stdout or "").splitlines() if x.strip()}

    def _is_local_commit(rev):
        has = subprocess.run(git + ["cat-file", "-t", rev], capture_output=True, text=True)
        return has.returncode == 0 and (has.stdout or "").strip() in ("commit", "tag")

    def _fetch_branch(name, kind, required=False):
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", name) or ".." in name:
            raise ValueError(f"bad {kind}")
        dest = f"refs/remotes/origin/{name}"
        if not _has_origin():
            return dest  # no origin: skip fetch
        r = subprocess.run(git + ["fetch", "-q", "origin", f"+refs/heads/{name}:{dest}"], capture_output=True, timeout=120)
        if r.returncode != 0 and required:
            err = (r.stderr or b"").decode("utf-8", "replace")[:300]
            raise RuntimeError(f"fetch {kind} {name} failed: {err}")
        return dest

    if source_rev:
        source_rev = str(source_rev).strip()
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}|[0-9a-fA-F]{64}", source_rev):
            raise ValueError("bad source_rev")
        # 0.1.14: a pinned local commit is enough. Do not fetch a not-yet-pushed work branch.
        if not _is_local_commit(source_rev):
            if _has_origin():
                if branch:
                    _fetch_branch(str(branch).strip(), "branch", required=False)
                fetched = subprocess.run(git + ["fetch", "-q", "origin", source_rev], capture_output=True, timeout=120)
                if not _is_local_commit(source_rev):
                    err = (fetched.stderr or b"").decode("utf-8", "replace")[:300]
                    raise RuntimeError(f"source_rev {source_rev} not available after fetch: {err}")
            else:
                raise RuntimeError(f"source_rev {source_rev} is not a local commit and this repo has no origin remote")
        rev = subprocess.run(git + ["rev-parse", "--verify", source_rev + "^{commit}"], capture_output=True, text=True, check=True).stdout.strip()
        tar = subprocess.run(git + ["archive", "--format=tar", rev], capture_output=True, check=True).stdout
        return rev, tar

    ref = "HEAD"
    if sync_branch:
        # Track a remote branch: fetch it fresh before every run and export that ref.
        # The local checkout is never reset, so nothing on the PC working tree changes.
        ref = _fetch_branch(sync_branch, "sync_branch", required=True)
    rev = subprocess.run(git + ["rev-parse", ref], capture_output=True, text=True, check=True).stdout.strip()
    tar = subprocess.run(git + ["archive", "--format=tar", rev], capture_output=True, check=True).stdout
    return rev, tar

async def _lag_probe(L, every=5.0):
    """Event-loop lag (how late a 5 s sleep wakes up), reported by /v1/health."""
    while True:
        t = time.monotonic(); await asyncio.sleep(every)
        L.loop_lag_ms = max(0.0, (time.monotonic() - t - every) * 1000)

def write_pidfile(cfg):
    """Record the real python pid (uv is only a wrapper) so stop.ps1 can kill it."""
    pf = Path(cfg["data_dir"]) / "launcher.py.pid"
    pf.write_text(str(os.getpid()), "utf-8")
    return pf

def serve(cfg, backend_name="cua"):
    check_bind(cfg["bind"])
    tok_file = Path(cfg.get("api_token_file") or Path(cfg["data_dir"]) / "orbit-api.token")
    token = tok_file.read_text("utf-8").strip()
    if len(token) < 32: raise SystemExit("api token missing/too short; run scripts/new-token.ps1")
    backend = FakeBackend(cfg) if backend_name == "fake" else CuaBackend(cfg)
    L = Launcher(cfg, backend)
    handler = make_handler(L, token)  # validate read-vs-control scopes before scheduling attempts
    L.provisioner = build_provisioner(cfg)
    write_pidfile(cfg)
    threading.Thread(target=L.loop.run_forever, daemon=True).start(); L.kick()
    asyncio.run_coroutine_threadsafe(_lag_probe(L), L.loop)
    srv = ThreadingHTTPServer((cfg["bind"], cfg["port"]), handler)
    for extra in cfg.get("extra_binds") or []:  # 0.1.11: optional extra listener, loopback only (local coordinator)
        if extra == cfg["bind"]: continue  # already the primary listener
        if not ipaddress.ip_address(extra).is_loopback: raise SystemExit(f"extra_binds allows loopback only, got {extra}")
        xs = ThreadingHTTPServer((extra, cfg["port"]), handler)
        threading.Thread(target=xs.serve_forever, daemon=True).start()
    print(f"orbit-launcher {VERSION} on http://{cfg['bind']}:{cfg['port']} max_concurrent={cfg['max_concurrent']} model={cfg['model']}", flush=True)
    srv.serve_forever()

def doctor(cfg):
    ok = True
    def line(name, good, info=""):
        nonlocal ok; ok &= bool(good); print(f"[{'ok' if good else 'FAIL'}] {name} {info}")
    try: check_bind(cfg["bind"]); line("bind", True, cfg["bind"])
    except SystemExit as e: line("bind", False, str(e))
    line("deepseek key present (value not shown)", bool(provider_key(cfg, "deepseek")))
    go = bool(provider_key(cfg, "go"))
    line("go key present (value not shown)", True, "yes" if go else "optional, not set")
    zoo = bool(provider_key(cfg, "zoo"))
    line("zoo key present (value not shown)", True, "yes" if zoo else "optional, not set")
    tf = Path(cfg.get("api_token_file") or Path(cfg["data_dir"]) / "orbit-api.token"); line("orbit api token file", tf.exists(), str(tf))
    try:
        import cua; line("cua sdk", True, cua.cua_sdk_version())
        probs = sdk_contract_check(); line("cua sdk contract", not probs, "; ".join(probs))
    except Exception as e: line("cua sdk", False, type(e).__name__)
    for name, good, info in agy_checks(cfg): line(name, good, info)
    spec = model_spec(cfg, cfg["model"])
    line("max_concurrent", True, str(cfg["max_concurrent"]))
    line("model", True, f"{spec['name']} -> {spec['model']} key={spec.get('key')}")
    return 0 if ok else 1

def agy_checks(cfg, home=None):
    """agy-cua readiness (read-only): agy binary, orbit-cua MCP entry, allow rule, and nothing broader allowed."""
    home = Path(home or Path.home()); out = [("agy exe", Path(cfg["agy_exe"]).exists(), cfg["agy_exe"])]
    try: mcp = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text("utf-8-sig")).get("mcpServers", {})
    except Exception: mcp = {}
    ent = mcp.get(AGY_MCP_NAME) or {}
    out.append(("agy mcp entry orbit-cua", "agy_cua_mcp.py" in " ".join(map(str, ent.get("args", []))), ""))
    try: allow = json.loads((home / ".gemini" / "antigravity-cli" / "settings.json").read_text("utf-8-sig")).get("permissions", {}).get("allow", [])
    except Exception: allow = []
    out.append(("agy allow rule " + AGY_ALLOW_RULE, AGY_ALLOW_RULE in allow, ""))
    out.append(("agy allow list has nothing else", set(allow) <= {AGY_ALLOW_RULE}, ", ".join(x for x in allow if x != AGY_ALLOW_RULE)))
    return out

async def agy_smoke(cfg, objective):
    """Manual end-to-end check of the agy-cua path OUTSIDE the running launcher: own data_dir, own Cua state dir,
    one throwaway sandbox, one agy print run, then the sandbox is deleted. Prints no secrets."""
    be = CuaBackend(cfg); aid = "smoke" + uuid.uuid4().hex[:12]
    tier = os.environ.get("ORBIT_AGY_SMOKE_TIER", AGY_DEFAULT_TIER)
    proj = os.environ.get("ORBIT_AGY_SMOKE_PROJECT")
    a = {"attempt_id": aid, "kind": "agy-cua", "tier": tier, "model": AGY_TIERS[tier], "mode": "code" if proj else "ui",
         "brief": {"objective": objective, "budget": {"timeout_s": 600}, **({"project": proj} if proj else {})}}
    try:
        ids = await be.start(a, ""); a.update(ids); print("sandbox", ids["sandbox"], "run", ids["run_id"], flush=True)
        cur = 0
        while True:
            evs, cur = await be.events(a, cur)
            for e in evs: print("event", e["kind"], (e.get("text") or e.get("line") or "")[:2000], flush=True)
            if any(e["kind"] in ("turn_ended", "exited") for e in evs): break
            await asyncio.sleep(2)
        if a.get("workdir"):
            pt = await be.patch(a); print("patch files", json.dumps(pt["files"])); print("patch diff\n" + pt["diff"][:3000])
        res = await be.result(a); print("result", json.dumps({k: res[k] for k in ("status", "stop_reason", "error", "tool_calls", "usage")}, indent=1))
        return 0 if res["status"] == "completed" else 1
    finally:
        await be.cleanup(a); print("cleanup: deleted", a.get("sandbox"))

async def _probe_all(cfg):
    """Probe every allow-listed free model, each in its own sandbox, in parallel."""
    rcs = await asyncio.gather(*[_probe(cfg, m) for m in FREE_POOL], return_exceptions=True)
    bad = [m for m, rc in zip(FREE_POOL, rcs) if rc != 0]
    print(f"[{'ok' if not bad else 'FAIL'}] free pool: {len(FREE_POOL) - len(bad)}/{len(FREE_POOL)} answered" + (f"; failed: {', '.join(bad)}" if bad else ""))
    return 1 if bad else 0

async def _probe(cfg, model=None):
    """Read-only provider check INSIDE a real worker sandbox: same image, harness, endpoint and model as tasks."""
    spec0 = model_spec(cfg, model)
    src = spec0.get("key") or "none"
    key = "" if src == "none" else provider_key(cfg, src)
    if src != "none" and not key:
        print(f"[FAIL] {src} key not configured"); return 1
    be = CuaBackend(cfg)
    a = {"attempt_id": "probe-" + uuid.uuid4().hex[:8], "model": spec0["name"],
         "brief": {"objective": "Reply with exactly the text ORBIT-PROBE-OK and nothing else. Do not use any tools."}}
    try:
        ids = await be.start(a, key); del key; a.update(ids); cur, t0 = 0, time.time()
        while time.time() - t0 < 300:
            evs, cur = await be.events(a, cur)
            for e in evs:
                if e["kind"] == "error": print("[FAIL] provider error:", e["line"][:300]); return 1
                if e["kind"] in ("turn_ended", "exited"):
                    res = await be.result(a); ok = "ORBIT-PROBE-OK" in (res.get("text") or "")
                    spec = model_spec(cfg, a["model"]); print(f"[{'ok' if ok else 'FAIL'}] model={spec['model']} key={spec.get('key')} stop={res.get('stop_reason')} usage={res.get('usage')}")
                    return 0 if ok else 1
            await asyncio.sleep(1)
        print("[FAIL] no reply within 300s"); return 1
    finally:
        await be.cleanup(a)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["serve", "doctor", "probe", "sdkcheck", "agysmoke"]); ap.add_argument("--objective", default="Take a screenshot and describe the desktop."); ap.add_argument("--config")
    _args_for_cfg = None
    ap.add_argument("--backend", default="cua", choices=["cua", "fake"]); ap.add_argument("--model")
    a = ap.parse_args(); cfg = load_config(a.config) if a.config else None
    if cfg is not None:
        cfg["_config_path"] = str(a.config)
    if a.cmd == "agysmoke": sys.exit(asyncio.run(agy_smoke(cfg, a.objective)))
    sys.exit((print(json.dumps(sdk_contract_check())) or (1 if sdk_contract_check() else 0)) if a.cmd == "sdkcheck" else doctor(cfg) if a.cmd == "doctor" else asyncio.run(_probe_all(cfg) if a.model == "free-all" else _probe(cfg, a.model)) if a.cmd == "probe" else serve(cfg, a.backend))
