"""The runtime ticket schema must ship in every install artifact C2-PKG builds.

C2 makes ``launcher._ticket_schema()`` read
``schemas/rizzstack-ticket.schema.json`` beside ``launcher.py`` at request time.
The defect this file pins: no packaging path carried it. A wheel built from the
tree had 25 entries and none under ``schemas/``; the sdist include list, the
wheel force-include map and the Windows ``$payload`` all omitted it; and
``install.sh`` proved only that the runtime modules import, so an installed
launcher failed with ``FileNotFoundError`` on its first ticket-carrying submit
while the checkout-resident suite stayed green.

Each class is one packaging surface, so a fix that covers three of four fails
by name rather than by an unexplained integration red:

* ``Declarations`` parses what the build and the installers declare.
* ``CopiedPayload`` materialises the Windows payload into a temp prefix and
  proves the launcher loads the schema from there with the checkout absent.
* ``BuiltArtifacts`` builds the real wheel when a build tool is available and
  proves the schema is in it and loads from the extracted wheel.
"""
from __future__ import annotations

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

import tomllib

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_REL = "schemas/rizzstack-ticket.schema.json"
SCHEMA_DIR = "schemas"
SCHEMA_TITLE = "rizzstack.ticket/1"
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
VERSION = PYPROJECT["project"]["version"]


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)


def payload_names(text: str, variable: str = "payload") -> list[str]:
    match = re.search(rf"\${re.escape(variable)} = @\((.*?)\)", text, re.S)
    if not match:
        raise AssertionError(f"{variable} array not found")
    return re.findall(r'"([^"]+)"', match.group(1))


def clean_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"}}
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def copy_payload_entry(prefix: Path, rel: str) -> None:
    """Mirror install.ps1 Copy-Payload: flat files, package directories recursively."""
    source = ROOT / rel
    target = prefix / rel
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def assert_schema_loads_from(test: unittest.TestCase, root: Path) -> None:
    """Prove the launcher resolves the ticket schema from *root*, not the checkout."""
    code = (
        "import json, launcher\n"
        "schema = launcher._ticket_schema()\n"
        "print(json.dumps({'launcher': launcher.__file__, 'title': schema.get('title')}))\n"
    )
    result = run([sys.executable, "-c", code], cwd=root, env=clean_env())
    test.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    test.assertEqual(Path(observed["launcher"]).resolve().parent, root.resolve(),
                     f"launcher did not import from the install root: {observed['launcher']}")
    test.assertEqual(observed["title"], SCHEMA_TITLE)


class Declarations(unittest.TestCase):
    """What the build backend and the installers SAY they ship."""

    def test_sdist_include_carries_the_runtime_schema(self):
        include = PYPROJECT["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
        covered = any(
            entry in (SCHEMA_DIR, SCHEMA_DIR + "/**", SCHEMA_REL)
            for entry in include
        )
        self.assertTrue(covered, f"sdist include omits {SCHEMA_REL}: {include}")

    def test_wheel_force_include_maps_the_runtime_schema(self):
        force = PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        self.assertEqual(force.get(SCHEMA_REL), SCHEMA_REL,
                         f"wheel force-include omits {SCHEMA_REL}: {sorted(force)}")

    def test_windows_payload_copies_the_schemas_directory(self):
        names = payload_names((ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8"))
        self.assertTrue(
            SCHEMA_DIR in names or SCHEMA_REL in names,
            f"install.ps1 payload omits {SCHEMA_DIR}; a fresh copy and -Update would drop the schema",
        )
        self.assertTrue((ROOT / SCHEMA_REL).is_file(), f"{SCHEMA_REL} is missing from the tree")

    def test_install_sh_fails_closed_when_the_ticket_schema_cannot_load(self):
        text = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        probes = re.findall(r"-c '([^']+)'", text)
        schema_probe = [body for body in probes if "rizzstack-ticket" in body or "_ticket_schema" in body]
        self.assertTrue(schema_probe,
                        "install.sh has no post-install probe for schemas/rizzstack-ticket.schema.json")
        self.assertRegex(text, r"exit 1",
                         "install.sh does not fail closed after a failed schema probe")


class CopiedPayload(unittest.TestCase):
    """The Windows payload, materialised, must start the launcher from itself."""

    def test_a_copied_install_contains_the_schema_and_launcher_loads_it(self):
        names = payload_names((ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="hyvv-c2pkg-payload-") as tmp:
            prefix = Path(tmp) / "prefix"
            prefix.mkdir()
            for rel in names:
                copy_payload_entry(prefix, rel)
            installed = prefix / SCHEMA_REL
            self.assertTrue(installed.is_file(),
                            f"copied payload has no {SCHEMA_REL}: {sorted(p.name for p in prefix.iterdir())}")
            assert_schema_loads_from(self, prefix)


class BuiltArtifacts(unittest.TestCase):
    """The real wheel, when a build tool exists (CI and dev boxes have uv)."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="hyvv-c2pkg-artifacts-")
        cls.tmp = Path(cls._tmp.name)
        cls.uv = shutil.which("uv")
        if not cls.uv:
            raise unittest.SkipTest("uv is required to build the real wheel")
        cls.dist = cls.tmp / "dist"
        cls.dist.mkdir()
        result = run([cls.uv, "build", "--wheel", "--out-dir", str(cls.dist), "--clear", str(ROOT)],
                     cwd=cls.tmp)
        if result.returncode != 0:
            raise AssertionError(f"wheel build failed\n{result.stdout}\n{result.stderr}")
        cls.wheel = cls.dist / f"hyvv-{VERSION}-py3-none-any.whl"
        assert cls.wheel.is_file(), f"missing {cls.wheel}"

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_wheel_carries_the_runtime_schema(self):
        with zipfile.ZipFile(self.wheel) as archive:
            names = set(archive.namelist())
        self.assertIn(SCHEMA_REL, names,
                      f"{SCHEMA_REL} missing from the wheel: {sorted(n for n in names if 'schema' in n)}")

    def test_an_extracted_wheel_starts_the_launcher_and_loads_the_schema(self):
        unpacked = self.tmp / "unpacked"
        with zipfile.ZipFile(self.wheel) as archive:
            archive.extractall(unpacked)
        self.assertTrue((unpacked / SCHEMA_REL).is_file(), f"extracted wheel has no {SCHEMA_REL}")
        assert_schema_loads_from(self, unpacked)


if __name__ == "__main__":
    unittest.main()
