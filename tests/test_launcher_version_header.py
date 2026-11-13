"""The launcher's Server header names the release it was built from."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules.setdefault("launcher", L)
spec.loader.exec_module(L)


class LauncherVersionHeader(unittest.TestCase):
    def test_server_header_carries_the_version(self):
        handler = L.make_handler(None, "t" * 40)
        self.assertEqual(handler.server_version, "orbit-launcher/0.1.16")
        self.assertEqual(handler.server_version, "orbit-launcher/" + L.VERSION)
