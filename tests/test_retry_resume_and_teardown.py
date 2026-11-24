"""RED for wave A slice HY-2 (review Fix now #3 and #4): retries resume, and
every sandbox is torn down exactly once.

#3: a same-model rate limit drops run_id and sandbox and restarts the bee
from zero with the full prompt. When the backend can resume, the launcher
keeps the sandbox and session and sends a short continue turn instead.
#4: cleanup is keyed by attempt id, so after a retry the second sandbox is
never torn down. Key it by sandbox.
Run: python3 -m pytest -q tests/test_retry_resume_and_teardown.py   (no cua, no network)
"""
import asyncio, os, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
sys.modules.setdefault("cua", mock.MagicMock())
import launcher  # noqa: E402

KEY_ENV = {"ORBIT_WORKER_GO_KEY": "fixture-value-not-a-real-key"}


class CountingBackend(launcher.FakeBackend):
    """FakeBackend with one distinct sandbox per start and a teardown ledger."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.cleaned = []

    async def start(self, a, key):
        out = await super().start(a, key)
        out["sandbox"] = f"sb-{self.starts}"
        return out

    async def cleanup(self, a):
        self.cleaned.append(a.get("sandbox"))


class ResumingBackend(CountingBackend):
    """A backend that can continue the same session in the same sandbox."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.resumes = []

    async def resume(self, a, prompt):
        self.resumes.append({"sandbox": a.get("sandbox"), "run_id": a.get("run_id"), "prompt": prompt})
        r = self.runs[a["run_id"]]
        base = len(r["events"])
        r["events"].extend(dict(e, seq=base + i + 1) for i, e in enumerate(
            [{"kind": "turn_started"}, {"kind": "message", "text": "done"}, {"kind": "turn_ended", "stop_reason": "end_turn"}]))
        return {"sandbox": a.get("sandbox"), "run_id": a["run_id"]}


def _launcher(d, backend_cls):
    cfg = {"bind": "127.0.0.1", "port": 0, "max_concurrent": 1, "data_dir": d, "backoff_s": [0.1],
           "projects": {}, "model": "deepseek-v4.1-flash", "base_url": launcher.GO_BASE_URL,
           "wire": "openai-chat", "image": "fake-image"}
    la = launcher.Launcher(cfg, backend_cls(cfg))
    threading.Thread(target=la.loop.run_forever, daemon=True).start()
    return la


def _wait(pred, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _settle(la, aid):
    ok = _wait(lambda: la.attempts[aid]["state"] in launcher.TERMINAL)
    time.sleep(0.3)  # let the controller's finally-teardown run
    return ok


class TeardownAcrossRetry(unittest.TestCase):
    def test_both_sandboxes_are_torn_down_once_after_a_restart_retry(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, KEY_ENV):
            la = _launcher(d, CountingBackend)
            aid = "att_tear0000001"
            la.submit(aid, {"objective": "x", "fake": {"rate_limit_first": 1}})
            self.assertTrue(_settle(la, aid), la.attempts[aid].get("detail"))
            self.assertEqual(la.attempts[aid]["state"], "succeeded", la.attempts[aid].get("detail"))
            self.assertEqual(la.backend.starts, 2)
            self.assertEqual(sorted(la.backend.cleaned), ["sb-1", "sb-2"])

    def test_cleanup_once_is_keyed_by_sandbox(self):
        with tempfile.TemporaryDirectory() as d:
            la = _launcher(d, CountingBackend)
            a = {"attempt_id": "att_tear0000002", "sandbox": "sb-1", "run_id": "r1"}

            async def go():
                await la._cleanup_once(a)
                await la._cleanup_once(a)
                a["sandbox"], a["run_id"] = "sb-2", "r2"
                await la._cleanup_once(a)
                await la._cleanup_once(a)

            asyncio.run_coroutine_threadsafe(go(), la.loop).result(5)
            self.assertEqual(la.backend.cleaned, ["sb-1", "sb-2"])


class ResumeAfterRateLimit(unittest.TestCase):
    def test_same_model_rate_limit_resumes_the_same_sandbox(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, KEY_ENV):
            la = _launcher(d, ResumingBackend)
            aid = "att_resm0000001"
            la.submit(aid, {"objective": "x" * 5000, "fake": {"rate_limit_first": 1}})
            self.assertTrue(_settle(la, aid), la.attempts[aid].get("detail"))
            a = la.attempts[aid]
            self.assertEqual(a["state"], "succeeded", a.get("detail"))
            self.assertEqual(la.backend.starts, 1, "a resumable rate limit must not start a fresh run")
            self.assertEqual(len(la.backend.resumes), 1)
            resumed = la.backend.resumes[0]
            self.assertEqual(resumed["sandbox"], "sb-1")
            self.assertLess(len(resumed["prompt"]), 500, "the continue turn must not resend the full prompt")
            self.assertIn("continue", resumed["prompt"].lower())
            self.assertEqual(la.backend.cleaned, ["sb-1"], "one sandbox, torn down once at the end")

    def test_backend_without_resume_still_restarts(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, KEY_ENV):
            la = _launcher(d, CountingBackend)
            aid = "att_resm0000002"
            la.submit(aid, {"objective": "x", "fake": {"rate_limit_first": 1}})
            self.assertTrue(_settle(la, aid))
            self.assertEqual(la.attempts[aid]["state"], "succeeded", la.attempts[aid].get("detail"))
            self.assertEqual(la.backend.starts, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
