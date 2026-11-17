"""Regression tests for the durable monotonic landing-export cursor.

The cursor is an export position persisted in ``events/export-index.json``.
It must follow append/registration order rather than canonical event-id order,
survive exporter restarts and concurrent publication, recover a file written
just before an index-registration crash, and reject unknown/legacy cursors
explicitly instead of returning an empty 200 page.
"""
from __future__ import annotations

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
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import landing_export as E

spec = importlib.util.spec_from_file_location("launcher_landing_export_cursor", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules["launcher_landing_export_cursor"] = L
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


def write_raw_event(data_dir, event_id, label):
    outbox = data_dir / "events" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    event = {
        "specversion": "1.0",
        "id": event_id,
        "type": E.LANDED_EVENT_TYPE,
        "data": {"schema": E.LANDED_DATA_SCHEMA, "label": label},
    }
    path = outbox / (event_id + ".json")
    path.write_text(json.dumps(event, sort_keys=True), "utf-8")
    return path, event


class CursorSequence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)

    def add(self, aid, **kwargs):
        return save_event(self.data_dir, aid, **kwargs)

    def test_index_persists_positions_and_hashes(self):
        self.add("att_index0001")
        body = E.export_events(self.data_dir)
        self.assertEqual(body["events"][0]["cursor"], 1)

        index = json.loads((self.data_dir / "events" / "export-index.json").read_text("utf-8"))
        self.assertEqual(index["schema"], E.EXPORT_INDEX_SCHEMA)
        self.assertEqual(index["next_position"], 2)
        self.assertEqual(index["head_position"], 1)
        entry = index["events"][0]
        self.assertEqual(entry["position"], 1)
        self.assertEqual(entry["event_id"], body["events"][0]["event_id"])
        self.assertEqual(entry["sha256"], body["events"][0]["sha256"])
        self.assertEqual(entry["file_sha256"], body["events"][0]["file_sha256"])

    def test_later_lower_canonical_id_is_exported_after_current_cursor(self):
        first_path, first = write_raw_event(self.data_dir, "zzz-event", "A")
        second_path, second = write_raw_event(self.data_dir, "aaa-event", "B")

        page1 = E.export_events(self.data_dir, limit=1)
        self.assertEqual([row["event_id"] for row in page1["events"]], ["zzz-event"])
        self.assertEqual(page1["next_cursor"], 1)
        self.assertTrue(page1["has_more"])

        page2 = E.export_events(self.data_dir, after=page1["next_cursor"], limit=10)
        self.assertEqual([row["event_id"] for row in page2["events"]], ["aaa-event"])
        self.assertEqual([row["cursor"] for row in page2["events"]], [2])
        self.assertFalse(page2["has_more"])
        self.assertEqual(page2["next_cursor"], 2)

    def test_append_between_pages_never_skips_or_duplicates(self):
        for i in range(3):
            self.add(f"att_between0{i}")
        page1 = E.export_events(self.data_dir, limit=2)
        self.assertEqual([row["cursor"] for row in page1["events"]], [1, 2])

        self.add("att_between99")
        page2 = E.export_events(self.data_dir, after=page1["next_cursor"], limit=2)
        self.assertEqual([row["cursor"] for row in page2["events"]], [3, 4])
        self.assertFalse(page2["has_more"])

        tail = E.export_events(self.data_dir, after=page2["next_cursor"])
        self.assertEqual(tail["events"], [])
        self.assertFalse(tail["has_more"])
        self.assertEqual(tail["next_cursor"], page2["next_cursor"])

    def test_exporter_restart_preserves_positions(self):
        for i in range(4):
            self.add(f"att_restart0{i}")
        before = E.export_events(self.data_dir, limit=500)

        # A restart is just a fresh read of the durable index; no in-process
        # state may influence the sequence.
        after = E.export_events(self.data_dir, limit=500)
        self.assertEqual(
            [(row["cursor"], row["event_id"]) for row in after["events"]],
            [(row["cursor"], row["event_id"]) for row in before["events"]],
        )
        self.assertEqual([row["cursor"] for row in after["events"]], [1, 2, 3, 4])

        # Continuing from the persisted head after the restart works directly.
        self.add("att_restart99")
        tail = E.export_events(self.data_dir, after=4)
        self.assertEqual([(row["cursor"], row["event_id"]) for row in tail["events"]],
                         [(5, tail["events"][0]["event_id"])])

    def test_crash_between_file_and_index_registration_is_recovered(self):
        self.add("att_crash0001")
        first = E.export_events(self.data_dir)
        self.assertEqual(first["next_cursor"], 1)

        # Simulate a crash after the immutable outbox file is durable but before
        # its index entry is written.
        with mock.patch.object(E, "register_landed_file", side_effect=OSError("crash")):
            path, event = self.add("att_crash0002")
        index = json.loads((self.data_dir / "events" / "export-index.json").read_text("utf-8"))
        self.assertEqual(index["next_position"], 2)
        self.assertNotIn(event["id"], [entry["event_id"] for entry in index["events"]])
        self.assertTrue(path.exists())

        recovered = E.export_events(self.data_dir, after=1)
        self.assertEqual([row["cursor"] for row in recovered["events"]], [2])
        self.assertEqual([row["event_id"] for row in recovered["events"]], [event["id"]])

        index = json.loads((self.data_dir / "events" / "export-index.json").read_text("utf-8"))
        self.assertEqual(index["next_position"], 3)
        self.assertEqual(len(index["events"]), 2)
        self.assertEqual(len({entry["position"] for entry in index["events"]}), 2)

    def test_concurrent_publication_assigns_unique_contiguous_positions(self):
        def publish(i):
            self.add(f"att_concurrent{i:02d}")

        threads = [threading.Thread(target=publish, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        body = E.export_events(self.data_dir, limit=500)
        positions = [row["cursor"] for row in body["events"]]
        event_ids = [row["event_id"] for row in body["events"]]
        self.assertEqual(len(event_ids), 6)
        self.assertEqual(positions, list(range(1, 7)))
        self.assertEqual(len(set(event_ids)), 6)
        self.assertEqual(len(set(positions)), 6)

    def test_malformed_expired_and_legacy_cursors_are_explicit(self):
        self.add("att_cursor_error")
        E.export_events(self.data_dir)  # establish head=1

        for bad in ("not-a-position", "1.5", "-1", "١", "1\x00"):
            with self.assertRaises(E.LandingExportError, msg=repr(bad)) as caught:
                E.export_events(self.data_dir, after=bad)
            self.assertIsInstance(caught.exception, E.MalformedCursorError)

        with self.assertRaises(E.ExpiredCursorError):
            E.export_events(self.data_dir, after=2)
        with self.assertRaises(E.ExpiredCursorError):
            E.export_events(self.data_dir, after="999999")

        for legacy in ("a" * 64, "0" * 64, "urn:rizzstack:bee-landed:" + "b" * 32):
            with self.assertRaises(E.LegacyCursorError, msg=legacy):
                E.export_events(self.data_dir, after=legacy)

    def test_unknown_positive_cursor_is_never_an_empty_success_page(self):
        # An outbox with no durable index head cannot validate a positive
        # cursor: it is expired, not silently empty.
        with self.assertRaises(E.ExpiredCursorError):
            E.export_events(self.data_dir, after=1)

        missing = self.data_dir / "missing"
        with self.assertRaises(E.ExpiredCursorError):
            E.export_events(missing, after=1)

    def test_ambiguous_stored_positions_do_not_yield_a_page(self):
        self.add("att_first")
        self.add("att_second")
        path = E.export_index_path(self.data_dir)
        index = json.loads(path.read_bytes())
        for position in (1, True, "2", 1.5, None):
            with self.subTest(position=position):
                index["events"][1]["position"] = position
                corrupt = json.dumps(index).encode()
                path.write_bytes(corrupt)
                with self.assertRaises(OSError):
                    E.export_events(self.data_dir, limit=1)
                self.assertEqual(path.read_bytes(), corrupt)

    def test_cursor_zero_starts_at_beginning(self):
        self.add("att_zero00001")
        body = E.export_events(self.data_dir, after=0)
        self.assertEqual([row["cursor"] for row in body["events"]], [1])

    def test_unreadable_index_is_not_rebuilt_from_retained_files(self):
        old, _ = write_raw_event(self.data_dir, "old", "A")
        self.assertEqual(E.export_events(self.data_dir)["next_cursor"], 1)
        old.unlink()  # operator retention: the missing file's position stays consumed
        write_raw_event(self.data_dir, "new", "B")
        index = E.export_index_path(self.data_dir)
        before = index.read_bytes()
        read_bytes = Path.read_bytes

        def fail_index(path):
            if path == index:
                raise PermissionError("index temporarily unreadable")
            return read_bytes(path)

        with mock.patch.object(Path, "read_bytes", fail_index), self.assertRaises(OSError):
            E.export_events(self.data_dir, after=1)
        self.assertEqual(index.read_bytes(), before)
        rows = E.export_events(self.data_dir, after=1)["events"]
        self.assertEqual([(r["cursor"], r["event_id"]) for r in rows], [(2, "new")])

    def test_unreadable_event_never_advances_cursor_past_it(self):
        first, _ = write_raw_event(self.data_dir, "first", "A")
        self.assertEqual(E.export_events(self.data_dir)["next_cursor"], 1)
        write_raw_event(self.data_dir, "second", "B")
        read_bytes = Path.read_bytes

        def fail_first(path):
            if path == first:
                raise OSError("temporary read failure")
            return read_bytes(path)

        with mock.patch.object(Path, "read_bytes", fail_first), self.assertRaises(OSError):
            E.export_events(self.data_dir, after=0, limit=1)
        page = E.export_events(self.data_dir, after=0, limit=1)
        self.assertEqual([(r["cursor"], r["event_id"]) for r in page["events"]], [(1, "first")])
        tail = E.export_events(self.data_dir, after=page["next_cursor"])
        self.assertEqual([(r["cursor"], r["event_id"]) for r in tail["events"]], [(2, "second")])

    def test_outbox_listing_failure_returns_retryable_response_without_cursor(self):
        # Call the handler directly without opening a socket or starting a host.
        write_raw_event(self.data_dir, "waiting", "A")
        handler_type = L.make_handler(SimpleNamespace(dir=self.data_dir), TOKEN)
        handler = handler_type.__new__(handler_type)
        handler.path = "/v1/landing/events?after=0"
        handler.headers = {"Authorization": "Bearer " + TOKEN}
        handler._send = lambda status, body: (status, body)
        with mock.patch.object(Path, "iterdir", side_effect=OSError("cannot list outbox")):
            status, body = handler.do_GET()
        self.assertEqual(status, 503)
        self.assertEqual(body["code"], "landing_storage_unavailable")
        self.assertNotIn("next_cursor", body)
        status, body = handler.do_GET()
        self.assertEqual(status, 200)
        self.assertEqual([r["event_id"] for r in body["events"]], ["waiting"])


class CursorHttp(unittest.TestCase):
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

    def test_http_pagination_uses_integer_positions_across_lower_id_append(self):
        write_raw_event(self.data_dir, "zzz-event", "A")
        status, page1 = self.get("/v1/landing/events?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual([row["cursor"] for row in page1["events"]], [1])
        self.assertEqual([row["event_id"] for row in page1["events"]], ["zzz-event"])
        self.assertFalse(page1["has_more"])

        write_raw_event(self.data_dir, "aaa-event", "B")
        cursor = urllib.parse.quote(str(page1["next_cursor"]), safe="")
        status, page2 = self.get(f"/v1/landing/events?after={cursor}&limit=10")
        self.assertEqual(status, 200)
        self.assertEqual([row["cursor"] for row in page2["events"]], [2])
        self.assertEqual([row["event_id"] for row in page2["events"]], ["aaa-event"])

    def test_http_cursor_errors_are_non_success(self):
        save_event(self.data_dir, "att_http_cursor1")
        self.get("/v1/landing/events")  # create index head=1

        for cursor, expected_code in (
            ("not-a-position", "malformed_cursor"),
            ("999999", "cursor_expired"),
            ("a" * 64, "legacy_cursor"),
            ("urn:rizzstack:bee-landed:" + "b" * 32, "legacy_cursor"),
        ):
            quoted = urllib.parse.quote(cursor, safe="")
            status, body = self.get(f"/v1/landing/events?after={quoted}")
            self.assertNotEqual(status, 200, cursor)
            self.assertIn("error", body)
            self.assertEqual(body.get("code"), expected_code)

    def test_http_auth_still_required(self):
        save_event(self.data_dir, "att_http_auth01")
        status, body = self.get("/v1/landing/events", token="wrong-token")
        self.assertEqual(status, 401)
        self.assertNotIn("events", body)


if __name__ == "__main__":
    unittest.main()
