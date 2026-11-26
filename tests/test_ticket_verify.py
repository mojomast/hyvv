"""RED for wave C slice C4: hyvv verifies the signed dispatch ticket.

hyvv ADR 0002 makes ``POST /v1/attempts`` the enforcement point: the ticket is
a compact Ed25519 JWS over the twelve ``rizzstack.ticket/1`` claims, and hyvv
holds only public keys. C4 is the verifier itself - signature, audience,
expiry, single-use ``jti`` and the JCS digest of the received brief. C5 wires
it into the launcher; C4 adds no refusal path of its own.

The frozen surface these tests pin down:

* ``verify_ticket(ticket, *, brief, public_keys, audience, now=None, replay)``
  returns the decoded twelve claims, or raises a ``TicketError`` whose ``code``
  is one of the ADR 0002 refusals: ``ticket_invalid``, ``ticket_expired``,
  ``ticket_replayed``, ``ticket_brief_mismatch``.
* ``brief_digest(brief)`` is SHA-256 over the RFC 8785 (JCS) form of the brief
  without its ``ticket`` key.
* ``load_public_keys(config)`` reads ``ticket_public_keys`` as a mapping of
  ``kid`` to a base64url-encoded raw 32-byte Ed25519 public key, and fails
  closed with ``TicketKeyConfigError`` when the mapping is missing, empty,
  malformed or carries private key material.
* ``ReplayCache(directory)`` is a bounded on-disk ``jti`` cache: ``consume``
  refuses a second use, ``prune`` drops entries past ``exp``.
* ``cryptography`` is an OPTIONAL extra. The module imports and loads without
  it; verification fails closed with the named ``TicketCryptoUnavailable``
  rather than a bare ``ImportError``.
* ``DEFAULT_TICKET_MODE`` is ``"report"`` and ``load_ticket_policy({})``
  returns ``report``: adding the verifier never turns enforcement on by
  itself, and the launcher's C2 default (``off``) is unchanged here.

RED now: ``hyvv/ticket_verify.py`` does not exist. Ed25519 and JCS helpers are
test-local so the test does not trust the implementation to check itself. Keys
are generated at test time and never written to disk.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
MODULE_PATH = HERE / "hyvv" / "ticket_verify.py"

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


def _decode_point(s):
    y = int.from_bytes(s, "little") & ((1 << 255) - 1)
    x = _xrecover(y)
    if (x & 1) != (s[31] >> 7):
        x = _P - x
    return (x, y)


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
        for key in value:
            if not isinstance(key, str):
                raise TypeError("JCS object keys must be strings")
        keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        return b"{" + b",".join(_jcs_string(key).encode("utf-8") + b":" + _jcs(value[key]) for key in keys) + b"}"
    raise TypeError(f"JCS cannot encode {type(value).__name__}")


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment):
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _digest(value):
    return hashlib.sha256(_jcs(value)).hexdigest()


# --------------------------------------------------------------------------- fixtures

KID = "coordinator-test-2026-11"
AUDIENCE = "hyvv-test-host"
NOW = 1764000000

BRIEF = {
    "objective": "verify one dispatch ticket",
    "project": "demo",
    "branch": "feat/wave-c-c4",
    "source_rev": "a" * 40,
    "assigned_files": ["hyvv/ticket_verify.py", "tests/test_ticket_verify.py"],
    "acceptance": ["the ticket verifies with the test key"],
    "risk": {"tier": "medium"},
    "contract": {"contract_version": "acdd/1", "id": "wave-c-c4"},
}


def _load_verify_module(name="hyvv_ticket_verify_c4"):
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _reexec(module):
    """Re-execute the module in place; importlib.reload needs a sys.path spec."""
    spec = importlib.util.spec_from_file_location(module.__name__, MODULE_PATH)
    module.__spec__ = spec
    spec.loader.exec_module(module)


IMPORT_ERROR = None
try:
    V = _load_verify_module() if MODULE_PATH.is_file() else None
except Exception as exc:  # pragma: no cover - the RED message carries the reason
    V = None
    IMPORT_ERROR = exc


class _ImportBlocker:
    def __init__(self, blocked):
        self.blocked = blocked

    def find_spec(self, name, path=None, target=None):
        if name == self.blocked or name.startswith(self.blocked + "."):
            raise ImportError(f"{name} is blocked for this test")
        return None


class _VerifyCase(unittest.TestCase):
    def setUp(self):
        if V is None:
            self.fail(
                "hyvv/ticket_verify.py is missing or does not import"
                + (f": {IMPORT_ERROR!r}" if IMPORT_ERROR else "")
            )
        self.seed = os.urandom(32)
        self.public = _public_key(self.seed)
        self.replay_dir = tempfile.mkdtemp(prefix="hyvv-c4-replay-")
        self.replay = V.ReplayCache(self.replay_dir)

    # -- ticket construction -------------------------------------------------
    def claims(self, brief=None, **overrides):
        brief = BRIEF if brief is None else brief
        contract = brief.get("contract")
        claims = {
            "brief_sha256": _digest({key: value for key, value in brief.items() if key != "ticket"}),
            "project": brief["project"],
            "source_rev": brief["source_rev"],
            "assigned_files_sha256": _digest(brief["assigned_files"]),
            "risk_tier": brief["risk"]["tier"],
            "contract_sha256": _digest(contract) if isinstance(contract, dict) else None,
            "assignment": None,
            "aud": AUDIENCE,
            "iat": NOW,
            "exp": NOW + 600,
            "jti": "6d1c0f5a2b7e4c3d9a8f0e1b2c3d4e5f",
            "kid": KID,
        }
        claims.update(overrides)
        return claims

    def jws(self, claims=None, *, seed=None, kid=KID, header=None):
        claims = self.claims() if claims is None else claims
        header = {"alg": "EdDSA", "kid": kid} if header is None else header
        header_segment = _b64url(_jcs(header))
        payload_segment = _b64url(_jcs(claims))
        signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
        signature = _ed25519_sign(self.seed if seed is None else seed, signing_input)
        return f"{header_segment}.{payload_segment}.{_b64url(signature)}"

    def verify(self, ticket=None, *, brief=None, audience=AUDIENCE, now=NOW, replay=..., keys=...):
        return V.verify_ticket(
            self.jws() if ticket is None else ticket,
            brief=BRIEF if brief is None else brief,
            public_keys={KID: self.public} if keys is ... else keys,
            audience=audience,
            now=now,
            replay=self.replay if replay is ... else replay,
        )

    def assertRefused(self, code, ticket=None, **kwargs):
        with self.assertRaises(V.TicketError) as caught:
            self.verify(ticket, **kwargs)
        self.assertEqual(caught.exception.code, code)


class ValidTicket(_VerifyCase):
    def test_a_valid_test_key_jws_is_accepted(self):
        claims = self.verify()
        self.assertEqual(claims["kid"], KID)
        self.assertEqual(claims["aud"], AUDIENCE)
        self.assertEqual(claims["brief_sha256"], _digest(BRIEF))

    def test_brief_digest_is_jcs_and_ignores_the_ticket_key(self):
        self.assertEqual(V.brief_digest(BRIEF), _digest(BRIEF))
        self.assertEqual(V.brief_digest(dict(reversed(list(BRIEF.items())))), _digest(BRIEF))
        self.assertEqual(V.brief_digest(dict(BRIEF, ticket="a.b.c")), _digest(BRIEF))
        self.assertNotEqual(V.brief_digest(dict(BRIEF, objective="another brief")), _digest(BRIEF))


class Refusals(_VerifyCase):
    def test_a_tampered_payload_is_refused(self):
        pieces = self.jws().split(".")
        claims = json.loads(_b64url_decode(pieces[1]))
        claims["project"] = "other"
        pieces[1] = _b64url(_jcs(claims))
        self.assertRefused("ticket_invalid", ".".join(pieces))

    def test_a_tampered_signature_is_refused(self):
        pieces = self.jws().split(".")
        signature = bytearray(_b64url_decode(pieces[2]))
        signature[0] ^= 0x01
        pieces[2] = _b64url(bytes(signature))
        self.assertRefused("ticket_invalid", ".".join(pieces))

    def test_a_ticket_signed_by_another_key_is_refused(self):
        other = os.urandom(32)
        self.assertRefused("ticket_invalid", self.jws(seed=other))

    def test_a_wrong_audience_is_refused(self):
        self.assertRefused("ticket_invalid", audience="some-other-hyvv")

    def test_an_unknown_kid_is_refused(self):
        self.assertRefused("ticket_invalid", self.jws(kid="unknown-key"))

    def test_an_expired_ticket_is_refused(self):
        claims = self.claims(iat=NOW - 700, exp=NOW - 100)
        self.assertRefused("ticket_expired", self.jws(claims))

    def test_a_ticket_lifetime_over_ten_minutes_is_refused(self):
        claims = self.claims(exp=NOW + 601)
        self.assertRefused("ticket_invalid", self.jws(claims))

    def test_a_ticket_for_another_brief_is_refused(self):
        other = dict(BRIEF, objective="a different brief")
        self.assertRefused("ticket_brief_mismatch", brief=other)

    def test_a_reused_jti_is_refused(self):
        ticket = self.jws()
        self.verify(ticket)
        with self.assertRaises(V.TicketError) as caught:
            self.verify(ticket)
        self.assertEqual(caught.exception.code, "ticket_replayed")

    def test_a_malformed_ticket_is_refused(self):
        for bad in ("not-a-jws", "a.b", "a.b.c.d", "....", "!!!.???.###"):
            self.assertRefused("ticket_invalid", bad)

    def test_a_payload_missing_a_claim_is_refused(self):
        claims = self.claims()
        del claims["kid"]
        self.assertRefused("ticket_invalid", self.jws(claims))

    def test_an_alg_other_than_eddsa_is_refused(self):
        ticket = self.jws(header={"alg": "HS256", "kid": KID})
        self.assertRefused("ticket_invalid", ticket)


class KeyConfig(_VerifyCase):
    def test_a_valid_public_key_mapping_loads(self):
        keys = V.load_public_keys({"ticket_public_keys": {KID: _b64url(self.public)}})
        self.assertEqual(keys[KID], self.public)

    def test_missing_empty_or_malformed_keys_fail_closed(self):
        for config in (
            {},
            {"ticket_public_keys": {}},
            {"ticket_public_keys": {KID: ""}},
            {"ticket_public_keys": {KID: _b64url(b"short")}},
            {"ticket_public_keys": {KID: "not base64 !!!"}},
        ):
            with self.assertRaises(V.TicketKeyConfigError):
                V.load_public_keys(config)

    def test_private_key_material_in_config_is_refused(self):
        # Assembled at runtime: the point is that a PEM blob is refused, not to
        # commit a key-shaped literal to the repository.
        pem = (
            "-----BEGIN " + "PRIVATE" + " KEY-----\nMIIB\n-----END "
            + "PRIVATE" + " KEY-----"
        )
        with self.assertRaises(V.TicketKeyConfigError):
            V.load_public_keys({"ticket_public_keys": {KID: pem}})


class ReplayCacheUnit(_VerifyCase):
    def test_consume_refuses_a_second_use_and_prune_keeps_it_bounded(self):
        jti = "a" * 32
        self.replay.consume(jti, NOW + 60, NOW)
        with self.assertRaises(V.TicketError) as caught:
            self.replay.consume(jti, NOW + 60, NOW)
        self.assertEqual(caught.exception.code, "ticket_replayed")

        self.replay.prune(NOW + 61)
        files = [path for path in Path(self.replay_dir).rglob("*") if path.is_file()]
        self.assertEqual(files, [], "an expired jti must not stay in the cache")
        self.replay.consume(jti, NOW + 120, NOW + 62)


class Policy(_VerifyCase):
    def test_ticket_mode_stays_report_by_default(self):
        self.assertEqual(V.DEFAULT_TICKET_MODE, "report")
        self.assertEqual(V.load_ticket_policy({}).mode, "report")
        self.assertEqual(V.load_ticket_policy({"ticket_mode": "report"}).mode, "report")
        self.assertEqual(V.load_ticket_policy({"ticket_mode": "enforce"}).mode, "enforce")
        self.assertEqual(V.load_ticket_policy({"ticket_mode": "off"}).mode, "off")
        with self.assertRaises(ValueError):
            V.load_ticket_policy({"ticket_mode": "bogus"})

    def test_the_c2_launcher_default_is_unchanged(self):
        # C4 adds the verifier, not a runtime mode: C2's load_config default
        # stays off and only C5 wires enforcement.
        spec = importlib.util.spec_from_file_location("launcher_c4", HERE / "launcher.py")
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        directory = Path(tempfile.mkdtemp(prefix="hyvv-c4-cfg-"))
        path = directory / "config.json"
        path.write_text(json.dumps({}), encoding="utf-8")
        self.assertEqual(launcher.load_config(path).get("ticket_mode"), "off")


class OptionalCryptography(_VerifyCase):
    def test_cryptography_stays_an_optional_extra(self):
        import tomllib

        project = tomllib.loads((HERE / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        core = " ".join(project.get("dependencies") or []).lower()
        self.assertNotIn("cryptography", core, "cryptography must not be a core dependency")
        extra = " ".join((project.get("optional-dependencies") or {}).get("tickets") or []).lower()
        self.assertIn("cryptography", extra, "the tickets extra must carry cryptography")

    def test_missing_cryptography_fails_closed_with_a_named_error(self):
        self.assertTrue(hasattr(V, "TicketCryptoUnavailable"))
        self.assertTrue(issubclass(V.TicketCryptoUnavailable, Exception))

        saved = {
            name: module
            for name, module in list(sys.modules.items())
            if name == "cryptography" or name.startswith("cryptography.")
        }
        for name in saved:
            del sys.modules[name]
        blocker = _ImportBlocker("cryptography")
        sys.meta_path.insert(0, blocker)
        try:
            try:
                _reexec(V)
            except ImportError as exc:
                self.fail(
                    "ticket_verify.py must import cryptography lazily so the module still "
                    f"loads without the extra: {exc}"
                )
            with self.assertRaises(V.TicketCryptoUnavailable) as caught:
                self.verify()
            self.assertIn("cryptography", str(caught.exception).lower())
        finally:
            sys.meta_path.remove(blocker)
            sys.modules.update(saved)
            _reexec(V)


if __name__ == "__main__":
    unittest.main()
