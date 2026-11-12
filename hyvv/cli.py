"""hyvv command-line entry. Installed with uv as the `hyvv` script."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# launcher.py lives at the repo / install root next to this package.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

PLANNED = (
    "install",
    "uninstall",
    "drain",
    "uncordon",
    "reload",
    "update",
    "rollback",
    "enroll",
    "logs",
)
IMPLEMENTED = ("serve", "doctor", "probe", "sdkcheck", "agysmoke", "version")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "hyvv — worker-host runtime\n"
            "Runs as the logged-in user by default; a system service is opt-in.\n\n"
            "Implemented: " + ", ".join(IMPLEMENTED) + "\n"
            "Planned: " + ", ".join(PLANNED) + "\n\n"
            "  hyvv serve --config PATH [--backend cua|fake]\n"
            "  hyvv doctor --config PATH\n"
            "  hyvv version\n"
        )
        return 0
    cmd = argv[0]
    if cmd in ("version", "-V", "--version"):
        from hyvv import __version__
        print(__version__)
        return 0
    if cmd in PLANNED:
        print(f"hyvv {cmd}: planned (see ROADMAP.md). Not implemented in 0.1.15.", file=sys.stderr)
        return 2
    if cmd in ("start", "stop", "status"):
        print(
            "Use scripts/start.ps1, stop.ps1 and status.ps1 on Windows, "
            "or the user-level service unit once Linux/macOS install ships.",
            file=sys.stderr,
        )
        return 2
    if cmd in ("serve", "doctor", "probe", "sdkcheck", "agysmoke"):
        return _run_launcher(argv)
    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2


def _run_launcher(argv: list[str]) -> int:
    import launcher

    ap = argparse.ArgumentParser(prog="hyvv")
    ap.add_argument("cmd", choices=["serve", "doctor", "probe", "sdkcheck", "agysmoke"])
    ap.add_argument("--objective", default="Take a screenshot and describe the desktop.")
    ap.add_argument("--config")
    ap.add_argument("--backend", default="cua", choices=["cua", "fake"])
    ap.add_argument("--model")
    args = ap.parse_args(argv)
    cfg = launcher.load_config(args.config) if args.config else _default_config(launcher)
    if args.cmd == "agysmoke":
        if not cfg:
            print("pass --config", file=sys.stderr)
            return 2
        return launcher.asyncio.run(launcher.agy_smoke(cfg, args.objective))
    if args.cmd == "sdkcheck":
        problems = launcher.sdk_contract_check()
        print(json.dumps(problems))
        return 1 if problems else 0
    if not cfg:
        print("pass --config or create a config from config.example.json", file=sys.stderr)
        return 2
    if args.cmd == "doctor":
        return launcher.doctor(cfg)
    if args.cmd == "probe":
        result = launcher.asyncio.run(
            launcher._probe_all(cfg) if args.model == "free-all" else launcher._probe(cfg, args.model)
        )
        return 0 if result else 1
    launcher.serve(cfg, args.backend)
    return 0


def _default_config(launcher):
    home = Path.home()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    for candidate in (home / ".orbit-launcher" / "config.json", xdg / "hyvv" / "config.json"):
        if candidate.exists():
            return launcher.load_config(str(candidate))
    return None


if __name__ == "__main__":
    raise SystemExit(main())
