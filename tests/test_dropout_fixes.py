"""Plan C (LAUNCHER-DROPOUTS.md): HTTP/1.1 keep-alive, event ts, heartbeat, offset-indexed events_since."""
import asyncio, http.client, importlib.util, json, os, sys, tempfile, threading, time, unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher", HERE / "launcher.py"); L = importlib.util.module_from_spec(spec)
sys.modules["launcher"] = L; spec.loader.exec_module(L)
TOKEN = "t" * 40


def mk(tmp, max_c=2):
    cfg = {"bind": "127.0.0.1", "port": 0, "max_concurrent": max_c, "data_dir": tmp, "backoff_s": [0.3], "projects": {}, "model": "deepseek-v4.1-flash",
           "base_url": "http://x", "wire": "openai"}
    la = L.Launcher(cfg, L.FakeBackend(cfg))
    threading.Thread(target=la.loop.run_forever, daemon=True).start()
    asyncio.run_coroutine_threadsafe(L._lag_probe(la, every=0.2), la.loop)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), L.make_handler(la, TOKEN))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return la, srv


class T(unittest.TestCase):
    def setUp(self): self.tmp = tempfile.mkdtemp(prefix="lch-")

    def test_events_since_incremental_and_partial_line(self):
        la, srv = mk(self.tmp)
        try:
            p = Path(self.tmp) / "attempts" / "att_x.events.jsonl"
            p.write_text("".join(json.dumps({"lseq": i, "kind": "state"}) + "\n" for i in range(1, 6)))
            self.assertEqual([e["lseq"] for e in la.events_since("att_x", 2)], [3, 4, 5])
            with open(p, "a") as f: f.write(json.dumps({"lseq": 6}) + "\n" + '{"lseq": 7')  # 7 is half-written
            self.assertEqual([e["lseq"] for e in la.events_since("att_x", 5)], [6])
            with open(p, "a") as f: f.write(', "kind": "x"}\n')
            self.assertEqual([e["lseq"] for e in la.events_since("att_x", 0)], [1, 2, 3, 4, 5, 6, 7])
            self.assertEqual(la.events_since("att_x", 7), [])
            self.assertEqual(la.events_since("nope", 0), [])
        finally: srv.shutdown()

    def test_heartbeat_and_event_ts(self):
        old = L.HEARTBEAT_S; L.HEARTBEAT_S = 0.5
        la, srv = mk(self.tmp)
        try:
            (Path(self.tmp) / "go.key").write_text("fake-provider-key")
            (Path(self.tmp) / "deepseek.key").write_text("fake-deepseek-key")
            la.submit("att_hb000001", {"objective": "x", "deliverables": ["a"], "acceptance": ["b"], "fake": {"delay": 2.5}})
            t = time.time()
            while time.time() - t < 10 and la.attempts["att_hb000001"]["state"] not in L.TERMINAL: time.sleep(0.1)
            evs = la.events_since("att_hb000001", 0)
            kinds = [e["kind"] for e in evs]
            self.assertIn("heartbeat", kinds, kinds)
            self.assertTrue(all("ts" in e for e in evs), evs)
            self.assertTrue(any(k.startswith("worker:") for k in kinds))
            self.assertEqual([e["lseq"] for e in evs], sorted(e["lseq"] for e in evs))
        finally: L.HEARTBEAT_S = old; srv.shutdown()

    def test_http11_keepalive_health_and_401_closes(self):
        la, srv = mk(self.tmp)
        try:
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            socks = set()
            for _ in range(3):
                c.request("GET", "/v1/health", headers={"Authorization": "Bearer " + TOKEN}); r = c.getresponse()
                self.assertEqual(r.version, 11); body = json.loads(r.read()); socks.add(id(c.sock))
                self.assertIn("uptime_s", body); self.assertIn("loop_lag_ms", body)
            self.assertEqual(len(socks), 1)
            c.request("POST", "/v1/attempts", body=b'{"x":1}', headers={"Authorization": "Bearer wrong", "Content-Type": "application/json"})
            r = c.getresponse(); r.read(); self.assertEqual(r.status, 401); self.assertTrue(r.will_close)
        finally: srv.shutdown()


class TokenInject(unittest.TestCase):
    """bbs_test_token opt-in: only the access token is uploaded, the local copy is deleted, failures are non-fatal."""
    def run_inject(self, rc, stdout, write=True):
        import subprocess, types
        tmp = Path(tempfile.mkdtemp(prefix="lch-tok-")); (tmp / "bbs-beetest" / "tokens").mkdir(parents=True)
        be = L.CuaBackend.__new__(L.CuaBackend); be.cfg = {"data_dir": str(tmp)}
        up = {}
        class SD:
            async def upload(self, path, data, opts): up[path] = data
        async def _sd(sb): return SD()
        be._sd = _sd
        def fake_run(argv, **kw):
            out = Path(argv[argv.index("-OutFile") + 1])
            if write: out.write_text("x" * 43)
            return types.SimpleNamespace(returncode=rc, stdout=stdout)
        old = subprocess.run; subprocess.run = fake_run
        try:
            a = {"attempt_id": "att_tok00001"}
            tf = asyncio.run(be._inject_bbs_token(object(), a))
        finally: subprocess.run = old
        return tf, a, up, list((tmp / "bbs-beetest" / "tokens").iterdir())

    def test_ok(self):
        tf, a, up, left = self.run_inject(0, "ok bee=att_tok00001 account=toh-beetest-agent file=X expires_at=2026-11-12T16:00:00Z")
        self.assertEqual(tf, "/tmp/toh-bbs-test.token"); self.assertEqual(up["/tmp/toh-bbs-test.token"], b"x" * 43)
        self.assertEqual(up["/tmp/toh-bbs-test.token.expires_at"], b"2026-11-12T16:00:00Z"); self.assertEqual(left, [])
        self.assertIn("injected", a["bbs_token"])

    def test_fail_is_nonfatal(self):
        tf, a, up, left = self.run_inject(2, "FAILED account=toh-beetest-agent code=rate_limited retry_after=40", write=False)
        self.assertIsNone(tf); self.assertEqual(up, {}); self.assertIn("rate_limited", a["bbs_token"])


if __name__ == "__main__": unittest.main()
