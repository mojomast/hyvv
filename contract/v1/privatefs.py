"""Owner-only files for the /v1 reference (artifacts, pins, capability hashes).

POSIX: mode 0600. Windows: no inherited ACEs and a single full-control ACE for
the current user's SID, set with icacls before any data is written. Failure to
restrict a file raises, so a result is never written readable by others.
"""
from __future__ import annotations

import functools
import os
import subprocess
from pathlib import Path

WELL_KNOWN_BROAD_SIDS = {
    "S-1-1-0": "Everyone",
    "S-1-5-11": "Authenticated Users",
    "S-1-5-32-545": "BUILTIN\\Users",
    "S-1-5-32-544": "BUILTIN\\Administrators",
    "S-1-5-18": "SYSTEM",
    "S-1-3-0": "CREATOR OWNER",
}


@functools.lru_cache(maxsize=1)
def current_user_sid() -> str:
    """SID of the user running this process (Windows only)."""
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, check=True).stdout
    sid = out.strip().split(",")[-1].strip().strip('"')
    if not sid.startswith("S-1-"):
        raise PermissionError("could not determine the current user SID")
    return sid


def make_owner_only(path: os.PathLike | str) -> None:
    """Restrict path so only the current user can read or write it."""
    p = str(Path(path))
    if os.name != "nt":
        os.chmod(p, 0o600)
        return
    sid = current_user_sid()
    r = subprocess.run(["icacls", p, "/inheritance:r", "/grant:r", f"*{sid}:(F)"], capture_output=True, text=True)
    if r.returncode != 0:
        raise PermissionError(f"icacls failed to restrict a private file (rc {r.returncode})")


def acl_sids(path: os.PathLike | str) -> list[str]:
    """SIDs that have any access entry on path (Windows only; used by tests)."""
    ps = (
        "$a = Get-Acl -LiteralPath $env:HYVV_ACL_PATH; "
        "$a.Access | ForEach-Object { $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value }"
    )
    env = dict(os.environ, HYVV_ACL_PATH=str(Path(path)))
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                         capture_output=True, text=True, check=True, env=env).stdout
    return [x.strip() for x in out.splitlines() if x.strip()]


def write_private(path: os.PathLike | str, data: bytes) -> None:
    """Atomically write data to path as an owner-only file (restricted before data lands)."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    os.close(fd)
    make_owner_only(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_BINARY", 0))
    try:
        os.write(fd, data)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    make_owner_only(path)
