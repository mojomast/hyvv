"""cleanup-sandboxes.ps1 must never remove a live bee's sandbox and must offer a dry run.

Found Nov 24 2026 while widening the sweep to the Nous/Kimi prefixes: the script removed every
matching sandbox, including ones a running attempt still uses. Pinned here (source-level, the
script runs only on Windows):
  * a -DryRun switch lists what would go and removes nothing;
  * sandboxes named by a non-terminal attempt journal are kept;
  * an unreadable attempt journal fails closed: nothing is removed.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

PS = (Path(__file__).resolve().parents[1] / "scripts" / "cleanup-sandboxes.ps1").read_text("utf-8")


class CleanupGuard(unittest.TestCase):
    def test_dry_run_switch_is_the_first_statement(self):
        code = [l for l in PS.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        self.assertRegex(code[0], r"^param\(\s*\[switch\]\$DryRun\s*\)")
        self.assertIn("would rm", PS)

    def test_live_attempt_sandboxes_are_kept(self):
        self.assertIn("attempts", PS)
        self.assertRegex(PS, r"succeeded.*failed.*cancelled")
        self.assertIn("keep (live attempt)", PS)

    def test_unreadable_journal_fails_closed(self):
        self.assertRegex(PS, r"(?s)catch\s*\{[^}]*\$unreadable")
        self.assertIn("refusing to remove", PS)

    def test_rm_only_runs_outside_dry_run_and_live_set(self):
        rm = [l for l in PS.splitlines() if "sb rm" in l]
        self.assertEqual(len(rm), 1)
        self.assertTrue(re.search(r"\$DryRun|\$live", PS[:PS.index("sb rm")]))


if __name__ == "__main__":
    unittest.main()
