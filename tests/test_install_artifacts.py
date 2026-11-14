"""Installer and package payloads must carry launcher's adjacent runtime modules.

The regression: provisioning.py existed in the checkout but was absent from the
Windows copy payload and from the sdist/wheel, so an installed launcher imported
successfully with ``launcher._provisioning is None``. These tests inspect the
supported payload definitions, build the real artifacts outside the checkout,
install the wheel into an isolated venv, and prove provisioning initialises with
an enabling config. Hashes are emitted so the evidence is artifact identity, not
just a version string.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
MODULES = ("provisioning.py", "landing_export.py")
VERSION = PYPROJECT["project"]["version"]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)


def payload_names(text: str, variable: str) -> list[str]:
    match = re.search(rf"\${re.escape(variable)} = @\((.*?)\)", text, re.S)
    if not match:
        raise AssertionError(f"{variable} array not found")
    return re.findall(r'"([^"]+)"', match.group(1))


class PayloadDefinitions(unittest.TestCase):
    def test_windows_payload_copies_provisioning_and_landing_export(self):
        text = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
        names = payload_names(text, "payload")
        for module in MODULES:
            self.assertIn(module, names, f"install.ps1 payload omits {module}")
            self.assertTrue((ROOT / module).is_file(), f"install.ps1 payload references missing {module}")
        self.assertIn("launcher.py", names)
        self.assertIn("provisioning.py", names)

    def test_pyproject_sdist_and_wheel_include_both_modules(self):
        build = PYPROJECT["tool"]["hatch"]["build"]["targets"]
        sdist_include = build["sdist"]["include"]
        force_include = build["wheel"]["force-include"]
        for module in MODULES:
            self.assertIn(module, sdist_include, f"sdist include omits {module}")
            self.assertEqual(force_include.get(module), module, f"wheel force-include omits {module}")

    def test_install_sh_verifies_installed_runtime_imports(self):
        text = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("import launcher, provisioning, landing_export", text)

    @unittest.skipUnless(shutil.which("sh"), "sh is required")
    def test_install_sh_dry_run_still_succeeds(self):
        result = run(["sh", str(ROOT / "scripts" / "install.sh"), "--dry-run"], cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("dry-run:", result.stdout)


class BuiltArtifacts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="hyvv-install-artifacts-")
        cls.tmp = Path(cls._tmp.name)
        cls.uv = shutil.which("uv")
        if not cls.uv:
            raise unittest.SkipTest("uv is required to build the real artifacts")
        cls.dist = cls.tmp / "dist"
        cls.dist.mkdir()
        result = run([cls.uv, "build", "--out-dir", str(cls.dist), "--clear", str(ROOT)], cwd=cls.tmp)
        if result.returncode != 0:
            raise AssertionError(f"artifact build failed\n{result.stdout}\n{result.stderr}")
        cls.sdist = cls.dist / f"hyvv-{VERSION}.tar.gz"
        cls.wheel = cls.dist / f"hyvv-{VERSION}-py3-none-any.whl"
        assert cls.sdist.is_file(), f"missing {cls.sdist}"
        assert cls.wheel.is_file(), f"missing {cls.wheel}"
        try:
            git_rev = run(["git", "rev-parse", "HEAD"], cwd=ROOT).stdout.strip()
        except OSError:
            git_rev = ""
        cls.identity = {
            "version": VERSION,
            "git_rev": git_rev,
            "build_backend": PYPROJECT["build-system"]["build-backend"],
            "sdist": {"name": cls.sdist.name, "sha256": sha256(cls.sdist)},
            "wheel": {"name": cls.wheel.name, "sha256": sha256(cls.wheel)},
            "source_modules": {name: sha256(ROOT / name) for name in ("launcher.py", *MODULES)},
        }

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_artifact_identity_is_sha256_not_only_version(self):
        for kind in ("sdist", "wheel"):
            digest = self.identity[kind]["sha256"]
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
            self.assertNotEqual(digest, self.identity["version"])
        for name, digest in self.identity["source_modules"].items():
            self.assertRegex(digest, r"^[0-9a-f]{64}$", name)
        print("ARTIFACT_IDENTITY " + json.dumps(self.identity, sort_keys=True))

    def test_sdist_contains_both_modules_at_payload_root(self):
        with tarfile.open(self.sdist) as archive:
            names = {Path(name).name for name in archive.getnames() if "/" in name}
            roots = {tuple(Path(name).parts[:1]) for name in archive.getnames() if name.endswith(".py")}
        for module in ("launcher.py", *MODULES):
            self.assertIn(module, names, f"{module} missing from sdist")
        self.assertEqual(len(roots), 1, f"sdist must have one top-level directory: {roots}")

    def test_wheel_contains_both_modules_at_payload_root(self):
        with zipfile.ZipFile(self.wheel) as archive:
            names = set(archive.namelist())
        for module in ("launcher.py", *MODULES):
            self.assertIn(module, names, f"{module} missing from wheel")
        self.assertIn(f"hyvv-{VERSION}.dist-info/METADATA", names)

    def test_isolated_install_imports_launcher_with_provisioning(self):
        venv = self.tmp / "venv"
        result = run([self.uv, "venv", "--python", f"{sys.version_info.major}.{sys.version_info.minor}", str(venv)], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        result = run([self.uv, "pip", "install", "--python", str(python), "--no-deps", str(self.wheel)], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        workspace = self.tmp / "isolated-run"
        workspace.mkdir()
        data_dir = self.tmp / "host-data"
        policy = data_dir / "provisioning-policy.json"
        policy.parent.mkdir(parents=True)
        policy.write_text(json.dumps({
            "schema": "rizzstack.provisioning/1",
            "profiles": {
                "smoke": {
                    "repo_patterns": ["mojomast/rizzstack-smoke"],
                    "workspace_root": str(self.tmp / "workspaces"),
                    "max_provisioned": 2,
                    "ttl_hours": 1,
                    "allow_cleanup": True,
                    "git_actions": ["clone", "fetch", "checkout"],
                    "task_scopes": ["smoke/answer.py"],
                }
            },
        }), encoding="utf-8")
        config = data_dir / "config.json"
        config.write_text(json.dumps({
            "bind": "127.0.0.1",
            "port": 47821,
            "data_dir": str(data_dir),
            "projects": {},
            "provisioning": {
                "enabled": True,
                "policy": str(policy),
                "journal": str(data_dir / "provisioning"),
            },
        }), encoding="utf-8")

        probe = workspace / "probe.py"
        probe.write_text(
            "import json, sys\n"
            "import launcher, provisioning, landing_export\n"
            "cfg = launcher.load_config(sys.argv[1])\n"
            "service = launcher.build_provisioner(cfg)\n"
            "print(json.dumps({\n"
            "    'launcher': launcher.__file__,\n"
            "    'provisioning_module': provisioning.__file__,\n"
            "    'landing_export_module': landing_export.__file__,\n"
            "    'provisioning_loaded': launcher._provisioning is not None,\n"
            "    'provisioning_enabled': bool((cfg.get('provisioning') or {}).get('enabled')),\n"
            "    'service': type(service).__name__ if service is not None else None,\n"
            "    'sys_path': sys.path,\n"
            "}))\n",
            encoding="utf-8",
        )
        env = {key: value for key, value in os.environ.items()
               if key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"}}
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        result = run([str(python), str(probe), str(config)], cwd=workspace, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        observed = json.loads(result.stdout.strip().splitlines()[-1])

        self.assertTrue(observed["provisioning_loaded"], "launcher._provisioning is None")
        self.assertTrue(observed["provisioning_enabled"])
        self.assertEqual(observed["service"], "Provisioner")
        repo = ROOT.resolve()
        for key in ("launcher", "provisioning_module", "landing_export_module"):
            installed = Path(observed[key]).resolve()
            self.assertNotIn(repo, installed.parents, f"{key} leaked from the checkout: {installed}")
            self.assertTrue(installed.is_file())
        self.assertNotIn(str(repo), observed["sys_path"], "checkout leaked onto the isolated sys.path")


if __name__ == "__main__":
    unittest.main()
