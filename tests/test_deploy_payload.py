"""apply-update deploys the built wheel's runtime files and proves the build by its sha.

Regression (worker PC, 2026-11-27): two apply-update runs to 521e9bc installed, restarted and
then reverted, because step 6 compared the running launcher's ``0.1.19+g521e9bc`` to the bare
``VERSION = "0.1.19"``. The same runs copied a hand-kept list of six files, so ``schemas/``,
``contract/v1/schemas/`` and the rails modules never reached the install directory.

These tests pin ``scripts/deploy_payload.py`` (the stdlib helper launcher-admin.ps1 runs from
its own scripts directory, never from the checkout) and the apply-update wiring in
``scripts/launcher-admin.ps1``.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "deploy_payload.py"
ADMIN = ROOT / "scripts" / "launcher-admin.ps1"

TARGET = "521e9bc319c9d1d7c7c3b36ba33189192a6fd963"
OLD = "7f73b7c8e16669d7240ce42b936dad3acae25203"
OTHER = "93acd11aa0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5"

DESCRIBE_LAUNCHER = (
    'VERSION = "0.1.19"\n\n'
    "def build_identity(root=None, cfg=None):\n    return {}\n\n"
    "def build_version(root=None, cfg=None):\n    return VERSION\n\n"
    'server_version = "orbit-launcher/" + build_version()\n'
)
PRE_DESCRIBE_LAUNCHER = (
    'VERSION = "0.1.19"\n\n'
    "class Handler:\n"
    '    server_version = "orbit-launcher/" + VERSION\n'
)


def load_helper():
    spec = importlib.util.spec_from_file_location("deploy_payload", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(cmd, *, cwd=None, env=None, timeout=300):
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)


def write_wheel(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


SYNTH = {
    "launcher.py": b'VERSION = "0.1.19"\n',
    "provisioning.py": b"X = 1\n",
    "schemas/rizzstack-ticket.schema.json": b'{"title": "ticket"}\n',
    "contract/v1/schemas/attempt.json": b'{"title": "attempt"}\n',
    "hyvv/__init__.py": b"",
    "hyvv/config.example.json": b"{}\n",
    "hyvv-0.1.19.dist-info/METADATA": b"Name: hyvv\n",
    "hyvv-0.1.19.dist-info/RECORD": b"",
}


class VersionCheck(unittest.TestCase):
    def setUp(self):
        self.dp = load_helper()

    def test_parse_running_version_forms(self):
        p = self.dp.parse_running_version
        self.assertEqual(p("0.1.19"), {"base": "0.1.19", "count": None, "sha": None})
        self.assertEqual(p("0.1.19+g521e9bc"), {"base": "0.1.19", "count": None, "sha": "521e9bc"})
        self.assertEqual(p("0.1.19+25.g521e9bc"), {"base": "0.1.19", "count": 25, "sha": "521e9bc"})
        for bad in ("", None, "0.1.19+unknown", "0.1.19+g521e9bc.dirty", "0.1.19+dirty",
                    "0.1.19+g52e9", "v0.1.19", "0.1.19+gXYZ1234", "0.1.19 +g521e9bc"):
            self.assertIsNone(p(bad), bad)

    def test_describe_capability_comes_from_the_target_launcher(self):
        self.assertTrue(self.dp.is_describe_capable(DESCRIBE_LAUNCHER))
        self.assertFalse(self.dp.is_describe_capable(PRE_DESCRIBE_LAUNCHER))

    def test_521e9bc_style_launcher_passes(self):
        ok, reason = self.dp.check_version("0.1.19+g521e9bc", "0.1.19", TARGET, True)
        self.assertTrue(ok, reason)

    def test_commit_count_form_passes_when_sha_matches(self):
        ok, reason = self.dp.check_version("0.1.19+25.g521e9bc3", "0.1.19", TARGET, True)
        self.assertTrue(ok, reason)

    def test_wrong_sha_fails(self):
        ok, reason = self.dp.check_version("0.1.19+g7f73b7c", "0.1.19", TARGET, True)
        self.assertFalse(ok)
        self.assertIn("sha", reason)

    def test_wrong_base_fails(self):
        ok, _ = self.dp.check_version("0.1.18+g521e9bc", "0.1.19", TARGET, True)
        self.assertFalse(ok)

    def test_bare_version_on_a_describe_target_fails(self):
        ok, _ = self.dp.check_version("0.1.19", "0.1.19", TARGET, True)
        self.assertFalse(ok)

    def test_rollback_to_pre_describe_build_passes(self):
        ok, reason = self.dp.check_version("0.1.19", "0.1.19", OLD, False)
        self.assertTrue(ok, reason)

    def test_pre_describe_target_reporting_a_sha_fails(self):
        ok, _ = self.dp.check_version("0.1.19+g521e9bc", "0.1.19", OLD, False)
        self.assertFalse(ok)

    def test_unparseable_or_missing_running_version_fails(self):
        for running in (None, "", "0.1.19+g521e9bc.dirty", "0.1.19+unknown"):
            ok, _ = self.dp.check_version(running, "0.1.19", TARGET, True)
            self.assertFalse(ok, running)

    def test_target_must_be_a_full_sha(self):
        ok, _ = self.dp.check_version("0.1.19+g521e9bc", "0.1.19", "521e9bc", True)
        self.assertFalse(ok)

    def test_cli_check_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            new = Path(tmp) / "launcher-new.py"
            new.write_text(DESCRIBE_LAUNCHER, encoding="utf-8")
            old = Path(tmp) / "launcher-old.py"
            old.write_text(PRE_DESCRIBE_LAUNCHER, encoding="utf-8")
            base = [sys.executable, str(HELPER), "check-version", "--want", "0.1.19"]
            r = run(base + ["--running", "0.1.19+g521e9bc", "--target", TARGET, "--target-launcher", str(new)])
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("version_check=ok", r.stdout)
            r = run(base + ["--running", "0.1.19+g7f73b7c", "--target", TARGET, "--target-launcher", str(new)])
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("version_check=fail", r.stdout)
            r = run(base + ["--running", "0.1.19", "--target", OLD, "--target-launcher", str(old)])
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("version_check=ok", r.stdout)

    def test_real_launcher_with_receipt_reports_a_sha_that_verifies(self):
        """The installed launcher (no .git) names the receipt sha; that sha must verify."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "install"
            root.mkdir()
            shutil.copy2(ROOT / "launcher.py", root / "launcher.py")
            receipt = {"repo": "mojomast/hyvv", "sha": TARGET, "version": "0.1.19",
                       "installed_at_utc": "2026-11-27T13:00:00Z", "files": {}}
            (root / "hyvv-deployed.json").write_text(json.dumps(receipt), encoding="utf-8")
            code = ("import sys; sys.path.insert(0, sys.argv[1]); import launcher; "
                    "print(launcher.build_version())")
            r = run([sys.executable, "-c", code, str(root)], cwd=root)
            self.assertEqual(r.returncode, 0, r.stderr)
            running = r.stdout.strip().splitlines()[-1]
            want = re.search(r'^VERSION\s*=\s*"([^"]+)"', (ROOT / "launcher.py").read_text("utf-8"), re.M).group(1)
            describe = self.dp.is_describe_capable((ROOT / "launcher.py").read_text("utf-8"))
            self.assertTrue(describe)
            ok, reason = self.dp.check_version(running, want, TARGET, describe)
            self.assertTrue(ok, f"{running}: {reason}")
            ok, _ = self.dp.check_version(running, want, OTHER, describe)
            self.assertFalse(ok)


