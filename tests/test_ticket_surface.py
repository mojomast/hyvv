"""RED for wave C slice C2: the ticket surface at ``POST /v1/attempts``.

hyvv ADR 0002 puts the signed dispatch ticket at the attempt boundary. C1
(workflow #171) froze the twelve claims as ``rizzstack.ticket/1``; C2 is the
hyvv half that accepts those claims in report mode and validates them against
a vendored copy of the schema. C2 does **not** verify signatures, hold keys or
compare the claims to the brief - C4 owns all of that once wave D's RFC 8785
core exists.

The frozen surface these tests pin down:

* ``brief.ticket`` is optional. In ``ticket_mode: off`` (the default) hyvv
  does not inspect it and adds nothing to the response or the journal, so a
  create without a ticket is byte-for-byte the pre-C2 path.
* In ``ticket_mode: report`` hyvv validates the claims object against
  ``schemas/rizzstack-ticket.schema.json`` and **always accepts** the attempt;
  the outcome is recorded as ``ticket_report`` on the attempt (and in the POST
  response) with ``status`` in ``missing | valid | invalid``, a ``violations``
  list whose entries carry ``code`` and a ``detail`` naming the claim, and
  ``signature: "not_checked"``.
* ``ticket_mode`` comes from config only, defaults to ``off``, and accepts only
  ``off``/``report``; ``enforce`` is refused at load so nobody can believe
  enforcement is on. No environment variable changes the mode.
* No crypto: no signature verification and no key material in this slice.

RED now: none of that exists on main. Two tests are regressions that already
pass at the red revision (no-ticket create; off-mode silence) and are marked
``regression`` in the contract.
"""

import http.client
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher_c2", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
spec.loader.exec_module(L)

TOKEN = "t" * 40

#: The ADR's twelve claims, in the ADR's order. Kept explicit: the tests are
#: the frozen contract, so they must not read the answer back out of the schema.
ADR_CLAIMS = (
    "brief_sha256",
    "project",
    "source_rev",
    "assigned_files_sha256",
    "risk_tier",
    "contract_sha256",
    "assignment",
    "aud",
    "iat",
    "exp",
    "jti",
    "kid",
)

VALID_CLAIMS = {
    "brief_sha256": "a" * 64,
    "project": "hyvv",
    "source_rev": "b" * 40,
    "assigned_files_sha256": "c" * 64,
    "risk_tier": "high",
    "contract_sha256": None,
    "assignment": None,
    "aud": "hyvv-worker-host",
    "iat": 1764000000,
    "exp": 1764000600,
    "jti": "6d1c0f5a2b7e4c3d9a8f0e1b2c3d4e5f",
    "kid": "coordinator-2026-11",
}

_MISSING = object()


def _brief(ticket=_MISSING):
    """A minimal brief bundlebees can post, optionally carrying ticket claims."""
    brief = {
        "objective": "exercise the C2 ticket surface",
        "project": "demo",
        "branch": "feat/wave-c-c2",
        "source_rev": "a" * 40,
        "assigned_files": ["launcher.py"],
        "acceptance": ["the attempt is accepted in report mode"],
        "fake": {"delay": 2},
    }
    if ticket is not _MISSING:
        brief["ticket"] = ticket
    return brief


class _TicketSurface(unittest.TestCase):
    """A real loopback launcher with this class's ticket_mode."""

    MODE = "off"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hyvv-c2-")
        (Path(self.tmp) / "go.key").write_text("fake-provider-key")
        (Path(self.tmp) / "deepseek.key").write_text("fake-deepseek-key")
        cfg = {
            "bind": "127.0.0.1",
            "port": 0,
            "max_concurrent": 2,
            "data_dir": self.tmp,
            "backoff_s": [0.3],
            "model": "deepseek-v4.1-flash",
            "base_url": "http://x",
            "wire": "openai",
            "image": "cua-bee:test",
            "runtime": "runc",
            "memory_mb": 4096,
            "ticket_mode": self.MODE,
            "projects": {
                "demo": {"path": "C:/work/demo", "repo": "https://example.invalid/r.git"},
                "other": {},
            },
        }
        self.la = L.Launcher(cfg, L.FakeBackend(cfg))
        threading.Thread(target=self.la.loop.run_forever, daemon=True).start()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), L.make_handler(self.la, TOKEN))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        conn.request(
            method,
            path,
            body=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
        )
        response = conn.getresponse()
        payload = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, payload

    def post(self, attempt_id, brief):
        return self.req("POST", "/v1/attempts", {"attempt_id": attempt_id, "brief": brief})

    def journal(self, attempt_id):
        return json.loads((Path(self.tmp) / "attempts" / f"{attempt_id}.json").read_text())


