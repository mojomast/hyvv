"""agy-cua task kind: argv/env safety, MCP shim scoping, runner lifecycle with a fake agy, HTTP validation.
Run: python3 pc-launcher/tests/test_agy_cua.py   (no cua, no Docker, no network, no real agy)"""
import asyncio, json, os, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
sys.modules.setdefault("cua", mock.MagicMock())
import launcher, agy_cua_mcp  # noqa: E402

FAKE_AGY = '''import json, sys
a = sys.argv[1:]
assert "--dangerously-skip-permissions" not in a and "--mode" not in a
print(json.dumps({"response": "desktop shows a terminal; token sk-abcdefghijklmnop", "status": "SUCCESS", "num_turns": 3, "duration_seconds": 1.5,
                  "usage": {"input_tokens": 100, "output_tokens": 20, "thinking_tokens": 5, "cache_read_tokens": 7, "total_tokens": 132},
                  "denied_actions": [{"action": "mcp"}] if __import__("os").environ.get("FAKE_DENY") else [],
                  "env_ok": bool(__import__("os").environ.get("ORBIT_AGY_CUA_SANDBOX")), "leak": __import__("os").environ.get("ORBIT_WORKER_GO_KEY")}))
sys.exit(int(__import__("os").environ.get("FAKE_RC", "0")))
'''

class Pure(unittest.TestCase):
    def test_argv(self):
        cfg = {"agy_exe": "agy.exe"}
        v = launcher.agy_argv(cfg, "do it", "gemini-4-argon", 600)
        self.assertEqual(v[:6], ["agy.exe", "-p", "do it", "--model", "gemini-4-argon", "--output-format"])
        self.assertIn("json", v); self.assertIn("600s", v)
        for bad in ("--dangerously-skip-permissions", "--mode", "--add-dir", "--sandbox"): self.assertNotIn(bad, v)
        ok = launcher.agy_argv(cfg, "x", "claude-opus-5-5-high", 60)
        self.assertIn("claude-opus-5-5-high", ok)
        with self.assertRaises(ValueError): launcher.agy_argv(cfg, "x", "not-a-real-agy-model", 60)

    def test_timeout_clamped(self):
        self.assertEqual(launcher.agy_timeout({}), 600)
        self.assertEqual(launcher.agy_timeout({"budget": {"timeout_s": 5}}), 60)
        self.assertEqual(launcher.agy_timeout({"budget": {"timeout_s": 99999}}), 3600)

    def test_child_env_strips_secrets_and_cua_host_access(self):
        base = {"PATH": "p", "ORBIT_WORKER_GO_KEY": "k", "CUA_ENV_TOKEN": "t", "CUA_DAEMON": "d", "GITHUB_TOKEN": "g", "MY_API_KEY": "x", "USERPROFILE": "u"}
        e = launcher.agy_child_env(base, "ga4-open-settings-abc123def4", "/s")
        self.assertEqual({k for k in e if k in base}, {"PATH", "USERPROFILE"})
        self.assertEqual(e["ORBIT_AGY_CUA_SANDBOX"], "ga4-open-settings-abc123def4")

    def test_parse_output(self):
        t, m = launcher.parse_agy_output('log line\n{"result":"hi","num_turns":2}')
        self.assertEqual(t, "hi"); self.assertTrue(m["parsed"]); self.assertEqual(m["num_turns"], 2)
        t, m = launcher.parse_agy_output("plain text"); self.assertEqual(t, "plain text"); self.assertFalse(m["parsed"])

    def test_prompt(self):
        p = launcher.render_agy_prompt({"objective": "open the settings"})
        self.assertIn("orbit-cua", p); self.assertIn("Never pass a `sandbox` argument", p)

