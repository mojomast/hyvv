#!/usr/bin/env python3
"""Deploy the built hyvv wheel's runtime files onto a launcher install root.

This helper is run by scripts/launcher-admin.ps1 from the admin scripts directory
(never from the hyvv checkout). The wheel built by ``uv build --wheel`` is the only
payload source: the admin script no longer keeps a hand-written list of files, so
schemas/, contract/v1/schemas/, the rails modules and the hyvv package all reach the
install dir. The running build is proven by its describe sha (``0.1.19+g521e9bc``),
not by the bare VERSION literal, so a build off the tag can no longer masquerade as
the tag and a rollback to a pre-describe build still verifies.

Standard library only, no network, no side effects at import time. Safety rules: a
wheel member that is absolute, has a drive, contains a backslash or has ``..``/``.``/
empty segments is refused, and the wheel must never carry admin scripts, config,
keys or tokens onto a host (anything under scripts/, any *.ps1, any basename
config.json, any *.key or *.token is refused).
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

# The only running-version forms accepted: the bare release, or the release with a
# describe local segment naming the exact build (+g<sha> or +<count>.g<sha>).
RUNNING_VERSION_RE = re.compile(r"^(\d+(?:\.\d+){1,3})(?:\+(?:(\d+)\.)?g([0-9a-f]{7,40}))?$")
TARGET_SHA_RE = re.compile(r"[0-9a-f]{40}")
DEPLOYED_VERSION_FILE = "hyvv-deployed.json"
BACKUP_SCHEMA = "hyvv.payload-backup/1"
TMP_SUFFIX = ".tmp-deploy"


def parse_running_version(text):
    """Parse a reported running version into {'base', 'count', 'sha'} or None.

    Only ``0.1.19``, ``0.1.19+g521e9bc`` and ``0.1.19+25.g521e9bc`` forms parse.
    ``.dirty``/``+dirty``/``+unknown``, a leading v, short (<7) or non-hex shas and
    inner spaces are all rejected (None).
    """
    if not isinstance(text, str) or not text:
        return None
    m = RUNNING_VERSION_RE.fullmatch(text)
    if not m:
        return None
    count = m.group(2)
    return {"base": m.group(1), "count": int(count) if count is not None else None, "sha": m.group(3)}


def is_describe_capable(launcher_text):
    """True iff the launcher source has a top-level ``def build_version(``.

    Parsed with ast; when the text does not parse, fall back to a multiline regex.
    A pre-describe launcher (no build_version) can only ever report the bare release.
    """
    if not isinstance(launcher_text, str):
        return False
    try:
        tree = ast.parse(launcher_text)
    except (SyntaxError, ValueError):
        return bool(re.search(r"^def build_version\(", launcher_text, re.M))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "build_version":
            return True
    return False


def check_version(running, want, target_sha, describe_capable):
    """Verify the running launcher's reported version against the deploy target.

    The build sha is the proof, not the version string: on a describe-capable target
    the running build must report a sha that is a prefix of the full target sha. On a
    pre-describe target (e.g. a rollback) only the bare release form is accepted.
    Returns (ok, reason).
    """
    if not (isinstance(target_sha, str) and TARGET_SHA_RE.fullmatch(target_sha)):
        return False, "target must be a full 40-char lowercase hex sha"
    parsed = parse_running_version(running)
    if parsed is None:
        return False, "unparseable running version %r" % (running,)
    if parsed["base"] != want:
        return False, "running base %s does not match want %s" % (parsed["base"], want)
    if describe_capable:
        if not parsed["sha"]:
            return False, "describe-capable target requires a build sha, running reports bare %s" % parsed["base"]
        if not target_sha.startswith(parsed["sha"]):
            return False, "running build sha %s is not a prefix of target %s" % (parsed["sha"], target_sha)
        return True, "describe build %s matches target" % parsed["sha"]
    if parsed["count"] is not None or parsed["sha"]:
        return False, "pre-describe target must report the bare release, running reports %s" % running
    return True, "pre-describe build reports bare %s" % parsed["base"]


def _check_member_name(name):
    """Refuse zip member names that could escape the install root or hide on disk."""
    if not isinstance(name, str) or not name:
        raise ValueError("empty member name")
    if name.startswith("/") or name.startswith("\\"):
        raise ValueError("absolute member name: %r" % name)
    if "\\" in name:
        raise ValueError("backslash in member name: %r" % name)
    if re.match(r"^[A-Za-z]:", name):
        raise ValueError("drive in member name: %r" % name)
    for seg in name.split("/"):
        if seg in ("", ".", ".."):
            raise ValueError("unsafe segment in member name: %r" % name)


def _is_guarded(name):
    """Members the wheel must never carry onto a host: admin scripts, config, keys, tokens."""
    parts = name.split("/")
    base = parts[-1].lower()
    if parts[0].lower() == "scripts":
        return True
    if base.endswith(".ps1"):
        return True
    if base == "config.json":
        return True
    if base.endswith(".key") or base.endswith(".token"):
        return True
    return False


def runtime_files(wheel_path):
    """Sorted posix names of the wheel members that belong on the install root.

    Directories and anything under a top-level *.dist-info/ dir are excluded. Any
    unsafe or guarded member anywhere in the archive raises ValueError: the whole
    wheel is rejected, never partially installed.
    """
    names = []
    with zipfile.ZipFile(wheel_path) as zf:
        for info in zf.infolist():
            name = info.filename
            is_dir = info.is_dir() or name.endswith("/")
            check = name[:-1] if is_dir and name.endswith("/") else name
            _check_member_name(check)
            if _is_guarded(check):
                raise ValueError("guarded member refused: %r" % name)
            if is_dir:
                continue
            parts = check.split("/")
            if len(parts) > 1 and parts[0].endswith(".dist-info"):
                continue
            names.append(check)
    return sorted(set(names))


def _dest_for(root, name):
    return Path(root).joinpath(*name.split("/"))


def _write_atomic(dest, data):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / (dest.name + TMP_SUFFIX)
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def install(wheel_path, root):
    """Install the wheel's runtime files under root (tmp + os.replace per file).

    The full member list is validated before anything is written; files not in the
    wheel's runtime list are never touched. Returns the installed names.
    """
    names = runtime_files(wheel_path)
    with zipfile.ZipFile(wheel_path) as zf:
        for name in names:
            _write_atomic(_dest_for(root, name), zf.read(name))
    return names


def verify_root(wheel_path, root):
    """Post-copy check: every runtime file exists under root with identical bytes.

    Returns a list of 'missing: <name>' / 'mismatch: <name>' problems ([] when ok).
    """
    problems = []
    with zipfile.ZipFile(wheel_path) as zf:
        for name in runtime_files(wheel_path):
            dest = _dest_for(root, name)
            if not dest.is_file():
                problems.append("missing: " + name)
                continue
            want = hashlib.sha256(zf.read(name)).hexdigest()
            got = hashlib.sha256(dest.read_bytes()).hexdigest()
            if got != want:
                problems.append("mismatch: " + name)
    return problems


def _receipt_file_names(root):
    """Names in an existing root/hyvv-deployed.json 'files' map ([] when absent)."""
    receipt = Path(root) / DEPLOYED_VERSION_FILE
    if not receipt.is_file():
        return []
    try:
        data = json.loads(receipt.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError("unreadable %s: %s" % (DEPLOYED_VERSION_FILE, exc))
    files = data.get("files") if isinstance(data, dict) else None
    if isinstance(files, dict):
        names = list(files)
    elif isinstance(files, list):
        names = list(files)
    else:
        names = []
    out = []
    for name in names:
        name = str(name)
        _check_member_name(name)
        if _is_guarded(name):
            raise ValueError("guarded receipt member refused: %r" % name)
        out.append(name)
    return out


def backup(wheel_path, root, dest):
    """Back up the previous payload: every runtime file of the NEW wheel, every file
    the existing deployed-version receipt lists, and the receipt itself.

    Each name is copied to dest/<name> when it exists; dest/manifest.json records
    {'existed', 'sha256'} per name so restore() can return the exact previous tree.
    Returns the backup dir as a Path.
    """
    root = Path(root)
    dest = Path(dest)
    names = set(runtime_files(wheel_path))
    names.update(_receipt_file_names(root))
    names.add(DEPLOYED_VERSION_FILE)
    dest.mkdir(parents=True, exist_ok=True)
    entries = {}
    for name in sorted(names):
        src = _dest_for(root, name)
        existed = src.is_file()
        digest = None
        if existed:
            data = src.read_bytes()
            _write_atomic(_dest_for(dest, name), data)
            digest = hashlib.sha256(data).hexdigest()
        entries[name] = {"existed": existed, "sha256": digest}
    manifest = {"schema": BACKUP_SCHEMA, "files": entries}
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return dest


def restore(dest, root):
    """Restore a backup() dir: existing files get their backed-up bytes back (tmp +
    os.replace); files that did not exist are removed (then now-empty parent dirs
    inside root, never root itself). Manifest names failing the safety rules refuse
    the whole restore.
    """
    dest = Path(dest)
    root = Path(root)
    try:
        manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError("unreadable backup manifest: %s" % exc)
    if not isinstance(manifest, dict) or manifest.get("schema") != BACKUP_SCHEMA:
        raise ValueError("not a %s backup" % BACKUP_SCHEMA)
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("backup manifest has no files map")
    for name in sorted(files):
        _check_member_name(name)
        if _is_guarded(name):
            raise ValueError("guarded manifest member refused: %r" % name)
        entry = files[name]
        if not isinstance(entry, dict):
            raise ValueError("bad backup manifest entry: %r" % name)
        existed = bool(entry.get("existed"))
        target = _dest_for(root, name)
        if existed:
            src = _dest_for(dest, name)
            if not src.is_file():
                raise ValueError("backup entry missing: %r" % name)
            _write_atomic(target, src.read_bytes())
        else:
            if target.is_file() or target.is_symlink():
                target.unlink()
            parent = target.parent
            while parent != root and root in parent.parents and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent


# The smoke child runs isolated (python -I -c CODE <root>) with the deploy root first
# on sys.path. It imports launcher, proves it came from the root, loads the ticket
# schema, then imports every top-level runtime module and every hyvv submodule. The
# wheel's runtime set carries no dist-info and no pyproject.toml, so throwaway package
# metadata is provided in a temp dir (outside the root) purely so hyvv/__init__ can
# resolve a version; every imported module's __file__ must still live under the root.
_SMOKE_CHILD = r"""
import importlib, json, shutil, sys, tempfile
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
result = {"ok": False, "ticket_schema": "unknown", "rizzstack_validators": "absent", "modules": {}, "error": ""}
errors = []
meta = Path(tempfile.mkdtemp(prefix="hyvv-smoke-meta-"))
try:
    dist = meta / "hyvv-0.0.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: hyvv\nVersion: 0.0.0\n", encoding="utf-8")
    sys.path.append(str(meta))

    def note(name):
        try:
            mod = importlib.import_module(name)
        except Exception as exc:
            result["modules"][name] = "error: %s" % exc
            errors.append("import %s: %s" % (name, exc))
            return None
        where = Path(getattr(mod, "__file__", "") or "").resolve()
        if where != root and root not in where.parents:
            result["modules"][name] = "error: outside root"
            errors.append("module %s loaded from %s, outside the deploy root" % (name, getattr(mod, "__file__", "?")))
            return mod
        result["modules"][name] = "ok"
        return mod

    launcher = note("launcher")
    if launcher is not None:
        try:
            if Path(launcher.__file__).resolve().parent != root:
                errors.append("launcher loaded from %s, not the deploy root" % launcher.__file__)
        except Exception as exc:
            errors.append("launcher path check failed: %s" % exc)
        try:
            schema = launcher._ticket_schema()
            if isinstance(schema, dict):
                result["ticket_schema"] = "ok"
            else:
                result["ticket_schema"] = "not a dict"
                errors.append("ticket schema is not a dict")
        except Exception as exc:
            result["ticket_schema"] = "%s: %s" % (type(exc).__name__, exc)
            errors.append("ticket schema: %s" % exc)

    runtime = None
    receipt = root / "hyvv-deployed.json"
    if receipt.is_file():
        try:
            data = json.loads(receipt.read_text(encoding="utf-8-sig"))
            files = data.get("files")
            if isinstance(files, dict):
                runtime = sorted(str(n) for n in files)
            elif isinstance(files, list):
                runtime = sorted(str(n) for n in files)
        except Exception as exc:
            errors.append("deployed-version receipt unreadable: %s" % exc)
    if runtime is None:
        runtime = sorted(p.name for p in root.glob("*.py"))
    for name in runtime:
        if "/" in name or "\\" in name or not name.endswith(".py"):
            continue
        stem = name[:-3]
        if stem != "launcher":
            note(stem)

    pkg = root / "hyvv"
    if pkg.is_dir():
        for f in sorted(pkg.glob("*.py")):
            if f.name == "__main__.py":
                continue
            note("hyvv" if f.name == "__init__.py" else "hyvv." + f.name[:-3])
    if (pkg / "rizzstack_validators.py").is_file():
        result["rizzstack_validators"] = "present"
