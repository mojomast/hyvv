"""Deterministic regression for the queue-position TOCTOU race in Launcher.public.

At 14650e34 public() evaluated self._queue() twice in one expression:
  d["queue_position"] = self._queue().index(a["attempt_id"]) + 1 if a["attempt_id"] in self._queue() else None
so the membership check and the .index() read two different queue snapshots; when the
queue changed in between, .index() raised ValueError: 'att_...' is not in list inside
the POST handler. These tests script _queue snapshots directly - no sleeps, no real
threads - so each interleaving is exact and reproducible.
"""
import importlib.util, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcherqrace", HERE / "launcher.py"); L = importlib.util.module_from_spec(spec); spec.loader.exec_module(L)

QUEUED_ID = "att_cafe00002"  # fake-id style reused from the suite


def launcher():
    tmp = tempfile.mkdtemp(prefix="lchqr-")
    cfg = {"bind": "127.0.0.1", "port": 0, "max_concurrent": 2, "data_dir": tmp, "backoff_s": [0.3],
           "model": "deepseek-v4.1-flash", "base_url": "http://x", "wire": "openai", "image": "cua-bee:test",
           "runtime": "runc", "memory_mb": 4096, "projects": {"demo": {}}}
    return L.Launcher(cfg, L.FakeBackend(cfg))


def script_queue(la, snapshots):
    """Replace _queue with a stub that returns snapshots[call_n] (last one repeats)."""
    calls = []
    def q():
        i = len(calls)
        calls.append(i)
        return list(snapshots[min(i, len(snapshots) - 1)])
    la._queue = q
    return calls


class T(unittest.TestCase):
    def test_race_id_left_queue_between_membership_check_and_index(self):
        la = launcher()
        a = {"attempt_id": QUEUED_ID, "state": "queued"}
        calls = script_queue(la, [[QUEUED_ID], []])  # 1st snapshot has it, later ones do not
        d = la.public(a)  # unmodified tree: ValueError('att_cafe00002' is not in list) right here
        self.assertEqual(d["queue_position"], 1)
        self.assertEqual(len(calls), 1)

    def test_queued_id_present_yields_position_from_one_snapshot(self):
        la = launcher()
        a = {"attempt_id": QUEUED_ID, "state": "queued"}
        calls = script_queue(la, [["att_bee000001", QUEUED_ID, "att_bee000003"]])
        d = la.public(a)
        self.assertEqual(d["queue_position"], 2)  # index(1) + 1, from the single snapshot
        self.assertEqual(len(calls), 1)

    def test_non_queued_attempt_never_reads_queue(self):
        la = launcher()
        a = {"attempt_id": QUEUED_ID, "state": "running"}
        calls = script_queue(la, [["att_bee000001"]])
        d = la.public(a)
        self.assertEqual(len(calls), 0)
        self.assertIsNone(d["queue_position"])  # set, but never numeric


if __name__ == "__main__":
    unittest.main()
