"""The main ruleset requires a status check named exactly "CI". Keep an
aggregate job with that name that needs every other job and always runs."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

CI = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"


def jobs(text: str) -> dict[str, str]:
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", body)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


class RequiredCheck(unittest.TestCase):
    def test_aggregate_job_named_exactly_CI(self):
        js = jobs(CI.read_text(encoding="utf-8"))
        named = [k for k, v in js.items() if re.search(r'(?m)^    name:\s*"?CI"?\s*$', v)]
        self.assertEqual(len(named), 1, "exactly one job must be named CI (the ruleset's required check)")
        agg = js[named[0]]
        self.assertRegex(agg, r"(?m)^    if:\s*always\(\)\s*$")
        m = re.search(r"(?m)^    needs:\s*\[([^\]]*)\]", agg)
        self.assertIsNotNone(m, "CI job must list needs")
        needs = {n.strip() for n in m.group(1).split(",") if n.strip()}
        self.assertEqual(needs, set(js) - {named[0]}, "CI must need every other job")
        for n in needs:
            self.assertIn(f'needs.{n}.result }}}}" = "success"', agg, f"CI must fail unless {n} succeeded")
        # No matrix on the aggregate, so the check name stays exactly "CI".
        self.assertNotIn("matrix", agg)


if __name__ == "__main__":
    unittest.main()
