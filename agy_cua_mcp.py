#!/usr/bin/env python3
"""stdio MCP shim for the launcher's `agy-cua` task kind (registered in agy as `orbit-cua`).

agy launches this for every session, but it only serves when the launcher started that
agy process for an attempt: it needs ORBIT_AGY_CUA_SANDBOX + ORBIT_AGY_CUA_STATE_DIR in
its environment. It then runs `cua mcp --embedded` with that sandbox as default and sits in the stdio
stream as a filter: cua's computer tools accept a per-call `sandbox` argument and the
embedded runtime still sees every local sandbox (a private --state-dir does NOT hide
them; verified Oct 21), so any tools/call naming another sandbox is refused here and the
`sandbox` property is removed from advertised tool schemas.
No daemon, no host spacesd, no tokens: CUA_DAEMON*/CUA_*TOKEN are stripped.
In an interactive agy session (no attempt env) it exits immediately: zero tools.
"""
import json, os, re, shutil, subprocess, sys
from pathlib import Path

NAME = re.compile(r"[a-z0-9]{2,10}-([a-z0-9]+(-[a-z0-9]+)*-)?[a-z0-9]{10}")  # launcher.sandbox_name(): <abbr>-<slug>-<short id>
DEFAULT_PERMS = "computer:screenshot,computer:click,computer:type,computer:key,computer:scroll,computer:drag,computer:hotkey"
ALLOWED_PERMS = set(DEFAULT_PERMS.split(",")) | {"computer:clipboard", "computer:window", "computer:accessibility", "computer:shell", "computer:file"}
STRIP = ("CUA_DAEMON", "CUA_DAEMON_TOKEN", "CUA_ENV_TOKEN", "CUA_SANDBOX", "CUA_MCP_PERMISSIONS",
         "CUA_CLIENT_ID", "CUA_CLIENT_SECRET", "FLEETS_TOKEN", "ORBIT_WORKER_GO_KEY")

def build(env):
    """Return (argv, child_env) or raise SystemExit with a reason (no secrets are read or printed)."""
    sb, sd = env.get("ORBIT_AGY_CUA_SANDBOX", ""), env.get("ORBIT_AGY_CUA_STATE_DIR", "")
    if not sb or not sd: raise SystemExit("orbit-cua: not started by the Orbit launcher for an agy-cua attempt; serving nothing")
    if not NAME.fullmatch(sb): raise SystemExit("orbit-cua: bad sandbox name")
    if not Path(sd).is_dir(): raise SystemExit("orbit-cua: attempt state dir missing")
    perms = [p for p in (env.get("ORBIT_AGY_CUA_PERMISSIONS") or DEFAULT_PERMS).split(",") if p]
    bad = [p for p in perms if p not in ALLOWED_PERMS]
    if bad or not perms: raise SystemExit(f"orbit-cua: permission not allowed: {bad}")
    exe = env.get("ORBIT_AGY_CUA_EXE") or shutil.which("cua", path=str(Path(sys.executable).parent)) or shutil.which("cua")
    if not exe: raise SystemExit("orbit-cua: cua CLI not found")
    child = {k: v for k, v in env.items() if k not in STRIP and not k.startswith("ORBIT_")
             and not re.search(r"(TOKEN|SECRET|API_KEY|PASSWORD)", k, re.I)}
    argv = [exe, "mcp", "--embedded", "--state-dir", sd, "--sandbox", sb, "--permissions", ",".join(perms)]
    return argv, child

def filter_client_msg(msg, bound):
    """Client->server. Returns (forward_msg_or_None, local_reply_or_None)."""
    if not isinstance(msg, dict) or msg.get("method") != "tools/call": return msg, None
    args = (msg.get("params") or {}).get("arguments")
    if isinstance(args, dict) and "sandbox" in args:
        if args["sandbox"] not in ("", None, bound, "local:" + bound):
            return None, {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"isError": True, "content": [
                {"type": "text", "text": f"refused: this session may only use its own sandbox ({bound}); omit the sandbox argument"}]}}
        args.pop("sandbox")
    return msg, None

def filter_server_msg(msg):
    """Server->client: hide the per-call sandbox parameter from tool schemas."""
    tools = ((msg or {}).get("result") or {}).get("tools") if isinstance(msg, dict) else None
    for t in tools or []:
        sch = t.get("inputSchema") or {}
        (sch.get("properties") or {}).pop("sandbox", None)
        if isinstance(sch.get("required"), list) and "sandbox" in sch["required"]: sch["required"].remove("sandbox")
    return msg

def _write(stream, msg, lock):
    with lock: stream.write((json.dumps(msg) + "\n").encode()); stream.flush()

def main():
    import threading
    try: argv, child = build(dict(os.environ))
    except SystemExit as e: print(e, file=sys.stderr); return 2
    bound = argv[argv.index("--sandbox") + 1]
    p = subprocess.Popen(argv, env=child, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out, lock = sys.stdout.buffer, threading.Lock()
    def up():
        for line in p.stdout:
            try: msg = filter_server_msg(json.loads(line))
            except Exception: msg = None
            if msg is None:
                with lock: out.write(line); out.flush()
            else: _write(out, msg, lock)
    threading.Thread(target=up, daemon=True).start()
    for line in sys.stdin.buffer:
        if not line.strip(): continue
        try: msg = json.loads(line)
        except Exception: continue  # MCP stdio is one JSON-RPC message per line; drop anything else
        fwd, reply = filter_client_msg(msg, bound)
        if reply: _write(out, reply, lock)
        if fwd is not None: p.stdin.write((json.dumps(fwd) + "\n").encode()); p.stdin.flush()
    p.stdin.close(); return p.wait()

if __name__ == "__main__":
    sys.exit(main())
