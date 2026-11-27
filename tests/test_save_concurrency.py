"""RED contract: two concurrent saves of one attempt must not share a temp file.

Incident: `Launcher._save` wrote every journal update to the same fixed path
(``attempts/<id>.tmp``) and then called ``os.replace``.  Two writers of the same
attempt (the dispatcher and a cancel/set path) raced: the second write replaced
the first's temp file, and the first ``os.replace`` then failed with
``FileNotFoundError: ... .tmp -> .json`` (observed on the ARM/py3.13 CI leg of
hyvv#90).  A unique temp file per write fixes it.

Run: python3 -m unittest tests.test_save_concurrency
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import launcher
from tests.test_win_save_reconcile import _attempt, _launcher


class ConcurrentSave(unittest.TestCase):
    def test_two_threads_saving_one_attempt_use_distinct_temp_files(self):
        with tempfile.TemporaryDirectory() as d:
            la, _ = _launcher(d)
            aid = "att_cc00000001"
            first = _attempt(aid=aid, state="running")
            second = _attempt(aid=aid, state="cancel_requested")
            la.attempts[aid] = first
            barrier = threading.Barrier(2)
            real_replace = os.replace

            def replace(src, dst, *args, **kwargs):
                if str(dst).endswith(aid + ".json"):
                    barrier.wait(timeout=10)
                return real_replace(src, dst, *args, **kwargs)

            errors: list[BaseException] = []

            def run(attempt):
                try:
                    la._save(attempt)
                except BaseException as exc:  # noqa: BLE001 - the RED failure is the point
                    errors.append(exc)

            with mock.patch.object(launcher.os, "replace", side_effect=replace):
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
