"""Offline storage regressions: payload I/O budgets, retention and process locks."""
from __future__ import annotations

import errno
import hashlib
import json
import multiprocessing
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import landing_export as E
import launcher as L
from rails_emitter import RailsEmitter


def store(root, name, *, event_id=None, timestamp=1, label="landed"):
    """Publish a complete file under a Windows-safe name (id needn't equal stem)."""
    outbox = E.landed_outbox(root)
    outbox.mkdir(exist_ok=True, parents=True)
    path = outbox / f"{name}.json"
    tmp = path.with_suffix(".tmp")
    event = {"type": E.LANDED_EVENT_TYPE, "id": event_id or name, "data": {"label": label}}
    tmp.write_bytes(json.dumps(event, indent=2).encode())
    # Explicit ordering, independent of directory order and clock resolution.
    os.utime(tmp, (1_700_000_000 + timestamp,) * 2)
    tmp.replace(path)
    return path


class PayloadBudget(unittest.TestCase):
    def test_indexed_outbox_at_100_and_1000(self):
        for n in (100, 1000):
            with self.subTest(retained=n), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for i in range(n):
                    store(root, f"row-{i}", timestamp=i)
                E.register_landed_file(root)

                def measure(operation, expected_reads):
                    reads = []
                    read_bytes = Path.read_bytes

                    def counted(path):
                        if path.parent == outbox:
                            reads.append(path.name)
                        return read_bytes(path)

                    with mock.patch.object(Path, "read_bytes", counted), mock.patch.object(
                        hashlib, "sha256", wraps=hashlib.sha256
                    ) as hashes:
                        result = operation()
                    self.assertEqual(reads, expected_reads)
                    self.assertLessEqual(hashes.call_count, 2 * len(expected_reads))
                    return result

                outbox = E.landed_outbox(root)
                page = measure(lambda: E.export_events(root, after=n-1, limit=1), [f"row-{n-1}.json"])
                self.assertEqual([row["event_id"] for row in page["events"]], [f"row-{n-1}"])
                self.assertEqual((page["next_cursor"], page["has_more"]), (n, False))
                page = measure(lambda: E.export_events(root, after=n, limit=1), [])
                self.assertEqual((page["events"], page["next_cursor"], page["has_more"]), ([], n, False))

                path = store(root, "new", timestamp=n)
                measure(lambda: E.register_landed_file(outbox, path), ["new.json"])
                with mock.patch.object(E, "_write_index", side_effect=AssertionError("index rewritten")):
                    measure(lambda: E.register_landed_file(outbox), [])  # launcher replay
                self.assertEqual(E.export_events(root, after=n)["events"][0]["cursor"], n+1)


