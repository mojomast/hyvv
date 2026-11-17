"""Offline: copy only the worker runtime from a reviewed BundleBees manifest."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

PACKAGE = "contrib/rizzstack_rails"
MODULES = {"__init__.py", "contracts.py", "ledger.py", "privacy.py"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundlebees", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--rev", help="read pinned files from a full BundleBees commit instead of the working tree")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.rev and (not re.fullmatch("[0-9a-f]{40}", args.rev) or subprocess.check_output(
        ["git", "cat-file", "-t", args.rev], cwd=args.bundlebees
    ) != b"commit\n"):
        parser.error("--rev requires a full BundleBees commit id")

    def source_bytes(name):
        if args.rev:
            return subprocess.check_output(["git", "show", f"{args.rev}:{name}"], cwd=args.bundlebees)
        return (args.bundlebees / name).read_bytes()

    manifest = json.loads(source_bytes(PACKAGE + "/vendor-manifest.json"))
    names = {PACKAGE + "/" + n for n in MODULES}
    entries = sorted((e for e in manifest["files"] if e["path"] in names), key=lambda e: e["path"])
    if (
        manifest.get("schema") != "rizzstack.vendor-manifest/1"
        or manifest.get("source_package") != PACKAGE
        or {e["path"] for e in entries} != names
        or len(entries) != len(names)
    ):
        raise SystemExit("Incomplete source manifest")
    files = {}
    for entry in entries:
        data = source_bytes(entry["path"])
        if hashlib.sha256(data).hexdigest() != entry["sha256"] or len(data) != entry["bytes"]:
            raise SystemExit("Source differs from manifest: " + entry["path"])
        files[entry["path"]] = data
    manifest["files"] = entries
    if args.rev:
        manifest["pin_label"] = args.rev
    files[PACKAGE + "/vendor-manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(
        "ascii"
    )
    for name, data in files.items():
        target = root / name
        if not target.is_file() or target.read_bytes() != data:
            if args.check:
                raise SystemExit("Vendor mismatch: " + name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


if __name__ == "__main__":
    main()
