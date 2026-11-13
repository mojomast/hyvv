import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import unittest
from hyvv import __version__
from hyvv.cli import main

class Cli(unittest.TestCase):
    def test_version(self):
        self.assertEqual(__version__, "0.1.15")
        self.assertEqual(main(["version"]), 0)

    def test_planned_verb_exits_2(self):
        self.assertEqual(main(["drain"]), 2)