class RetainedFiles(unittest.TestCase):
    def test_replacement_between_discovery_and_page_read_is_not_misattributed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store(root, "slot", event_id="old")
            E.register_landed_file(root)
            read_row = E._read_row
            replaced = False

            def replace_once(path):
                nonlocal replaced
                if not replaced:
                    store(root, "slot", event_id="new")
                    replaced = True
                return read_row(path)

            with mock.patch.object(E, "_read_row", replace_once):
                page = E.export_events(root, limit=1)
            self.assertEqual([(r["cursor"], r["event_id"], r["event"]["id"]) for r in page["events"]],
                             [(2, "new", "new")])
            self.assertEqual(E.export_events(root, after=1), page)

    def test_legacy_index_raw_hash_changes_and_alias_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = store(root, "alias", event_id="urn:one", timestamp=1)
            second = store(root, "second", event_id="urn:one", timestamp=2)
            initial = E.export_events(root)["events"][0]
            self.assertEqual(initial["event"]["data"]["label"], "landed")
            index_file = E.export_index_path(root)
            index = json.loads(index_file.read_bytes())
            # Previous /1 indexes only have identity, hashes and position.
            index["schema"] = "rizzstack.bee-landed-export-index/1"
            del index["events"][0]["file"]
            index_file.write_text(json.dumps(index), encoding="utf-8")
            self.assertEqual(E.export_events(root)["events"], [initial])
            self.assertEqual(json.loads(index_file.read_bytes())["schema"], E.EXPORT_INDEX_SCHEMA)

            second.unlink()
            first.write_bytes(first.read_bytes() + b"\n\n")
            formatted = E.export_events(root)["events"][0]
            self.assertEqual(formatted["sha256"], initial["sha256"])
            self.assertNotEqual(formatted["file_sha256"], initial["file_sha256"])
            self.assertEqual(formatted["file_sha256"], hashlib.sha256(first.read_bytes()).hexdigest())
            self.assertEqual(formatted["cursor"], 1)

            store(root, "second", event_id="urn:one", timestamp=2)
            first.unlink()
            remaining = E.export_events(root)["events"]
            self.assertEqual(remaining, [initial])

    def test_divergence_blocks_even_past_cursor_before_or_after_first_alias_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = store(root, "original", event_id="one", timestamp=2)
            initial = E.export_events(root)["events"]
            index_file = E.export_index_path(root)
            before = index_file.read_bytes()
            store(root, "later", timestamp=1)  # discovered before the conflict
            alias = store(root, "alias", event_id="one", timestamp=3, label="different")
            handler_type = L.make_handler(SimpleNamespace(dir=root), "t" * 40)
            handler = handler_type.__new__(handler_type)
            handler.path = "/v1/landing/events?after=1"
            handler.headers = {"Authorization": "Bearer " + "t" * 40}
            handler._send = lambda status, body: (status, body)
            for remove_original in (False, True):
                if remove_original:
                    first.unlink()
                status, body = handler.do_GET()
                self.assertEqual((status, body["code"]), (409, "landing_content_conflict"))
                self.assertNotIn("next_cursor", body)
                with self.assertRaises(E.LandingContentConflict):
                    E.register_landed_file(root)
                self.assertEqual(index_file.read_bytes(), before)
            alias.unlink()
            store(root, "restored", event_id="one")
            rows = E.export_events(root)["events"]
            self.assertEqual(rows[0], initial[0])
            self.assertEqual((rows[1]["event_id"], rows[1]["cursor"]), ("later", 2))

    def test_conflicting_first_scan_preserves_first_observed_commitment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = store(root, "first", event_id="one")
            expected = E._read_row(first)["sha256"]
            store(root, "later", timestamp=2)
            store(root, "second", event_id="one", timestamp=3, label="different")
            with self.assertRaises(E.LandingContentConflict):
                E.export_events(root)
            first.unlink()
            with self.assertRaises(E.LandingContentConflict):
                E.export_events(root, after=1)
            index = json.loads(E.export_index_path(root).read_bytes())
            self.assertEqual([(e["position"], e["sha256"]) for e in index["events"]], [(1, expected)])

    def test_invalid_legacy_commitment_is_not_replaced_from_current_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store(root, "one")
            E.register_landed_file(root)
            path = E.export_index_path(root)
            index = json.loads(path.read_bytes())
            index["schema"] = "rizzstack.bee-landed-export-index/1"
            del index["events"][0]["sha256"]
            corrupt = json.dumps(index).encode()
            path.write_bytes(corrupt)
            with self.assertRaises(OSError):
                E.export_events(root, after=1)
            self.assertEqual(path.read_bytes(), corrupt)

    def test_legacy_hint_cannot_conceal_mismatch_behind_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store(root, "one")
            E.register_landed_file(root)
            path = E.export_index_path(root)
            index = json.loads(path.read_bytes())
            row = index["events"][0]
            index["schema"] = "rizzstack.bee-landed-export-index/1"
            row["sha256"] = "0" * 64  # retained bytes no longer match this pin
            path.write_bytes(json.dumps(index).encode())
            with self.assertRaises(E.LandingContentConflict):
                E.export_events(root, after=1)
            migrated = json.loads(path.read_bytes())
            self.assertEqual(migrated["schema"], E.EXPORT_INDEX_SCHEMA)
            self.assertEqual(migrated["events"][0]["sha256"], "0" * 64)
            self.assertEqual(migrated["next_position"], 2)

    def test_semantic_replacement_during_first_page_read_is_not_repinned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = store(root, "one")
            digest = E._read_row(first)["sha256"]
            store(root, "later", timestamp=2)
            real_page = E._read_page

            def replace(*args):
                store(root, "one", label="different")
                return real_page(*args)

            with mock.patch.object(E, "_read_page", replace), self.assertRaises(E.LandingContentConflict):
                E.export_events(root)
            with self.assertRaises(E.LandingContentConflict):
                E.export_events(root, after=1)
            index = json.loads(E.export_index_path(root).read_bytes())
            self.assertEqual([(e["position"], e["sha256"]) for e in index["events"]], [(1, digest)])

    def test_missing_and_malformed_tail_never_supply_has_more_or_recycle_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [store(root, f"row-{i}", timestamp=i) for i in range(3)]
            E.register_landed_file(root)
            paths[1].write_bytes(b"incomplete")
            paths[2].unlink()
            page = E.export_events(root, limit=1)
            self.assertEqual((page["next_cursor"], page["has_more"]), (1, False))
            paths[0].unlink()
            self.assertEqual(E.export_events(root)["next_cursor"], 0)
            self.assertEqual(E.export_events(root, after=3)["next_cursor"], 3)
            # A reused filename isn't proof the original ID has returned.
            store(root, "row-0", event_id="new-id")
            store(root, "other-name", event_id="row-1", timestamp=2)
            rows = E.export_events(root, after=1)["events"]
            self.assertEqual([(r["event_id"], r["cursor"]) for r in rows], [("row-1", 2), ("new-id", 4)])