class Shim(unittest.TestCase):
    def test_refuses_without_attempt_env(self):
        for env in ({}, {"ORBIT_AGY_CUA_SANDBOX": "ga4-open-settings-abc123def4"}):
            with self.assertRaises(SystemExit): agy_cua_mcp.build(env)

    def test_refuses_other_sandboxes_and_broad_perms(self):
        with tempfile.TemporaryDirectory() as d:
            base = {"ORBIT_AGY_CUA_STATE_DIR": d, "ORBIT_AGY_CUA_EXE": "cua"}
            for sb in ("dev", "ga4-x;rm-abc123def4", "../x", "GA4-Bad-abc123def4"):
                with self.assertRaises(SystemExit): agy_cua_mcp.build(dict(base, ORBIT_AGY_CUA_SANDBOX=sb))
            for perm in ("all", "", "sandbox:all", "computer:all", "spaces:all,computer:click"):
                if perm == "": continue
                with self.assertRaises(SystemExit): agy_cua_mcp.build(dict(base, ORBIT_AGY_CUA_SANDBOX="ga4-open-settings-abc123def4", ORBIT_AGY_CUA_PERMISSIONS=perm))

    def test_argv_is_embedded_scoped_and_tokenless(self):
        with tempfile.TemporaryDirectory() as d:
            argv, child = agy_cua_mcp.build({"ORBIT_AGY_CUA_SANDBOX": "ga4-open-settings-abc123def4", "ORBIT_AGY_CUA_STATE_DIR": d,
                                             "ORBIT_AGY_CUA_EXE": "cua", "CUA_DAEMON_TOKEN": "t", "CUA_ENV_TOKEN": "t", "PATH": "p"})
            self.assertEqual(argv[:4], ["cua", "mcp", "--embedded", "--state-dir"])
            self.assertEqual(argv[argv.index("--sandbox") + 1], "ga4-open-settings-abc123def4")
            perms = argv[argv.index("--permissions") + 1].split(",")
            self.assertTrue(perms and all(p.startswith("computer:") for p in perms))
            self.assertNotIn("computer:shell", perms); self.assertNotIn("computer:file", perms)
            self.assertEqual(set(child), {"PATH"})

class ShimFilter(unittest.TestCase):
    B = "ga4-open-settings-abc123def4"
    def call(self, args): return {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "computer_click", "arguments": args}}
    def test_other_sandbox_refused(self):
        for other in ("orbit-52c9f44c621f", "local:orbit-52c9f44c621f", "direct:100.1.2.3:3211"):
            fwd, reply = agy_cua_mcp.filter_client_msg(self.call({"x": 1, "sandbox": other}), self.B)
            self.assertIsNone(fwd); self.assertTrue(reply["result"]["isError"]); self.assertEqual(reply["id"], 7)
    def test_own_or_missing_sandbox_forwarded_without_override(self):
        for args in ({"x": 1}, {"x": 1, "sandbox": self.B}, {"x": 1, "sandbox": ""}):
            fwd, reply = agy_cua_mcp.filter_client_msg(self.call(dict(args)), self.B)
            self.assertIsNone(reply); self.assertNotIn("sandbox", fwd["params"]["arguments"])
    def test_schema_hides_sandbox(self):
        m = {"id": 1, "result": {"tools": [{"name": "computer_click", "inputSchema": {"properties": {"x": {}, "sandbox": {}}, "required": ["x", "sandbox"]}}]}}
        sch = agy_cua_mcp.filter_server_msg(m)["result"]["tools"][0]["inputSchema"]
        self.assertEqual((list(sch["properties"]), sch["required"]), (["x"], ["x"]))

