"""Tests for the authenticated canonical landing-outbox export.

The export is a read-only view of persisted CloudEvents in
``<data_dir>/events/outbox``.  These tests pin the wire shape, raw-byte content
identity, bounded pagination, restart stability, auth refusal, retention, and
the rule that attempt logs are never reconstructed into events.
"""
from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import landing_export as E

spec = importlib.util.spec_from_file_location("launcher_landing_export", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules["launcher_landing_export"] = L
spec.loader.exec_module(L)

TOKEN = "t" * 40


def save_event(data_dir, aid, state="succeeded", transition=None):
    a = {
        "attempt_id": aid,
        "kind": "opencode",
        "state": state,
        "created_at": 100.0,
        "updated_at": 100.0,
        "brief": {
            "objective": "SECRET-OBJECTIVE",
            "feature_id": "feat_1",
            "job_id": "job_1",
            "task_id": "task_1",
            "source_rev": "a" * 40,
            "coordinator": {"id": "orbit", "session": "sess_1"},
            "lease_sha256": "b" * 64,
            "terminal_transition_id": transition,
        },
        "model": "deepseek-v4.1-flash",
    }
    event = L.build_landed_event(a, {"launcher_id": "worker-1"})
    path = L.write_landed_event(L._landed_outbox(data_dir), event)
    return path, event


class ExportModule(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def add(self, aid, **kwargs):
        return save_event(self.data_dir, aid, **kwargs)

    def test_exports_persisted_canonical_events_with_content_and_file_identity(self):
        paths = [self.add(f"att_export000{i}")[0] for i in range(3)]
        outbox = self.data_dir / "events" / "outbox"

        (outbox / "broken.json").write_text("{not json", "utf-8")
        (outbox / ".hidden.json").write_text(json.dumps({"id": "hidden"}), "utf-8")
        (outbox / "ignored.tmp").write_text("{}", "utf-8")

        body = E.export_events(self.data_dir)

        self.assertEqual(body["schema"], "rizzstack.bee-landed-export/1")
        self.assertEqual(len(body["events"]), 3)
        self.assertFalse(body["has_more"])
        self.assertEqual(body["retention"], E.RETENTION)
        self.assertFalse(body["retention"]["deletes"])
        self.assertIsNone(body["retention"]["max_events"])
        self.assertIsNone(body["retention"]["max_age_seconds"])

        self.assertEqual([row["cursor"] for row in body["events"]], [1, 2, 3])
        for row in body["events"]:
            raw = (outbox / f"{row['event_id']}.json").read_bytes()
            canonical = (
                json.dumps(row["event"], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                .encode("utf-8") + b"\n"
            )
            self.assertEqual(row["sha256"], hashlib.sha256(canonical).hexdigest())
            self.assertEqual(row["file_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(row["event"], json.loads(raw.decode("utf-8")))
            self.assertEqual(row["event_id"], row["event"]["id"])
            self.assertIsInstance(row["cursor"], int)
            self.assertEqual(row["event"]["type"], "rizzstack.bee.landed.v1")
            self.assertEqual(row["event"]["data"]["schema"], "rizzstack.bee-landed/1")

    def test_pagination_is_bounded_and_stable_across_restarts(self):
        self.add("att_page00001")
        self.add("att_page00002", state="failed")
        self.add("att_page00003", state="cancelled")
        self.add("att_page00004")
        self.add("att_page00005", state="failed")

        all_rows = E.export_events(self.data_dir, limit=500)["events"]
        all_cursors = [row["cursor"] for row in all_rows]
        self.assertEqual(len(all_cursors), 5)
        self.assertEqual(all_cursors, sorted(all_cursors))

        seen = []
        after = None
        pages = 0
        while True:
            body = E.export_events(self.data_dir, after=after, limit=2)
            self.assertLessEqual(len(body["events"]), 2)
            pages += 1
            seen.extend(row["cursor"] for row in body["events"])
            if not body["has_more"]:
                break
            self.assertIsNotNone(body["next_cursor"])
            after = body["next_cursor"]
            self.assertLess(pages, 10)

        self.assertEqual(seen, all_cursors)
        self.assertEqual(pages, 3)

        # A fresh read after a simulated restart observes exactly the same order,
        # because cursors are derived from immutable canonical file identity.
        self.assertEqual([row["cursor"] for row in E.export_events(self.data_dir, limit=500)["events"]],
                         all_cursors)

        exhausted = E.export_events(self.data_dir, after=all_cursors[-1], limit=2)
        self.assertEqual(exhausted["events"], [])
        self.assertFalse(exhausted["has_more"])
        self.assertEqual(exhausted["next_cursor"], all_cursors[-1])

        empty = E.export_events(Path(self._tmp.name) / "missing", after=None)
        self.assertEqual(empty["events"], [])
        self.assertFalse(empty["has_more"])
        self.assertIsNone(empty["next_cursor"])

    def test_limit_is_required_to_stay_in_range(self):
        self.add("att_limit0001")
        for bad in (0, -1, 501, "not-a-number", "1.5", True, []):
            with self.assertRaises(E.LandingExportError, msg=repr(bad)):
                E.export_events(self.data_dir, limit=bad)
        self.assertEqual(len(E.export_events(self.data_dir, limit=1)["events"]), 1)
        self.assertEqual(len(E.export_events(self.data_dir, limit=500)["events"]), 1)

    def test_unknown_cursor_is_an_explicit_error(self):
        self.add("att_cursor001")
        with self.assertRaises(E.MalformedCursorError):
            E.export_events(self.data_dir, after="zzzz-not-a-stored-cursor")

    def test_attempt_logs_and_journals_are_never_reconstructed(self):
        attempts = self.data_dir / "attempts"
        attempts.mkdir(parents=True)
        terminal = {
            "attempt_id": "att_logonly001",
            "kind": "opencode",
            "state": "succeeded",
            "created_at": 1.0,
            "updated_at": 2.0,
            "brief": {"objective": "SECRET-OBJECTIVE", "source_rev": "a" * 40},
        }
        (attempts / "att_logonly001.json").write_text(json.dumps(terminal), "utf-8")
        (attempts / "att_logonly001.events.jsonl").write_text(
            json.dumps({"lseq": 1, "kind": "state", "state": "succeeded", "detail": "done"}) + "\n",
            "utf-8",
        )

        body = E.export_events(self.data_dir)
        self.assertEqual(body["events"], [])
        self.assertFalse(body["has_more"])
        self.assertFalse((self.data_dir / "events" / "outbox").exists())

        self.add("att_stored0001")
        body = E.export_events(self.data_dir)
        self.assertEqual([row["event"]["data"]["attempt_id"] for row in body["events"]],
                         ["att_stored0001"])


class ExportHttp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        stub = SimpleNamespace(cfg={"data_dir": str(self.data_dir)}, dir=self.data_dir)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), L.make_handler(stub, TOKEN))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def get(self, path, token=TOKEN):
        headers = {} if token is None else {"Authorization": "Bearer " + token}
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        body = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, body

    def test_authenticated_export_and_pagination(self):
        save_event(self.data_dir, "att_http00001")
        save_event(self.data_dir, "att_http00002")
        save_event(self.data_dir, "att_http00003")

        status, body = self.get("/v1/landing/events?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(body["schema"], "rizzstack.bee-landed-export/1")
        self.assertEqual(len(body["events"]), 2)
        self.assertTrue(body["has_more"])
        self.assertEqual(body["retention"], E.RETENTION)
        self.assertEqual([row["cursor"] for row in body["events"]], [1, 2])

        cursor = urllib.parse.quote(str(body["next_cursor"]), safe="")
        status, body2 = self.get(f"/v1/landing/events?after={cursor}&limit=2")
        self.assertEqual(status, 200)
        self.assertEqual([row["cursor"] for row in body2["events"]], [3])
        self.assertFalse(body2["has_more"])
        self.assertEqual(body2["next_cursor"], 3)

        status, empty = self.get(f"/v1/landing/events?after={urllib.parse.quote(str(body2['next_cursor']), safe='')}")
        self.assertEqual(status, 200)
        self.assertEqual(empty["events"], [])
        self.assertFalse(empty["has_more"])

    def test_missing_or_wrong_bearer_auth_is_refused(self):
        save_event(self.data_dir, "att_auth00001")
        for token in (None, "", "wrong-token"):
            status, body = self.get("/v1/landing/events", token=token)
            self.assertEqual(status, 401, token)
            self.assertNotIn("events", body)

    def test_invalid_limit_is_a_bad_request(self):
        save_event(self.data_dir, "att_limit0001")
        for limit in ("0", "501", "-1", "abc", "1.5"):
            status, body = self.get(f"/v1/landing/events?limit={limit}")
            self.assertEqual(status, 400, limit)
            self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