class NotifierRegistration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        transport = mock.patch.object(L, "notify_landed_event", return_value=True)
        self.notify = transport.start()
        self.addCleanup(transport.stop)

    def test_replay_requires_prior_registration_and_rejects_divergence(self):
        original = store(self.root, "one")
        index = E.export_index_path(self.root)
        with self.assertRaises(OSError):
            L.replay_landed_events(self.root)
        self.assertFalse(index.exists())
        E.register_landed_file(self.root)
        before = index.read_bytes()
        store(self.root, "alias", event_id="one", label="different")
        for remove in (False, True):
            if remove:
                original.unlink()
            with self.assertRaises(L.LandedEventConflict):
                L.replay_landed_events(self.root)
            self.assertEqual(index.read_bytes(), before)
        index.write_bytes(b"bad index")
        with self.assertRaises(OSError):
            L.replay_landed_events(self.root)
        self.notify.assert_not_called()

    def test_replay_sends_verified_objects_across_pages_once_per_position(self):
        first = store(self.root, "one")
        second = store(self.root, "two", timestamp=2)
        expected = [json.loads(p.read_bytes()) for p in (first, second)]
        E.register_landed_file(self.root)
        first.write_bytes(first.read_bytes() + b"\n")
        store(self.root, "alias", event_id="one", timestamp=3)
        cfg = {"launcher_id": "fixture"}
        with mock.patch.object(E, "DEFAULT_LIMIT", 1):
            self.assertEqual(L.replay_landed_events(self.root, cfg), (2, 2))
        self.assertEqual(self.notify.call_args_list, [mock.call(event, cfg, None) for event in expected])

    def test_redelivery_does_not_reopen_verified_files(self):
        store(self.root, "one")
        second = store(self.root, "two", timestamp=2)
        pinned = json.loads(second.read_bytes())
        E.register_landed_file(self.root)

        def deliver(event, cfg, notifier):
            if event["id"] == "one":
                store(self.root, "two", timestamp=2, label="changed after page read")
            return True

        self.notify.side_effect = deliver
        self.assertEqual(L.replay_landed_events(self.root), (2, 2))
        self.assertEqual(self.notify.call_args.args[0], pinned)
        with self.assertRaises(L.LandedEventConflict):
            L.replay_landed_events(self.root)
        self.assertEqual(self.notify.call_count, 2)

    def test_fresh_publication_signals_only_after_matching_registration(self):
        attempt = {"attempt_id": "att_native", "kind": "check", "state": "succeeded", "updated_at": 100,
                   "brief": {"source_rev": "a" * 40}}
        cfg = {"launcher_id": "fixture"}
        with mock.patch.object(E, "_write_index", side_effect=OSError("unavailable")):
            path, event, created = L.emit_landed_record(attempt, self.root, cfg)
        self.assertTrue(created)
        self.assertEqual(json.loads(path.read_bytes()), event)
        with self.assertRaises(OSError):
            L.replay_landed_events(self.root, cfg)
        self.notify.assert_not_called()

        _, fresh, created = L.emit_landed_record(dict(attempt, attempt_id="att_fresh"), self.root, cfg)
        self.assertTrue(created)
        self.notify.assert_called_once_with(fresh, cfg)
        self.assertEqual(L.replay_landed_events(self.root, cfg), (2, 2))
        self.assertCountEqual([call.args[0] for call in self.notify.call_args_list[1:]], [event, fresh])

    def test_publication_registration_gap_cannot_signal_an_unpinned_value(self):
        attempt = {"attempt_id": "att_native", "kind": "check", "state": "succeeded", "updated_at": 100,
                   "brief": {"source_rev": "a" * 40}}
        register = E.register_landed_file

        def replace_then_register(outbox, **kwargs):
            path, = outbox.glob("*.json")
            changed = dict(json.loads(path.read_bytes()), subject="changed")
            path.write_text(json.dumps(changed), encoding="utf-8")
            return register(outbox, **kwargs)

        with mock.patch.object(E, "register_landed_file", replace_then_register), self.assertRaises(L.LandedEventConflict):
            L.emit_landed_record(attempt, self.root)
        self.notify.assert_not_called()
        self.assertFalse(E.export_index_path(self.root).exists())


