"""Exercise the actual local publisher, including on native Windows CI."""
import hashlib
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import landing_export as E
import launcher as L


def attempt(state="succeeded"):
    return {"attempt_id": "att_native", "kind": "check", "state": state, "updated_at": 100,
            "brief": {"source_rev": "a" * 40}}


def config(root):
    return {"launcher_id": "worker", "landed_notifier": str(root / "notifications.jsonl")}


def publish_after_barrier(root, barrier, process_id, state):
    barrier.wait(timeout=30)
    try:
        _, _, created = L.emit_landed_record(attempt(state), root, config(root))
        status = "created" if created else "duplicate"
    except L.LandedEventConflict:
        status = "conflict"
    (root / f"writer-{process_id}").write_text(status, encoding="ascii")


class Publication(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.outbox = E.landed_outbox(self.root)

    def test_canonical_urn_publish_export_and_replay(self):
        cfg = config(self.root)
        path, event, created = L.emit_landed_record(attempt(), self.root, cfg)
        self.assertTrue(created)
        event_id = L.landed_event_id("att_native", "succeeded")
        self.assertEqual(event["id"], event_id)
        if os.name == "nt":
            self.assertNotIn(":", path.name)
        else:
            self.assertEqual(path.name, event_id + ".json")
        raw = json.dumps(event, sort_keys=True, indent=1, ensure_ascii=False).encode()
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(L.emit_landed_record(attempt(), self.root, cfg), (path, event, False))
        self.assertEqual(L.write_landed_event(self.outbox, event), path)

        page = E.export_events(self.root, limit=1)
        canonical = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()
        self.assertEqual(page["events"], [{"cursor": 1, "event_id": event_id, "event": event,
                                           "sha256": hashlib.sha256(canonical).hexdigest(),
                                           "file_sha256": hashlib.sha256(raw).hexdigest()}])
        self.assertEqual((page["next_cursor"], page["has_more"]), (1, False))
        self.assertEqual(E.export_events(self.root, after=1)["events"], [])
        self.assertEqual(L.replay_landed_events(self.root, cfg), (1, 1))
        signals = (self.root / "notifications.jsonl").read_bytes().splitlines()
        self.assertEqual(list(map(json.loads, signals)), [event, event])

    def test_windows_names_distinguish_case_punctuation_devices_and_legacy_files(self):
        with mock.patch.object(L, "_WINDOWS_LANDED_NAMES", True):
            event = L.build_landed_event(attempt(), config(self.root))
            self.outbox.mkdir(parents=True)
            legacy = self.outbox / "Old.json"
            legacy.write_text(json.dumps(dict(event, id="Old")), encoding="utf-8")
            ids = ("Old", "old", "X:Y", "X_Y", "x:y", "CON", "NUL.ext", ".hidden", "A" * 200)
            paths = [L.write_landed_event(self.outbox, dict(event, id=i)) for i in ids]
            self.assertEqual(paths[0], legacy)
            self.assertEqual(len({p.name.casefold() for p in paths}), len(ids))
            for path in paths[1:]:
                self.assertRegex(path.name, r"^landed~[0-9a-f]{64}\.json$")
            self.assertEqual({r["event_id"] for r in E.export_events(self.root)["events"]}, set(ids))

    def test_unreadable_prior_event_cannot_authorize_a_second_terminal_state(self):
        cfg = config(self.root)
        first, _, _ = L.emit_landed_record(attempt(), self.root, cfg)
        read = Path.read_text

        def unavailable(path, *args, **kwargs):
            if path == first:
                raise PermissionError("fixture: temporarily unreadable")
            return read(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", unavailable), self.assertRaises(OSError):
            L.emit_landed_record(attempt("failed"), self.root, cfg)
        self.assertEqual(list(self.outbox.glob("*.json")), [first])
        with self.assertRaises(L.LandedEventConflict):
            L.emit_landed_record(attempt("failed"), self.root, cfg)

    def test_overlapping_process_observations_create_and_signal_only_the_winner(self):
        states = ("succeeded", "succeeded", "failed", "failed")
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(len(states))
        children = []
        try:
            for i, state in enumerate(states):
                p = ctx.Process(target=publish_after_barrier, args=(self.root, barrier, i, state))
                p.start()
                children.append(p)
            for p in children:
                p.join(40)
                self.assertEqual(p.exitcode, 0)
        finally:
            for p in children:
                if p.is_alive():
                    p.kill()
                p.join(10)
                p.close()
        statuses = [p.read_text("ascii") for p in self.root.glob("writer-*")]
        self.assertCountEqual(statuses, ["created", "duplicate", "conflict", "conflict"])
        events = E.export_events(self.root)["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads((self.root / "notifications.jsonl").read_bytes()), events[0]["event"])
