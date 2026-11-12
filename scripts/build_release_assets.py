#!/usr/bin/env python3
"""Build the GitHub Release assets for hyvv, separate from the PyPI dist.

    uv build                                   # dist/: sdist + wheel only
    python scripts/build_release_assets.py     # release-assets/: installers + checksums

dist/ is what PyPI receives, so it must hold exactly one sdist and one wheel for
the version in pyproject.toml and nothing else. release-assets/ holds the
installers (install.sh, install.ps1) and checksums.txt, which covers the
installers and the two dist files. Signing bundles are added to release-assets/
by release.yml. Nothing here tags, signs, uploads or publishes; CI runs it on
every PR.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLERS = ("install.sh", "install.ps1")


def project() -> tuple[str, str]:
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return meta["name"], meta["version"]


def expected_dist_names(name: str, version: str) -> tuple[str, str]:
    norm = name.replace("-", "_")
    return f"{norm}-{version}.tar.gz", f"{norm}-{version}-py3-none-any.whl"


def check_dist(dist: Path, name: str, version: str) -> list[Path]:
    if not dist.is_dir():
        raise SystemExit(f"{dist} does not exist; run `uv build` first")
    files = sorted(p for p in dist.iterdir() if not p.name.startswith("."))
    want = set(expected_dist_names(name, version))
    have = {p.name for p in files}
    if have != want:
        extra, missing = sorted(have - want), sorted(want - have)
        raise SystemExit(f"dist/ must hold only the sdist and wheel for {name} {version}; extra={extra} missing={missing}")
    return files


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(dist: Path, out: Path) -> list[Path]:
    name, version = project()
    dist_files = check_dist(dist, name, version)
    if out.resolve() == dist.resolve():
        raise SystemExit("release-assets dir must differ from dist/")
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.iterdir():
        if stale.is_file():
            stale.unlink()
    assets = []
    for inst in INSTALLERS:
        dst = out / inst
        shutil.copyfile(ROOT / "scripts" / inst, dst)
        assets.append(dst)
    lines = [f"{sha256(p)}  {p.name}" for p in sorted(dist_files + assets, key=lambda p: p.name)]
    (out / "checksums.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    assets.append(out / "checksums.txt")
    return assets


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dist", default=str(ROOT / "dist"))
    ap.add_argument("--out", default=str(ROOT / "release-assets"))
    a = ap.parse_args(argv)
    assets = build(Path(a.dist), Path(a.out))
    for p in assets:
        print(p.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
