"""Kimi direct (api.kimi.com) is a first-class key source: `models` entries with "key": "kimi"
(k3.1-pro, kimi-for-coding) must resolve the owner's Kimi key, never borrow an OpenCode Go,
DeepSeek or Zoo key, and hand it to the OpenAI-compatible wire as OPENAI_API_KEY.

Regression: on Nov 24, 2026 five k3.1-pro attempts failed with
"provider key not configured on this PC" although kimi.key sat in the data dir, because
provider_key() only knew go/deepseek/zoo.
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules["launcher"] = L
spec.loader.exec_module(L)

KEY_VARS = ("ORBIT_WORKER_KIMI_KEY", "ORBIT_WORKER_ZOO_KEY", "DEEPSEEK_API_KEY", "ORBIT_WORKER_GO_KEY")


class KimiKeySource(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.pop(k) for k in KEY_VARS if k in os.environ}
        self.tmp = Path(tempfile.mkdtemp(prefix="kimi-key-"))

    def tearDown(self):
        for k in KEY_VARS:
            os.environ.pop(k, None)
        os.environ.update(self._saved)

    def test_kimi_is_a_first_class_key_source(self):
        self.assertEqual(L._KEY_FILE_CFG["kimi"], "kimi_key_file")
        self.assertEqual(L._KEY_FILE_NAME["kimi"], "kimi.key")
        self.assertEqual(L._KEY_ENV["kimi"], "ORBIT_WORKER_KIMI_KEY")

    def test_key_from_data_dir_kimi_key_file(self):
        (self.tmp / "kimi.key").write_text("kimi-file-key\n", "utf-8")
        self.assertEqual(L.provider_key({"data_dir": str(self.tmp)}, "kimi"), "kimi-file-key")

    def test_key_file_override(self):
        p = self.tmp / "elsewhere.key"
        p.write_text("kimi-alt-key", "utf-8")
        self.assertEqual(L.provider_key({"data_dir": str(self.tmp), "kimi_key_file": str(p)}, "kimi"), "kimi-alt-key")

    def test_env_wins_over_file(self):
        (self.tmp / "kimi.key").write_text("from-file", "utf-8")
        os.environ["ORBIT_WORKER_KIMI_KEY"] = "from-env"
        self.assertEqual(L.provider_key({"data_dir": str(self.tmp)}, "kimi"), "from-env")

    def test_kimi_never_borrows_another_providers_key(self):
        for n in ("go.key", "deepseek.key", "zoo.key"):
            (self.tmp / n).write_text("other-" + n, "utf-8")
        self.assertEqual(L.provider_key({"data_dir": str(self.tmp)}, "kimi"), "")

    def test_worker_env_sends_the_kimi_key_as_openai_api_key(self):
        source = (HERE / "launcher.py").read_text("utf-8")
        self.assertRegex(source, r'if spec\["key"\] in \([^)]*"kimi"[^)]*\): env\["OPENAI_API_KEY"\] = key')


if __name__ == "__main__":
    unittest.main()
