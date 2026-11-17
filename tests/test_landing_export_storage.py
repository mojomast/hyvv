"""Offline storage regressions: payload I/O budgets, retention and process locks."""
from __future__ import annotations

import errno
import hashlib
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import landing_export as E


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
            second = store(root, "second", event_id="urn:one", timestamp=2, label="duplicate")
            initial = E.export_events(root)["events"][0]
            self.assertEqual(initial["event"]["data"]["label"], "landed")
            index_file = E.export_index_path(root)
            index = json.loads(index_file.read_bytes())
            # Previous /1 indexes only have identity, hashes and position.
            del index["events"][0]["file"]
            index_file.write_text(json.dumps(index), encoding="utf-8")
            self.assertEqual(E.export_events(root)["events"], [initial])

            second.unlink()
            first.write_bytes(first.read_bytes() + b"\n\n")
            formatted = E.export_events(root)["events"][0]
            self.assertEqual(formatted["sha256"], initial["sha256"])
            self.assertNotEqual(formatted["file_sha256"], initial["file_sha256"])
            self.assertEqual(formatted["file_sha256"], hashlib.sha256(first.read_bytes()).hexdigest())
            self.assertEqual(formatted["cursor"], 1)

            store(root, "second", event_id="urn:one", timestamp=2, label="duplicate")
            first.unlink()
            remaining = E.export_events(root)["events"]
            self.assertEqual([(r["cursor"], r["event"]["data"]["label"]) for r in remaining], [(1, "duplicate")])

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
