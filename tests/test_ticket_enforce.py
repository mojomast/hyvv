"""RED for wave C slice C5: ticket_mode enforce at the attempt boundary.

C3 mints the Ed25519 ticket and C4 verifies it; C5 is the slice that makes the
verification *matter*. With ``ticket_mode: enforce`` on the worker host,
``POST /v1/attempts`` refuses an attempt whose ticket is missing, invalid,
expired, replayed or bound to another brief, and lets a valid ticket through.
``off`` and ``report`` keep their C2 behavior exactly.

The frozen surface these tests pin down:

* ``load_config`` keeps ``off`` as the default, accepts ``report`` and now also
  accepts ``enforce``; anything else raises ``ValueError``. No environment
  variable changes the mode.
* ``ticket_audience`` names this hyvv instance and ``ticket_public_keys`` maps
  ``kid`` to a base64url raw 32-byte Ed25519 public key; ``ticket_replay_dir``
  is the on-disk single-use ``jti`` cache. A missing/empty key mapping fails
  closed: every create is refused, never silently accepted.
* In ``enforce``: a valid ticket proceeds (201) and the attempt records
  ``ticket_report.status=valid`` / ``signature=verified``; a missing ticket is
  ``403 ticket_missing``; a bad signature or wrong audience is ``403
  ticket_invalid``; an expired ticket is ``403 ticket_expired``; a second use
  of the same ``jti`` is ``403 ticket_replayed``; a ticket for another brief is
  ``403 ticket_brief_mismatch``. A refused create leaves no attempt journal.
* In ``off`` nothing is inspected (no ``ticket_report``) and in ``report`` the
  attempt is accepted with ``ticket_report.status=invalid`` and
  ``signature=not_checked`` - C2 unchanged.

This branch also supersedes C2 AC-007's refusal of ``enforce`` at
``load_config``: the one assertion in ``tests/test_ticket_surface.py`` changes
so the earlier contract's node still resolves while the mode becomes legal.
That is the only edit to the C2 test file; its behavior tests are untouched.

RED now: the launcher refuses ``enforce`` and has no ticket verification. The
Ed25519 and JCS helpers are test-local; keys are generated at test time and
never written to disk.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- Ed25519
_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _inv(x):
    return pow(x, _P - 2, _P)


def _xrecover(y):
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if x % 2 != 0:
        x = _P - x
    return x


_BY = (4 * _inv(5)) % _P
_BX = _xrecover(_BY)
_B = (_BX % _P, _BY % _P)


def _ed_add(p, q):
    x1, y1 = p
    x2, y2 = q
    k = (_D * x1 * x2 * y1 * y2) % _P
    x3 = ((x1 * y2 + x2 * y1) * _inv(1 + k)) % _P
    y3 = ((y1 * y2 + x1 * x2) * _inv(1 - k)) % _P
    return (x3 % _P, y3 % _P)


def _ed_mul(p, e):
    q = (0, 1)
    while e > 0:
        if e & 1:
            q = _ed_add(q, p)
        p = _ed_add(p, p)
        e >>= 1
    return q


def _encode_point(p):
    x, y = p
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _public_key(seed):
    h = hashlib.sha512(seed).digest()
    a = 2**254 + sum(2**i * ((h[i // 8] >> (i % 8)) & 1) for i in range(3, 254))
    return _encode_point(_ed_mul(_B, a))


def _ed25519_sign(seed, message):
    h = hashlib.sha512(seed).digest()
    a = 2**254 + sum(2**i * ((h[i // 8] >> (i % 8)) & 1) for i in range(3, 254))
    a_bytes = _encode_point(_ed_mul(_B, a))
    r = int.from_bytes(hashlib.sha512(h[32:] + message).digest(), "little") % _L
    r_bytes = _encode_point(_ed_mul(_B, r))
    s = (r + int.from_bytes(hashlib.sha512(r_bytes + a_bytes + message).digest(), "little") * a) % _L
    return r_bytes + s.to_bytes(32, "little")


# --------------------------------------------------------------------------- JCS
def _jcs_string(value):
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20:
            out.append("\\u%04x" % code)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _jcs(value):
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, int):
        return str(value).encode("ascii")
    if isinstance(value, str):
        return _jcs_string(value).encode("utf-8")
    if isinstance(value, (list, tuple)):
        return b"[" + b",".join(_jcs(item) for item in value) + b"]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        return b"{" + b",".join(_jcs_string(key).encode("utf-8") + b":" + _jcs(value[key]) for key in keys) + b"}"
    raise TypeError(f"JCS cannot encode {type(value).__name__}")


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _digest(value):
    return hashlib.sha256(_jcs(value)).hexdigest()


# --------------------------------------------------------------------------- fixtures

KID = "coordinator-test-2026-11"
AUDIENCE = "hyvv-test-host"
TOKEN = "t" * 40
NOW = int(time.time())  # the live server checks the real clock

BRIEF = {
    "objective": "enforce one dispatch ticket",
    "project": "demo",
    "branch": "feat/wave-c-c5",
    "source_rev": "a" * 40,
    "assigned_files": ["launcher.py", "tests/test_ticket_enforce.py"],
    "acceptance": ["a valid ticket is admitted and an invalid one is refused"],
    "risk": {"tier": "high"},
    "fake": {"delay": 2},
}


def _ticket(seed, *, brief=None, audience=AUDIENCE, kid=KID, iat=NOW, exp=NOW + 600, jti="6d1c0f5a2b7e4c3d9a8f0e1b2c3d4e5f"):
    brief = BRIEF if brief is None else brief
    claims = {
        "brief_sha256": _digest({key: value for key, value in brief.items() if key != "ticket"}),
        "project": brief["project"],
        "source_rev": brief["source_rev"],
        "assigned_files_sha256": _digest(brief["assigned_files"]),
        "risk_tier": brief["risk"]["tier"],
        "contract_sha256": None,
        "assignment": None,
        "aud": audience,
        "iat": iat,
        "exp": exp,
        "jti": jti,
        "kid": kid,
    }
    header_segment = _b64url(_jcs({"alg": "EdDSA", "kid": kid}))
    payload_segment = _b64url(_jcs(claims))
    signature = _ed25519_sign(seed, f"{header_segment}.{payload_segment}".encode("ascii"))
    return f"{header_segment}.{payload_segment}.{_b64url(signature)}"


class _EnforceCase(unittest.TestCase):
    MODE = "enforce"
    KEYS = True

    def setUp(self):
        if L is None:
            self.fail(f"launcher.py does not import: {IMPORT_ERROR!r}")
        self.tmp = tempfile.mkdtemp(prefix="hyvv-c5-")
        (Path(self.tmp) / "go.key").write_text("fake-provider-key")
        (Path(self.tmp) / "deepseek.key").write_text("fake-deepseek-key")
        self.seed = os.urandom(32)
        self.public = _public_key(self.seed)
        self.keys = {KID: _b64url(self.public)} if self.KEYS else {}
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
            "ticket_audience": AUDIENCE,
            "ticket_public_keys": self.keys,
            "ticket_replay_dir": str(Path(self.tmp) / "tickets"),
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

    def ticketed(self, attempt_id, ticket, brief=None):
        brief = dict(BRIEF if brief is None else brief)
        brief["ticket"] = ticket
        return self.post(attempt_id, brief)

    def journal(self, attempt_id):
        return Path(self.tmp) / "attempts" / f"{attempt_id}.json"

    def assertRefused(self, code, attempt_id, ticket, brief=None):
        status, body = self.ticketed(attempt_id, ticket, brief)
        self.assertEqual(status, 403, body)
        self.assertEqual(body.get("error"), code, body)
        self.assertFalse(self.journal(attempt_id).exists(), "a refused create must not journal an attempt")


def _load_launcher():
    spec = importlib.util.spec_from_file_location("launcher_c5", HERE / "launcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    L = _load_launcher()
except Exception as exc:  # pragma: no cover
    L = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


class EnforceAdmitsAndRefuses(_EnforceCase):
    def test_a_valid_ticket_proceeds(self):
        status, body = self.ticketed("att_c5ok00001", _ticket(self.seed))
        self.assertEqual(status, 201, body)
        self.assertIn(body["state"], ("queued", "starting", "running"))
        self.assertEqual(body["ticket_report"]["status"], "valid")
        self.assertEqual(body["ticket_report"]["signature"], "verified")

        record = json.loads(self.journal("att_c5ok00001").read_text())
        self.assertEqual(record["ticket_report"]["status"], "valid")
        self.assertEqual(record["ticket_report"]["signature"], "verified")

    def test_a_missing_ticket_is_refused(self):
        status, body = self.post("att_c5miss0001", BRIEF)
        self.assertEqual(status, 403, body)
        self.assertEqual(body.get("error"), "ticket_missing", body)
        self.assertFalse(self.journal("att_c5miss0001").exists())

    def test_an_invalid_signature_is_refused(self):
        ticket = _ticket(os.urandom(32))
        self.assertRefused("ticket_invalid", "att_c5sig00001", ticket)

    def test_a_wrong_audience_is_refused(self):
        ticket = _ticket(self.seed, audience="another-hyvv")
        self.assertRefused("ticket_invalid", "att_c5aud00001", ticket)

    def test_an_expired_ticket_is_refused(self):
        ticket = _ticket(self.seed, iat=NOW - 700, exp=NOW - 100)
        self.assertRefused("ticket_expired", "att_c5exp00001", ticket)

    def test_a_ticket_for_another_brief_is_refused(self):
        other = dict(BRIEF, objective="some other brief")
        ticket = _ticket(self.seed, brief=other)
        self.assertRefused("ticket_brief_mismatch", "att_c5brf00001", ticket)

    def test_a_reused_jti_is_refused_on_the_second_create(self):
        ticket = _ticket(self.seed)
        status, body = self.ticketed("att_c5rep00001", ticket)
        self.assertEqual(status, 201, body)
        self.assertRefused("ticket_replayed", "att_c5rep00002", ticket)

    def test_a_claims_object_instead_of_a_jws_is_refused(self):
        self.assertRefused("ticket_invalid", "att_c5obj00001", {"project": "demo"})


class EnforceFailsClosedWithoutKeys(_EnforceCase):
    KEYS = False

    def test_every_create_is_refused_when_no_public_key_is_configured(self):
        status, body = self.ticketed("att_c5nok00001", _ticket(self.seed))
        self.assertEqual(status, 403, body)
        self.assertEqual(body.get("error"), "ticket_invalid", body)


class OffAndReportAreUnchanged(_EnforceCase):
    MODE = "off"

    def test_off_accepts_an_invalid_ticket_without_a_report(self):
        status, body = self.ticketed("att_c5off00001", _ticket(os.urandom(32)))
        self.assertEqual(status, 201, body)
        self.assertNotIn("ticket_report", body)
        record = json.loads(self.journal("att_c5off00001").read_text())
        self.assertNotIn("ticket_report", record)


class ReportStillOnlyReports(_EnforceCase):
    MODE = "report"

    def test_report_accepts_an_invalid_ticket_and_says_it_was_not_checked(self):
        status, body = self.ticketed("att_c5rep00003", _ticket(os.urandom(32)))
        self.assertEqual(status, 201, body)
        report = body["ticket_report"]
        self.assertEqual(report["schema"], "rizzstack.ticket-report/1")
        self.assertEqual(report["mode"], "report")
        self.assertEqual(report["status"], "invalid")
        self.assertEqual(report["signature"], "not_checked")


class TicketModeConfig(unittest.TestCase):
    @staticmethod
    def _config(payload):
        directory = Path(tempfile.mkdtemp(prefix="hyvv-c5-cfg-"))
        path = directory / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_default_is_off_enforce_is_accepted_and_the_environment_cannot_set_it(self):
        if L is None:
            self.fail(f"launcher.py does not import: {IMPORT_ERROR!r}")
        self.assertEqual(L.load_config(self._config({})).get("ticket_mode"), "off")
        self.assertEqual(L.load_config(self._config({"ticket_mode": "report"}))["ticket_mode"], "report")
        # C5 supersedes C2 AC-007: enforce is now a legal mode.
        self.assertEqual(L.load_config(self._config({"ticket_mode": "enforce"}))["ticket_mode"], "enforce")
        with self.assertRaises(ValueError):
            L.load_config(self._config({"ticket_mode": "bogus"}))

        names = ("HYYV_TICKET_MODE", "TICKET_MODE", "RIZZSTACK_TICKET_MODE", "BUNDLEBEES_TICKET_MODE")
        saved = {name: os.environ.get(name) for name in names}
        try:
            for name in names:
                os.environ[name] = "enforce"
            self.assertEqual(L.load_config(self._config({})).get("ticket_mode"), "off")
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
