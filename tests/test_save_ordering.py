"""RED contract: a stale journal save must not overwrite a newer state.

The launcher's journal is written by several threads.  The per-attempt temp
file makes each write atomic, but a stale in-memory snapshot (an older ``lseq``)
could still land last and put "running" back over "cancel_requested".  The
ordering rule refuses a write older than the journal.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.test_win_save_reconcile import _attempt, _launcher


class SaveOrdering(unittest.TestCase):
    def test_stale_running_save_does_not_overwrite_cancel_requested(self):
        with tempfile.TemporaryDirectory() as d:
            la, _ = _launcher(d)
            aid = "att_dd00000001"
            running = _attempt(aid=aid, state="running", lseq=1)
            la.attempts[aid] = running
            la._save(running)

            cancel = dict(running, state="cancel_requested", lseq=2)
            la._save(cancel)
            la._save(running)  # stale write from the dispatcher

            stored = json.loads(la._path(aid).read_text(encoding="utf-8"))
            self.assertEqual(stored["state"], "cancel_requested")

    def test_stale_save_appends_an_event_and_reloads_memory(self):
        with tempfile.TemporaryDirectory() as d:
            la, _ = _launcher(d)
            aid = "att_dd00000003"
            running = _attempt(aid=aid, state="running", lseq=1)
            la.attempts[aid] = running
            la._save(running)
            la._save(dict(running, state="cancel_requested", lseq=2))

            la._save(running)  # stale write from the dispatcher

            self.assertEqual(la.attempts[aid]["state"], "cancel_requested")
            events = [
                json.loads(line)
                for line in (la.dir / "attempts" / f"{aid}.events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            refused = [e for e in events if e["kind"] == "journal_stale_save_refused"]
            self.assertEqual(len(refused), 1)
            self.assertEqual(refused[0]["state"], "cancel_requested")
            self.assertEqual(refused[0]["attempted_lseq"], 1)

    def test_stale_reload_updates_the_same_object(self):
        with tempfile.TemporaryDirectory() as d:
            la, _ = _launcher(d)
            aid = "att_dd00000004"
            running = _attempt(aid=aid, state="running", lseq=1)
            la.attempts[aid] = running
            la._save(running)
            la._save(dict(running, state="cancel_requested", lseq=2))

            la._save(running)

            self.assertIs(la.attempts[aid], running)
            self.assertEqual(running["state"], "cancel_requested")
            self.assertEqual(running["lseq"], 2)

    def test_a_stale_object_that_is_not_live_is_refreshed_too(self):
        with tempfile.TemporaryDirectory() as d:
            la, _ = _launcher(d)
            aid = "att_dd00000005"
            live = _attempt(aid=aid, state="running", lseq=1)
            la.attempts[aid] = live
            la._save(live)
            la._save(dict(live, state="cancel_requested", lseq=2))
            stale = dict(live)  # a caller-held snapshot, not the live object

            la._save(stale)

            self.assertIs(la.attempts[aid], live)
            self.assertEqual(stale["state"], "cancel_requested")
            self.assertEqual(stale["lseq"], 2)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_saved_journal_keeps_the_previous_mode(self):
        with tempfile.TemporaryDirectory() as d:
            la, _ = _launcher(d)
            a = _attempt(aid="att_dd00000002", state="running", lseq=1)
            la._save(a)

            self.assertEqual(la._path(a["attempt_id"]).stat().st_mode & 0o777, 0o644)


if __name__ == "__main__":
    unittest.main()
