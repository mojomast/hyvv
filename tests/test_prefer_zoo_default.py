"""The Zoo free-pass preference is opt-in (2026-11-19).

Zoo's free GLM 5.3 Flash pass refuses programmatic (non-IDE) use and ends ~Nov 29, so a host that
merely has a zoo key must keep its configured model; only `"prefer_zoo": true` opts in.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import launcher as L

GO_FLASH = "go:deepseek-v4.1-flash"


def cfg(tmp, **extra):
    c = {"data_dir": str(tmp), "model": GO_FLASH, "models": {}, "projects": {}}
    c.update(extra)
    return c


class PreferZooDefault(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)
        env = {k: v for k, v in os.environ.items() if not k.endswith("ZOO_KEY") and "ZOO" not in k}
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._t.cleanup()

    def test_the_default_is_off(self):
        self.assertIs(L.PREFER_ZOO_DEFAULT, False)

    def test_a_zoo_key_alone_does_not_route_unnamed_briefs_to_zoo(self):
        (self.tmp / "zoo.key").write_text("fake-zoo-credential", "utf-8")
        self.assertEqual(L.default_model(cfg(self.tmp)), GO_FLASH)

    def test_prefer_zoo_true_opts_in_when_a_zoo_key_is_readable(self):
        (self.tmp / "zoo.key").write_text("fake-zoo-credential", "utf-8")
        self.assertEqual(L.default_model(cfg(self.tmp, prefer_zoo=True)), L.PREFERRED_MODEL)

    def test_prefer_zoo_true_without_a_zoo_key_keeps_the_host_model(self):
        self.assertEqual(L.default_model(cfg(self.tmp, prefer_zoo=True)), GO_FLASH)

    def test_explicit_false_keeps_the_host_model(self):
        (self.tmp / "zoo.key").write_text("fake-zoo-credential", "utf-8")
        self.assertEqual(L.default_model(cfg(self.tmp, prefer_zoo=False)), GO_FLASH)


if __name__ == "__main__":
    unittest.main()
