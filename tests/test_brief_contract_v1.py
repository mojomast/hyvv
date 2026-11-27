"""RED: hyvv reports rizzstack.brief/1 validation on POST /v1/attempts.

The approved behaviour is report-only: a v1 brief is accepted whether or not it
validates, and the attempt record says what the vendored validator saw. An
unversioned brief keeps working and is recorded as unversioned. Nothing here
adds a refusal path (that is B6, behind ticket_mode=enforce).

This file is the specification for the slice; the implementer must not edit it.
The vendored validator and the two schemas beside it are read-only givens and
are committed in the same RED commit.
"""
from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import subprocess
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
VALIDATOR = HERE / "hyvv" / "rizzstack_validators.py"
BRIEF_SCHEMA = HERE / "schemas" / "rizzstack-brief.schema.json"
ATTEMPT_SCHEMA = HERE / "schemas" / "rizzstack-attempt.schema.json"

# Frozen workflow sha256 values (workflow main 1ea44bea, the B3 landing).
VALIDATOR_SHA256 = "8a5ef6afa969a037ab5e719daa5e102fb39b5120b962e3ba8aab405ac1d5c5a9"
BRIEF_SCHEMA_SHA256 = "d9c28454ad7583fc53ac88ebb1eb42a0980d09c37b811bc6f6886a6146a97ff0"
ATTEMPT_SCHEMA_SHA256 = "cb2f6cd9e17606a915b568e148b7593e23f2fd67ccfaa1fabefba2978d21c7ab"


def _v1_brief(**overrides):
    brief = {
        "schema": "rizzstack.brief/1",
        "objective": "Report the brief contract on the attempt record.",
        "project": "demo",
        "source_rev": "a" * 40,
        "assigned_files": ["launcher.py"],
        "acceptance": {"criteria": ["the attempt carries brief_validation"]},
        "risk": {"tier": "low"},
    }
    brief.update(overrides)
    return brief


class _LauncherServer:
    def __init__(self):
        tmp = tempfile.mkdtemp(prefix="hyvv-b4-")
        for name in ("go.key", "deepseek.key"):
            (Path(tmp) / name).write_text("fake-key-not-real", encoding="utf-8")
        cfg = {
            "bind": "127.0.0.1", "port": 0, "max_concurrent": 1, "data_dir": tmp,
            "backoff_s": [0.2], "model": "deepseek-v4.1-flash", "base_url": "http://x",
            "wire": "openai", "image": "cua-bee:test", "runtime": "runc", "memory_mb": 512,
            "projects": {"demo": {}},
        }
        self.la = L.Launcher(cfg, L.FakeBackend(cfg))
        from http.server import ThreadingHTTPServer
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), L.make_handler(self.la, TOKEN))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def post(self, brief, attempt_id="att_b4_case"):
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


class BriefV1Report(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _LauncherServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_a_complete_v1_brief_is_valid(self):
        status, body = self.server.post(_v1_brief(), "att_b4_valid")
        self.assertEqual(status, 201, body)
        validation = body.get("brief_validation")
        self.assertIsInstance(validation, dict, body)
        self.assertEqual(validation["status"], "valid")
        self.assertEqual(validation["problems"], [])
        self.assertEqual(validation["unknown_fields"], [])

    def test_a_v1_brief_missing_risk_is_recorded_invalid_and_accepted(self):
        brief = _v1_brief()
        brief.pop("risk")
        status, body = self.server.post(brief, "att_b4_missing_risk")
        self.assertEqual(status, 201, "report-only: an invalid v1 brief is still accepted")
        validation = body.get("brief_validation") or {}
        self.assertEqual(validation.get("status"), "invalid")
        self.assertTrue(any("risk" in p for p in validation.get("problems") or []), validation)
        self.assertIn("att_b4_missing_risk", self.server.la.attempts)

    def test_an_undeclared_key_is_reported_as_unknown(self):
        status, body = self.server.post(_v1_brief(surprise="undeclared"), "att_b4_unknown_key")
        self.assertEqual(status, 201, "report-only: an undeclared key does not refuse the brief")
        validation = body.get("brief_validation") or {}
        self.assertIn("surprise", validation.get("unknown_fields") or [])
        self.assertTrue(any("surprise" in p for p in validation.get("problems") or []), validation)

    def test_an_unversioned_brief_is_recorded_as_unversioned(self):
        brief = {"objective": "Legacy brief without a schema key.", "project": "demo"}
        status, body = self.server.post(brief, "att_b4_unversioned")
        self.assertEqual(status, 201, body)
        validation = body.get("brief_validation") or {}
        self.assertEqual(validation.get("status"), "unversioned")
        for field in ("objective", "project"):
            self.assertIn(field, validation.get("legacy_fields") or [])


class VendoredCopy(unittest.TestCase):
    def test_the_vendored_validator_and_schemas_are_the_frozen_originals(self):
        for path, expected in ((VALIDATOR, VALIDATOR_SHA256),
                               (BRIEF_SCHEMA, BRIEF_SCHEMA_SHA256),
                               (ATTEMPT_SCHEMA, ATTEMPT_SCHEMA_SHA256)):
            self.assertTrue(path.is_file(), path)
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(actual, expected, f"{path.name} differs from the frozen workflow copy")

    def test_the_vendored_validator_runs_isolated_without_jsonschema(self):
        script = (
            "import json, sys;"
            f"sys.path.insert(0, {str(HERE)!r});"
            "from hyvv import rizzstack_validators as v;"
            f"doc = json.loads({json.dumps(_v1_brief())!r});"
            f"print(json.dumps(v.validate_brief(doc, schema_dir={str(HERE / 'schemas')!r})))"
        )
        out = subprocess.run([sys.executable, "-I", "-S", "-c", script],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [])


class ExistingRefusals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _LauncherServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_a_brief_without_objective_is_still_refused(self):
        status, body = self.server.post({"project": "demo"}, "att_b4_no_objective")
        self.assertEqual(status, 400, body)
        self.assertIn("objective", body.get("error", ""))


class PublicAttemptSchema(unittest.TestCase):
    def test_the_public_attempt_schema_declares_brief_validation(self):
        schema = json.loads((HERE / "contract" / "v1" / "schemas" / "attempt.json").read_text(encoding="utf-8"))
        self.assertIs(schema.get("additionalProperties"), False, "the public attempt schema stays a closed object")
        props = schema.get("properties") or {}
        self.assertIn("brief_validation", props, "the additive attempt field must be declared, not an extra key")
        self.assertEqual(props["brief_validation"].get("type"), ["object", "null"])


if __name__ == "__main__":
    unittest.main()
