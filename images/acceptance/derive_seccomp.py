#!/usr/bin/env python3
"""Derive the hyvv `acceptance` seccomp profile from Docker's default profile.

Input : seccomp/moby-default-v28.4.0.json (moby v28.4.0, vendored moby/profiles
        seccomp v0.1.0; matches the Docker Engine 28.4.0 on the worker PC).
Output: seccomp/acceptance.json

Exactly two changes, nothing else:
  1. The non-CAP_SYS_ADMIN `clone` rules' flag mask drops CLONE_NEWUSER
     (0x7E020000 -> 0x6E020000), so clone(CLONE_NEWUSER) is allowed.
  2. `unshare` is allowed (upstream allows it only with CAP_SYS_ADMIN).
setns, mount, clone3 (ENOSYS -> glibc falls back to clone) and every other rule
stay as upstream. No caps are added; the container is never privileged.

  python3 derive_seccomp.py          # write acceptance.json
  python3 derive_seccomp.py --check  # exit 1 if acceptance.json is stale
"""
from __future__ import annotations
import copy, json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
UPSTREAM = HERE / "seccomp" / "moby-default-v28.4.0.json"
OUT = HERE / "seccomp" / "acceptance.json"
OLD_MASK, NEW_MASK = 0x7E020000, 0x6E020000  # CLONE_NEWUSER = 0x10000000
UNSHARE_RULE = {"names": ["unshare"], "action": "SCMP_ACT_ALLOW",
                "comment": "hyvv acceptance: allow unshare(2) without CAP_SYS_ADMIN so bwrap / unshare -U can create a user namespace"}


def derive(upstream: dict) -> dict:
    d = copy.deepcopy(upstream)
    n = 0
    for rule in d["syscalls"]:
        if rule.get("names") == ["clone"] and rule.get("action") == "SCMP_ACT_ALLOW":
            for arg in rule.get("args") or []:
                if arg.get("op") == "SCMP_CMP_MASKED_EQ" and arg.get("value") == OLD_MASK:
                    arg["value"] = NEW_MASK; n += 1
    if n != 2:
        raise SystemExit(f"expected 2 clone mask rules in upstream profile, found {n}")
    if any("unshare" in r.get("names", []) and not r.get("includes") for r in d["syscalls"]):
        raise SystemExit("upstream already allows unshare unconditionally; re-review before deriving")
    d["syscalls"].append(dict(UNSHARE_RULE))
    return d


def render(d: dict) -> str:
    return json.dumps(d, indent="\t") + "\n"


def main(argv):
    out = render(derive(json.loads(UPSTREAM.read_text("utf-8"))))
    if "--check" in argv:
        if not OUT.exists() or OUT.read_text("utf-8") != out:
            print(f"{OUT.name} is stale: run python3 {Path(__file__).name}"); return 1
        print(f"{OUT.name} is up to date"); return 0
    OUT.write_text(out, "utf-8"); print(f"wrote {OUT}"); return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