class OffModeTicketSurface(_TicketSurface):
    """ticket_mode off is the default and must stay invisible."""

    MODE = "off"

    def test_no_ticket_brief_is_accepted_unchanged(self):
        # Regression: the pre-C2 create path. No ticket, no new response field.
        status, body = self.post("att_c2off00001", _brief())
        self.assertEqual(status, 201, body)
        self.assertEqual(body["attempt_id"], "att_c2off00001")
        self.assertIn(body["state"], ("queued", "starting", "running"))
        self.assertNotIn("ticket_report", body)
        record = self.journal("att_c2off00001")
        self.assertNotIn("ticket_report", record)
        self.assertNotIn("ticket", record["brief"])

    def test_off_mode_ignores_a_malformed_ticket(self):
        # Regression: off never inspects the ticket, so a broken one is stored
        # as sent and cannot influence admission.
        claims = dict(VALID_CLAIMS, source_rev="deadbeef")
        status, body = self.post("att_c2off00002", _brief(claims))
        self.assertEqual(status, 201, body)
        self.assertNotIn("ticket_report", body)
        record = self.journal("att_c2off00002")
        self.assertNotIn("ticket_report", record)
        self.assertEqual(record["brief"]["ticket"]["source_rev"], "deadbeef")


class ReportModeTicketSurface(_TicketSurface):
    """ticket_mode report validates and records, but never refuses."""

    MODE = "report"

    def _report(self, attempt_id, brief):
        status, body = self.post(attempt_id, brief)
        self.assertEqual(status, 201, body)
        self.assertIn("ticket_report", body)
        report = body["ticket_report"]
        self.assertEqual(report["schema"], "rizzstack.ticket-report/1")
        self.assertEqual(report["mode"], "report")
        self.assertEqual(report["signature"], "not_checked")
        self.assertIsInstance(report["violations"], list)
        return report, self.journal(attempt_id)

    def test_report_mode_records_a_malformed_ticket_and_still_accepts(self):
        claims = dict(VALID_CLAIMS, source_rev="deadbeef")
        report, record = self._report("att_c2rep00001", _brief(claims))
        self.assertEqual(report["status"], "invalid")
        codes = {violation["code"] for violation in report["violations"]}
        self.assertIn("ticket_invalid", codes)
        self.assertTrue(
            any("source_rev" in violation["detail"] for violation in report["violations"]),
            report["violations"],
        )
        # Recorded on the attempt, not just echoed once.
        self.assertEqual(record["ticket_report"]["status"], "invalid")
        self.assertTrue(
            any("source_rev" in violation["detail"] for violation in record["ticket_report"]["violations"]),
            record["ticket_report"]["violations"],
        )
        self.assertIn(record["state"], ("queued", "starting", "running"))

    def test_report_mode_records_a_missing_ticket_and_still_accepts(self):
        report, record = self._report("att_c2rep00002", _brief())
        self.assertEqual(report["status"], "missing")
        self.assertEqual([violation["code"] for violation in report["violations"]], ["ticket_missing"])
        self.assertIn("ticket", report["violations"][0]["detail"])
        self.assertEqual(record["ticket_report"]["status"], "missing")

    def test_twelve_claim_ticket_passes_schema_validation(self):
        schema_path = HERE / "schemas" / "rizzstack-ticket.schema.json"
        self.assertTrue(schema_path.is_file(), "schemas/rizzstack-ticket.schema.json is not vendored")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema.get("$schema"), "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema.get("title"), "rizzstack.ticket/1")
        self.assertFalse(schema.get("additionalProperties", True), "unknown claims must be refused")
        self.assertEqual(len(schema.get("required", [])), 12)
        self.assertEqual(set(schema["required"]), set(ADR_CLAIMS))
        self.assertEqual(len(VALID_CLAIMS), 12)
        self.assertIsNone(VALID_CLAIMS["contract_sha256"])
        self.assertIn("null", schema["properties"]["contract_sha256"]["type"])
        self.assertTrue(hasattr(L, "ticket_violations"), "launcher.ticket_violations is missing")
        self.assertEqual(L.ticket_violations(VALID_CLAIMS), [])

        report, record = self._report("att_c2rep00003", _brief(VALID_CLAIMS))
        self.assertEqual(report["status"], "valid")
        self.assertEqual(report["violations"], [])
        self.assertEqual(record["ticket_report"]["status"], "valid")

    def test_no_signature_verification_or_key_material_in_this_slice(self):
        # A schema-valid claims set is "valid" without any signature, and the
        # report says so. A JWS-shaped ticket is not verified: it is just a
        # claims value that fails the object schema, and the attempt is accepted.
        report, _ = self._report("att_c2rep00004", _brief(VALID_CLAIMS))
        self.assertEqual(report["status"], "valid")
        self.assertEqual(report["signature"], "not_checked")

        jws_report, _ = self._report("att_c2rep00005", _brief("header.payload.signature"))
        self.assertEqual(jws_report["status"], "invalid")
        self.assertEqual(jws_report["signature"], "not_checked")
        self.assertTrue(
            any("object" in violation["detail"].lower() for violation in jws_report["violations"]),
            jws_report["violations"],
        )

        source = (HERE / "launcher.py").read_text(encoding="utf-8").lower()
        for token in (
            "import cryptography",
            "from cryptography",
            "import nacl",
            "from nacl",
            "import jwt",
            "from jwt",
            "ed25519",
            "begin private key",
            "begin public key",
        ):
            self.assertNotIn(token, source, f"launcher.py must not bring {token!r} into C2")

        schema_path = HERE / "schemas" / "rizzstack-ticket.schema.json"
        self.assertTrue(schema_path.is_file(), "schemas/rizzstack-ticket.schema.json is not vendored")
        schema_text = schema_path.read_text(encoding="utf-8")
        self.assertNotIn("BEGIN ", schema_text, "the vendored schema must not carry key material")
        self.assertNotIn("public_key", schema_text)
        self.assertNotIn("private_key", schema_text)


class ConfigTicketMode(unittest.TestCase):
    """ticket_mode is config-only, defaults to off and cannot be forced on."""

    @staticmethod
    def _config(payload):
        directory = Path(tempfile.mkdtemp(prefix="hyvv-c2-cfg-"))
        path = directory / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_ticket_mode_defaults_off_and_environment_cannot_enable_enforce(self):
        cfg = L.load_config(self._config({}))
        self.assertEqual(cfg.get("ticket_mode"), "off")

        cfg = L.load_config(self._config({"ticket_mode": "report"}))
        self.assertEqual(cfg["ticket_mode"], "report")

        with self.assertRaises(ValueError) as caught:
            L.load_config(self._config({"ticket_mode": "enforce"}))
        self.assertIn("ticket_mode", str(caught.exception))

        names = ("HYYV_TICKET_MODE", "TICKET_MODE", "RIZZSTACK_TICKET_MODE", "BUNDLEBEES_TICKET_MODE")
        saved = {name: os.environ.get(name) for name in names}
        try:
            for name in names:
                os.environ[name] = "enforce"
            cfg = L.load_config(self._config({}))
            self.assertEqual(cfg.get("ticket_mode"), "off")
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