finally:
    shutil.rmtree(str(meta), ignore_errors=True)

result["ok"] = not errors and result["ticket_schema"] == "ok"
result["error"] = "; ".join(errors)
print(json.dumps(result))
"""


def smoke(root, python=None, timeout=180):
    """Run the isolated child against root and parse its one JSON result line.

    Never raises: any failure to run or parse the child lands in 'error' with
    ok False. ok means launcher and every runtime module imported from under root
    and the ticket schema loaded as a dict.
    """
    result = {"ok": False, "ticket_schema": "unknown", "rizzstack_validators": "unknown", "modules": {}, "error": ""}
    exe = python or sys.executable
    try:
        proc = subprocess.run([exe, "-I", "-c", _SMOKE_CHILD, str(root)], cwd=str(root),
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        result["error"] = "smoke child failed to run: %s" % exc
        return result
    line = ""
    for candidate in proc.stdout.splitlines():
        if candidate.strip():
            line = candidate
    try:
        data = json.loads(line)
    except ValueError:
        detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:300]
        result["error"] = "smoke child printed no result (exit %s): %s" % (proc.returncode, detail)
        return result
    if not isinstance(data, dict):
        result["error"] = "smoke child result is not an object"
        return result
    modules = data.get("modules")
    result["ok"] = bool(data.get("ok"))
    result["ticket_schema"] = str(data.get("ticket_schema", "unknown"))
    result["rizzstack_validators"] = str(data.get("rizzstack_validators", "unknown"))
    result["modules"] = modules if isinstance(modules, dict) else {}
    result["error"] = str(data.get("error", ""))
    if proc.returncode != 0 and result["ok"]:
        result["ok"] = False
        result["error"] = (result["error"] + " child exit %s" % proc.returncode).strip()
    return result


def _one_line(text):
    return re.sub(r"\s+", " ", str(text)).strip()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="deploy_payload.py",
                                 description="Deploy the built hyvv wheel's runtime files onto a launcher install root.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check-version", help="verify the running build against the deploy target")
    p.add_argument("--running", required=True)
    p.add_argument("--want", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--target-launcher", required=True)

    p = sub.add_parser("files", help="list the wheel's runtime files")
    p.add_argument("--wheel", required=True)

    p = sub.add_parser("install", help="install the wheel's runtime files into a root")
    p.add_argument("--wheel", required=True)
    p.add_argument("--root", required=True)

    p = sub.add_parser("verify-root", help="prove every runtime file under root matches the wheel")
    p.add_argument("--wheel", required=True)
    p.add_argument("--root", required=True)

    p = sub.add_parser("backup", help="back up the previous payload before an install")
    p.add_argument("--wheel", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--dest", required=True)

    p = sub.add_parser("restore", help="restore a backup dir over a root")
    p.add_argument("--backup", required=True)
    p.add_argument("--root", required=True)

    p = sub.add_parser("smoke", help="import-prove an installed root in an isolated child")
    p.add_argument("--root", required=True)
    p.add_argument("--python", default=None)

    args = ap.parse_args(argv)
    try:
        if args.cmd == "check-version":
            try:
                text = Path(args.target_launcher).read_text(encoding="utf-8")
            except OSError as exc:
                print("error=cannot read target launcher: %s" % exc, file=sys.stderr)
                return 2
            capable = is_describe_capable(text)
            ok, reason = check_version(args.running, args.want, args.target, capable)
            mode = "describe" if capable else "pre-describe"
            print("version_check=%s mode=%s running=%s reason=%s" % ("ok" if ok else "fail", mode, args.running, _one_line(reason)))
            return 0 if ok else 1
        if args.cmd == "files":
            for name in runtime_files(args.wheel):
                print(name)
            return 0
        if args.cmd == "install":
            names = install(args.wheel, args.root)
            print("installed=%d" % len(names))
            return 0
        if args.cmd == "verify-root":
            problems = verify_root(args.wheel, args.root)
            if not problems:
                print("verify_root=ok files=%d" % len(runtime_files(args.wheel)))
                return 0
            print("verify_root=fail")
            for problem in problems:
                print(problem)
            return 1
        if args.cmd == "backup":
            dest = backup(args.wheel, args.root, args.dest)
            print("backup=%s" % dest)
            return 0
        if args.cmd == "restore":
            restore(args.backup, args.root)
            print("restore=ok")
            return 0
        if args.cmd == "smoke":
            result = smoke(args.root, python=args.python)
            print("smoke=%s ticket_schema=%s rizzstack_validators=%s modules=%d" % (
                "ok" if result["ok"] else "fail", _one_line(result["ticket_schema"]),
                result["rizzstack_validators"], len(result["modules"])))
            if not result["ok"]:
                print("error=%s" % _one_line(result["error"]))
            return 0 if result["ok"] else 1
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        print("error=%s" % _one_line(exc), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
