#!/usr/bin/env python3
"""Bake the build identity beside launcher.py for installs that carry no .git.

    python scripts/bake_build_info.py                  # writes ./hyvv-build.json
    python scripts/bake_build_info.py --out DIR        # writes DIR/hyvv-build.json
    python scripts/bake_build_info.py --root CHECKOUT  # bake another checkout's identity

The launcher derives its reported version from `git describe --tags --always --dirty`; a
packaged or copied install has no metadata, so it reads this file instead. Baking refuses a
tree with no git identity: writing `+unknown` would be no better than the tag claim this
mechanism exists to prevent. Nothing here tags, releases or deploys.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ("version", "release", "describe", "sha", "short_sha", "commits_past_tag", "dirty")


def launcher_module():
    spec = importlib.util.spec_from_file_location("launcher", ROOT / "launcher.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("launcher", mod)
    spec.loader.exec_module(mod)
    return mod


def bake(root, out_dir):
    """Write ``<out_dir>/hyvv-build.json`` from *root*'s git metadata; returns the path."""
    L = launcher_module()
    ident = L.build_identity(root=root)
    if ident["source"] != "git":
        raise SystemExit(f"no git build identity under {root}; refusing to bake a guess")
    record = {k: ident[k] for k in FIELDS}
    path = Path(out_dir) / L.BUILD_INFO_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bake the build identity beside launcher.py.")
    ap.add_argument("--root", default=str(ROOT), help="checkout whose git metadata is baked")
    ap.add_argument("--out", default=str(ROOT), help="directory that receives hyvv-build.json")
    a = ap.parse_args(argv)
    print(bake(a.root, a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
