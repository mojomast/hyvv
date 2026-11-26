"""RED: hyvv refuses an invalid rizzstack.brief/1 only when ticket_mode=enforce.

The default stays off. `off` and `report` keep B4's report-only behaviour exactly;
`enforce` refuses an invalid v1 brief with 400 invalid_brief, the validator's
problems, and no attempt row. An unversioned brief is still accepted and recorded
as unversioned. The mode is validated at load_config, so a typo cannot silently
arm or disarm enforcement.

This file is the specification for the slice; the implementer must not edit it.
It is stacked on B4's RED commit (the vendored validator and schemas are there);
the branch must merge current main after B4 lands before implementing.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules["launcher"] = L
spec.loader.exec_module(L)

TOKEN = "t" * 40


def _v1_brief(**overrides):
    brief = {
        "schema": "rizzstack.brief/1",
        "objective": "Enforce the brief contract at the admission boundary.",
        "project": "demo",
        "source_rev": "a" * 40,
        "assigned_files": ["launcher.py"],
        "acceptance": {"criteria": ["ticket_mode decides whether an invalid brief is refused"]},
        "risk": {"tier": "low"},
    }
    brief.update(overrides)
    return brief


class _LauncherServer:
    def __init__(self, mode):
        tmp = tempfile.mkdtemp(prefix="hyvv-b6-")
        for name in ("go.key", "deepseek.key"):
            (Path(tmp) / name).write_text("fake-key-not-real", encoding="utf-8")
        cfg = {
            "bind": "127.0.0.1", "port": 0, "max_concurrent": 1, "data_dir": tmp,
            "backoff_s": [0.2], "model": "deepseek-v4.1-flash", "base_url": "http://x",
            "wire": "openai", "image": "cua-bee:test", "runtime": "runc", "memory_mb": 512,
            "projects": {"demo": {}}, "ticket_mode": mode,
        }
        self.la = L.Launcher(cfg, L.FakeBackend(cfg))
        from http.server import ThreadingHTTPServer
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), L.make_handler(self.la, TOKEN))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def post(self, brief, attempt_id):
        body = json.dumps({"attempt_id": attempt_id, "brief": brief}).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        conn.request("POST", "/v1/attempts", body=body,
                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN})
        resp = conn.getresponse()
        raw = resp.read()
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {}
        return resp.status, parsed

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class TicketModeConfig(unittest.TestCase):
    def test_ticket_mode_defaults_off_and_refuses_unknown_values(self):
        with tempfile.TemporaryDirectory(prefix="hyvv-b6-cfg-") as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{}", encoding="utf-8")
            self.assertEqual(L.load_config(path).get("ticket_mode"), "off")
            path.write_text('{"ticket_mode": "banana"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                L.load_config(path)


class EnforceMode(unittest.TestCase):
    def _server(self, mode):
        return _LauncherServer(mode)

    def test_off_and_report_still_accept(self):
        for mode in ("off", "report"):
            server = self._server(mode)
            try:
                brief = _v1_brief()
                brief.pop("risk")
                status, body = server.post(brief, f"att_b6_accept_{mode}")
                self.assertEqual(status, 201, f"ticket_mode={mode} is report-only: {body}")
                self.assertIn(f"att_b6_accept_{mode}", server.la.attempts)
            finally:
                server.close()

    def test_enforce_refuses_an_invalid_brief_without_creating_an_attempt(self):
        server = self._server("enforce")
        try:
            brief = _v1_brief()
            brief.pop("risk")
            status, body = server.post(brief, "att_b6_refused")
            self.assertEqual(status, 400, body)
            self.assertEqual(body.get("error"), "invalid_brief")
            self.assertIsInstance(body.get("problems"), list, body)
            self.assertTrue(any("risk" in problem for problem in body["problems"]), body)
            self.assertNotIn("att_b6_refused", server.la.attempts)
        finally:
            server.close()

    def test_enforce_accepts_a_valid_brief(self):
        server = self._server("enforce")
        try:
            status, body = server.post(_v1_brief(), "att_b6_valid")
            self.assertEqual(status, 201, body)
            validation = body.get("brief_validation") or {}
            self.assertEqual(validation.get("status"), "valid")
        finally:
            server.close()

    def test_enforce_does_not_break_the_unversioned_path(self):
        server = self._server("enforce")
        try:
            status, body = server.post({"objective": "Legacy brief.", "project": "demo"}, "att_b6_unversioned")
            self.assertEqual(status, 201, body)
            validation = body.get("brief_validation") or {}
            self.assertEqual(validation.get("status"), "unversioned")
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
