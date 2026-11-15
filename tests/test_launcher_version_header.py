"""The launcher's Server header names the release it was built from."""
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
# pyproject.toml is the single version source; a stale launcher.VERSION copy
# must fail here instead of matching a hard-coded value.
PYPROJECT_VERSION = tomllib.loads((HERE / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


class LauncherVersionHeader(unittest.TestCase):
    def test_server_header_carries_the_version(self):
        handler = L.make_handler(None, "t" * 40)
        self.assertEqual(handler.server_version, "orbit-launcher/" + L.VERSION)
        self.assertEqual(handler.server_version, "orbit-launcher/" + PYPROJECT_VERSION)
