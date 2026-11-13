"""Acceptance job class: seccomp profile, image layer, per-job security_profile,
base-commit patch collector, and the check-run result channel (Hermes t417u)."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules["launcher"] = L
spec.loader.exec_module(L)

ACC = HERE / "images" / "acceptance"
UPSTREAM = ACC / "seccomp" / "moby-default-v28.4.0.json"
PROFILE = ACC / "seccomp" / "acceptance.json"
# sha256 of moby/moby v28.4.0 (commit 249d679a) vendor/github.com/moby/profiles/seccomp/default.json
UPSTREAM_SHA256 = "01536f1d1df938ae611eba20d6349e0de7a99b6ecdee1549427a0b01b8301e28"
# gitleaks_8.21.2_checksums.txt (github.com/gitleaks/gitleaks releases), linux_x64 tarball
GITLEAKS_SHA256 = "5bc41815076e6ed6ef8fbecc9d9b75bcae31f39029ceb55da08086315316e3ba"


def _load_derive():
    s = importlib.util.spec_from_file_location("derive_seccomp", ACC / "derive_seccomp.py")
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


class SeccompProfile(unittest.TestCase):
    def test_upstream_is_pinned(self):
        self.assertEqual(hashlib.sha256(UPSTREAM.read_bytes()).hexdigest(), UPSTREAM_SHA256)

    def test_committed_profile_is_derived_from_upstream(self):
        d = _load_derive()
        up = json.loads(UPSTREAM.read_text("utf-8"))
        self.assertEqual(d.derive(up), json.loads(PROFILE.read_text("utf-8")))

    def test_profile_differs_only_by_unshare_and_clone_newuser(self):
        up = json.loads(UPSTREAM.read_text("utf-8"))
        acc = json.loads(PROFILE.read_text("utf-8"))
        for k in ("defaultAction", "defaultErrnoRet", "archMap"):
            self.assertEqual(up[k], acc[k])
        self.assertEqual(set(up), set(acc))
        added = [s for s in acc["syscalls"] if s not in up["syscalls"]]
        removed = [s for s in up["syscalls"] if s not in acc["syscalls"]]
        # exactly the two non-SYS_ADMIN clone rules changed (x86/arm + s390 arg order)
        self.assertEqual(len(removed), 2)
        for s in removed:
            self.assertEqual(s["names"], ["clone"])
            self.assertEqual(s["args"][0]["value"], 0x7E020000)
        changed = [s for s in added if s["names"] == ["clone"]]
        self.assertEqual(len(changed), 2)
        for s in changed:
            self.assertEqual(s["args"][0]["value"], 0x6E020000)
            self.assertEqual(s["action"], "SCMP_ACT_ALLOW")
            self.assertEqual(s["args"][0]["op"], "SCMP_CMP_MASKED_EQ")
            # same rule otherwise (index, excludes/includes untouched)
            orig = next(r for r in removed if r.get("includes") == s.get("includes") and r.get("excludes") == s.get("excludes"))
            o2, s2 = copy.deepcopy(orig), copy.deepcopy(s)
            o2["args"][0].pop("value"); s2["args"][0].pop("value")
            self.assertEqual(o2, s2)
        new = [s for s in added if s["names"] != ["clone"]]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["names"], ["unshare"])
        self.assertEqual(new[0]["action"], "SCMP_ACT_ALLOW")
        self.assertNotIn("args", new[0])
        self.assertNotIn("includes", new[0])
        # nothing else became allowed: setns, mount, clone3 stay as upstream
        for s in acc["syscalls"]:
            if s not in up["syscalls"]:
                self.assertTrue(set(s["names"]) <= {"clone", "unshare"})

    def test_check_mode_passes(self):
        r = subprocess.run([sys.executable, str(ACC / "derive_seccomp.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class AcceptanceImage(unittest.TestCase):
    def setUp(self):
        self.df = (ACC / "Dockerfile").read_text("utf-8")

    def test_layer_on_bee_image(self):
        self.assertIn("ARG BASE=cua-bee:node24-oc1.18.32", self.df)
        self.assertIn("FROM ${BASE}", self.df)

    def test_bubblewrap_and_pinned_gitleaks(self):
        self.assertIn("bubblewrap", self.df)
        self.assertIn("GITLEAKS_VERSION=8.21.2", self.df)
        self.assertIn(f"GITLEAKS_SHA256={GITLEAKS_SHA256}", self.df)
        self.assertIn("sha256sum -c", self.df)

    def test_no_privilege_widening_in_image(self):
        low = self.df.lower()
        for bad in ("--privileged", "setcap", "chmod u+s", "chmod 4755", "unconfined"):
            self.assertNotIn(bad, low)


ACC_PROFILE = {"image": "hyvv-acceptance:test", "seccomp": str(PROFILE), "network": "none"}


def _cfg(tmp, **kw):
    cfg = {"bind": "127.0.0.1", "port": 0, "max_concurrent": 2, "data_dir": tmp, "backoff_s": [0.2],
           "model": "deepseek-v4.1-flash", "base_url": "http://x", "wire": "openai",
           "image": "cua-bee:test", "runtime": "runc", "memory_mb": 4096, "cpus": 2, "projects": {},
           "security_profiles": {"acceptance": dict(ACC_PROFILE)}}
    cfg.update(kw)
    return cfg


class SecurityProfileSelection(unittest.TestCase):
    def test_coding_bees_keep_default(self):
        cfg = _cfg("/tmp")
        for kind in ("opencode", "agy-cua"):
            self.assertEqual(L.resolve_security_profile(cfg, kind, {}), "default")
            self.assertEqual(L.resolve_security_profile(cfg, kind, {"security_profile": "default"}), "default")
            with self.assertRaises(ValueError):
                L.resolve_security_profile(cfg, kind, {"security_profile": "acceptance"})

    def test_check_jobs_select_configured_profile(self):
        cfg = _cfg("/tmp")
        self.assertEqual(L.resolve_security_profile(cfg, "check", {"security_profile": "acceptance"}), "acceptance")
        self.assertEqual(L.resolve_security_profile(cfg, "check", {}), "default")
        cfg["check_security_profile"] = "acceptance"
        self.assertEqual(L.resolve_security_profile(cfg, "check", {}), "acceptance")
        with self.assertRaises(ValueError):
            L.resolve_security_profile(cfg, "check", {"security_profile": "nope"})

    def test_profile_config_rejects_widening(self):
        for bad in ({"privileged": True}, {"cap_add": ["SYS_ADMIN"]}, {"seccomp": "unconfined"},
                    {"apparmor": "unconfined"}, {"security_opt": ["seccomp=unconfined"]}, {"network": "host"}):
            prof = dict(ACC_PROFILE, **bad)
            with self.assertRaises(ValueError, msg=str(bad)):
                L.validate_security_profiles({"security_profiles": {"acceptance": prof}})
        with self.assertRaises(ValueError):
            L.validate_security_profiles({"security_profiles": {"default": dict(ACC_PROFILE)}})
        L.validate_security_profiles({"security_profiles": {"acceptance": dict(ACC_PROFILE)}})

    def test_docker_argv_for_acceptance(self):
        cfg = _cfg("/tmp")
        argv = L.docker_check_argv(cfg, "acceptance", "hyvv-check-att_00000001")
        self.assertEqual(argv[:3], ["docker", "run", "-d"])
        self.assertIn("seccomp=" + str(PROFILE), argv)
        i = argv.index("--network"); self.assertEqual(argv[i + 1], "none")
        i = argv.index("--memory"); self.assertEqual(argv[i + 1], "4096m")
        i = argv.index("--cpus"); self.assertEqual(argv[i + 1], "2")
        self.assertIn("hyvv-acceptance:test", argv)
        joined = " ".join(argv).lower()
        for bad in ("--privileged", "--cap-add", "unconfined", "apparmor", "--userns", "--pid=host", "--network=host"):
            self.assertNotIn(bad, joined)

    def test_docker_argv_default_profile_has_no_security_opt(self):
        cfg = _cfg("/tmp")
        argv = L.docker_check_argv(cfg, "default", "hyvv-check-att_00000002")
        self.assertNotIn("--security-opt", argv)
        self.assertIn("cua-bee:test", argv)


async def _bash(line, env):
    p = await asyncio.create_subprocess_exec("bash", "-c", line, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    o, e = await p.communicate()
    return o.decode(), e.decode(), p.returncode


class _LocalSD:
    def __init__(self, root): self.root = root
    async def upload(self, path, data, _):
        Path(self.root, Path(path).name).write_bytes(data)


def _local_backend(tmp):
    """A CuaBackend whose 'sandbox' shell is a local bash with HOME=tmp."""
    be = object.__new__(L.CuaBackend)
    be.cfg, be._sb, be._sdc = {}, {}, {}
    env = dict(os.environ, HOME=tmp, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    tarp = str(Path(tmp, "orbit-src.tar"))
    async def _sh(sb, line, timeout_ms=120000):
        return await _bash(line.replace("/tmp/orbit-src.tar", tarp), env)
    async def _sd(sb): return _LocalSD(tmp)
    async def _sandbox(name): return object()
    be._sh, be._sd, be._sandbox = _sh, _sd, _sandbox
    return be, env


def _src_repo(tmp):
    src = Path(tmp, "src"); src.mkdir()
    g = ["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    (src / "a.txt").write_text("one\n")
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-qm", "init"], check=True)
    return src


class PatchCollectorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hyvv-patch-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.be, self.env = _local_backend(self.tmp)
        self.src = _src_repo(self.tmp)

    def _seed(self):
        out = asyncio.run(self.be._seed_project(object(), {"path": str(self.src)}, "proj"))
        self.assertEqual(len(out), 3)
        wd, rev, seed = out
        self.assertRegex(seed, r"^[0-9a-f]{40}$")
        self.assertRegex(rev, r"^[0-9a-f]{40}$")
        return wd, rev, seed

    def _git(self, wd, *args):
        subprocess.run(["git", "-C", wd, "-c", "user.name=bee", "-c", "user.email=bee@x", *args], check=True, env=self.env, capture_output=True)

    def _attempt(self, wd, seed):
        return {"kind": "opencode", "sandbox": "x", "workdir": wd, "seed_commit": seed, "brief": {"assigned_files": ["b.txt"]}}

    def test_bee_commit_does_not_empty_the_diff(self):
        wd, _, seed = self._seed()
        Path(wd, "b.txt").write_text("two\n")
        self._git(wd, "add", "-A"); self._git(wd, "commit", "-qm", "RED first")
        p = asyncio.run(self.be.patch(self._attempt(wd, seed)))
        self.assertIn("b.txt", p["diff"])
        self.assertEqual([f["path"] for f in p["files"]], ["b.txt"])
        self.assertTrue(p["files"][0]["in_assigned_scope"])
        self.assertEqual(p["base"], seed)

    def test_checkout_B_does_not_empty_the_diff(self):
        wd, _, seed = self._seed()
        self._git(wd, "checkout", "-q", "-B", "ci-under-test")
        Path(wd, "b.txt").write_text("two\n")
        self._git(wd, "add", "-A"); self._git(wd, "commit", "-qm", "x")
        self._git(wd, "checkout", "-q", "-B", "ci-under-test-2")
        p = asyncio.run(self.be.patch(self._attempt(wd, seed)))
        self.assertIn("b.txt", p["diff"])

    def test_uncommitted_work_still_collected(self):
        wd, _, seed = self._seed()
        Path(wd, "b.txt").write_text("two\n")
        p = asyncio.run(self.be.patch(self._attempt(wd, seed)))
        self.assertIn("b.txt", p["diff"])

    def test_bad_seed_value_falls_back_to_head_and_is_not_interpolated(self):
        wd, _, _ = self._seed()
        Path(wd, "b.txt").write_text("two\n")
        p = asyncio.run(self.be.patch(self._attempt(wd, "HEAD; touch pwned")))
        self.assertIn("b.txt", p["diff"])
        self.assertEqual(p["base"], "HEAD")
        self.assertFalse(Path(wd, "pwned").exists())


def _launcher(tmp, **kw):
    cfg = _cfg(tmp, **kw)
    la = L.Launcher(cfg, L.FakeBackend(cfg))
    import threading
    threading.Thread(target=la.loop.run_forever, daemon=True).start()
    return la


def _wait(la, aid, t=10):
    end = time.time() + t
    while time.time() < end:
        if la.attempts[aid]["state"] in L.TERMINAL: return la.attempts[aid]
        time.sleep(0.05)
    raise AssertionError(f"attempt {aid} stuck in {la.attempts[aid]['state']}")


class CheckRunChannel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hyvv-check-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_check_kind_is_known(self):
        self.assertIn("check", L.KINDS)

    def test_check_run_result_does_not_depend_on_diff(self):
        la = _launcher(self.tmp)
        brief = {"objective": "ci: acceptance-light", "commands": ["unshare -Ur true", "bash acceptance/cold_start.sh"],
                 "security_profile": "acceptance", "fake": {"check_exits": [0, 0]}}
        a, created = la.submit("att_00000000aa", brief, kind="check")
        self.assertTrue(created)
        a = _wait(la, "att_00000000aa")
        self.assertEqual(a["state"], "succeeded")
        r = json.loads((Path(self.tmp) / "attempts" / "att_00000000aa.result.json").read_text("utf-8"))
        self.assertEqual(r["schema"], "orbit.local-worker.check-result/v1")
        self.assertIsNone(r["patch"])
        cr = r["check_run"]
        self.assertEqual(cr["conclusion"], "success")
        self.assertEqual(cr["security_profile"], "acceptance")
        self.assertEqual([c["exit_code"] for c in cr["checks"]], [0, 0])
        self.assertEqual([c["command"] for c in cr["checks"]], brief["commands"])
        for c in cr["checks"]:
            self.assertIn("stdout_tail", c); self.assertIn("stderr_tail", c); self.assertIn("duration_s", c)
        self.assertEqual(r["source"]["image"], "hyvv-acceptance:test")

    def test_failing_check_fails_attempt_with_exit_codes(self):
        la = _launcher(self.tmp)
        brief = {"objective": "ci", "commands": ["true", "false"], "security_profile": "acceptance", "fake": {"check_exits": [0, 1]}}
        la.submit("att_00000000bb", brief, kind="check")
        a = _wait(la, "att_00000000bb")
        self.assertEqual(a["state"], "failed")
        r = json.loads((Path(self.tmp) / "attempts" / "att_00000000bb.result.json").read_text("utf-8"))
        self.assertEqual(r["check_run"]["conclusion"], "failure")
        self.assertEqual([c["exit_code"] for c in r["check_run"]["checks"]], [0, 1])

    def test_check_needs_commands(self):
        la = _launcher(self.tmp)
        with self.assertRaises(ValueError):
            la.submit("att_00000000cc", {"objective": "ci"}, kind="check")

    def test_coding_bee_cannot_pick_acceptance_profile(self):
        la = _launcher(self.tmp)
        with self.assertRaises(ValueError):
            la.submit("att_00000000dd", {"objective": "code", "security_profile": "acceptance"})


class DockerRunner(unittest.TestCase):
    def test_runner_lifecycle_uses_profile_and_cleans_only_own_container(self):
        tmp = tempfile.mkdtemp(prefix="hyvv-dock-"); self.addCleanup(shutil.rmtree, tmp, True)
        src = _src_repo(tmp)
        cfg = _cfg(tmp, projects={"p": {"path": str(src)}})
        calls = []
        def fake_run(argv, input=None, timeout=None):
            calls.append(list(argv))
            if argv[:2] == ["docker", "exec"] and "rev-parse" in argv[-1]:
                return 0, "a" * 40 + "\n", ""
            if argv[:2] == ["docker", "exec"] and argv[-1] == "exit 3":
                return 3, "out\n", "boom\n"
            return 0, "", ""
        r = L.DockerCheckRunner(cfg, run=fake_run)
        a = {"attempt_id": "att_0000000011", "kind": "check",
             "brief": {"objective": "ci", "project": "p", "commands": ["true", "exit 3"], "security_profile": "acceptance"}}
        ids = asyncio.run(r.start(a))
        a.update(ids)
        self.assertEqual(ids["security_profile"], "acceptance")
        self.assertEqual(ids["seed_commit"], "a" * 40)
        run = calls[0]
        self.assertEqual(run[:3], ["docker", "run", "-d"])
        self.assertIn("seccomp=" + str(PROFILE), run)
        checks = asyncio.run(r.run_checks(a))
        self.assertEqual([c["exit_code"] for c in checks], [0, 3])
        self.assertIn("boom", checks[1]["stderr_tail"])
        asyncio.run(r.cleanup(a))
        self.assertEqual(calls[-1], ["docker", "rm", "-f", ids["sandbox"]])
        self.assertTrue(ids["sandbox"].startswith("hyvv-check-"))
        for c in calls:
            self.assertNotIn("prune", c)


if __name__ == "__main__":
    unittest.main()
