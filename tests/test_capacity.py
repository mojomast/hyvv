"""GET /v1/capacity: the admission inputs a coordinator needs before it fills bee slots.

A coordinator that "goes max" must not count only its own bees: the launcher is the single
place that sees every attempt (Hermes Forge, box bots, direct callers), the host's free memory
and CPU, whether Docker answers, and which provider lanes recently hit a 429. /v1/capacity
publishes exactly those facts as ``hyvv.capacity/1``. Unknown values are ``null`` -- never a
guess -- so the caller can fail closed. The payload never carries tokens, paths or hosts.
"""
import http.client
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules.setdefault("launcher", L)
spec.loader.exec_module(L)
TOKEN = "c" * 40
LANES = {"kimi", "nous", "go", "other"}
KIMI_CFG = {"k3.1-pro": {"key": "kimi", "base_url": "https://example.invalid/v1", "wire": "openai-chat"}}


def serve(tmp, docker_runner=None):
    cfg = {"bind": "127.0.0.1", "port": 0, "max_concurrent": 12, "data_dir": tmp, "projects": {},
           "model": "go:deepseek-v4.1-flash", "base_url": "https://example.invalid/v1", "wire": "openai-chat",
           "image": "img", "models": dict(KIMI_CFG)}
    la = L.Launcher(cfg, L.FakeBackend(cfg))
    if docker_runner is not None:
        la.docker_probe = L.DockerProbe(runner=docker_runner)
    threading.Thread(target=la.loop.run_forever, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), L.make_handler(la, TOKEN))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return la, srv


def get(srv, path, token=TOKEN):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    c.request("GET", path, headers={"Authorization": "Bearer " + token} if token else {})
    r = c.getresponse()
    raw = r.read()
    return r.status, json.loads(raw), raw.decode("utf-8")


def ok_runner(argv, timeout=None):
    return 0, "8 17179869184 3 27.3.1\n", ""


class CapacityEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lch-cap-")

    def test_shape_and_schema(self):
        la, srv = serve(self.tmp, docker_runner=ok_runner)
        st, body, _ = get(srv, "/v1/capacity")
        self.assertEqual(st, 200)
        self.assertEqual(body["schema"], "hyvv.capacity/1")
        self.assertEqual(body["max_concurrent"], 12)
        self.assertEqual(body["active"], 0)
        self.assertEqual(body["queued"], 0)
        self.assertEqual(set(body["active_by_lane"]), LANES)
        self.assertEqual(set(body["queued_by_lane"]), LANES)
        for key in ("mem_total_mb", "mem_available_mb", "cpu_count", "cpu_busy_pct"):
            self.assertIn(key, body["host"])
        for key in ("ok", "age_s", "ncpu", "mem_total_mb", "containers_running", "detail"):
            self.assertIn(key, body["docker"])
        self.assertEqual(body["rate_limits"]["window_s"], L.CAPACITY_RATE_LIMIT_WINDOW_S)
        self.assertEqual(set(body["rate_limits"]["by_lane"]), LANES)
        self.assertIn("split_brain", body)
        self.assertIn("observed_at", body)

    def test_requires_the_bearer_token(self):
        la, srv = serve(self.tmp, docker_runner=ok_runner)
        st, _body, _ = get(srv, "/v1/capacity", token="")
        self.assertEqual(st, 401)

    def test_active_and_queued_are_counted_by_lane(self):
        la, srv = serve(self.tmp, docker_runner=ok_runner)
        now = time.time()
        rows = [("a1", "running", "nous:hy4-flash"), ("a2", "starting", "k3.1-pro"),
                ("a3", "running", "go:deepseek-v4.1-flash"), ("a4", "queued", "nous:deepseek-v4.1-flash-1017"),
                ("a5", "succeeded", "go:deepseek-v4.1-flash")]
        with la.lock:
            for aid, state, model in rows:
                la.attempts[aid] = {"attempt_id": aid, "state": state, "model": model, "kind": "opencode",
                                    "created_at": now, "brief": {}}
        st, body, _ = get(srv, "/v1/capacity")
        self.assertEqual(body["active"], 3)
        self.assertEqual(body["queued"], 1)
        self.assertEqual(body["active_by_lane"], {"kimi": 1, "nous": 1, "go": 1, "other": 0})
        self.assertEqual(body["queued_by_lane"], {"kimi": 0, "nous": 1, "go": 0, "other": 0})

    def test_recent_rate_limits_are_reported_per_lane_and_expire(self):
        la, srv = serve(self.tmp, docker_runner=ok_runner)
        la.note_rate_limit("nous:hy4-flash")
        la.note_rate_limit("k3.1-pro")
        la.note_rate_limit("go:deepseek-v4.1-flash", at=time.time() - L.CAPACITY_RATE_LIMIT_WINDOW_S - 5)
        st, body, _ = get(srv, "/v1/capacity")
        lanes = body["rate_limits"]["by_lane"]
        self.assertEqual(lanes["nous"]["count"], 1)
        self.assertEqual(lanes["kimi"]["count"], 1)
        self.assertEqual(lanes["go"]["count"], 0)
        self.assertIsNotNone(lanes["nous"]["last_at"])

    def test_backoff_records_a_rate_limit(self):
        src = (HERE / "launcher.py").read_text(encoding="utf-8")
        body = src.split("async def _backoff", 1)[1].split("\n    async def ", 1)[0]
        self.assertIn("note_rate_limit", body)
        body = src.split("async def _fallback_to_deepseek", 1)[1].split("\n    async def ", 1)[0]
        self.assertIn("note_rate_limit", body)

    def test_payload_carries_no_token_or_path(self):
        la, srv = serve(self.tmp, docker_runner=ok_runner)
        _st, _body, raw = get(srv, "/v1/capacity")
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn(self.tmp, raw)
        self.assertNotIn("example.invalid", raw)


