"""Bounded launcher recovery (hyvv#106): restart cost is linear in the attempt journal.

Startup on a worker PC took ~414 s at 1297 attempts because every terminal attempt re-read
the whole landed outbox, reloaded the export index and opened its own rails.sqlite writers.
These tests pin the bounded recovery: one outbox scan, one batched index registration, one
reused rails writer, and an idempotent skip of attempts whose landed record and receipt are
already recorded, without losing restart, partial-state, conflict, interrupted-write or replay
behaviour.

File and database opens are counted with a CPython audit hook ("open", "sqlite3.connect"),
so the counts do not depend on internal function names.

Run: python3 -m pytest -q -p no:cacheprovider tests/test_recovery_bounded.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest import mock

import launcher
import rails_emitter

_RECORDERS: list = []


def _audit(event, args):
    if _RECORDERS and event in ("open", "sqlite3.connect"):
        for rec in _RECORDERS:
            rec.append((event, args))


sys.addaudithook(_audit)


@contextmanager
def recording():
    rec: list = []
    _RECORDERS.append(rec)
    try:
        yield rec
    finally:
        _RECORDERS.remove(rec)


def _path_of(arg):
    if isinstance(arg, bytes):
        return os.fsdecode(arg)
    return arg if isinstance(arg, str) else (os.fspath(arg) if hasattr(arg, "__fspath__") else None)


def _writes(args):
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else 0
    if isinstance(mode, str):
        return any(c in mode for c in "wax+")
    return bool(isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))


class Counts:
    def __init__(self, rec, data_dir):
        root = Path(data_dir).resolve()
        self.outbox_reads = self.index_reads = self.event_writes = 0
        self.writer_opens = self.reader_opens = 0
        outbox = str(root / "events" / "outbox") + os.sep
        index = str(root / "events" / "export-index.json")
        rails = str(root / "rails.sqlite")
        for event, args in rec:
            if event == "open":
                path = _path_of(args[0])
                if path is None:
                    continue
                path = os.path.abspath(path)
                if path.startswith(str(root / "events") + os.sep) and _writes(args):
                    if not path.endswith(".publish.lock") and not path.endswith(".lock"):
                        self.event_writes += 1
                    continue
                if path.startswith(outbox) and path.endswith(".json") and not _writes(args):
                    self.outbox_reads += 1
                elif path == index and not _writes(args):
                    self.index_reads += 1
            else:
                db = _path_of(args[0]) or ""
                if db.startswith("file:"):
                    if rails.replace(os.sep, "/") in db.replace("%2F", "/") or db.endswith("rails.sqlite?mode=ro"):
                        self.reader_opens += 1
                elif os.path.abspath(db) == rails:
                    self.writer_opens += 1


def _attempt(aid, state="succeeded"):
    brief = {"objective": "o", "feature_id": "feat_1", "job_id": "job_1", "task_id": "task_1",
             "source_rev": "a" * 40, "coordinator": {"id": "orbit", "session": "sess_1"},
             "lease_sha256": "b" * 64}
    return {"attempt_id": aid, "kind": "opencode", "brief": brief, "state": "running", "detail": "",
            "created_at": 100.0, "updated_at": 100.0, "lseq": 0, "model": "deepseek-v4.1-flash"}


def _aid(i):
    return "att_%016x" % (0xB0B0000 + i)


class Journal:
    """A data dir with N terminal attempts; `missing` of them crashed before their outbox write."""

    def __init__(self, n, *, missing=0, no_receipt=0):
        self._tmp = tempfile.TemporaryDirectory(prefix="hyvv-recovery-")
        self.dir = Path(self._tmp.name)
        self.n = n
        self.cfg = {"data_dir": str(self.dir), "model": "deepseek-v4.1-flash", "image": "fixture",
                    "projects": {}, "max_concurrent": 1, "base_url": "http://x", "wire": "openai",
                    "launcher_id": "worker-1", "rails_enabled": True}
        la = self.launch()
        self.missing = [_aid(i) for i in range(n - missing, n)]
        self.no_receipt = [_aid(i) for i in range(no_receipt)]
        real_receipt = rails_emitter.RailsEmitter.receipt

        def receipt(emitter, aid, *args, **kwargs):
            if aid in self.no_receipt:
                return None  # crash between the outbox write and the receipt append
            return real_receipt(emitter, aid, *args, **kwargs)

        with mock.patch.object(rails_emitter.RailsEmitter, "receipt", receipt):
            for i in range(n):
                aid = _aid(i)
                a = _attempt(aid)
                la.attempts[aid] = a
                la._save(a)
                self.write_result(aid)
                if aid in self.missing:
                    # Durable terminal journal write, then a crash before the outbox write.
                    a["state"], a["lseq"] = "succeeded", a["lseq"] + 1
                    la._save(a)
                else:
                    la._set(a, "succeeded", "done")
        la.loop.close()

    def write_result(self, aid):
        path = self.dir / "attempts" / f"{aid}.result.json"
        path.write_text(json.dumps({"schema": "orbit.local-worker.result/v1", "attempt_id": aid,
                                    "observed": {"usage": {"input_tokens": 11, "output_tokens": 7}}}),
                        "utf-8")

    def launch(self):
        return launcher.Launcher(self.cfg, launcher.FakeBackend(self.cfg))

    def restart(self):
        with recording() as rec:
            la = self.launch()
        la.loop.close()
        return la, Counts(rec, self.dir)

    @property
    def outbox(self):
        return self.dir / "events" / "outbox"

    @property
    def index(self):
        return self.dir / "events" / "export-index.json"

    def outbox_files(self):
        return {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in self.outbox.glob("*.json")}

    def receipts(self):
        with closing(sqlite3.connect("file:%s?mode=ro" % (self.dir / "rails.sqlite"), uri=True)) as db:
            return sorted(r[0] for r in db.execute(
                "SELECT dedupe_key FROM dedupe_index WHERE dedupe_key LIKE '%:final'"))

    def rails_rows(self):
        with closing(sqlite3.connect("file:%s?mode=ro" % (self.dir / "rails.sqlite"), uri=True)) as db:
            return db.execute("SELECT COUNT(*) FROM event_rows").fetchone()[0]

    def close(self):
        self._tmp.cleanup()


class JournalCase(unittest.TestCase):
    def journal(self, n, **kwargs):
        j = Journal(n, **kwargs)
        self.addCleanup(j.close)
        return j


class RecoveryScalingTest(JournalCase):
    """Cost of one restart on a mixed journal (most attempts recorded, some missing)."""

    def test_restart_parses_each_outbox_file_at_most_twice(self):
        j = self.journal(24, missing=4)
        _, counts = j.restart()
        self.assertLessEqual(counts.outbox_reads, 2 * j.n, vars(counts))

    def test_outbox_reads_grow_linearly_not_quadratically(self):
        small, large = self.journal(10, missing=2), self.journal(30, missing=6)
        a, b = small.restart()[1].outbox_reads, large.restart()[1].outbox_reads
        # 3x the attempts may cost at most ~3x the reads; a per-attempt full scan costs ~9x.
        self.assertLessEqual(b, 3 * a + 6, (a, b))

    def test_restart_loads_the_export_index_a_constant_number_of_times(self):
        j = self.journal(24, missing=4)
        _, counts = j.restart()
        self.assertLessEqual(counts.index_reads, 2, vars(counts))

    def test_restart_writes_the_export_index_once_for_a_batch(self):
        j = self.journal(24, missing=4)
        before = {p.name for p in j.outbox.glob("*.json")}
        with mock.patch("landing_export._write_index", wraps=launcher._landing_export._write_index) as write:
            j.restart()
        self.assertEqual(len({p.name for p in j.outbox.glob("*.json")} - before), 4)
        self.assertLessEqual(write.call_count, 1)

    def test_restart_reuses_one_rails_writer(self):
        small, large = self.journal(6, missing=1), self.journal(24, missing=4)
        a, b = small.restart()[1], large.restart()[1]
        self.assertLessEqual(b.writer_opens, 3, vars(b))
        self.assertEqual(a.writer_opens, b.writer_opens, (vars(a), vars(b)))


class RecoveryIdempotenceTest(JournalCase):
    def test_recorded_attempts_are_skipped_on_restart(self):
        j = self.journal(16)
        j.restart()  # settle anything a first restart may still record
        calls = {"emit": 0, "receipt": 0}
        real_emit, real_receipt = launcher.emit_landed_record, rails_emitter.RailsEmitter.receipt

        def emit(*args, **kwargs):
            calls["emit"] += 1
            return real_emit(*args, **kwargs)

        def receipt(*args, **kwargs):
            calls["receipt"] += 1
            return real_receipt(*args, **kwargs)

        with mock.patch.object(launcher, "emit_landed_record", emit), \
                mock.patch.object(rails_emitter.RailsEmitter, "receipt", receipt):
            _, counts = j.restart()
        self.assertEqual(calls, {"emit": 0, "receipt": 0})
        self.assertLessEqual(counts.outbox_reads, j.n, vars(counts))
        self.assertLessEqual(counts.writer_opens, 2, vars(counts))

    def test_second_restart_writes_nothing(self):
        j = self.journal(12, missing=3, no_receipt=2)
        j.restart()
        files, index, rows, receipts = j.outbox_files(), j.index.read_bytes(), j.rails_rows(), j.receipts()
        _, counts = j.restart()
        self.assertEqual(counts.event_writes, 0, vars(counts))
        self.assertEqual(j.outbox_files(), files)
        self.assertEqual(j.index.read_bytes(), index)
        self.assertEqual(j.rails_rows(), rows)
        self.assertEqual(j.receipts(), receipts)

    def test_partial_state_recovers_exactly_the_missing_events(self):
        j = self.journal(12, missing=3)
        before = j.outbox_files()
        la, _ = j.restart()
        after = j.outbox_files()
        self.assertEqual(len(after), 12)
        self.assertEqual({k: v for k, v in after.items() if k in before}, before)
        new = [json.loads(v[1])["data"]["attempt_id"] for k, v in after.items() if k not in before]
        self.assertEqual(sorted(new), sorted(j.missing))
        self.assertEqual(la.landing_conflicts, [])
        index = json.loads(j.index.read_text("utf-8"))
        self.assertEqual(len(index["events"]), 12)
        self.assertEqual([e["position"] for e in index["events"]], list(range(1, 13)))

    def test_conflicting_outbox_evidence_is_still_detected(self):
        j = self.journal(8)
        victim = _aid(3)
        attempt_path = j.dir / "attempts" / f"{victim}.json"
        a = json.loads(attempt_path.read_text("utf-8"))
        a["state"] = "failed"  # the journal now disagrees with the recorded landed event
        attempt_path.write_text(json.dumps(a), "utf-8")
        before = j.outbox_files()
        la, _ = j.restart()
        self.assertEqual([c["attempt_id"] for c in la.landing_conflicts], [victim])
        self.assertEqual(j.outbox_files(), before)


class InterruptedWriteTest(JournalCase):
    def test_stale_outbox_temp_file_is_ignored_and_the_event_written_once(self):
        j = self.journal(8, missing=1)
        stale = j.outbox / f".urn:rizzstack:bee-landed:deadbeef.{os.getpid()}.cafe.tmp"
        stale.write_bytes(b'{"specversion": "1.0", "id": "urn:riz')  # torn write
        j.restart()
        j.restart()
        events = [json.loads(p.read_text("utf-8")) for p in j.outbox.glob("*.json")]
        self.assertEqual(sorted(e["data"]["attempt_id"] for e in events), sorted(_aid(i) for i in range(8)))

    def test_lost_export_index_is_rebuilt_in_one_pass(self):
        j = self.journal(10, missing=2)
        j.index.unlink()  # crash before the first index write ever landed
        la, counts = j.restart()
        index = json.loads(j.index.read_text("utf-8"))
        self.assertEqual(len(index["events"]), 10)
        self.assertEqual(la.landing_conflicts, [])
        # Rebuilding the index may read each file once more, but never once per attempt.
        self.assertLessEqual(counts.outbox_reads, 3 * j.n, vars(counts))

    def test_interrupted_receipt_is_recorded_exactly_once(self):
        j = self.journal(10, no_receipt=4)
        self.assertEqual(len(j.receipts()), 6)
        j.restart()
        self.assertEqual(j.receipts(), sorted(_aid(i) + ":final" for i in range(10)))
        rows = j.rails_rows()
        j.restart()
        self.assertEqual(j.rails_rows(), rows)


class ReplayTest(JournalCase):
    def test_replay_after_bounded_restart_is_unchanged(self):
        j = self.journal(10, missing=2)
        sink = Path(tempfile.mkdtemp(prefix="hyvv-replay-"))
        self.addCleanup(shutil.rmtree, sink, True)

        def replay(la, name):
            result = la.replay_landed_events(notifier=str(sink / name))
            lines = (sink / name).read_text("utf-8").splitlines() if (sink / name).exists() else []
            return result, [json.loads(line)["id"] for line in lines]

        la, _ = j.restart()
        first, sent = replay(la, "first.jsonl")
        la2, _ = j.restart()
        second, again = replay(la2, "second.jsonl")
        self.assertEqual(first, (10, 10))
        self.assertEqual(second, first)
        self.assertEqual(again, sent)

if __name__ == "__main__":
    unittest.main()
