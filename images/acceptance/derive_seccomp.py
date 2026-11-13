#!/usr/bin/env python3
"""Derive the hyvv `acceptance` seccomp profile from Docker's default profile.

Input : seccomp/moby-default-v28.4.0.json (moby v28.4.0, vendored moby/profiles
        seccomp v0.1.0; matches the Docker Engine 28.4.0 on the worker PC).
Output: seccomp/acceptance.json

Exactly two changes, nothing else (each proven necessary by strace of
`bwrap --unshare-user --dev-bind / / true` on Docker Engine 28.4.0):
  1. The non-CAP_SYS_ADMIN `clone` rules' flag mask drops CLONE_NEWUSER
     (0x10000000) and CLONE_NEWNS (0x00020000): 0x7E020000 -> 0x6E000000.
     bwrap creates its sandbox with clone(CLONE_NEWNS|CLONE_NEWUSER|SIGCHLD).
  2. Allowed without CAP_SYS_ADMIN (upstream allows them only with it):
       unshare    - `unshare -U`, and bwrap's own namespace setup
       mount      - bwrap makes / private and bind-mounts the new root
       umount2    - bwrap detaches the old root after pivot_root
       pivot_root - bwrap switches into the new root (not in upstream at all)
     These only take effect inside the new user+mount namespace; the container's
     own capability set is unchanged.
setns, clone3 (ENOSYS -> glibc falls back to clone), the new mount API
(fsopen/fsmount/move_mount/open_tree/mount_setattr), bpf and every other rule stay
as upstream. No caps are added; the container is never privileged.

  python3 derive_seccomp.py          # write acceptance.json
  python3 derive_seccomp.py --check  # exit 1 if acceptance.json is stale
"""
from __future__ import annotations
import copy, json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
UPSTREAM = HERE / "seccomp" / "moby-default-v28.4.0.json"
OUT = HERE / "seccomp" / "acceptance.json"
OLD_MASK, NEW_MASK = 0x7E020000, 0x6E000000  # drops CLONE_NEWUSER 0x10000000 and CLONE_NEWNS 0x00020000
ALLOW_SYSCALLS = ("mount", "pivot_root", "umount2", "unshare")
ALLOW_RULE = {"names": list(ALLOW_SYSCALLS), "action": "SCMP_ACT_ALLOW",
              "comment": "hyvv acceptance: bwrap / unshare -U need these without CAP_SYS_ADMIN to build a user+mount namespace sandbox"}


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
    for name in ALLOW_SYSCALLS:
        if any(name in r.get("names", []) and r.get("action") == "SCMP_ACT_ALLOW" and not r.get("includes") for r in d["syscalls"]):
            raise SystemExit(f"upstream already allows {name} unconditionally; re-review before deriving")
    d["syscalls"].append(copy.deepcopy(ALLOW_RULE))
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
