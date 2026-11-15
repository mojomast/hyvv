import sys
import tomllib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import unittest
from hyvv import __version__
from hyvv.cli import main

ROOT = Path(__file__).resolve().parents[1]
# pyproject.toml is the single version source; the runtime copy must agree with
# it, so a stale copy fails here instead of matching a hard-coded value.
PYPROJECT_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

class Cli(unittest.TestCase):
    def test_version(self):
        self.assertEqual(__version__, PYPROJECT_VERSION)
        self.assertEqual(main(["version"]), 0)

    def test_planned_verb_exits_2(self):
        self.assertEqual(main(["drain"]), 2)
