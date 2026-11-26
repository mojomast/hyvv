"""The launcher's Server header names the build it was made from, not a tag it sits near."""
from __future__ import annotations

import importlib.util
import sys
import tomllib
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules.setdefault("launcher", L)
spec.loader.exec_module(L)
# pyproject.toml is the single version source for the release base; a stale launcher.VERSION
# copy must fail here instead of matching a hard-coded value.
PYPROJECT_VERSION = tomllib.loads((HERE / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


class LauncherVersionHeader(unittest.TestCase):
    def test_server_header_carries_the_build(self):
        handler = L.make_handler(None, "t" * 40)
        self.assertEqual(handler.server_version, "orbit-launcher/" + L.build_version())

    def test_a_build_off_the_tag_does_not_name_the_release(self):
        ident = L.build_identity()
        if ident["commits_past_tag"] == 0 and not ident["dirty"]:
            self.assertEqual(ident["version"], L.VERSION)  # exactly at the tag: the release is honest
            return
        self.assertNotEqual(ident["version"], L.VERSION, "a build off the tag claimed the tag")
        self.assertTrue(ident["version"].startswith(L.VERSION + "+"), ident["version"])

    def test_release_base_matches_pyproject(self):
        self.assertEqual(L.VERSION, PYPROJECT_VERSION)
