"""Bee-capacity ceiling: the clamp accepts the owner ceiling (14) and refuses anything above it.

Owner directive supersedes the interim cap-12 shared-with-Forge note; the
coordinator keeps implementation lanes 8-10 typical (see docs/capacity.md).
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules.setdefault("launcher", L)
spec.loader.exec_module(L)

OWNER_CEILING = 14


class WorkerCap(unittest.TestCase):
    def test_max_workers_constant_is_the_owner_ceiling(self):
        self.assertEqual(L.MAX_WORKERS, OWNER_CEILING)

    def test_load_config_accepts_the_owner_ceiling(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            p.write_text(json.dumps({"max_concurrent": OWNER_CEILING}), encoding="utf-8")
            self.assertEqual(L.load_config(p)["max_concurrent"], OWNER_CEILING)

    def test_load_config_clamps_above_the_owner_ceiling(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            p.write_text(json.dumps({"max_concurrent": OWNER_CEILING + 99}), encoding="utf-8")
            self.assertEqual(L.load_config(p)["max_concurrent"], OWNER_CEILING)

    def test_config_patch_accepts_the_owner_ceiling_and_refuses_above_it(self):
        stub = type("Stub", (), {})()
        stub.cfg = {"max_concurrent": 1, "model": "deepseek-flash"}
        code, out = L.apply_config_patch(stub, {"max_concurrent": OWNER_CEILING})
        self.assertEqual(code, 200)
        self.assertEqual(stub.cfg["max_concurrent"], OWNER_CEILING)
        code, out = L.apply_config_patch(stub, {"max_concurrent": OWNER_CEILING + 1})
        self.assertEqual(code, 400)
        self.assertEqual(out["error"], f"max_concurrent must be 1..{OWNER_CEILING}")
        self.assertEqual(stub.cfg["max_concurrent"], OWNER_CEILING)


if __name__ == "__main__":
    unittest.main()