class Lanes(unittest.TestCase):
    def test_lane_mapping(self):
        cfg = {"model": "go:deepseek-v4.1-flash", "models": dict(KIMI_CFG)}
        self.assertEqual(L.capacity_lane(cfg, "nous:hy4-flash"), "nous")
        self.assertEqual(L.capacity_lane(cfg, "nous:deepseek-v4.1-flash-1017"), "nous")
        self.assertEqual(L.capacity_lane(cfg, "go:deepseek-v4.1-flash"), "go")
        self.assertEqual(L.capacity_lane(cfg, "k3.1-pro"), "kimi")
        self.assertEqual(L.capacity_lane(cfg, "deepseek:deepseek-flash"), "other")
        self.assertEqual(L.capacity_lane(cfg, "no-such-model"), "other")
        self.assertEqual(L.capacity_lane(cfg, None), "other")


class Docker(unittest.TestCase):
    def test_healthy_probe(self):
        p = L.DockerProbe(runner=ok_runner)
        p.refresh()
        snap = p.snapshot()
        self.assertIs(snap["ok"], True)
        self.assertEqual(snap["ncpu"], 8)
        self.assertEqual(snap["mem_total_mb"], 16384)
        self.assertEqual(snap["containers_running"], 3)

    def test_timeout_is_unhealthy(self):
        def runner(argv, timeout=None):
            raise subprocess.TimeoutExpired(argv, timeout)
        p = L.DockerProbe(runner=runner)
        p.refresh()
        snap = p.snapshot()
        self.assertIs(snap["ok"], False)
        self.assertEqual(snap["detail"], "timeout")

    def test_nonzero_exit_is_unhealthy(self):
        p = L.DockerProbe(runner=lambda argv, timeout=None: (1, "", "error during connect: pipe"))
        p.refresh()
        self.assertIs(p.snapshot()["ok"], False)
        self.assertEqual(p.snapshot()["detail"], "exit_1")

    def test_never_probed_is_unknown(self):
        p = L.DockerProbe(runner=ok_runner)
        snap = p.snapshot(kick=False)
        self.assertIsNone(snap["ok"])
        self.assertEqual(snap["detail"], "not_probed")

    def test_stale_probe_is_unknown(self):
        p = L.DockerProbe(runner=ok_runner)
        p.refresh()
        p._at -= L.CAPACITY_DOCKER_STALE_S + 1
        snap = p.snapshot(kick=False)
        self.assertIsNone(snap["ok"])
        self.assertEqual(snap["detail"], "stale")

    def test_probe_argv_is_read_only(self):
        argv = L.DockerProbe.ARGV
        self.assertEqual(argv[:2], ["docker", "info"])
        self.assertNotIn("run", argv)


class Host(unittest.TestCase):
    def test_parse_meminfo(self):
        text = "MemTotal:       32768000 kB\nMemFree:  1000 kB\nMemAvailable:   16384000 kB\n"
        self.assertEqual(L.parse_meminfo(text), {"mem_total_mb": 32000, "mem_available_mb": 16000})
        self.assertIsNone(L.parse_meminfo("garbage"))

    def test_cpu_busy_from_two_samples(self):
        # (idle, total) cumulative jiffies
        self.assertEqual(L.cpu_busy_pct((100, 1000), (150, 1100)), 50.0)
        self.assertIsNone(L.cpu_busy_pct((100, 1000), (100, 1000)))
        self.assertIsNone(L.cpu_busy_pct(None, (100, 1000)))

    def test_host_snapshot_keys(self):
        snap = L.host_snapshot()
        self.assertEqual(set(snap), {"mem_total_mb", "mem_available_mb", "cpu_count", "cpu_busy_pct"})


if __name__ == "__main__":
    unittest.main()
