#!/usr/bin/env python3
"""hyvv worker-host runtime (runs on a worker PC or SSH host, never on the control surface).

Owns every Cua call, holds the provider key and never returns it, enforces one
execution controller per attempt, journals attempts locally, and exposes a
narrow token-scoped HTTP API on loopback or a Tailscale address only.

  uv run --with cua==0.5.0 launcher.py serve  --config config.json
  uv run --with cua==0.5.0 launcher.py doctor --config config.json
"""
from __future__ import annotations
import argparse, asyncio, bisect, platform, hashlib, hmac, ipaddress, json, os, re, sys, threading, time, uuid

try:
    import provisioning as _provisioning
except ImportError:  # pragma: no cover - the module ships beside launcher.py
    _provisioning = None
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "0.1.16"
HEARTBEAT_S = 60  # controller emits a heartbeat event after this many quiet seconds
MAX_WORKERS = 10  # hard ceiling for max_concurrent
TERMINAL = {"succeeded", "failed", "cancelled"}
GO_BASE_URL = "https://opencode.ai/inference/go/openai/v1"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"  # official OpenAI-compatible endpoint
# Workers must not fan out: deny the OpenCode `task` (subagent) tool.
WORKER_OPENCODE_CONFIG = {"$schema": "https://opencode.ai/config.json", "permission": {"task": "deny"}}
# Allow-listed per-run models.
# "key": "deepseek" sends DEEPSEEK_API_KEY; "go" sends the OpenCode Go key; "none" sends no key.
# The free model only works from inside OpenCode, so it uses OpenCode's built-in provider
# (opencode/<id>, no base_url) rather than an OpenAI-compatible endpoint.
def _free(model_id, ctx_tokens):
    # Prompt cap: leave ~80k tokens for OpenCode's own system prompt/tools (a probe used ~43k) and output;
    # ~3 chars per token; never above 300k chars (~100k tokens) for the free tier.
    # fallback aliases to DeepSeek official deepseek-flash (see MODELS).
    return {"model": f"opencode/{model_id}", "base_url": None, "wire": None, "key": "none", "free": True,
            "fallback": "deepseek:deepseek-flash", "context_tokens": ctx_tokens,
            "max_prompt_chars": max(60_000, min(300_000, (ctx_tokens - 80_000) * 3)), "backoff_s": [20, 60]}

def _go_free(model_id, ctx_tokens):
    # OpenCode Go also serves some *-free ids (docs: opencode.ai/docs/go, Oct 21 2026). Route them through Go's
    # OpenAI-compatible endpoint with the Go key, instead of Zen's built-in provider.
    return {**_free(model_id, ctx_tokens), "model": model_id, "base_url": GO_BASE_URL, "wire": "openai-chat", "key": "go", "via": "go"}