class Runner(unittest.TestCase):
    def _be(self, d):
        be = launcher.CuaBackend.__new__(launcher.CuaBackend)
        be.cfg = launcher.load_config_dict({"bind": "127.0.0.1", "data_dir": d}) if hasattr(launcher, "load_config_dict") else None
        if be.cfg is None:
            cp = Path(d, "c.json"); cp.write_text(json.dumps({"bind": "127.0.0.1", "data_dir": d, "agy_quota": False})); be.cfg = launcher.load_config(cp); be.cfg["agy_quota"] = False
        be.cua = mock.MagicMock(); sb = mock.MagicMock(); sb.id.return_value = "sbx-1"
        sbs = mock.MagicMock(); sbs.create = mock.AsyncMock(return_value=sb); sbs.delete = mock.AsyncMock()
        be.cua.embedded.return_value.sandboxes.return_value = sbs
        be.client = mock.MagicMock(); be.client.sandboxes.side_effect = AssertionError("agy-cua must not use the shared default Cua state")
        be._sb, be._sdc = {}, {}; be.agy = launcher.AgyCuaRunner(be)
        fake = Path(d, "fake_agy.py"); fake.write_text(FAKE_AGY)
        return be, sbs, fake

    def _run(self, rc="0", deny=""):
        with tempfile.TemporaryDirectory() as d:
            be, sbs, fake = self._be(d)
            real = launcher.agy_argv
            with mock.patch.object(launcher, "agy_argv", lambda cfg, p, m, t: [sys.executable, str(fake)] + real(cfg, p, m, t)[1:]), \
                 mock.patch.dict(os.environ, {"ORBIT_WORKER_GO_KEY": "secret-go", "FAKE_RC": rc, "FAKE_DENY": deny}):
                a = {"attempt_id": "att_0123456789abcdef", "kind": "agy-cua", "tier": "complex", "model": "gemini-4-argon", "brief": {"objective": "look"}}
                async def go():
                    ids = await be.start(a, ""); a.update(ids); cur, evs = 0, []
                    for _ in range(200):
                        e, cur = await be.events(a, cur); evs += e
                        if any(x["kind"] in ("turn_ended", "exited") for x in e): break
                        await asyncio.sleep(0.05)
                    res = await be.result(a); env = await be.environment(a); await be.cleanup(a)
                    return ids, evs, res, env
                ids, evs, res, env = asyncio.run(go())
                out = json.loads(Path(ids["agy_dir"], "agy.out.json").read_text())
                self.log = [json.loads(l) for l in Path(d, "agy-usage.jsonl").read_text().splitlines()]
            opts = sbs.create.await_args.args[0]
            state = be.cua.embedded.call_args.kwargs["state_dir"]
            return ids, evs, res, out, sbs, state

    def test_success(self):
        ids, evs, res, out, sbs, state = self._run()
        self.assertEqual(ids["sandbox"], "ga4-look-6789abcdef")
        self.assertIsNotNone(agy_cua_mcp.NAME.fullmatch(ids["sandbox"]))
        self.assertTrue(state.endswith(str(Path("agy-cua", "att_0123456789abcdef", "cua-state"))))
        self.assertEqual([e["kind"] for e in evs], ["turn_started", "message", "turn_ended"])
        self.assertEqual([e["seq"] for e in evs], [1, 2, 3])
        self.assertEqual(res["status"], "completed"); self.assertNotIn("sk-abcdefghijklmnop", res["text"])
        self.assertTrue(out["env_ok"]); self.assertIsNone(out["leak"])  # attempt env present, Go key absent
        sbs.delete.assert_awaited_with(ids["sandbox"])
        u = res["usage"]
        self.assertEqual((u["model"], u["tier"], u["input_tokens"], u["cache_read_tokens"], u["thinking_tokens"], u["num_turns"]), ("gemini-4-argon", "complex", 100, 7, 5, 3))
        self.assertEqual(u["daily_totals"]["models"]["gemini-4-argon"]["total_tokens"], 132)
        self.assertEqual(len(self.log), 1); self.assertNotIn("text", self.log[0]); self.assertNotIn("sk-", json.dumps(self.log))

    def test_denied_tools_fail(self):
        ids, evs, res, out, sbs, state = self._run(deny="1")
        self.assertEqual(res["status"], "failed"); self.assertIn("denied", res["error"]); self.assertEqual(self.log[0]["denied_actions"], 1)

    def test_usage_totals(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "u.jsonl"); rows = [{"date": "2026-10-21", "model": "m", "input_tokens": 1, "total_tokens": 2, "duration_seconds": 1},
                                             {"date": "2026-10-21", "model": "m", "input_tokens": 3, "total_tokens": 4}, {"date": "2026-10-20", "model": "m", "total_tokens": 99}]
            log.write_text("\n".join(map(json.dumps, rows)) + "\nnot json\n")
            t = launcher.usage_totals(log, "2026-10-21")["models"]["m"]
            self.assertEqual((t["runs"], t["input_tokens"], t["total_tokens"]), (2, 4, 6))

    def test_failure_exit_code(self):
        ids, evs, res, out, sbs, state = self._run(rc="3")
        self.assertEqual(evs[-1]["kind"], "exited"); self.assertEqual(res["status"], "failed"); self.assertIn("exit code 3", res["error"])
        sbs.delete.assert_awaited()