class WheelPayload(unittest.TestCase):
    def setUp(self):
        self.dp = load_helper()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.wheel = write_wheel(self.tmp / "hyvv-0.1.19-py3-none-any.whl", SYNTH)
        self.root = self.tmp / "root"
        self.root.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_runtime_files_come_from_the_wheel_with_its_metadata(self):
        """The dist-info ships too: hyvv/__init__ resolves its version from it on a host."""
        files = self.dp.runtime_files(self.wheel)
        self.assertEqual(files, sorted(SYNTH))

    def test_two_dist_info_dirs_are_refused(self):
        wheel = write_wheel(self.tmp / "two.whl", {**SYNTH, "other-1.0.dist-info/METADATA": b"Name: other\n"})
        with self.assertRaises(ValueError):
            self.dp.runtime_files(wheel)

    def test_unsafe_member_names_are_refused(self):
        for bad in ("../evil.py", "/abs.py", "C:/x.py", "a\\b.py", "schemas/../../x.json"):
            wheel = write_wheel(self.tmp / "bad.whl", {**SYNTH, bad: b"x"})
            with self.assertRaises(ValueError, msg=bad):
                self.dp.runtime_files(wheel)

    def test_guarded_paths_are_refused(self):
        for bad in ("scripts/launcher-admin.ps1", "config.json", "go.key", "launcher.token", "start.ps1"):
            wheel = write_wheel(self.tmp / "guarded.whl", {**SYNTH, bad: b"x"})
            with self.assertRaises(ValueError, msg=bad):
                self.dp.runtime_files(wheel)

    def test_install_then_verify_root(self):
        (self.root / "config.json").write_text("{}", encoding="utf-8")
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "start.ps1").write_text("# keep", encoding="utf-8")
        installed = self.dp.install(self.wheel, self.root)
        self.assertEqual(sorted(installed), self.dp.runtime_files(self.wheel))
        for name in installed:
            self.assertEqual((self.root / name).read_bytes(), SYNTH[name])
        self.assertEqual(self.dp.verify_root(self.wheel, self.root), [])
        self.assertEqual((self.root / "config.json").read_text("utf-8"), "{}")
        self.assertEqual((self.root / "scripts" / "start.ps1").read_text("utf-8"), "# keep")

    def test_verify_root_names_missing_and_changed_files(self):
        self.dp.install(self.wheel, self.root)
        (self.root / "schemas" / "rizzstack-ticket.schema.json").unlink()
        (self.root / "provisioning.py").write_text("X = 2\n", encoding="utf-8")
        problems = self.dp.verify_root(self.wheel, self.root)
        self.assertIn("missing: schemas/rizzstack-ticket.schema.json", problems)
        self.assertIn("mismatch: provisioning.py", problems)

    def test_backup_and_restore_return_the_exact_previous_tree(self):
        (self.root / "launcher.py").write_bytes(b"OLD\n")
        (self.root / "config.json").write_text("{}", encoding="utf-8")
        dest = self.tmp / "payload.bak"
        self.dp.backup(self.wheel, self.root, dest)
        self.assertTrue((dest / "manifest.json").is_file())
        self.dp.install(self.wheel, self.root)
        self.assertTrue((self.root / "schemas" / "rizzstack-ticket.schema.json").is_file())
        self.dp.restore(dest, self.root)
        self.assertEqual((self.root / "launcher.py").read_bytes(), b"OLD\n")
        self.assertFalse((self.root / "schemas" / "rizzstack-ticket.schema.json").exists())
        self.assertFalse((self.root / "provisioning.py").exists())
        self.assertEqual((self.root / "config.json").read_text("utf-8"), "{}")

    def test_prune_removes_what_the_new_wheel_no_longer_ships_and_restore_undoes_it(self):
        receipt = {"repo": "mojomast/hyvv", "sha": OLD, "version": "0.1.18",
                   "installed_at_utc": "2026-11-24T00:23:18Z",
                   "files": {"launcher.py": "x", "old_module.py": "x", "hyvv-0.1.18.dist-info/METADATA": "x"}}
        (self.root / "hyvv-deployed.json").write_text(json.dumps(receipt), encoding="utf-8")
        (self.root / "launcher.py").write_bytes(b"OLD\n")
        (self.root / "old_module.py").write_bytes(b"OLDMOD\n")
        stale = self.root / "hyvv-0.1.17.dist-info"
        stale.mkdir()
        (stale / "METADATA").write_bytes(b"Name: hyvv\nVersion: 0.1.17\n")
        (self.root / "hyvv-0.1.18.dist-info").mkdir()
        (self.root / "hyvv-0.1.18.dist-info" / "METADATA").write_bytes(b"Name: hyvv\nVersion: 0.1.18\n")
        (self.root / "config.json").write_text("{}", encoding="utf-8")
        dest = self.tmp / "payload.bak"
        self.dp.backup(self.wheel, self.root, dest)
        self.dp.install(self.wheel, self.root)
        removed = self.dp.prune(self.wheel, self.root)
        self.assertIn("old_module.py", removed)
        self.assertIn("hyvv-0.1.18.dist-info/METADATA", removed)
        self.assertIn("hyvv-0.1.17.dist-info/METADATA", removed)
        self.assertFalse((self.root / "old_module.py").exists())
        self.assertFalse(stale.exists())
        self.assertFalse((self.root / "hyvv-0.1.18.dist-info").exists())
        self.assertTrue((self.root / "hyvv-0.1.19.dist-info" / "METADATA").is_file())
        self.assertEqual((self.root / "config.json").read_text("utf-8"), "{}")
        self.assertEqual(self.dp.verify_root(self.wheel, self.root), [])
        self.dp.restore(dest, self.root)
        self.assertEqual((self.root / "launcher.py").read_bytes(), b"OLD\n")
        self.assertEqual((self.root / "old_module.py").read_bytes(), b"OLDMOD\n")
        self.assertTrue((stale / "METADATA").is_file())
        self.assertTrue((self.root / "hyvv-0.1.18.dist-info" / "METADATA").is_file())
        self.assertFalse((self.root / "hyvv-0.1.19.dist-info").exists())
        self.assertFalse((self.root / "schemas").exists())
        self.assertEqual((self.root / "config.json").read_text("utf-8"), "{}")

    def test_prune_refuses_guarded_receipt_names(self):
        receipt = {"repo": "mojomast/hyvv", "sha": OLD, "version": "0.1.19",
                   "installed_at_utc": "2026-11-24T00:23:18Z", "files": {"config.json": "x"}}
        (self.root / "hyvv-deployed.json").write_text(json.dumps(receipt), encoding="utf-8")
        (self.root / "config.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.dp.prune(self.wheel, self.root)
        self.assertTrue((self.root / "config.json").is_file())

    def test_cli_files_install_verify(self):
        r = run([sys.executable, str(HELPER), "files", "--wheel", str(self.wheel)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), self.dp.runtime_files(self.wheel))
        r = run([sys.executable, str(HELPER), "install", "--wheel", str(self.wheel), "--root", str(self.root)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = run([sys.executable, str(HELPER), "verify-root", "--wheel", str(self.wheel), "--root", str(self.root)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("verify_root=ok", r.stdout)
        (self.root / "launcher.py").unlink()
        r = run([sys.executable, str(HELPER), "verify-root", "--wheel", str(self.wheel), "--root", str(self.root)])
        self.assertEqual(r.returncode, 1)
        self.assertIn("verify_root=fail", r.stdout)
        self.assertIn("missing: launcher.py", r.stdout)


def _imported_names(tree: ast.AST) -> list[str]:
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.append(node.module)
            names += [f"{node.module}.{a.name}" for a in node.names]
    return names


def _local_file(root: Path, name: str) -> str | None:
    """The repo file a dotted import name resolves to (longest prefix), or None."""
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        for hit in (root.joinpath(*parts[:i]).with_suffix(".py"), root.joinpath(*parts[:i], "__init__.py")):
            if hit.is_file():
                rel = hit.relative_to(root).as_posix()
                return None if rel.startswith("tests") else rel
    return None


def local_import_closure(root: Path) -> set[str]:
    """Repo-relative .py files launcher.py reaches through imports of modules that live in this repo."""
    seen: set[str] = set()
    todo = ["launcher.py"]
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        for name in _imported_names(ast.parse((root / rel).read_text("utf-8"))):
            hit = _local_file(root, name)
            if hit:
                todo.append(hit)
    return seen


RUNTIME_DATA = (
    "schemas/rizzstack-ticket.schema.json",
    "schemas/rizzstack-brief.schema.json",
    "schemas/rizzstack-attempt.schema.json",
    "contract/v1/schemas/attempt.json",
    "brief.schema.json",
    "result.schema.json",
    "contrib/rizzstack_rails/vendor-manifest.json",
)


class BuiltWheelPayload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dp = load_helper()
        cls.uv = shutil.which("uv")
        if not cls.uv:
            raise unittest.SkipTest("uv is required to build the real wheel")
        cls._tmp = tempfile.TemporaryDirectory(prefix="hyvv-deploy-payload-")
        cls.tmp = Path(cls._tmp.name)
        dist = cls.tmp / "dist"
        r = run([cls.uv, "build", "--wheel", "--out-dir", str(dist), str(ROOT)], cwd=cls.tmp, timeout=600)
        if r.returncode != 0:
            raise AssertionError(f"wheel build failed\n{r.stdout}\n{r.stderr}")
        cls.wheel = next(dist.glob("hyvv-*.whl"))
        cls.files = cls.dp.runtime_files(cls.wheel)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_runtime_closure_of_launcher_is_in_the_wheel(self):
        closure = local_import_closure(ROOT)
        self.assertIn("provisioning.py", closure)
        self.assertIn("rails_emitter.py", closure)
        # launcher.py loads rizzstack_validators through its import fallback too, so the
        # AST walk sees it; name it explicitly anyway with the other B4 runtime file.
        closure |= {"hyvv/rizzstack_validators.py", "hyvv/ticket_verify.py"}
        needed = (closure | set(RUNTIME_DATA)
                  | {p.relative_to(ROOT).as_posix() for p in (ROOT / "schemas").rglob("*") if p.is_file()}
                  | {p.relative_to(ROOT).as_posix() for p in (ROOT / "contract" / "v1" / "schemas").glob("*.json")})
        missing = sorted(needed - set(self.files))
        self.assertEqual(missing, [], f"runtime files absent from the wheel: {missing}")

    def test_wheel_runtime_files_carry_no_scripts_and_one_metadata(self):
        self.assertFalse([f for f in self.files if f.startswith("scripts/")])
        metadata = [f for f in self.files if re.fullmatch(r"hyvv-[^/]+\.dist-info/METADATA", f)]
        self.assertEqual(len(metadata), 1, metadata)

    def test_installed_tree_passes_smoke(self):
        root = self.tmp / "smoke-ok"
        root.mkdir()
        self.dp.install(self.wheel, root)
        self.assertEqual(self.dp.verify_root(self.wheel, root), [])
        result = self.dp.smoke(root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["ticket_schema"], "ok")
        for mod in ("launcher", "provisioning", "landing_export", "rails_emitter", "rails_export",
                    "rails_log_source", "hyvv.ticket_verify"):
            self.assertIn(mod, result["modules"])
        self.assertIn("hyvv", result["modules"])
        version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text("utf-8"), re.M).group(1)
        self.assertEqual(result["hyvv_version"], version)
        self.assertEqual(result["rizzstack_validators"], "present")
        self.assertIn("hyvv.rizzstack_validators", result["modules"])
        self.assertEqual(result["brief_schema"], "ok")
        self.assertIn(result["brief_validation"], ("valid", "invalid"))

    def test_smoke_fails_without_the_brief_schema(self):
        """No brief schema means brief_validation answers "unavailable": a failed deploy."""
        root = self.tmp / "smoke-no-brief-schema"
        root.mkdir()
        self.dp.install(self.wheel, root)
        (root / "schemas" / "rizzstack-brief.schema.json").unlink()
        result = self.dp.smoke(root)
        self.assertFalse(result["ok"], result)
        self.assertNotEqual(result["brief_schema"], "ok")
        self.assertEqual(result["brief_validation"], "unavailable")
        self.assertIn("brief_validation status is unavailable", result["error"])

    def test_smoke_fails_when_brief_validation_is_unavailable(self):
        """The validator module missing from the deploy root: the launcher still imports,
        but brief_validation reports "unavailable", and smoke fails."""
        root = self.tmp / "smoke-no-validator"
        root.mkdir()
        self.dp.install(self.wheel, root)
        (root / "hyvv" / "rizzstack_validators.py").unlink()
        result = self.dp.smoke(root)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["modules"].get("launcher"), "ok")
        self.assertEqual(result["brief_validation"], "unavailable")

    def test_cli_smoke_fails_on_unavailable_brief_validation(self):
        root = self.tmp / "smoke-cli-unavailable"
        root.mkdir()
        self.dp.install(self.wheel, root)
        (root / "hyvv" / "rizzstack_validators.py").unlink()
        r = run([sys.executable, str(HELPER), "smoke", "--root", str(root)])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("smoke=fail", r.stdout)
        self.assertIn("brief_validation=unavailable", r.stdout)

    def test_smoke_fails_when_the_hyvv_metadata_is_missing(self):
        """No faked metadata: a host without the dist-info cannot import hyvv, and smoke says so."""
        root = self.tmp / "smoke-no-metadata"
        root.mkdir()
        self.dp.install(self.wheel, root)
        for d in root.glob("hyvv-*.dist-info"):
            shutil.rmtree(d)
        result = self.dp.smoke(root)
        self.assertFalse(result["ok"])
        self.assertIn("hyvv", result["error"])

    def test_smoke_fails_without_the_ticket_schema(self):
        root = self.tmp / "smoke-missing"
        root.mkdir()
        self.dp.install(self.wheel, root)
        (root / "schemas" / "rizzstack-ticket.schema.json").unlink()
        result = self.dp.smoke(root)
        self.assertFalse(result["ok"])
        self.assertNotEqual(result["ticket_schema"], "ok")

    def test_cli_smoke(self):
        root = self.tmp / "smoke-cli"
        root.mkdir()
        self.dp.install(self.wheel, root)
        r = run([sys.executable, str(HELPER), "smoke", "--root", str(root)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("smoke=ok", r.stdout)
        self.assertIn("ticket_schema=ok", r.stdout)
        self.assertIn("rizzstack_validators=present", r.stdout)
        self.assertIn("brief_schema=ok", r.stdout)
        self.assertRegex(r.stdout, r"brief_validation=(valid|invalid) ")


def function_body(text: str, name: str) -> str:
    m = re.search(rf"^function {re.escape(name)}\b.*?^}}", text, re.M | re.S)
    if not m:
        raise AssertionError(f"function {name} not found")
    return m.group(0)


class LauncherAdminApplyUpdate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = ADMIN.read_text("utf-8")

    def test_no_hand_kept_install_payload(self):
        self.assertNotRegex(self.text, r"\$InstallPayload\s*=\s*@\(")

    def test_helper_runs_from_the_admin_scripts_dir_not_the_checkout(self):
        self.assertRegex(self.text, r"Join-Path \$ScriptDir 'deploy_payload\.py'")
        self.assertNotRegex(self.text, r"Join-Path \$HyvvCheckout '?scripts")

    def test_apply_update_uses_the_helper_for_every_step(self):
        body = function_body(self.text, "Invoke-ApplyUpdate")
        for sub in ("check-version", "verify-root", "smoke", "backup", "restore", "install", "prune"):
            self.assertTrue(re.search(rf"'{sub}'", self.text), sub)
        self.assertIn("uv", self.text)
        self.assertRegex(self.text, r"'build'")
        self.assertIn("'--no-project'", self.text)
        install = function_body(self.text, "Invoke-Install")
        order = [install.find("'install'"), install.find("'prune'"), install.find("'verify-root'"), install.find("'smoke'")]
        self.assertTrue(all(i > -1 for i in order) and order == sorted(order), order)
        self.assertIn("Invoke-", body)

    def test_exact_version_compare_is_gone(self):
        self.assertNotIn("$probe.Version -ne $want", self.text)

    def test_outcome_is_printed_before_the_revert_restart(self):
        body = function_body(self.text, "Invoke-ApplyUpdate")
        first_outcome = body.find("Format-OutcomeLine 'failed'")
        self.assertGreater(first_outcome, -1)
        revert_restart = body.rfind("Invoke-RestartIdleCore")
        self.assertLess(first_outcome, revert_restart)
        self.assertIn("Format-OutcomeLine 'ok'", body)

    def test_post_restart_smoke_failure_reverts(self):
        """Step 6 smoke (brief schema + brief_validation) runs inside the try whose catch reverts."""
        body = function_body(self.text, "Invoke-ApplyUpdate")
        smoke = body.find("post-restart smoke failed")
        catch = body.find("} catch {", smoke)
        self.assertGreater(smoke, body.find("Invoke-RestartIdleCore"))
        self.assertGreater(catch, smoke)
        self.assertIn("Write-Phase 'reverting'", body[catch:])
        self.assertIn("Restore-Payload", body[catch:])

    def test_every_outcome_line_names_deployed_version_build_sha_and_revert(self):
        body = function_body(self.text, "Invoke-ApplyUpdate")
        self.assertNotRegex(body, r'Write-Output "outcome=')
        self.assertEqual(len(re.findall(r"Format-OutcomeLine '(ok|failed)'", body)), 3)
        fmt = function_body(self.text, "Format-OutcomeLine")
        for key in ("deployed=", "version=", "build_sha=", "revert="):
            self.assertIn(key, fmt)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh is required to run the outcome formatter")
    def test_outcome_line_carries_the_build_sha(self):
        fns = function_body(self.text, "Get-BuildSha") + "\n" + function_body(self.text, "Format-OutcomeLine")
        cmd = (fns + "\nFormat-OutcomeLine 'ok' '%s' '0.1.19+g521e9bc' 'none' ''"
               "\nFormat-OutcomeLine 'failed' 'none' '' 'pending' 'target=x why it broke'"
               "\nFormat-OutcomeLine 'failed' '%s' '0.1.19' 'ok' ''" % (TARGET, OLD))
        r = run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", cmd])
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(lines[0], f"outcome=ok deployed={TARGET} version=0.1.19+g521e9bc build_sha=521e9bc revert=none")
        self.assertEqual(lines[1], "outcome=failed deployed=none version=unknown build_sha=none revert=pending reason=target=x why it broke")
        self.assertEqual(lines[2], f"outcome=failed deployed={OLD} version=0.1.19 build_sha=none revert=ok")

    def test_update_status_is_an_allowed_read_only_op(self):
        allowed = re.search(r"\$AllowedOps = @\(([^)]*)\)", self.text).group(1)
        self.assertIn("'update-status'", allowed)
        body = function_body(self.text, "Invoke-UpdateStatus")
        for verb in ("Acquire-AdminLock", "Write-Audit", "Set-Content", "Remove-Item", "Copy-Item",
                     "Move-Item", "Add-Content", "Stop-Process", "start.ps1", "stop.ps1"):
            self.assertNotIn(verb, body, verb)

    def test_no_personal_paths(self):
        self.assertNotRegex(self.text, r"(?i)C:\\Users\\")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh is required to parse the admin script")
    def test_script_parses(self):
        cmd = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
               f"'{ADMIN}', [ref]$null, [ref]$e); $e.Count")
        r = run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", cmd])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0", r.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh is required to run update-status")
    def test_update_status_reports_last_outcome_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            data = home / ".orbit-launcher"
            data.mkdir()
            audit = data / "admin-audit.jsonl"
            rec = {"ts": "2026-11-27T08:12:45-05:00", "op": "apply-update", "ok": False,
                   "detail": "failed: running launcher reports version '0.1.19+g521e9bc', expected 0.1.19; revert=ok",
                   "backup": None, "args": {"new_sha": TARGET}, "user": "u", "src": "ssh"}
            audit.write_text(json.dumps({"ts": "x", "op": "status", "ok": True}) + "\n" + json.dumps(rec) + "\n",
                             encoding="utf-8")
            state = data / "apply-update.state.json"
            state.write_text(json.dumps({"op": "apply-update", "target": TARGET, "old_sha": OLD,
                                         "phase": "reverted", "outcome": "failed",
                                         "deployed": OLD, "version": "0.1.19", "build_sha": "none",
                                         "revert": "ok",
                                         "started_at": "2026-11-27T12:58:37Z",
                                         "updated_at": "2026-11-27T13:12:45Z"}), encoding="utf-8")
            before = {p.name: p.read_bytes() for p in data.iterdir()}
            env = {**os.environ, "USERPROFILE": str(home), "HOME": str(home)}
            env.pop("SSH_ORIGINAL_COMMAND", None)
            r = run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(ADMIN), "update-status"], env=env, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            out = r.stdout
            self.assertIn("ok=yes op=update-status", out)
            self.assertIn("state_phase=reverted", out)
            self.assertIn(f"state_target={TARGET}", out)
            self.assertIn("state_outcome=failed", out)
            self.assertIn(f"state_deployed={OLD}", out)
            self.assertIn("state_version=0.1.19", out)
            self.assertIn("state_build_sha=none", out)
            self.assertIn("state_revert=ok", out)
            self.assertIn("last_apply_update_ok=no", out)
            self.assertIn("last_apply_update_detail=failed: running launcher reports version", out)
            self.assertIn("lock_held=no", out)
            after = {p.name: p.read_bytes() for p in data.iterdir()}
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
