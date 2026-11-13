"""Packaging metadata, runtime and installer examples must agree, and the
release-asset build must keep dist/ PyPI-clean. No tagging or publishing."""
from __future__ import annotations

import importlib.util
import io
import re
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
NAME = PYPROJECT["project"]["name"]
VERSION = PYPROJECT["project"]["version"]


def _load_script():
    spec = importlib.util.spec_from_file_location("build_release_assets", ROOT / "scripts/build_release_assets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class MetadataAgrees(unittest.TestCase):
    def test_name_and_entry_point(self):
        self.assertEqual(NAME, "hyvv")
        self.assertEqual(PYPROJECT["project"]["scripts"], {"hyvv": "hyvv.cli:main"})
        mod, func = PYPROJECT["project"]["scripts"]["hyvv"].split(":")
        target = getattr(__import__(mod, fromlist=[func]), func)
        self.assertTrue(callable(target))

    def test_runtime_versions_match_pyproject(self):
        import hyvv
        self.assertEqual(hyvv.__version__, VERSION)
        self.assertRegex(_read("launcher.py"), rf'(?m)^VERSION = "{re.escape(VERSION)}"$')
        from hyvv.cli import main
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main(["version"]), 0)
        self.assertEqual(buf.getvalue().strip(), VERSION)

    def test_install_sh_defaults_and_flags(self):
        sh = _read("scripts/install.sh")
        self.assertRegex(sh, rf'(?m)^version="{re.escape(VERSION)}"$')
        self.assertIn(f'"{NAME}==$version"', sh)
        self.assertIn("/.venv/bin/hyvv", sh)  # the entry-point name
        # Fallback config path matches the wheel's force-include target.
        fi = PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
        self.assertEqual(fi["config.example.json"], "hyvv/config.example.json")
        self.assertIn("site-packages/hyvv/config.example.json", sh)
        accepted = set(re.findall(r"^\s+(--[a-z-]+)\)", sh, re.M))
        for doc in ("README.md", "docs/DEPLOYMENT.md"):
            for line in re.findall(r"(?m)^.*install\.sh\b.*$", _read(doc)):
                if "curl" in line or "cosign" in line:
                    continue
                for flag in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", line):
                    self.assertIn(flag, accepted, f"{doc}: `{line.strip()}` uses {flag}")

    def test_install_ps1_examples_and_payload(self):
        ps = _read("scripts/install.ps1")
        params = {p.lower() for p in re.findall(r"\[(?:string|switch|int)\]\$(\w+)", ps)}
        for doc in ("README.md", "scripts/install.ps1", "docs/DEPLOYMENT.md"):
            for line in re.findall(r"(?m)^.*install\.ps1\b.*$", _read(doc)):
                for flag in re.findall(r"(?<![\w-])-([A-Z][A-Za-z]+)", line):
                    self.assertIn(flag.lower(), params, f"{doc}: `{line.strip()}` uses -{flag}")
        block = re.search(r"\$payload = @\((.*?)\)", ps, re.S).group(1)
        for rel in re.findall(r'"([^"]+)"', block):
            self.assertTrue((ROOT / rel).is_file(), f"install.ps1 payload {rel} missing")

    def test_readme_install_examples(self):
        readme = _read("README.md")
        self.assertIn(f"uv tool install {NAME}", readme)
        self.assertIn("uv run hyvv version", readme)
        self.assertNotIn("exits 2 until that installer ships", readme)


class ReleaseAssets(unittest.TestCase):
    def _fake_dist(self, d: Path, extra: str | None = None):
        sdist, wheel = _load_script().expected_dist_names(NAME, VERSION)
        with tarfile.open(d / sdist, "w:gz"):
            pass
        with zipfile.ZipFile(d / wheel, "w") as z:
            z.writestr("hyvv/__init__.py", f'__version__ = "{VERSION}"\n')
        if extra:
            (d / extra).write_text("x", encoding="utf-8")

    def test_build_writes_installers_and_checksums_outside_dist(self):
        mod = _load_script()
        with tempfile.TemporaryDirectory() as t:
            dist, out = Path(t, "dist"), Path(t, "release-assets")
            dist.mkdir()
            self._fake_dist(dist)
            mod.build(dist, out)
            self.assertEqual(sorted(p.name for p in dist.iterdir()), sorted(mod.expected_dist_names(NAME, VERSION)))
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["checksums.txt", "install.ps1", "install.sh"])
            sums = dict(reversed(l.split("  ", 1)) for l in (out / "checksums.txt").read_text().splitlines())
            self.assertEqual(set(sums), set(mod.expected_dist_names(NAME, VERSION)) | {"install.sh", "install.ps1"})
            for n in ("install.sh", "install.ps1"):
                self.assertEqual(sums[n], mod.sha256(out / n))

    def test_build_refuses_extra_files_in_dist(self):
        mod = _load_script()
        for extra in ("checksums.txt", "install.sh", "hyvv-0.0.1-py3-none-any.whl"):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as t:
                dist = Path(t, "dist")
                dist.mkdir()
                self._fake_dist(dist, extra)
                with self.assertRaises(SystemExit):
                    mod.build(dist, Path(t, "out"))

    def test_release_workflow_separates_pypi_and_release_assets(self):
        y = _read(".github/workflows/release.yml")
        self.assertIn("python scripts/build_release_assets.py --dist dist --out release-assets", y)
        self.assertIn("packages-dir: dist", y)
        self.assertNotRegex(y, r"cp [^\n]*install\.[^\n]* dist/")
        self.assertNotIn("(cd dist && sha256sum", y)
        self.assertNotRegex(y, r"gh release upload [^\n]*dist/\*(\s|$)")  # no blanket dist/*
        self.assertIn('dist/*.tar.gz dist/*.whl release-assets/*', y)
        self.assertIn("release-assets/", y)
        self.assertIn("gh release upload", y)
        self.assertIn("tags: [\"v*\"]", y)  # only a pushed tag triggers it

    def test_ci_runs_the_asset_build_without_tagging(self):
        y = _read(".github/workflows/ci.yml")
        self.assertIn("python scripts/build_release_assets.py --dist dist --out release-assets", y)
        self.assertIn('test "$(ls dist | wc -l | tr -d \' \')" = 2', y)
        self.assertNotIn("git tag", y)
        self.assertNotIn("gh release", y)


if __name__ == "__main__":
    unittest.main()
