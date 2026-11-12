"""One-command /v1 conformance run.

    uv run python -m contract                 # in-process hyvv (fake backend) + schema/reference tests
    HYVV_BASE_URL=http://127.0.0.1:47821 HYVV_TOKEN_FILE=path/to/token uv run python -m contract
    uv run python -m contract --base-url URL --token-file PATH [--writes] [-v] [-k PATTERN]

Live runs are read-only unless --writes (or HYVV_CONTRACT_WRITES=1) is given.
The base URL and token are never printed.
"""
from __future__ import annotations

import argparse
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m contract", description="Run the hyvv /v1 contract tests.")
    ap.add_argument("--base-url", help="live /v1 base URL (else HYVV_BASE_URL; else an in-process server)")
    ap.add_argument("--token-file", help="file holding the bearer token for a live host (else HYVV_TOKEN_FILE or HYVV_TOKEN)")
    ap.add_argument("--writes", action="store_true", help="also run create/review cases against a live host")
    ap.add_argument("-k", dest="patterns", action="append", default=[], help="only tests matching this substring (repeatable)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    if a.base_url:
        os.environ["HYVV_BASE_URL"] = a.base_url
    if a.token_file:
        os.environ["HYVV_TOKEN_FILE"] = a.token_file
    if a.writes:
        os.environ["HYVV_CONTRACT_WRITES"] = "1"
    live = bool((os.environ.get("HYVV_BASE_URL") or "").strip())
    target = "live host (HYVV_BASE_URL)" if live else "in-process hyvv on loopback"
    writes = "writes on" if (not live or os.environ.get("HYVV_CONTRACT_WRITES") == "1") else "read-only"
    print(f"hyvv /v1 contract: target={target}, {writes}", flush=True)
    sys.path.insert(0, str(ROOT))
    loader = unittest.TestLoader()
    if a.patterns:
        loader.testNamePatterns = [f"*{p}*" for p in a.patterns]
    suite = loader.discover(str(ROOT / "contract" / "v1" / "tests"), top_level_dir=str(ROOT))
    res = unittest.TextTestRunner(verbosity=2 if a.verbose else 1).run(suite)
    return 0 if res.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
