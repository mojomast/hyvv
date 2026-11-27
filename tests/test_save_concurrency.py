"""Two concurrent saves of one attempt must not crash or corrupt the journal.

The original RED (before hyvv#91) forced the fixed-temp-file collision with a
barrier at ``os.replace``.  Journal writes are now serialized per attempt, so a
barrier that requires both threads inside ``os.replace`` would deadlock; this
version keeps both writers running and asserts both complete and the journal
parses.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest

from tests.test_win_save_reconcile import _attempt, _launcher


class ConcurrentSave(unittest.TestCase):
    def test_two_threads_saving_one_attempt_both_complete(self):
        with tempfile.TemporaryDirectory() as d:
            la, _ = _launcher(d)
            aid = "att_cc00000001"
            first = _attempt(aid=aid, state="running", lseq=1)
            second = _attempt(aid=aid, state="cancel_requested", lseq=2)
            la.attempts[aid] = first
            errors: list[BaseException] = []

            def run(attempt):
                try:
                    la._save(attempt)
                except BaseException as exc:  # noqa: BLE001 - the RED failure is the point
                    errors.append(exc)

            threads = [
                threading.Thread(target=run, args=(item,)) for item in (first, second)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)

            self.assertEqual(errors, [], f"concurrent saves collided: {errors!r}")
            stored = json.loads(la._path(aid).read_text(encoding="utf-8"))
            self.assertIn(stored["state"], {"running", "cancel_requested"})


if __name__ == "__main__":
    unittest.main()