class JournalAck(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.aid = "att_1234567890abcdef"
        self.attempt = {"attempt_id": self.aid, "lseq": 1}
        self.record = {"kind": "state", "state": "running", "lseq": 1}
        (self.root / "attempts").mkdir()
        self.path = self.root / "attempts" / (self.aid + ".events.jsonl")
        self.rails = RailsEmitter(self.root)
        self.launcher = SimpleNamespace(dir=self.root, rails=self.rails)

    def test_journal_barriers_precede_observer_and_replay(self):
        actions = []
        sync = os.fsync

        def observe(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                actions.append("directory")
            else:
                self.assertEqual(json.loads(self.path.read_bytes()), self.record)
                actions.append("file")
            sync(fd)

        def unavailable():
            actions.append("ingest")
            raise OSError("unavailable")

        with mock.patch.object(os, "fsync", observe), mock.patch.object(self.rails, "_writer", unavailable):
            L.Launcher._append_events(self.launcher, self.attempt, [self.record])
        self.assertEqual(actions, ["file", "ingest"] if os.name == "nt" else ["file", "directory", "ingest"])
        self.assertIn("replay_required", self.rails.health()["warnings"])
        restarted = RailsEmitter(self.root)
        restarted.recover({self.aid: self.attempt})
        health = restarted.health()
        self.assertEqual((health["completeness"], health["completeness_scope"]), ("complete", "retained_journals"))
        self.assertEqual(health["power_loss_durability"], "unproven")

    def test_source_sync_failure_is_not_acknowledged_or_ingested(self):
        for directory in ((False,) if os.name == "nt" else (False, True)):
            with self.subTest(directory=directory):
                def fail(fd):
                    if stat.S_ISDIR(os.fstat(fd).st_mode) == directory:
                        raise OSError("private")

                with mock.patch.object(os, "fsync", fail), mock.patch.object(self.rails, "ingest") as ingest:
                    with self.assertRaises(OSError):
                        L.Launcher._append_events(self.launcher, self.attempt, [self.record])
                    ingest.assert_not_called()
                self.assertEqual(self.rails.health()["warnings"], ["journal_write_unconfirmed"])

    def test_clean_eof_before_known_manifest_tail_degrades_after_restart(self):
        self.path.write_text(json.dumps(self.record) + "\n", "utf-8")
        self.rails.recover({self.aid: dict(self.attempt, lseq=2)})
        health = self.rails.health()
        self.assertEqual((health["recording"], health["completeness"]), ("degraded", "partial"))
        self.assertIn("journal_shorter_than_manifest", health["warnings"])


# All child targets are importable at module scope for Windows spawn.
def paused_writer(root, paused, resume, crash):
    store(root, "first")
    real_write = E._write_index

    def before_replace(path, index):
        paused.set()  # read+discovery are complete but the index is unpublished
        if not resume.wait(30):
            raise TimeoutError("parent did not release writer")
        if crash:
            os._exit(17)  # deliberately bypass Python cleanups while holding the lock
        real_write(path, index)

    with mock.patch.object(E, "_write_index", before_replace):
        E.register_landed_file(root)


def second_writer(root, attempting):
    store(root, "second", timestamp=2)
    attempting.set()
    E.register_landed_file(root)


class ProcessLock(unittest.TestCase):
    def start(self, ctx, target, *args):
        child = ctx.Process(target=target, args=args)
        child.start()

        def cleanup():
            if child.is_alive():
                child.kill()
            child.join(10)
            child.close()

        self.addCleanup(cleanup)
        return child

    def contend(self, crash):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        ctx = multiprocessing.get_context("spawn")
        paused, resume, attempting = (ctx.Event() for _ in range(3))
        first = self.start(ctx, paused_writer, root, paused, resume, crash)
        self.assertTrue(paused.wait(30), "first writer never reached index publication")
        try:
            second = self.start(ctx, second_writer, root, attempting)
            self.assertTrue(attempting.wait(30), "second writer did not start")
            # Probe byte zero nonblocking from a *different* process. No timing
            # threshold, sleeps, or self-healing export masking a lost update.
            with (root / "events" / "export-index.lock").open("r+b") as f:
                with self.assertRaises(OSError) as blocked:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertIn(blocked.exception.errno, (errno.EACCES, errno.EAGAIN, errno.EDEADLK))
        finally:
            resume.set()
        first.join(30)
        second.join(30)
        self.assertEqual(first.exitcode, 17 if crash else 0)
        self.assertEqual(second.exitcode, 0)
        # Inspect disk *before* export can reconcile it. The first position is
        # durable even if the earlier writer died in the file/index gap.
        index = json.loads(E.export_index_path(root).read_bytes())
        self.assertEqual([(r["position"], r["event_id"]) for r in index["events"]], [(1, "first"), (2, "second")])
        self.assertEqual(E.export_events(root, after=1)["next_cursor"], 2)

    def test_processes_serialize_index_registration(self):
        self.contend(crash=False)

    def test_process_death_releases_lock_and_recovers_orphan_before_later_file(self):
        self.contend(crash=True)
