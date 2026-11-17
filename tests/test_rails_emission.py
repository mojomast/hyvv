"""Fixture-only worker journal, restart, corruption and bounded original-byte export."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rails_emitter import RailsEmitter
from rails_export import export_page, ExportError
from rails_log_source import LogSource

AID = "att_1234567890abcdef"
CALL = "call_1234567890abcdef"


class Emission(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(prefix="rails ü ")
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        attempts = self.root / "attempts"
        attempts.mkdir()
        self.log = attempts / (AID + ".events.jsonl")
        self.emitter = RailsEmitter(self.root)

    def write(self, *rows):
        with self.log.open("ab") as f:
            for row in rows:
                f.write((json.dumps(row, ensure_ascii=False) + "\r\n").encode())

    def page(self, after=0, limit=500):
        return export_page(self.emitter.path, after, limit)

    def test_rotation_partial_restart_and_privacy(self):
        self.write(
            dict(lseq=1, kind="worker:tool_call", line="[tool execute] bash", text="private-☃", tool_id=CALL)
        )
        with self.log.open("ab") as f:
            f.write(b'{"kind":"worker:tool_update"')
        self.emitter.ingest(AID)
        first = self.page()
        self.assertEqual(
            [e["kind"] for e in first["events"] if e["kind"].startswith("tool.")], ["tool.start"]
        )
        with self.log.open("ab") as f:
            f.write(b',"lseq":2,"tool_status":"completed","tool_id":"' + CALL.encode() + b'"}\r\n')
        self.emitter = RailsEmitter(self.root)
        self.emitter.ingest(AID)
        page = self.page()
        tools = [e for e in page["events"] if e["kind"].startswith("tool.")]
        self.assertEqual([e["kind"] for e in tools], ["tool.start", "tool.finish"])
        self.assertEqual(tools[0]["payload"]["call_id"], tools[1]["payload"]["call_id"])
        self.assertTrue(all(e["duration_ms"] is None for e in tools))
        self.assertNotIn("private-", json.dumps(page))
        self.assertNotIn(b"private-", self.emitter.path.read_bytes())
        self.emitter.ingest(AID)
        self.assertEqual(self.page(), page)
        os.replace(self.log, self.log.with_suffix(".old"))
        self.write(dict(lseq=1, kind="worker:tool_call", line="[tool edit] write"))
        self.emitter.ingest(AID)
        added = self.page(page["head"]["seq"])["events"]
        self.assertIn("source.gap", [e["kind"] for e in added])
        self.assertEqual([e["payload"]["tool_name"] for e in added if e["kind"] == "tool.start"], ["write"])
        self.assertEqual(self.emitter.health()["completeness"], "partial")

    def test_broken_framing_and_missing_pair(self):
        self.log.write_bytes(b'{"private-content":\xff}\r\n')
        self.write(
            dict(lseq=2, kind="worker:tool_call", line="[tool execute] bash; private-content"),
            dict(lseq=3, kind="worker:tool_update", line="[tool in_progress] private-content"),
            dict(lseq=4, kind="worker:tool_update", line="[tool completed] private-content"),
        )
        self.emitter.ingest(AID)
        events = self.page()["events"]
        self.assertIn("source.error", [e["kind"] for e in events])
        tools = [e for e in events if e["kind"].startswith("tool.")]
        self.assertEqual(tools[0]["payload"]["tool_name"], "unknown")
        self.assertTrue(all(e["payload"]["call_id"] != tools[0]["payload"]["call_id"] for e in tools[1:]))
        self.assertNotIn("private-content", json.dumps(events))

    def test_source_bounds_and_oversized_record(self):
        self.log.write_bytes(b"x" * 70000 + b"\r\n")
        self.write(dict(lseq=2, kind="worker:tool_call", line="[tool edit] edit"))
        source = LogSource(self.log, AID)
        token = None
        facts = []
        for _ in range(8):
            page = source.read_batch(token, limit=1)
            self.assertLessEqual(len(page["records"]), 1)
            token = page["next_cursor"]
            facts.extend(page["records"])
            if not page["has_more"]:
                break
        self.assertEqual([e["kind"] for e in facts], ["source.error", "source.gap", "tool.start"])
        self.assertNotIn("x" * 100, json.dumps(facts))
        self.assertEqual(self.page()["head"], None)  # source reads cannot advance ledger

    def test_atomic_failure_and_exact_replay(self):
        self.write(dict(lseq=1, kind="worker:tool_call", line="[tool execute] bash"))

        def fail(args):
            if args["kind"] == "source.checkpoint":
                raise OSError("private-disk-failure")
            return original(args)

        original = self.emitter._writer_args
        with patch.object(self.emitter, "_writer_args", side_effect=fail):
            self.emitter.ingest(AID)
        self.assertEqual(self.page()["events"], [])
        self.assertEqual(self.emitter.health()["recording"], "degraded")
        self.assertNotIn("private", json.dumps(self.emitter.health()))
        self.emitter.recover({})
        before = self.page()
        self.assertEqual(self.emitter.health()["completeness"], "complete")
        # A lost/rebuilt local cursor replays the same source bytes by fact
        # identity; neither the mark nor its checkpoint is appended twice.
        db = sqlite3.connect(self.emitter.path)
        try:
            db.execute("DELETE FROM hyvv_log_cursor")
            db.commit()
        finally:
            db.close()
        self.emitter.ingest(AID)
        self.assertEqual(self.page(), before)
        self.log.unlink()
        restarted = RailsEmitter(self.root)
        restarted.recover({})
        self.assertIn("missing_journal", restarted.health()["warnings"])
        self.assertEqual(self.page(), before)

    def test_export_integrity_cursor_and_no_init(self):
        self.write(dict(lseq=1, kind="worker:tool_call", line="[tool read] read"))
        self.emitter.ingest(AID)
        page = self.page(limit=1)
        self.assertEqual(page["next_cursor"], 1)
        with self.assertRaises(ExportError) as cm:
            self.page(after=100)
        self.assertEqual(cm.exception.code, "reset_required")
        db = sqlite3.connect(self.emitter.path)
        try:
            db.execute("DROP TRIGGER event_rows_no_update")
            db.execute(
                "UPDATE event_rows SET envelope_json=replace(envelope_json, 'worker_report','owner_record') WHERE seq=1"
            )
            db.commit()
        finally:
            db.close()
        with self.assertRaises(ExportError):
            self.page()
        self.emitter.path.unlink()
        with self.assertRaises(ExportError):
            self.page()
        self.assertFalse(self.emitter.path.exists())