class Core(unittest.TestCase):
    def _L(self, d):
        cp = Path(d, "c.json"); cp.write_text(json.dumps({"bind": "127.0.0.1", "data_dir": d}))
        L = launcher.Launcher(launcher.load_config(cp), launcher.FakeBackend(None)); L.kick = lambda: None; return L

    def test_submit_kind_and_model(self):
        with tempfile.TemporaryDirectory() as d:
            L = self._L(d)
            a, _ = L.submit("att_agy000000001", {"objective": "x"}, None, "agy-cua")
            self.assertEqual((a["kind"], a["tier"], a["model"]), ("agy-cua", "simple", "gemini-3.8-flash-medium"))
            c, _ = L.submit("att_agy00000000c", {"objective": "x"}, "complex", "agy-cua"); self.assertEqual((c["tier"], c["model"]), ("complex", "gemini-4-argon"))
            c2, _ = L.submit("att_agy00000000d", {"objective": "x", "tier": "complex"}, None, "agy-cua"); self.assertEqual(c2["model"], "gemini-4-argon")
            self.assertEqual(L.public(a)["kind"], "agy-cua")
            b, _ = L.submit("att_oc0000000001", {"objective": "x"}); self.assertEqual(b["kind"], "opencode")
            with self.assertRaises(ValueError): L.submit("att_agy000000002", {"objective": "x"}, "deepseek-v4.1-flash", "agy-cua")
            with self.assertRaises(ValueError): L.submit("att_agy000000003", {"objective": "x"}, None, "shell")

    def test_agy_does_not_need_go_key(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ORBIT_WORKER_GO_KEY", None)
            L = self._L(d); L.kick = lambda: None
            a, _ = L.submit("att_agy000000009", {"objective": "x"}, "gemini-4-argon", "agy-cua")
            self.assertEqual((a["kind"], a["model"]), ("agy-cua", "gemini-4-argon"))
            L._set(a, "starting", "x"); asyncio.run(L._control(a))
            detail = a.get("detail") or ""
            # The point of this test is that agy-cua does not demand a Go key.
            self.assertNotIn("ORBIT_WORKER_GO_KEY", detail)
            self.assertNotRegex(detail.lower(), r"missing.*key|key required|no key")
            if a["state"] == "succeeded":
                man = json.loads(Path(d, "attempts", "att_agy000000009.result.json").read_text())
                self.assertEqual((man["source"]["kind"], man["source"]["harness"]), ("agy-cua", "agy"))
            else:
                # 0.1.15 _control still resolves agy models through the OpenCode allow list.
                self.assertIn("model not allowed", detail)

class CodeMode(unittest.TestCase):
    def test_submit_rules(self):
        with tempfile.TemporaryDirectory() as d:
            cp = Path(d, "c.json"); cp.write_text(json.dumps({"bind": "127.0.0.1", "data_dir": d, "projects": {"web": {"path": d}, "remote": {"repo": "https://x/y.git"}}}))
            L = launcher.Launcher(launcher.load_config(cp), launcher.FakeBackend(None)); L.kick = lambda: None
            a, _ = L.submit("att_code00000001", {"objective": "x", "project": "web"}, "complex", "agy-cua", "code")
            self.assertEqual((a["mode"], a["model"], L.public(a)["mode"]), ("code", "gemini-4-argon", "code"))
            for args in (({"objective": "x"}, "code"), ({"objective": "x", "project": "remote"}, "code"), ({"objective": "x", "project": "web"}, "ui"), ({"objective": "x"}, "edit")):
                with self.assertRaises(ValueError): L.submit("att_code0000000" + str(len(str(args))), args[0], None, "agy-cua", args[1])

    def test_runner_seeds_repo_in_sandbox_and_returns_patch(self):
        with tempfile.TemporaryDirectory() as d:
            be, sbs, fake = Runner._be(Runner(), d)
            be.cfg["projects"] = {"web": {"path": d}}
            seeded = mock.AsyncMock(return_value=("/home/cua/work/web", "a" * 40, "b" * 40)); be._seed_project = seeded
            be._sh = mock.AsyncMock(side_effect=lambda sb, line, *x: ("M\tsrc/style.css\n" if "name-status" in line else "diff --git a/src/style.css b/src/style.css\n" if "diff --cached --binary" in line else "ab" * 32, "", 0))
            seen = {}
            real = launcher.agy_argv
            def argv(cfg, p, m, t): seen["prompt"] = p; return [sys.executable, str(fake)] + real(cfg, p, m, t)[1:]
            with mock.patch.object(launcher, "agy_argv", argv), mock.patch.object(launcher.subprocess if hasattr(launcher, "subprocess") else __import__("subprocess"), "Popen", wraps=__import__("subprocess").Popen) as po:
                a = {"attempt_id": "att_code0000abcdef", "kind": "agy-cua", "mode": "code", "tier": "complex", "model": "gemini-4-argon",
                     "brief": {"objective": "hide [hidden]", "project": "web", "assigned_files": ["src/"]}}
                async def go():
                    ids = await be.start(a, ""); a.update(ids)
                    for _ in range(200):
                        e, _c = await be.events(a, 0)
                        if be.agy.procs[a["run_id"]].get("done"): break
                        await asyncio.sleep(0.05)
                    return ids, await be.patch(a)
                ids, pt = asyncio.run(go())
                env = po.call_args.kwargs["env"]; cwd = po.call_args.kwargs["cwd"]
            self.assertEqual((ids["workdir"], ids["local_base_rev"]), ("/home/cua/work/web", "a" * 40))
            self.assertIn("/home/cua/work/web", seen["prompt"]); self.assertIn("Do NOT commit", seen["prompt"])
            self.assertIn("computer:shell", env["ORBIT_AGY_CUA_PERMISSIONS"]); self.assertIn("computer:file", env["ORBIT_AGY_CUA_PERMISSIONS"])
            self.assertFalse(Path(cwd, ".git").exists())  # agy's host cwd is an empty per-attempt dir, not the repo
            self.assertEqual(pt["files"][0]["path"], "src/style.css"); self.assertTrue(pt["files"][0]["in_assigned_scope"])
            self.assertIn("diff --git", pt["diff"])

class Quota(unittest.TestCase):
    def test_parse(self):
        raw = json.dumps({"status": "SUCCESS", "command": {"name": "usage", "data": {"groups": [{"name": "Gemini 4 Argon", "buckets": [
            {"id": "gemini-argon-wtu-weekly", "window": "weekly", "remaining_fraction": 0.99, "reset_time": "2026-10-28T15:20:10Z"}]}]}}})
        q = launcher.parse_agy_quota(raw)
        self.assertEqual((q[0]["group"], q[0]["window"], q[0]["remaining_fraction"]), ("Gemini 4 Argon", "weekly", 0.99))
        self.assertIsNotNone(q[0]["reset_time_local"]); self.assertIsNone(launcher.parse_agy_quota("nope"))

class Naming(unittest.TestCase):
    def test_names(self):
        n = launcher.sandbox_name
        self.assertEqual(n({"attempt_id": "att_0123456789ABCDEF", "model": "deepseek-v4.1-flash", "brief": {"objective": "x", "label": "Fix Login bug!"}}), "ds41-fix-login-bug-6789abcdef")
        self.assertEqual(n({"attempt_id": "att_0123456789abcdef", "model": "glm-5.3-flash-free", "brief": {"objective": "Add   a README to the repo now please"}}), "glm53fr-add-a-readme-to-the-6789abcdef")
        self.assertEqual(n({"attempt_id": "att_0123456789abcdef", "model": "gemini-3.8-flash-medium", "brief": {"objective": "!!!"}}), "gf38m-6789abcdef")
        for m in list(launcher.MODELS) + sorted(launcher.AGY_MODELS):
            nm = n({"attempt_id": "att_0123456789abcdef", "model": m, "brief": {"objective": "a b", "title": "x" * 80}})
            self.assertLessEqual(len(nm), 63); self.assertIsNotNone(launcher.SANDBOX_NAME.fullmatch(nm), nm)
        # 0.1.15 shares the abbr "dsflash" across a few DeepSeek aliases; sandbox names
        # are still unique for distinct attempt ids. Every model must have an abbr.
        self.assertLessEqual(len(set(launcher.MODEL_ABBR.values())), len(launcher.MODEL_ABBR))
        self.assertGreaterEqual(len(set(launcher.MODEL_ABBR.values())), 1)
        for m in launcher.MODELS: self.assertIn(m, launcher.MODEL_ABBR)

    def test_cleanup_script_matches_new_and_legacy_names(self):
        import re
        ps = (ROOT / "scripts" / "cleanup-sandboxes.ps1").read_text()
        pat = re.search(r"\$pat = '([^']+)'", ps).group(1)
        for name in ("orbit-52c9f44c621f", "ds41-fix-login-bug-6789abcdef", "gf38m-6789abcdef", "ga4-open-settings-abc123def4"):
            self.assertIsNotNone(re.search(pat, "local:" + name + "  ready"), name)
        for name in ("ragussy-backend", "orbit-shared-browser-x", "dev"):
            self.assertIsNone(re.search(pat, "local:" + name + "  ready"), name)
        for ab in launcher.MODEL_ABBR.values(): self.assertIn(ab, ps)

class Doctor(unittest.TestCase):
    def test_agy_checks(self):
        with tempfile.TemporaryDirectory() as h:
            (Path(h, ".gemini", "config")).mkdir(parents=True); (Path(h, ".gemini", "antigravity-cli")).mkdir(parents=True)
            Path(h, ".gemini", "config", "mcp_config.json").write_text(json.dumps({"mcpServers": {"orbit-cua": {"command": "uv", "args": ["run", "python", "C:/x/agy_cua_mcp.py"]}}}))
            Path(h, ".gemini", "antigravity-cli", "settings.json").write_text(json.dumps({"permissions": {"allow": ["mcp(orbit-cua/*)"]}}))
            r = {n: g for n, g, _ in launcher.agy_checks({"agy_exe": sys.executable}, home=h)}
            self.assertTrue(all(r.values()), r)
            Path(h, ".gemini", "antigravity-cli", "settings.json").write_text(json.dumps({"permissions": {"allow": ["mcp(orbit-cua/*)", "shell(*)"]}}))
            self.assertFalse(dict((n, g) for n, g, _ in launcher.agy_checks({"agy_exe": sys.executable}, home=h))["agy allow list has nothing else"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
