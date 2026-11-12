"""0.1.11: endpoints bundlebees expects (list attempts, projects, containers, review) and the loopback extra bind."""
import http.client, importlib.util, json, sys, tempfile, threading, time, unittest, asyncio
from http.server import ThreadingHTTPServer
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher011", HERE / "launcher.py"); L = importlib.util.module_from_spec(spec); spec.loader.exec_module(L)
TOKEN = "t" * 40

class T(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lch11-"); (Path(self.tmp) / "go.key").write_text("fake-provider-key"); (Path(self.tmp) / "deepseek.key").write_text("fake-deepseek-key")
        cfg = {"bind": "127.0.0.1", "port": 0, "max_concurrent": 2, "data_dir": self.tmp, "backoff_s": [0.3], "model": "deepseek-v4.1-flash",
               "base_url": "http://x", "wire": "openai", "image": "cua-bee:test", "runtime": "runc", "memory_mb": 4096,
               "projects": {"demo": {"path": "C:/secret/path/demo", "repo": "https://example.invalid/r.git"}, "other": {}}}
        self.la = L.Launcher(cfg, L.FakeBackend(cfg)); threading.Thread(target=self.la.loop.run_forever, daemon=True).start()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), L.make_handler(self.la, TOKEN)); threading.Thread(target=self.srv.serve_forever, daemon=True).start()
    def tearDown(self): self.srv.shutdown()
    def req(self, m, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        c.request(m, path, body=json.dumps(body).encode() if body is not None else None, headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
        r = c.getresponse(); return r.status, json.loads(r.read() or b"{}")
    def wait(self, aid):
        t = time.time()
        while time.time() - t < 10 and self.la.attempts[aid]["state"] not in L.TERMINAL: time.sleep(0.1)

    def test_projects_names_only(self):
        st, b = self.req("GET", "/v1/projects")
        self.assertEqual(st, 200); self.assertEqual(b["projects"], [{"name": "demo"}, {"name": "other"}])
        self.assertNotIn("secret", json.dumps(b)); self.assertNotIn("example.invalid", json.dumps(b))
    def test_containers(self):
        st, b = self.req("GET", "/v1/containers")
        self.assertEqual(b["containers"][0], {"image": "cua-bee:test", "runtime": "runc", "memory_mb": 4096})
    def test_list_has_lease_fields_not_brief(self):
        self.la.submit("att_list0001", {"objective": "SECRET-OBJECTIVE", "deliverables": ["a"], "acceptance": ["b"], "project": "other", "branch": "feat/x",
                                        "assigned_files": ["src/a.py"], "fake": {"delay": 3}})
        st, b = self.req("GET", "/v1/attempts")
        self.assertEqual(st, 200); row = b["attempts"][0]
        self.assertEqual((row["attempt_id"], row["project"], row["branch"], row["assigned_files"]), ("att_list0001", "other", "feat/x", ["src/a.py"]))
        self.assertNotIn("SECRET-OBJECTIVE", json.dumps(b)); self.assertNotIn("brief", row)
        st, b2 = self.req("GET", "/v1/attempts?active=1"); self.assertEqual(len(b2["attempts"]), 1)
    def test_review_flow(self):
        self.la.submit("att_rev00001", {"objective": "x", "deliverables": ["a"], "acceptance": ["b"]})
        st, b = self.req("POST", "/v1/attempts/att_nope0001/review", {"accepted": True}); self.assertEqual(st, 404)
        self.wait("att_rev00001")
        st, b = self.req("POST", "/v1/attempts/att_rev00001/review", {"accepted": "yes"}); self.assertEqual(st, 400)
        st, b = self.req("POST", "/v1/attempts/att_rev00001/review", {"accepted": True, "note": "looks good"})
        self.assertEqual(st, 200); self.assertIs(b["accepted"], True)
        self.assertTrue(json.loads((Path(self.tmp) / "attempts" / "att_rev00001.json").read_text())["review"]["accepted"])
        st, b = self.req("GET", "/v1/attempts"); self.assertIs(b["attempts"][0]["reviewed"], True)
    def test_review_refuses_running(self):
        self.la.submit("att_rev00002", {"objective": "x", "deliverables": ["a"], "acceptance": ["b"], "fake": {"delay": 3}})
        time.sleep(0.5); st, b = self.req("POST", "/v1/attempts/att_rev00002/review", {"accepted": False}); self.assertEqual(st, 409)

    def test_verify_of_checks(self):
        work = {"objective": "x", "deliverables": ["a"], "acceptance": ["b"], "project": "other", "source_rev": "a" * 40}
        ver = dict(work, verify_of="att_abcd00001", role="verify")
        with self.assertRaises(ValueError): self.la.submit("att_beef00000", dict(ver, verify_of="../etc"))
        with self.assertRaises(ValueError): self.la.submit("att_beef00001", ver)  # parent unknown
        self.la.submit("att_abcd00001", dict(work, fake={"delay": 2}))
        with self.assertRaises(ValueError): self.la.submit("att_beef00002", ver)  # parent still running
        self.wait("att_abcd00001"); self.assertEqual(self.la.attempts["att_abcd00001"]["state"], "succeeded")
        with self.assertRaises(ValueError): self.la.submit("att_beef00003", dict(ver, source_rev="b" * 40))  # rev mismatch
        st, b = self.req("POST", "/v1/attempts", {"attempt_id": "att_beef00004", "brief": dict(ver, project="demo")})
        self.assertEqual(st, 400)  # project mismatch -> 400 over HTTP
        a, created = self.la.submit("att_beef00005", ver)
        self.assertTrue(created); self.assertEqual(a["brief"]["verify_of"], "att_abcd00001")

    def test_bundlebees_acceptance_dict_accepted(self):
        brief = {"objective": "x", "deliverables": ["a"], "acceptance": {"criteria": ["c1"], "commands": ["pytest -q"], "tests": ["tests/t.py"]}}
        st, b = self.req("POST", "/v1/attempts", {"attempt_id": "att_cafe00001", "brief": brief})
        self.assertIn(st, (200, 201, 202))
        self.assertEqual(self.la.attempts["att_cafe00001"]["brief"]["acceptance"], ["c1", "Run: pytest -q", "Test passes: tests/t.py"])

    def test_v011_brief_shape_accepted(self):
        # bundlebees v0.1.1: only objective is required; acceptance is a string list.
        brief = {"objective": "add a comment", "project": "demo", "branch": "feat/x",
                 "source_rev": "deadbeef", "assigned_files": ["src/a.py"],
                 "acceptance": ["tests pass"], "deliverables": ["src/a.py"], "commands": ["pytest -q"]}
        st, b = self.req("POST", "/v1/attempts", {"attempt_id": "att_cafe00002", "brief": brief})
        self.assertIn(st, (200, 201, 202))
        self.assertEqual(self.la.attempts["att_cafe00002"]["brief"]["acceptance"], ["tests pass"])

    def test_main_c10aa42_brief_shape_accepted(self):
        # bundlebees main at c10aa42: objective, full source_rev, assigned_files, acceptance (object ok).
        brief = {"objective": "add a comment", "project": "demo", "branch": "feat/x",
                 "source_rev": "a" * 40, "assigned_files": ["src/a.py"],
                 "acceptance": {"criteria": ["tests pass"], "commands": ["pytest -q"], "tests": ["tests/t.py"]}}
        st, b = self.req("POST", "/v1/attempts", {"attempt_id": "att_cafe00003", "brief": brief})
        self.assertIn(st, (200, 201, 202))
        self.assertEqual(self.la.attempts["att_cafe00003"]["brief"]["source_rev"], "a" * 40)
        self.assertEqual(self.la.attempts["att_cafe00003"]["brief"]["acceptance"],
                         ["tests pass", "Run: pytest -q", "Test passes: tests/t.py"])

    def test_unknown_extra_fields_ignored(self):
        # Upcoming ACDD/TDD fields (contract, RED/GREEN evidence) must not be rejected.
        brief = {"objective": "add a comment", "project": "demo", "source_rev": "b" * 64,
                 "assigned_files": ["src/a.py"],
                 "acceptance": {"criteria": ["ok"]},
                 "contract": {"id": "C1", "requires": ["src/a.py"]},
                 "evidence": {"RED": ["test_fails"], "GREEN": ["test_passes"]}}
        self.assertIsNone(L.validate_brief(L.normalize_brief(dict(brief))))
        st, b = self.req("POST", "/v1/attempts", {"attempt_id": "att_cafe00004", "brief": brief})
        self.assertIn(st, (200, 201, 202))
        stored = self.la.attempts["att_cafe00004"]["brief"]
        self.assertEqual(stored["contract"], brief["contract"])
        self.assertEqual(stored["evidence"], brief["evidence"])
        self.assertEqual(stored["source_rev"], "b" * 64)

    def test_omitted_attempt_id_is_minted(self):
        # 0.1.13: bundlebees posts {brief, model} with no attempt_id; mint att_ + 16 hex and return it.
        brief = {"objective": "x", "deliverables": ["a"], "acceptance": ["b"]}
        st, b = self.req("POST", "/v1/attempts", {"brief": brief, "model": "deepseek-v4.1-flash"})
        self.assertIn(st, (200, 201), b)
        aid = b.get("attempt_id")
        self.assertRegex(aid, r"^att_[0-9a-f]{16}$")
        self.assertIn(aid, self.la.attempts)
        st2, b2 = self.req("POST", "/v1/attempts", {"brief": brief})
        self.assertIn(st2, (200, 201), b2)
        self.assertRegex(b2.get("attempt_id") or "", r"^att_[0-9a-f]{16}$")
        self.assertNotEqual(b2["attempt_id"], aid)

    def test_explicit_attempt_id_still_used(self):
        brief = {"objective": "x", "deliverables": ["a"], "acceptance": ["b"]}
        st, b = self.req("POST", "/v1/attempts", {"attempt_id": "att_explicit00001", "brief": brief})
        self.assertIn(st, (200, 201), b)
        self.assertEqual(b["attempt_id"], "att_explicit00001")

    def test_invalid_attempt_id_still_rejected(self):
        brief = {"objective": "x", "deliverables": ["a"], "acceptance": ["b"]}
        st, b = self.req("POST", "/v1/attempts", {"attempt_id": "nope", "brief": brief})
        self.assertEqual(st, 400)
        self.assertEqual(b.get("error"), "attempt_id required")

class Bind(unittest.TestCase):

    def test_load_config_defaults_extra_binds_without_writing(self):
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="lch11-cfg-"))
        src = d / "config.json"
        # key set matches the live PC file; values are fakes
        src.write_text(json.dumps({
            "bind": "127.0.0.1", "port": 47821, "max_concurrent": 10, "model": "deepseek-v4.1-flash",
            "base_url": "http://example.invalid/v1", "wire": "openai-chat", "image": "cua-bee:test",
            "runtime": "runc", "memory_mb": 6144, "backoff_s": [30],
            "projects": {"demo": {"path": "C:/secret/demo"}, "orbitdesktop": {}},
            "models": {"flash": {}}
        }))
        before = src.read_bytes()
        cfg = L.load_config(src)
        self.assertEqual(src.read_bytes(), before)
        self.assertEqual(cfg["extra_binds"], ["127.0.0.1"])
        self.assertEqual(sorted(cfg["projects"]), ["demo", "orbitdesktop"])

    def test_extra_bind_loopback_only(self):
        import ipaddress
        self.assertTrue(ipaddress.ip_address("127.0.0.1").is_loopback)
        src = (HERE / "launcher.py").read_text()
        self.assertIn('extra_binds allows loopback only', src)

if __name__ == "__main__": unittest.main()
