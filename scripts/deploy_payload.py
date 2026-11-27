#!/usr/bin/env python3
"""Deploy the built hyvv wheel's runtime files onto a launcher install root.

This helper is run by scripts/launcher-admin.ps1 from the admin scripts directory
(never from the hyvv checkout). The wheel built by ``uv build --wheel`` is the only
payload source: the admin script no longer keeps a hand-written list of files, so
schemas/, contract/v1/schemas/, the rails modules and the hyvv package all reach the
install dir. The wheel's single dist-info directory ships too: an install root has
no pyproject.toml and no .git, so hyvv/__init__ resolves its version from the
installed dist-info metadata, and a launcher importing hyvv would crash without it.
Files a previous deploy installed but the new wheel no longer ships are pruned, so
stale modules and stale dist-info metadata never linger on a host. The running
build is proven by its describe sha (``0.1.19+g521e9bc``), not by the bare VERSION
literal, so a build off the tag can no longer masquerade as the tag and a rollback
to a pre-describe build still verifies.

Standard library only, no network, no side effects at import time. Safety rules: a
wheel member that is absolute, has a drive, contains a backslash or has ``..``/``.``/
empty segments is refused, a wheel carrying more than one top-level *.dist-info dir
is refused, and the wheel must never carry admin scripts, config, keys or tokens
onto a host (anything under scripts/, any *.ps1, any basename config.json, any
*.key or *.token is refused). prune() applies the same member-name and guard rules
to every removal candidate before anything is removed, and never follows symlinks
or reparse points.
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


def _scan_wheel_member(info):
    """Validate one wheel member; return (name, is_dir, dist_info_top_or_None).

    Any unsafe or guarded member raises ValueError, rejecting the whole wheel.
    """
    name = info.filename
    is_dir = info.is_dir() or name.endswith("/")
    check = name[:-1] if is_dir and name.endswith("/") else name
    _check_member_name(check)
    if _is_guarded(check):
        raise ValueError("guarded member refused: %r" % name)
    parts = check.split("/")
    top = parts[0] if parts[0].endswith(".dist-info") and (is_dir or len(parts) > 1) else None
    return check, is_dir, top


def runtime_files(wheel_path):
    """Sorted posix names of the wheel members that belong on the install root.

    The wheel's one top-level *.dist-info dir ships with everything else: on a host
    the install root has no pyproject.toml, so hyvv/__init__ resolves its version
    from that installed metadata. Directory entries themselves are excluded. Any
    unsafe or guarded member anywhere in the archive, or more than one top-level
    *.dist-info dir, raises ValueError: the whole wheel is rejected, never
    partially installed.
    """
    names = []
    dist_info = set()
    with zipfile.ZipFile(wheel_path) as zf:
        for info in zf.infolist():
            check, is_dir, top = _scan_wheel_member(info)
            if top is not None:
                dist_info.add(top)
            if is_dir:
                continue
            names.append(check)
    if len(dist_info) > 1:
        raise ValueError("wheel carries %d top-level dist-info dirs, expected exactly one: %s"
                         % (len(dist_info), ", ".join(sorted(dist_info))))
    return sorted(set(names))


def _wheel_dist_info_dir(wheel_path):
    """The wheel's single top-level *.dist-info dir name, or None when it has none."""
    for name in runtime_files(wheel_path):
        top = name.split("/")[0]
        if top.endswith(".dist-info"):
            return top
    return None


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


def _dist_info_dirs(root):
    """Top-level hyvv-*.dist-info directories under root, sorted by name.

    A symlink (or any reparse-point stand-in) named like a dist-info dir is never
    followed: it raises ValueError so the caller refuses the whole operation.
    """
    root = Path(root)
    out = []
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if not (child.name.startswith("hyvv-") and child.name.endswith(".dist-info")):
            continue
        if child.is_symlink():
            raise ValueError("refusing symlinked dist-info dir: %r" % child.name)
        if child.is_dir():
            out.append(child)
    return out