# Routing (0.1.15): a model with no prefix, or a `go:` prefix (including bare
# `deepseek-v4.1-flash`), goes to OpenCode Go. `deepseek:<model>` (e.g.
# `deepseek:deepseek-flash`), or a brief with priority: high, goes to DeepSeek official.
# A Go monthly-limit 429 retries once on DeepSeek direct and records fallback="deepseek".
# Free models only work from inside OpenCode, so they use OpenCode's built-in provider (opencode/<id>, no base_url).
# Every free id below answered `opencode run -m opencode/<id>` on Oct 20, 2026 (jev-1.13-free and space-bunny-free were "Model unavailable").
# Context sizes from models.dev (input limit where it is lower).
_DEEPSEEK_FLASH = {"model": "deepseek-flash", "base_url": DEEPSEEK_BASE_URL, "wire": "openai-chat", "key": "deepseek"}
_GO_FLASH = {"model": "deepseek-v4.1-flash", "base_url": GO_BASE_URL, "wire": "openai-chat", "key": "go"}
MODELS = {
    "deepseek-flash": dict(_GO_FLASH),  # unprefixed -> Go (0.1.15)
    "deepseek-v4.1-flash": dict(_GO_FLASH),  # unprefixed / default briefs -> Go
    "go:deepseek-v4.1-flash": dict(_GO_FLASH),
    "deepseek:deepseek-flash": dict(_DEEPSEEK_FLASH),
    "deepseek:deepseek-v4.1-flash": dict(_DEEPSEEK_FLASH),
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
TIERS = {"simple": "pool:free", "standard": "deepseek-v4.1-flash"}
FREE_TIER_DENIED = re.compile(r"FreeTierError|free tier can only be used", re.I)
RATE_LIMIT = re.compile(r"\b429\b|rate.?limit|too many requests|quota", re.I)
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
AGY_TIMEOUT_S = (60, 600, 3600)  # min, default, max for brief.budget.timeout_s
AGY_MODES = ("ui", "code")  # code: repo seeded into the attempt's sandbox; agy edits/runs ONLY there via orbit-cua shell+file
AGY_CODE_PERMS = "computer:shell,computer:file,computer:screenshot,computer:click,computer:type,computer:key,computer:scroll"
# ---- sandbox naming: <model abbr>-<slug>-<short attempt id>, e.g. ds41-fix-login-bug-89abcdef01
MODEL_ABBR = {
    "deepseek-flash": "dsflash", "deepseek-v4.1-flash": "ds41", "go:deepseek-v4.1-flash": "ds41go",
    "deepseek:deepseek-flash": "dsflash", "deepseek:deepseek-v4.1-flash": "dsflash",
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

def redact(s):
    return SECRETISH.sub("<redacted>", s) if isinstance(s, str) else s

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
        t = int(((b.get("budget") or {}).get("timeout_s")) or CHECK_TIMEOUT_S[1]); t = max(CHECK_TIMEOUT_S[0], min(CHECK_TIMEOUT_S[2], t))
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

_KEY_ENV = {"go": "ORBIT_WORKER_GO_KEY", "deepseek": "DEEPSEEK_API_KEY"}
_KEY_FILE_CFG = {"go": "go_key_file", "deepseek": "deepseek_key_file"}
_KEY_FILE_NAME = {"go": "go.key", "deepseek": "deepseek.key"}

def provider_key(cfg, source):
    """Provider key for a named source (`go` or `deepseek`): env first, else a user-only file. Never logged."""
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

    async def _seed_project(self, sb, proj, name, source_rev=None, branch=None):
        """Copy an authorized local git repo into the sandbox; returns (workdir, base_rev, seed_commit).

        seed_commit is the in-sandbox commit the copy starts from (also kept as refs/hyvv/seed); the patch
        collector diffs against it, so a bee that commits or moves HEAD does not empty the patch.

        Prefer brief source_rev (optionally after fetching branch); otherwise sync_branch / HEAD.
        """
        import base64, subprocess
        src = proj["path"]
        rev, tar = export_project(src, sync_branch=proj.get("sync_branch"), source_rev=source_rev, branch=branch)
        home = (await self._sh(sb, "printf %s \"$HOME\""))[0].strip() or "/root"
        wd = f"{home}/work/{name}"
        await self._sh(sb, f"rm -rf '{wd}' /tmp/orbit-src.tar && mkdir -p '{wd}'")
        await (await self._sd(sb)).upload("/tmp/orbit-src.tar", tar, None)
        o, e, c = await self._sh(sb, f"cd '{wd}' && tar -xf /tmp/orbit-src.tar && git init -q && "
                                     f"printf '__pycache__/\\n*.pyc\\n.pytest_cache/\\nnode_modules/\\n' >> .git/info/exclude && git add -A && "
                                     f"git -c user.name=orbit -c user.email=orbit@local commit -qm 'base {rev}' && git update-ref refs/hyvv/seed HEAD && git rev-parse HEAD")
        if c not in (0, None): raise RuntimeError(f"seeding project failed: {e[-300:]}")
        seed = (o.strip().splitlines() or [""])[-1].strip()
        return wd, rev, (seed if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", seed) else None)

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
        if not a.get("workdir"): return None
        sb = await self.agy.sandbox(a) if a.get("kind") == "agy-cua" else await self._sandbox(a["sandbox"])
        wd = a["workdir"]
        # Diff against the seed commit recorded at start, not HEAD: a bee that commits (or `git checkout -B`)
        # moves HEAD, and a HEAD diff would then come back empty. Older attempts without a seed use HEAD.
        seed = a.get("seed_commit")
        base = seed if isinstance(seed, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", seed) else "HEAD"
        await self._sh(sb, f"cd '{wd}' && git add -A")
        diff = (await self._sh(sb, f"cd '{wd}' && git diff --cached --binary {base}"))[0]
        names = (await self._sh(sb, f"cd '{wd}' && git diff --cached --name-status --no-renames {base}"))[0]
        files = []
        for l in names.splitlines():
            st, _, path = l.partition("\t")
            if not path: continue
            sha = None if st.startswith("D") else ((await self._sh(sb, f"cd '{wd}' && sha256sum -- '{path}' | cut -d' ' -f1"))[0].strip()[:64] or None)
            files.append({"path": path, "status": st, "sha256": sha, "change": "deleted" if st.startswith("D") else ("added" if st.startswith("A") else "changed"),
                          "in_assigned_scope": in_assigned_scope(path, a["brief"].get("assigned_files"))})
        return {"diff": diff, "files": files, "base": base}

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
        name = attempt.get("sandbox") or sandbox_name(attempt)
        attempt["sandbox"] = name  # recorded before create so a failed start still cleans up the right sandbox
        sb = await self.client.sandboxes().create(cua.SandboxCreateOptions(
            on="local", kind="container", runtime=cfg["runtime"], image=cfg["image"], name=name,
            memory_mb=cfg["memory_mb"], cpus=cfg.get("cpus", 2), wait_for=[cua.ReadinessProbe(service="env")]))
        self._sb[name] = sb
        proj = cfg["projects"].get(b.get("project", ""), {})
        wd, base, seed = (None, None, None)
        if proj.get("path"):
            wd, base, seed = await self._seed_project(sb, proj, b["project"], source_rev=b.get("source_rev"), branch=b.get("branch"))
        spec = model_spec(cfg, attempt.get("model"), attempt.get("brief"))
        env = {"OPENCODE_CONFIG_CONTENT": json.dumps(dict(WORKER_OPENCODE_CONFIG, **({"model": spec["model"]} if spec["key"] == "none" else {})))}
        if spec["key"] in ("go", "deepseek"): env["OPENAI_API_KEY"] = key  # OpenAI-compatible: DeepSeek official or OpenCode Go
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
        return [{"seq": e.seq, "kind": e.kind, "line": redact(e.line or ""), "stop_reason": e.stop_reason,
                 "text": redact(e.text) if e.kind in ("message", "error", "turn_ended") else None} for e in page.events], page.cursor

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
        elif f.get("rate_limit_first", 0) > a.get("rate_limited", 0) or a.get("model") in f.get("rate_limit_models", []):
            evs = [{"kind": "error", "text": "HTTP 429 Too Many Requests"}]
        if a.get("model") in f.get("deny_models", []):
            evs = [{"kind": "error", "text": "FreeTierError: OpenCode's free tier can only be used from within OpenCode."}]
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

def render_prompt(b):
    parts = [f"# Objective\n{b['objective']}"]
    for k, title in [("deliverables", "Deliverables"), ("acceptance", "Acceptance criteria"), ("assigned_files", "Files you may change"),
                     ("commands", "Tools/commands you may run"), ("deps", "Dependencies/context")]:
        if b.get(k): parts.append(f"# {title}\n" + "\n".join(f"- {x}" for x in b[k]))
    parts.append("# Rules\n- Work only inside the working directory.\n- Do not start subagents.\n- Do not print secrets or environment variables.")
    parts.append("# Return format\nEnd with a JSON block: {\"summary\":..., \"changed_files\":[...], \"checks\":[{\"name\":...,\"command\":...,\"passed\":bool}], \"open_issues\":[...]}")
    return "\n\n".join(parts)

def normalize_brief(b):
    """Accept both brief shapes: v0.1.1 (acceptance is a string list) and bundlebees main at c10aa42
    (acceptance may be {criteria, commands, tests}). Flatten a dict to a list of strings. Unknown
    extra fields are left untouched."""
    acc = b.get("acceptance") if isinstance(b, dict) else None
    if isinstance(acc, dict):
        b = dict(b)
        crit = [x for x in acc.get("criteria") or [] if isinstance(x, str)]
        b["acceptance"] = crit + [f"Run: {c}" for c in acc.get("commands") or [] if isinstance(c, str)] + \
                          [f"Test passes: {t}" for t in acc.get("tests") or [] if isinstance(t, str)]
    return b

def validate_brief(b):
    """v0.1.1 requires only objective; main (c10aa42) also sends source_rev, assigned_files and
    acceptance. Unknown extra fields (contract, evidence, etc.) are ignored, not rejected."""
    if not isinstance(b, dict) or not str(b.get("objective", "")).strip(): return "brief.objective is required"
    for k in ("deliverables", "acceptance", "assigned_files", "commands", "deps"):
        if k in b and not (isinstance(b[k], list) and all(isinstance(x, str) for x in b[k])): return f"brief.{k} must be a list of strings"
    return None

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
class Launcher:
    def __init__(self, cfg, backend):
        self.cfg, self.backend = cfg, backend
        self.model_health = {m: {"last_limited": 0.0, "last_used": 0.0} for m in FREE_POOL}
        self.dir = Path(cfg["data_dir"]); (self.dir / "attempts").mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.attempts = {}
        self.controllers = {}  # attempt_id -> asyncio task (exactly one per attempt)
        self.loop = asyncio.new_event_loop()
        self._ev_lock = threading.Lock(); self._ev_idx = {}  # events_since offset index
        self.started_at = time.time(); self.loop_lag_ms = 0.0
        self._load()

    # journal: one JSON per attempt (atomic replace) + append-only events log
    def _path(self, aid): return self.dir / "attempts" / f"{aid}.json"
    def _save(self, a):
        p = self._path(a["attempt_id"]); tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(a, indent=1), "utf-8"); os.replace(tmp, p)
    def _append_events(self, a, evs):
        with open(self.dir / "attempts" / f"{a['attempt_id']}.events.jsonl", "a", encoding="utf-8") as f:
            for e in evs: f.write(json.dumps(e) + "\n")
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
        if d["state"] == "queued": d["queue_position"] = self._queue().index(a["attempt_id"]) + 1 if a["attempt_id"] in self._queue() else None
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

    def submit(self, aid, brief, model=None, kind=None, mode=None):
        with self.lock:
            if aid in self.attempts: return self.attempts[aid], False  # idempotent: never a second controller
            kind = kind or brief.get("kind") or "opencode"
            if kind not in KINDS: raise ValueError(f"unknown kind: {kind} (allowed: {', '.join(KINDS)})")
            prof = resolve_security_profile(self.cfg, kind, brief)  # ValueError -> 400; coding bees: "default" only
            if kind == "check":
                if not brief.get("commands"): raise ValueError("check jobs need brief.commands (a non-empty list of strings)")
                if brief.get("verify_of") is not None: raise ValueError("verify_of applies to opencode attempts only")
                now = time.time()
                a = {"attempt_id": aid, "kind": kind, "brief": brief, "state": "queued", "detail": "waiting for a worker slot", "created_at": now,
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
            a = {"attempt_id": aid, "kind": kind, "brief": brief, "state": "queued", "detail": "waiting for a worker slot", "created_at": now,
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
        with self.lock:
            a = self.attempts.get(aid)
            if not a: return None
            if a["state"] in TERMINAL: return a
            if a["state"] in ("queued", "backoff"): self._set(a, "cancelled", "cancelled before start")
            else: a["cancel"] = True; self._set(a, "cancel_requested", "interrupt sent")
        self.kick(); return a

    def _set(self, a, state, detail, ev=None):
        a["state"], a["detail"], a["updated_at"] = state, detail, time.time()
        a["lseq"] += 1
        e = {"lseq": a["lseq"], "ts": a["updated_at"], "kind": "state", "state": state, "detail": detail}
        if ev: e.update(ev)
        self._append_events(a, [e]); self._save(a)

    def _active(self): return [x for x in self.attempts.values() if x["state"] in ("starting", "running", "cancel_requested")]

    def kick(self): asyncio.run_coroutine_threadsafe(self._schedule(), self.loop)

    async def _schedule(self):
        with self.lock:
            # resume controllers for attempts that were live before a restart (reattach, no re-dispatch)
            for a in self._active():
                if a["attempt_id"] not in self.controllers and a.get("run_id"):
                    self.controllers[a["attempt_id"]] = asyncio.ensure_future(self._control(a))
                elif a["attempt_id"] not in self.controllers and a["state"] == "starting":
                    self._set(a, "failed", "launcher restarted during start; no run id recorded (retry from Orbit)")
            free = self.cfg["max_concurrent"] - len(self._active())
            for aid in self._queue():
                if free <= 0: break
                a = self.attempts[aid]
                if a["state"] == "backoff" and time.time() < a.get("retry_at", 0): continue
                free -= 1; self._set(a, "starting", "creating sandbox")
                self.controllers[aid] = asyncio.ensure_future(self._control(a))

    async def _control(self, a):
        aid = a["attempt_id"]
        try:
            if a.get("kind") == "check": return await self._control_check(a)
            if not a.get("run_id"):
                agy = a.get("kind") == "agy-cua"
                if a.get("pool") and not a.get("model"):
                    a["model"] = self._pick_free(a) or MODELS["deepseek-v4.1-flash-free"]["fallback"]; a["model_history"].append({"model": a["model"], "at": time.time()}); self._save(a)
                # Pick the key after the model is known so free-pool / fallback / aliases use the right source.
                spec = model_spec(self.cfg, a.get("model"), a.get("brief"))
                src = spec.get("key") or "none"
                key = "" if agy or src == "none" else provider_key(self.cfg, src)  # agy uses the host's own agy sign-in; no key passed
                if not agy and src != "none" and not key:
                    self._set(a, "failed", "provider key not configured on this PC"); return
                ids = await self.backend.start(a, key); del key
                a.update(ids); self._set(a, "running", ("agy" if agy else "opencode") + " run started", {"run_id": ids["run_id"]})
            last_hb = last_ev = time.time()
            while True:
                if a.get("cancel") and not a.get("interrupted"):
                    await self.backend.interrupt(a); a["interrupted"] = True
                    self._set(a, "cancelled", "interrupt confirmed; run stopped"); await self.backend.cleanup(a); return
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
                    if e["kind"] == "error" and RATE_LIMIT.search(txt):
                        if is_monthly_limit(txt) and is_go_model(self.cfg, a.get("model"), a.get("brief")) and not a.get("fallback"):
                            return await self._fallback_to_deepseek(a, reason="go monthly-limit 429")
                        return await self._backoff(a)
                    if e["kind"] in ("turn_ended", "exited"):
                        return await self._finish(a, e)
                await asyncio.sleep(1.0)
        except Exception as ex:  # noqa
            self._set(a, "failed", f"controller error: {type(ex).__name__}: {redact(str(ex))[:300]}")
            if not a.get("sandbox") and a.get("model"): a["sandbox"] = sandbox_name(a)  # created but not recorded yet
            try: await self.backend.cleanup(a)
            except Exception: pass
        finally:
            self.controllers.pop(aid, None); self.kick()

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

    async def _fallback_to_deepseek(self, a, reason="go monthly-limit 429"):
        # Retry once on DeepSeek official after a Go monthly-limit 429. Records fallback="deepseek".
        await self.backend.cleanup(a)
        nxt = "deepseek:deepseek-flash"
        a["fallback"] = "deepseek"
        a["model"] = nxt
        a.setdefault("model_history", []).append({"model": nxt, "fallback": "deepseek", "after": reason, "at": time.time()})
        a["rate_limited"], a["cursor"] = 0, 0
        for k in ("run_id", "sandbox"): a.pop(k, None)
        self._set(a, "queued", f"{reason}; fallback=deepseek")

    async def _backoff(self, a, exhausted=False, reason="provider rate limit"):
        if a.get("pool") and a.get("model") in FREE_POOL:
            await self.backend.cleanup(a)
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
        await self.backend.cleanup(a)
        if exhausted or n >= len(delays):
            fb = spec.get("fallback")
            if fb and not a.get("fallback"):
                a["fallback"] = {"from": spec["name"], "to": fb, "reason": f"{reason} after {n} retr{'y' if n == 1 else 'ies'}", "at": time.time()}
                a["model"], a["rate_limited"], a["cursor"] = fb, 0, 0
                for k in ("run_id", "sandbox"): a.pop(k, None)
                self._set(a, "queued", f"{reason} on {spec['name']}; falling back to {fb}"); return
            self._set(a, "failed", f"{reason} persisted after retries"); return
        a["rate_limited"] = n + 1; a["retry_at"] = time.time() + delays[n]
        for k in ("run_id", "sandbox"): a.pop(k, None)
        a["cursor"] = 0
        self._set(a, "backoff", f"provider returned 429; retry {n + 1}/{len(delays)} in {delays[n]}s (queued)")
        self.loop.call_later(delays[n] + 0.05, self.kick)

    async def _finish(self, a, e):
        res = await self.backend.result(a)
        manifest = await self._manifest(a, res)
        (self.dir / "attempts" / f"{a['attempt_id']}.result.json").write_text(json.dumps(manifest, indent=1), "utf-8")
        ok = e.get("stop_reason") in ("end_turn", None) and not res.get("error") and e["kind"] == "turn_ended"
        self._set(a, "succeeded" if ok else "failed", f"turn ended: {e.get('stop_reason') or e['kind']}")
        await self.backend.cleanup(a)

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
                "observed": {"node": platform.node(), "run_id": a.get("run_id"), "sandbox_id": a.get("sandbox_id"), "workdir": a.get("workdir"), "local_base_rev": a.get("local_base_rev"),
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


def make_handler(L, token):
    class H(BaseHTTPRequestHandler):
        server_version = "orbit-launcher/" + VERSION
        protocol_version = "HTTP/1.1"  # keep-alive (plan C); every reply sets Content-Length
        timeout = 120  # idle keep-alive sockets close after 2 min (the box client drops them after 60 s)
        def log_message(self, *a): pass
        def _send(self, code, obj):
            body = json.dumps(obj).encode(); self.send_response(code)
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
                return self._send(200, {"ok": True, "version": VERSION, "max_concurrent": L.cfg["max_concurrent"], "active": len(L._active()), "queued": len(L._queue()), "model": L.cfg["model"],
                                       "uptime_s": round(time.time() - L.started_at), "loop_lag_ms": round(L.loop_lag_ms, 1)})
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
                if isinstance(b.get("brief"), dict): b["brief"] = normalize_brief(b["brief"])
                err = validate_brief(b.get("brief"))
                if err: return self._send(400, {"error": err})
                if b["brief"].get("project") and b["brief"]["project"] not in L.cfg["projects"]: return self._send(403, {"error": "project not authorized on this PC"})
                try: a, created = L.submit(aid, b["brief"], b.get("model") or b.get("tier"), b.get("kind"), b.get("mode"))
                except ValueError as ex: return self._send(400, {"error": str(ex)})
                return self._send(201 if created else 200, L.public(a))
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
                # runtime-only: set max_concurrent (1..MAX_WORKERS); config.json on disk is unchanged, a restart reverts
                try: n = int(b.get("max_concurrent"))
                except Exception: return self._send(400, {"error": "max_concurrent must be an integer"})
                if not 1 <= n <= MAX_WORKERS: return self._send(400, {"error": f"max_concurrent must be 1..{MAX_WORKERS}"})
                with L.lock: L.cfg["max_concurrent"] = n
                L.kick()
                return self._send(200, {"ok": True, "max_concurrent": n})
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

def model_spec(cfg, name, brief=None):
    """Resolve an allow-listed model name (or tier) to its run spec.

    0.1.15 routing: no prefix or `go:` -> OpenCode Go (including bare deepseek-v4.1-flash).
    `deepseek:<model>` or brief priority: high -> DeepSeek official. Free-pool ids are unchanged.
    """
    name = TIERS.get(name, name) if name else cfg["model"]
    if name == "pool:free": return {"name": "pool:free", "pool": "free"}
    high = is_high_priority(brief)
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
    L.provisioner = build_provisioner(cfg)
    write_pidfile(cfg)
    threading.Thread(target=L.loop.run_forever, daemon=True).start(); L.kick()
    asyncio.run_coroutine_threadsafe(_lag_probe(L), L.loop)
    srv = ThreadingHTTPServer((cfg["bind"], cfg["port"]), make_handler(L, token))
    for extra in cfg.get("extra_binds") or []:  # 0.1.11: optional extra listener, loopback only (local coordinator)
        if extra == cfg["bind"]: continue  # already the primary listener
        if not ipaddress.ip_address(extra).is_loopback: raise SystemExit(f"extra_binds allows loopback only, got {extra}")
        xs = ThreadingHTTPServer((extra, cfg["port"]), make_handler(L, token))
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
