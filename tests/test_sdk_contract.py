"""Checks CuaBackend against the REAL installed cua package (no network, no Docker).
Run: uv run --python 3.12 --with cua==0.5.0 python tests/test_sdk_contract.py
1) every method/field in SDK_CONTRACT/SDK_TYPES exists with the right async-ness and params;
2) every backend path (start, events, interrupt, result, artifacts, sha256, cleanup, reattach)
   runs against autospec'd doubles of the real classes, so a call on the wrong type
   (e.g. SandboxInfo.agents) or a wrong argument fails here."""
import asyncio, inspect, sys, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher  # noqa: E402
def _real_cua_native():
    """Only the real Cua SDK counts. A MagicMock left in sys.modules by another test does not."""
    existing = sys.modules.get("cua")
    if existing is not None and isinstance(existing, mock.Mock):
        return None
    try:
        from cua import _native as native  # noqa: E402
    except Exception:
        return None
    if isinstance(native, mock.Mock):
        return None
    return native

n = _real_cua_native()


def spec(cls, **attrs):
    m = mock.create_autospec(cls, instance=True)
    for name, fn in inspect.getmembers(cls, inspect.iscoroutinefunction):
        if not name.startswith("_"):
            setattr(m, name, mock.AsyncMock(spec=fn, side_effect=None))
            getattr(m, name).__signature__ = inspect.signature(fn)
    for k, v in attrs.items(): setattr(getattr(m, k), "return_value", v) if callable(getattr(m, k, None)) else setattr(m, k, v)
    return m

def check_call(mock_fn, real):
    sig = inspect.signature(real)
    for c in mock_fn.call_args_list:
        sig.bind(None, *c.args, **c.kwargs)  # raises TypeError on a wrong signature

@unittest.skipIf(n is None, "cua SDK is not installed")
class Contract(unittest.TestCase):
    def setUp(self):
        native = _real_cua_native()
        if native is None:
            self.skipTest("cua SDK is not installed")

    def test_contract(self):
        self.assertEqual(launcher.sdk_contract_check(), [])

    def test_backend_paths(self):
        page = n.AgentEventPage(events=[n.AgentEvent(seq=1, ts_ms=0, turn=1, kind="turn_ended", text=None, tool_id=None, tool_title=None,
                                tool_kind=None, tool_status=None, stop_reason="end_turn", category="x", summary=None, line="done", json="{}")], cursor=1, caught_up=True)
        res = n.AgentRunResult(run_id="r1", status="completed", turn=1, text="ok", stop_reason="end_turn", usage_json=None, error=None, tool_calls=0)
        run = spec(n.AgentRun); run.run_id.return_value = "r1"
        run.events.return_value = page; run.result.return_value = res
        run.artifacts.return_value = [n.AgentArtifact(path="a.txt", size=3, modified_ms=0)]
        agents = spec(n.Agents); agents.run.return_value = run; agents.get.return_value = run
        sb = spec(n.Sandbox); sb.agents.return_value = agents
        sd = spec(n.SpacesdClient)
        sd.sh.return_value = mock.Mock(spec=n.ProcessOutput, stdout=b"ab" * 32 + b"\n", stderr=b"", pty=b"", exit=mock.Mock(code=0))
        sb.spacesd.return_value = sd
        sb.guest_sh.side_effect = AssertionError("guest_sh is Lume/agentless only; containers use spacesd().sh")
        sb.id.return_value = "sbx-1"
        sbs = spec(n.Sandboxes); sbs.create.return_value = sb; sbs.connect.return_value = sb
        sbs.get.side_effect = AssertionError("Sandboxes.get returns SandboxInfo; use connect()")
        client = mock.create_autospec(n.Cua, instance=True); client.sandboxes.return_value = sbs
        with mock.patch("cua.embedded", return_value=client):
            cfg = {"runtime": "runc", "image": "img", "memory_mb": 1024, "projects": {}, "model": "deepseek-v4.1-flash",
                   "base_url": launcher.GO_BASE_URL, "wire": "openai-chat"}
            be = launcher.CuaBackend(cfg)
            a = {"attempt_id": "att_0123456789abcdef", "brief": {"objective": "x"}}
            async def go():
                ids = await be.start(a, "k"); a.update(ids)
                evs, cur = await be.events(a, 0)
                self.assertEqual((evs[0]["kind"], cur), ("turn_ended", 1))
                r = await be.result(a); self.assertEqual(r["artifacts"][0]["path"], "a.txt")
                self.assertEqual(await be.file_sha256(a, "a.txt"), "ab" * 32)
                a["workdir"] = "/home/cua/work/demo"; await be.patch(a); await be.environment(a)
                import subprocess, tempfile
                repo = tempfile.mkdtemp(); open(f"{repo}/m.py", "w").write("x=1\n")
                for c in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "b"]): subprocess.run(["git", "-C", repo] + c, check=True)
                cfg["projects"] = {"p": {"path": repo}}
                b = {"attempt_id": "att_fedcba9876543210", "brief": {"objective": "x", "project": "p"}}
                ids = await be.start(b, "k"); self.assertTrue(ids["workdir"].endswith("/work/p")); self.assertEqual(len(ids["local_base_rev"]), 40)
                up = sd.upload.await_args.args; self.assertEqual(up[0], "/tmp/orbit-src.tar"); self.assertIsInstance(up[1], bytes)
                self.assertEqual(agents.run.await_args.args[2].cwd, ids["workdir"])
                be2 = launcher.CuaBackend(cfg)  # restart: fresh process reattaches by name + run id
                await be2.events(a, 0); await be2.interrupt(a); await be2.cleanup(a)
            asyncio.run(go())
        check_call(sbs.create, n.Sandboxes.create); check_call(sbs.connect, n.Sandboxes.connect); check_call(sbs.delete, n.Sandboxes.delete)
        check_call(agents.run, n.Agents.run); check_call(agents.get, n.Agents.get); check_call(run.events, n.AgentRun.events)
        check_call(sd.sh, n.SpacesdClient.sh); check_call(sd.upload, n.SpacesdClient.upload); check_call(sb.spacesd, n.Sandbox.spacesd)
        self.assertEqual(sbs.connect.await_args.args[-1], a["sandbox"]); self.assertEqual(sbs.delete.await_args.args[-1], a["sandbox"])
        run.interrupt.assert_awaited(); run.stop.assert_awaited()
        opts = agents.run.call_args.args[2]
        self.assertIsInstance(opts, n.AgentRunOptions)
        self.assertEqual((opts.model, opts.base_url, opts.wire), ("deepseek-v4.1-flash", launcher.GO_BASE_URL, "openai-chat"))
        self.assertIsInstance(sbs.create.call_args.args[0], n.SandboxCreateOptions)
        self.assertEqual(sbs.create.call_args.args[0].cpus, 2)
        self.assertEqual(sbs.create.call_args.args[0].memory_mb, 1024)

if __name__ == "__main__":
    unittest.main(verbosity=2)