def _files_under(root, base):
    """Sorted posix names (relative to root) of every file under base.

    Walks without ever following symlinks: symlinked subdirectories are not
    descended into and a symlinked file is listed (unlinking it later removes the
    link, never its target).
    """
    root = Path(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(str(base), followlinks=False):
        dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
        for filename in filenames:
            out.append((Path(dirpath) / filename).relative_to(root).as_posix())
    return sorted(out)


def _stale_dist_info_files(root):
    """Sorted names of every file under any root/hyvv-*.dist-info dir."""
    root = Path(root)
    out = []
    for dist in _dist_info_dirs(root):
        out.extend(_files_under(root, dist))
    return sorted(out)


def _check_candidate_name(name):
    """The member-name and guard rules every prune/backup candidate must pass."""
    _check_member_name(name)
    if _is_guarded(name):
        raise ValueError("guarded payload member refused: %r" % name)


def backup(wheel_path, root, dest):
    """Back up the previous payload: every runtime file of the NEW wheel, every file
    the existing deployed-version receipt lists, every file under any existing
    root/hyvv-*.dist-info dir (so restore() brings stale metadata back), and the
    receipt itself.

    Each name is copied to dest/<name> when it exists; dest/manifest.json records
    {'existed', 'sha256'} per name so restore() can return the exact previous tree.
    Every candidate name passes the member-name and guard rules before anything is
    copied. Returns the backup dir as a Path.
    """
    root = Path(root)
    dest = Path(dest)
    names = set(runtime_files(wheel_path))
    names.update(_receipt_file_names(root))
    for name in _stale_dist_info_files(root):
        _check_candidate_name(name)
        names.add(name)
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


def _read_backup_manifest(dest):
    """The 'files' map of a backup() dir's manifest, validated against BACKUP_SCHEMA."""
    try:
        manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError("unreadable backup manifest: %s" % exc)
    if not isinstance(manifest, dict) or manifest.get("schema") != BACKUP_SCHEMA:
        raise ValueError("not a %s backup" % BACKUP_SCHEMA)
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("backup manifest has no files map")
    return files


def _remove_restored_file(root, target):
    """Remove a not-previously-existing file, then now-empty parents inside root."""
    if target.is_file() or target.is_symlink():
        target.unlink()
    parent = target.parent
    while parent != root and root in parent.parents and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def _restore_entry(dest, root, name, entry):
    """Restore one manifest entry: backed-up bytes back, or a new file removed."""
    _check_member_name(name)
    if _is_guarded(name):
        raise ValueError("guarded manifest member refused: %r" % name)
    if not isinstance(entry, dict):
        raise ValueError("bad backup manifest entry: %r" % name)
    target = _dest_for(root, name)
    if not entry.get("existed"):
        _remove_restored_file(root, target)
        return
    src = _dest_for(dest, name)
    if not src.is_file():
        raise ValueError("backup entry missing: %r" % name)
    _write_atomic(target, src.read_bytes())


def restore(dest, root):
    """Restore a backup() dir: existing files get their backed-up bytes back (tmp +
    os.replace); files that did not exist are removed (then now-empty parent dirs
    inside root, never root itself). Manifest names failing the safety rules refuse
    the whole restore.
    """
    dest = Path(dest)
    root = Path(root)
    files = _read_backup_manifest(dest)
    for name in sorted(files):
        _restore_entry(dest, root, name, files[name])


def _remove_empty_parents(root, start):
    """Remove now-empty parent dirs of a removed file, inside root, never root."""
    root = Path(root)
    parent = Path(start)
    while (parent != root and root in parent.parents and parent.is_dir()
           and not parent.is_symlink() and not any(parent.iterdir())):
        parent.rmdir()
        parent = parent.parent


def prune(wheel_path, root):
    """Remove payload files the new wheel no longer ships. Returns sorted removed names.

    Candidates are (a) every name in an existing root/hyvv-deployed.json 'files' map
    that is not in the new wheel's runtime set, and (b) every file under a top-level
    root/hyvv-*.dist-info dir whose dir name differs from the new wheel's dist-info
    dir. Every candidate passes the member-name and guard rules before anything is
    removed (any failure raises ValueError and removes nothing). Symlinks are never
    followed: a symlinked dist-info dir raises ValueError. Missing files are not
    errors. After a removal, now-empty parent dirs inside root are removed (never
    root itself).
    """
    root = Path(root)
    runtime = set(runtime_files(wheel_path))
    keep_dist_info = _wheel_dist_info_dir(wheel_path)
    candidates = set()
    for name in _receipt_file_names(root):
        if name not in runtime:
            candidates.add(name)
    for dist in _dist_info_dirs(root):
        if dist.name == keep_dist_info:
            continue
        candidates.update(_files_under(root, dist))
    for name in sorted(candidates):
        _check_candidate_name(name)
    removed = []
    for name in sorted(candidates):
        target = _dest_for(root, name)
        if target.is_file() or target.is_symlink():
            target.unlink()
            removed.append(name)
            _remove_empty_parents(root, target.parent)
    return sorted(removed)


# The smoke child runs isolated (python -I -c CODE <root>) with the deploy root first
# on sys.path. It imports launcher, proves it came from the root, loads the ticket
# schema, then imports every top-level runtime module and every hyvv submodule. hyvv
# is imported honestly like any other module: the wheel's dist-info ships in the
# runtime set, so on a real install hyvv/__init__ resolves its version from that
# metadata, and when the metadata is missing the import fails and smoke says so
# (no faked dist-info, no temp package). Isolation does not hide the interpreter's
# own installed distributions, so importing hyvv is not proof enough: the dist-info
# importlib.metadata resolves for hyvv, and hyvv.__file__ itself, must both live
# under the deploy root, or an interpreter whose environment carries hyvv would
# make a metadata-less root look importable. Every imported module's __file__ must
# still live under the root.
_SMOKE_CHILD = r"""
import importlib, importlib.metadata, json, sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
result = {"ok": False, "ticket_schema": "unknown", "rizzstack_validators": "absent",
          "brief_schema": "n/a", "brief_validation": "n/a",
          "modules": {}, "hyvv_version": None, "error": ""}
errors = []

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

def check_hyvv_origin(mod):
    where = Path(getattr(mod, "__file__", "") or "").resolve()
    if where != root and root not in where.parents:
        return "hyvv loaded from %s, outside the deploy root" % getattr(mod, "__file__", "?")
    try:
        dist = importlib.metadata.distribution("hyvv")
    except Exception as exc:
        return "hyvv metadata not found: %s" % exc
    base = Path(dist.locate_file("")).resolve()
    if base == root or root in base.parents:
        return ""
    dist_info = Path(getattr(dist, "_path", base)).resolve()
    if dist_info == root or root in dist_info.parents:
        return ""
    return "hyvv metadata resolved outside the deploy root"

# A launcher that validates briefs (B4 on) must load the deployed brief schema and its
# vendored validator must answer: a "unavailable" status is what a broken deploy looks
# like at runtime, so it fails smoke (and with it, the deploy). A launcher that predates
# brief validation (a rollback target) reports n/a for both.
BRIEF_PROBE = {"schema": "rizzstack.brief/1"}

def check_brief(launcher):
    if not callable(getattr(launcher, "brief_validation", None)):
        return
    try:
        schema = json.loads((root / "schemas" / "rizzstack-brief.schema.json").read_text(encoding="utf-8"))
        props = schema.get("properties") if isinstance(schema, dict) else None
        if isinstance(props, dict) and props:
            result["brief_schema"] = "ok"
        else:
            result["brief_schema"] = "no properties"
            errors.append("brief schema has no properties")
    except Exception as exc:
        result["brief_schema"] = "%s: %s" % (type(exc).__name__, exc)
        errors.append("brief schema: %s" % exc)
    try:
        report = launcher.brief_validation(dict(BRIEF_PROBE))
        status = str(report.get("status")) if isinstance(report, dict) else "not a dict"
    except Exception as exc:
        status = "error: %s: %s" % (type(exc).__name__, exc)
    result["brief_validation"] = status
    if status not in ("valid", "invalid"):
        errors.append("brief_validation status is %s after deploy" % status)

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
    check_brief(launcher)

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
        mod = note("hyvv" if f.name == "__init__.py" else "hyvv." + f.name[:-3])
        if f.name == "__init__.py" and mod is not None:
            try:
                result["hyvv_version"] = str(mod.__version__)
            except Exception:
                result["hyvv_version"] = None
            origin_error = check_hyvv_origin(mod)
            if origin_error:
                result["modules"]["hyvv"] = "error: " + origin_error
                errors.append(origin_error)
if (pkg / "rizzstack_validators.py").is_file():
    result["rizzstack_validators"] = "present"

result["ok"] = not errors and result["ticket_schema"] == "ok"
result["error"] = "; ".join(errors)
print(json.dumps(result))
"""


def _run_smoke_child(exe, root, timeout):
    """Run the isolated smoke child; return (proc, error) with error set on failure."""
    try:
        proc = subprocess.run([exe, "-I", "-c", _SMOKE_CHILD, str(root)], cwd=str(root),
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "smoke child failed to run: %s" % exc
    return proc, ""


def _parse_smoke_child_output(proc):
    """Parse the child's one JSON result line; return (data, error) on failure."""
    line = ""
    for candidate in proc.stdout.splitlines():
        if candidate.strip():
            line = candidate
    try:
        data = json.loads(line)
    except ValueError:
        detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:300]
        return None, "smoke child printed no result (exit %s): %s" % (proc.returncode, detail)
    if not isinstance(data, dict):
        return None, "smoke child result is not an object"
    return data, ""


def _smoke_result_fields(data):
    """Normalize the child's result object into the smoke() result fields."""
    modules = data.get("modules")
    hyvv_version = data.get("hyvv_version")
    return {"ok": bool(data.get("ok")),
            "ticket_schema": str(data.get("ticket_schema", "unknown")),
            "rizzstack_validators": str(data.get("rizzstack_validators", "unknown")),
            "brief_schema": str(data.get("brief_schema", "unknown")),
            "brief_validation": str(data.get("brief_validation", "unknown")),
            "modules": modules if isinstance(modules, dict) else {},
            "hyvv_version": str(hyvv_version) if hyvv_version else None,
            "error": str(data.get("error", ""))}


def smoke(root, python=None, timeout=180):
    """Run the isolated child against root and parse its one JSON result line.

    Never raises: any failure to run or parse the child lands in 'error' with
    ok False. ok means launcher and every runtime module imported from under root,
    the ticket schema loaded as a dict, hyvv's module and dist-info metadata both
    resolved from under root, and (for a launcher with brief validation) the brief
    schema loaded and brief_validation answered valid/invalid, never "unavailable".
    """
    result = {"ok": False, "ticket_schema": "unknown", "rizzstack_validators": "unknown",
              "brief_schema": "unknown", "brief_validation": "unknown",
              "modules": {}, "hyvv_version": None, "error": ""}
    exe = python or sys.executable
    # The child runs with cwd=root, so hand it an absolute root (a relative one would
    # resolve against itself).
    root = Path(root).resolve()
    proc, error = _run_smoke_child(exe, root, timeout)
    if proc is None:
        result["error"] = error
        return result
    data, error = _parse_smoke_child_output(proc)
    if data is None:
        result["error"] = error
        return result
    result.update(_smoke_result_fields(data))
    if proc.returncode != 0 and result["ok"]:
        result["ok"] = False
        result["error"] = (result["error"] + " child exit %s" % proc.returncode).strip()
    return result


def _one_line(text):
    return re.sub(r"\s+", " ", str(text)).strip()


def _build_parser():
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

    p = sub.add_parser("prune", help="remove payload files the new wheel no longer ships")
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
    return ap


def _cmd_check_version(args):
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


def _cmd_files(args):
    for name in runtime_files(args.wheel):
        print(name)
    return 0


def _cmd_install(args):
    names = install(args.wheel, args.root)
    print("installed=%d" % len(names))
    return 0


def _cmd_prune(args):
    removed = prune(args.wheel, args.root)
    print("pruned=%d" % len(removed))
    for name in removed:
        print(name)
    return 0


def _cmd_verify_root(args):
    problems = verify_root(args.wheel, args.root)
    if not problems:
        print("verify_root=ok files=%d" % len(runtime_files(args.wheel)))
        return 0
    print("verify_root=fail")
    for problem in problems:
        print(problem)
    return 1


def _cmd_backup(args):
    dest = backup(args.wheel, args.root, args.dest)
    print("backup=%s" % dest)
    return 0


def _cmd_restore(args):
    restore(args.backup, args.root)
    print("restore=ok")
    return 0


def _cmd_smoke(args):
    result = smoke(args.root, python=args.python)
    print("smoke=%s ticket_schema=%s rizzstack_validators=%s brief_schema=%s brief_validation=%s"
          " modules=%d hyvv_version=%s" % (
        "ok" if result["ok"] else "fail", _one_line(result["ticket_schema"]),
        result["rizzstack_validators"], _one_line(result["brief_schema"]),
        _one_line(result["brief_validation"]), len(result["modules"]),
        result["hyvv_version"] if result["hyvv_version"] else "none"))
    if not result["ok"]:
        print("error=%s" % _one_line(result["error"]))
    return 0 if result["ok"] else 1


_COMMANDS = {
    "check-version": _cmd_check_version,
    "files": _cmd_files,
    "install": _cmd_install,
    "prune": _cmd_prune,
    "verify-root": _cmd_verify_root,
    "backup": _cmd_backup,
    "restore": _cmd_restore,
    "smoke": _cmd_smoke,
}


def main(argv=None):
    args = _build_parser().parse_args(argv)
    handler = _COMMANDS.get(args.cmd)
    if handler is None:
        return 2
    try:
        return handler(args)
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        print("error=%s" % _one_line(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
