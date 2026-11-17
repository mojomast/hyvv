"""Existing launcher/controller/HTTP; only the worker and credentials are synthetic."""

import asyncio
import hashlib
import http.client
import json
import os
import secrets
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import launcher

ROOT = Path(__file__).resolve().parents[1]
AID = "att_1234567890abcdef"


class Native(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.admin, self.reader = secrets.token_hex(32), secrets.token_hex(32)
        (self.root / "reader").write_text(self.reader, "utf-8")
        self.enterContext(mock.patch.dict(os.environ, HYVV_RAILS_READ_TOKEN_FILE=""))
        self.cfg = dict(
            data_dir=str(self.root),
            model="deepseek-flash",
            image="fixture",
            projects={},
            max_concurrent=1,
            base_url=launcher.GO_BASE_URL,
            wire="openai-chat",
            rails_enabled=True,
            rails_read_token_file=str(self.root / "reader"),
            rails_read_peers=["127.0.0.1"],
        )
        self.L = launcher.Launcher(self.cfg, launcher.FakeBackend(self.cfg))
        self.addCleanup(self.L.loop.close)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), launcher.make_handler(self.L, self.admin))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def req(self, path, token=None, method="GET", **headers):
        with closing(http.client.HTTPConnection(*self.srv.server_address, timeout=4)) as c:
            return self.send(c, path, token, method, **headers)

    def send(self, c, path, token=None, method="GET", **headers):
        if token:
            headers["Authorization"] = "Bearer " + token
        c.request(method, path, headers=headers)
        r = c.getresponse()
        return r.status, r.read()

    def test_scopes_query_and_readonly(self):
        head = "/v1/rails/head"
        events = "/v1/rails/events"
        for cred in (None, self.admin):
            self.assertIn(self.req(events, cred)[0], (401, 403))
        self.assertEqual(self.req("/v1/version", self.reader)[0], 401)
        self.assertEqual(self.req("/v1/version", self.admin)[0], 200)
        self.assertEqual(self.req("/v1/config", self.reader, "POST")[0], 401)
        self.assertEqual(self.req(events + "?token=" + self.reader, self.reader)[0], 400)
        for query in ("after=-1", "after=0&after=1", "limit=501", "limit=0", "db=x"):
            self.assertEqual(self.req(events + "?" + query, self.reader)[0], 400)
        self.assertEqual(self.req(head, self.reader, Host="other.invalid")[0], 403)
        self.assertEqual(self.req(head, self.reader, Origin="https://other.invalid")[0], 403)
        self.assertEqual(self.req(head, self.reader)[0], 200)
        self.assertEqual(self.req(events + "?after=2", self.reader)[0], 409)
        # A GET never imports existing source data.
        (self.root / "attempts" / (AID + ".events.jsonl")).write_text(
            '{"kind":"state","state":"running","lseq":1}\n', "utf8"
        )
        self.assertEqual(json.loads(self.req(events, self.reader)[1])["events"], [])

    def test_peer_allowlist_and_distinct_secrets(self):
        class WithPeers:
            def __init__(self, cfg):
                self.cfg = cfg

        self.srv.RequestHandlerClass = launcher.make_handler(
            WithPeers(dict(self.cfg, rails_read_peers=["192.0.2.1"])), self.admin
        )
        self.assertEqual(self.req("/v1/rails/head", self.reader, **{"X-Forwarded-For": "192.0.2.1"})[0], 403)
        (self.root / "reader").write_text(self.admin, "utf-8")
        with self.assertRaises(RuntimeError):
            launcher.make_handler(self.L, self.admin)

    def set_read_deadline(self, value):
        self.L.cfg["rails_read_expires_at"] = value
        self.srv.RequestHandlerClass = launcher.make_handler(self.L, self.admin)

    def assert_read_refused(self, response):
        status, raw = response
        self.assertEqual(status, 401)
        body = json.loads(raw)
        self.assertEqual(set(body), {"ok", "error"})
        self.assertIs(body["ok"], False)
        self.assertEqual(body["error"]["code"], "auth_required")
        self.assertIs(body["error"]["retryable"], False)

    def test_read_deadline_refuses_without_advancing_ledger_or_cursor(self):
        deadline = 2_000_000_000
        self.L._append_events({"attempt_id": AID}, [dict(kind="state", state="running", lseq=1)])
        page = "/v1/rails/events?after=0&limit=1"
        head = "/v1/rails/head"
        with (
            mock.patch("rails_export.time", return_value=deadline - 0.25) as clock,
            closing(http.client.HTTPConnection(*self.srv.server_address, timeout=4)) as conn,
            closing(sqlite3.connect(self.L.rails.path.as_uri() + "?mode=ro", uri=True)) as db,
        ):
            self.set_read_deadline(deadline)
            self.assertEqual(self.send(conn, head, self.reader)[0], 200)
            status, raw = self.send(conn, page, self.reader)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["next_cursor"], 1)

            cursor = db.execute("SELECT cursor FROM hyvv_log_cursor WHERE attempt=?", (AID,)).fetchone()
            self.assertIsNotNone(cursor)
            before = list(db.iterdump())  # includes origin identity, rows and durable cursors
            for now in (deadline, deadline + 1):
                clock.return_value = now
                for path in (head, page):
                    self.assert_read_refused(self.send(conn, path, self.reader))  # same keep-alive connection
            self.set_read_deadline(deadline)  # rebuilding the handler must not renew the grant
            self.assert_read_refused(self.req(page, self.reader))
            self.assertEqual(list(db.iterdump()), before)
        self.assertEqual(self.req("/v1/version", self.admin)[0], 200)

    def test_explicit_invalid_deadline_fails_closed(self):
        with mock.patch("rails_export.time", return_value=0):
            for value in (None, True, False, "2000000000", 2e9, -1, 253_402_300_800, {}):
                with self.subTest(value=value):
                    self.set_read_deadline(value)
                    self.assert_read_refused(self.req("/v1/rails/events", self.reader))
                    self.assertEqual(self.req("/v1/version", self.admin)[0], 200)

    def test_sdk_adapter_keeps_only_structured_fields(self):
        native = SimpleNamespace(
            seq=1,
            kind="tool_update",
            line="[tool completed] private-tool-args",
            tool_id="call_0000000000000011",
            tool_title="private-title",
            tool_status="completed",
            stop_reason=None,
            json="private-json",
        )
        backend = object.__new__(launcher.CuaBackend)
        backend.cfg = self.cfg
        backend._run = mock.AsyncMock(
            return_value=SimpleNamespace(
                events=mock.AsyncMock(return_value=SimpleNamespace(events=[native], cursor=1))
            )
        )
        evs, cursor = asyncio.run(backend.events({}, 0))
        self.assertEqual(cursor, 1)
        (row,) = evs
        self.assertEqual(row["tool_id"], native.tool_id)
        self.assertEqual(row["tool_status"], "completed")
        self.assertTrue(set(row).isdisjoint({"json", "tool_title", "tool_name"}))
        self.L._append_events({"attempt_id": AID}, [dict(row, kind="worker:tool_update", lseq=1)])
        status, page = self.req("/v1/rails/events", self.reader)
        self.assertEqual(status, 200)
        self.assertNotIn(b"private-", page)
        mark = json.loads(page)["events"][0]
        self.assertEqual(mark["kind"], "tool.finish")
        self.assertEqual(mark["payload"]["call_id"], native.tool_id)
        self.assertIsNone(mark["duration_ms"])

    def test_actual_controller_receipt_restart_and_original_bytes(self):
        self.L.kick = lambda: None
        fake = dict(
            events=[
                dict(kind="tool_call", line="[tool execute] bash", tool_id="call_1234567890abcdef"),
                dict(
                    kind="tool_update",
                    tool_status="completed",
                    tool_id="call_1234567890abcdef",
                    line="private-result",
                ),
                dict(kind="turn_ended", stop_reason="end_turn"),
            ]
        )
        a, _ = self.L.submit(AID, {"objective": "fixture", "fake": fake})
        result = {
            "status": "completed",
            "text": "private-result",
            "artifacts": [],
            "usage": {"input": 11, "output": 0},
        }
        with (
            mock.patch.object(launcher, "provider_key", return_value="fixture"),
            mock.patch.object(self.L.backend, "result", new=mock.AsyncMock(return_value=result)),
        ):
            asyncio.run(self.L._control(a))
        self.assertEqual(a["state"], "succeeded")
        path = "/v1/rails/events"
        code, body = self.req(path, self.reader)
        self.assertEqual(code, 200)
        page = json.loads(body)
        self.assertEqual(page["schema"], "rizzstack.rails-export/1")
        self.assertNotIn(b"private-result", body)
        self.assertEqual(
            [e["kind"] for e in page["events"] if e["kind"].startswith("tool.")],
            ["tool.start", "tool.finish"],
        )
        self.assertTrue(all(e["authority"] == "worker_report" for e in page["events"]))
        for e in page["events"]:
            canonical = json.dumps(e, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode(
                "ascii"
            )
            self.assertIn(canonical, body)
        (usage,) = [e["payload"] for e in page["events"] if e["kind"] == "usage.observed"]
        self.assertEqual(
            (usage["input_tokens"], usage["output_tokens"], usage["thinking_tokens"], usage["usage_basis"]),
            (11, 0, None, "final"),
        )
        # Disconnect without consuming/acknowledging the first response. The
        # next GET at the same position returns exactly the same origin bytes.
        c = http.client.HTTPConnection(*self.srv.server_address, timeout=4)
        c.request(
            "GET",
            path + "?limit=1",
            headers={"Authorization": "Bearer " + self.reader, "Connection": "close"},
        )
        first = c.getresponse()
        self.assertEqual(first.status, 200)
        first.close()
        c.close()
        first_page = json.loads(self.req(path + "?limit=1", self.reader)[1])
        self.assertEqual(first_page["events"], page["events"][:1])
        self.assertEqual(
            json.loads(self.req(path + "?after=1", self.reader)[1])["events"], page["events"][1:]
        )
        L2 = launcher.Launcher(self.cfg, launcher.FakeBackend(self.cfg))
        self.addCleanup(L2.loop.close)
        self.assertEqual(self.req(path, self.reader)[1], body)

    def test_runtime_pin(self):
        manifest = json.loads((ROOT / "contrib/rizzstack_rails/vendor-manifest.json").read_bytes())
        for entry in manifest["files"]:
            self.assertEqual(hashlib.sha256((ROOT / entry["path"]).read_bytes()).hexdigest(), entry["sha256"])
