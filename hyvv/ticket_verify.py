from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time


DEFAULT_TICKET_MODE = "report"
TICKET_MODES = ("off", "report", "enforce")


class TicketError(Exception):
    code = "ticket_invalid"


class TicketKeyConfigError(TicketError):
    pass


class TicketExpired(TicketError):
    code = "ticket_expired"


class TicketReplayed(TicketError):
    code = "ticket_replayed"


class TicketBriefMismatch(TicketError):
    code = "ticket_brief_mismatch"


class TicketCryptoUnavailable(TicketError):
    pass


@dataclass(frozen=True)
class TicketPolicy:
    mode: str = DEFAULT_TICKET_MODE


def load_ticket_policy(config) -> TicketPolicy:
    mode = config.get("ticket_mode", DEFAULT_TICKET_MODE)
    if mode not in TICKET_MODES:
        raise ValueError("ticket_mode must be off, report, or enforce")
    return TicketPolicy(mode)


def _jcs_string(value: str) -> bytes:
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
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out).encode("utf-8")


def jcs(value) -> bytes:
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, int):
        return str(value).encode("ascii")
    if isinstance(value, str):
        return _jcs_string(value)
    if isinstance(value, (list, tuple)):
        return b"[" + b",".join(jcs(item) for item in value) + b"]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JCS object keys must be strings")
        keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        return b"{" + b",".join(_jcs_string(key) + b":" + jcs(value[key]) for key in keys) + b"}"
    raise TypeError(f"JCS cannot encode {type(value).__name__}")


def brief_digest(brief) -> str:
    without_ticket = {key: value for key, value in brief.items() if key != "ticket"}
    return hashlib.sha256(jcs(without_ticket)).hexdigest()


_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
_CONFIGURED_KEY = re.compile(r"[A-Za-z0-9_-]+={0,1}")
_REPLAY_FILE = re.compile(r"[0-9a-f]{64}\.json")


def _b64url_decode(segment: str) -> bytes:
    if not isinstance(segment, str) or _BASE64URL.fullmatch(segment) is None:
        raise ValueError("invalid base64url")
    if len(segment) % 4 == 1:
        raise ValueError("invalid base64url length")
    try:
        return base64.b64decode(
            segment + "=" * (-len(segment) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid base64url") from exc


def load_public_keys(config) -> dict[str, bytes]:
    try:
        configured = config.get("ticket_public_keys")
    except AttributeError as exc:
        raise TicketKeyConfigError("ticket_public_keys must be a non-empty mapping") from exc
    if not isinstance(configured, Mapping) or not configured:
        raise TicketKeyConfigError("ticket_public_keys must be a non-empty mapping")
    return {kid: _decode_public_key(kid, value) for kid, value in configured.items()}


def _decode_public_key(kid, value) -> bytes:
    """Validate one configured kid/public-key pair and return the raw 32 key bytes."""
    if not isinstance(kid, str) or not kid:
        raise TicketKeyConfigError("each ticket public key needs a non-empty string kid")
    if not isinstance(value, str) or _CONFIGURED_KEY.fullmatch(value) is None:
        raise TicketKeyConfigError("ticket public keys must be base64url raw Ed25519 public keys")
    unpadded = value[:-1] if value.endswith("=") else value
    if value.endswith("=") and len(value) % 4:
        raise TicketKeyConfigError("ticket public key has invalid base64url padding")
    try:
        raw = _b64url_decode(unpadded)
    except ValueError as exc:
        raise TicketKeyConfigError("ticket public key is not valid base64url") from exc
    if len(raw) != 32:
        raise TicketKeyConfigError("ticket public keys must decode to exactly 32 bytes")
    return raw


class ReplayCache:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.Lock()

    def _path(self, jti) -> Path:
        if not isinstance(jti, str) or not jti:
            raise TicketError("invalid ticket jti")
        try:
            digest = hashlib.sha256(jti.encode("utf-8")).hexdigest()
        except UnicodeError as exc:
            raise TicketError("invalid ticket jti") from exc
        return self.directory / f"{digest}.json"

    @staticmethod
    def _read_exp(path: Path):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or "exp" not in value:
            raise ValueError("invalid replay entry")
        return value["exp"]

    def consume(self, jti, exp, now):
        path = self._path(jti)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        with self._lock:
            while not self._take(path, flags, exp, now):
                continue

    def _take(self, path: Path, flags: int, exp, now) -> bool:
        """Create the replay entry; True when it was written, False to retry the loop."""
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            self._reclaim(path, now)
            return False
        self._write_entry(fd, path, exp)
        return True

    def _reclaim(self, path: Path, now):
        """Drop a dead entry, keep a live one refused; a vanished entry means retry."""
        try:
            previous_exp = self._read_exp(path)
            live = previous_exp > now
        except FileNotFoundError:
            return
        except (OSError, TypeError, ValueError) as exc:
            raise TicketReplayed("ticket jti has already been used") from exc
        if live:
            raise TicketReplayed("ticket jti has already been used")
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _write_entry(self, fd: int, path: Path, exp):
        """Write the exp record through the open fd; unlink on any failure, then re-raise."""
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as entry:
                json.dump({"exp": exp}, entry, separators=(",", ":"))
                entry.flush()
                os.fsync(entry.fileno())
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            raise

    def prune(self, now):
        with self._lock:
            for path in self.directory.glob("*.json"):
                if _REPLAY_FILE.fullmatch(path.name) is None:
                    continue
                try:
                    exp = self._read_exp(path)
                    if exp <= now:
                        path.unlink()
                except FileNotFoundError:
                    continue
                except (OSError, TypeError, ValueError):
                    continue


_CLAIMS = frozenset({
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
})


def _json_object(raw: bytes) -> dict:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("invalid JSON constant")

    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=invalid_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _invalid() -> TicketError:
    return TicketError("invalid dispatch ticket")


def _ticket_parts(ticket) -> list[str]:
    """Split a compact JWS into its three non-empty segments."""
    if not isinstance(ticket, str):
        raise _invalid()
    parts = ticket.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise _invalid()
    return parts


def _decode_segments(parts):
    """Decode the header, claims and signature segments; anything malformed is ticket_invalid."""
    try:
        header = _json_object(_b64url_decode(parts[0]))
        claims = _json_object(_b64url_decode(parts[1]))
        signature = _b64url_decode(parts[2])
    except (TypeError, ValueError, RecursionError) as exc:
        raise _invalid() from exc
    return header, claims, signature


def _checked_header(header, claims, public_keys) -> str:
    """Return the kid after every header/claims-shape check; refuse anything off-spec."""
    kid = header.get("kid")
    if header.get("alg") != "EdDSA" or not isinstance(kid, str):
        raise _invalid()
    if not isinstance(public_keys, Mapping) or kid not in public_keys:
        raise _invalid()
    if set(claims) != _CLAIMS or claims["kid"] != kid:
        raise _invalid()
    if not isinstance(claims["jti"], str) or not claims["jti"]:
        raise _invalid()
    return kid


def _verify_signature(parts, signature, public_key_bytes):
    """Verify Ed25519 with a lazy cryptography import; a bad signature is ticket_invalid."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise TicketCryptoUnavailable(
            "cryptography is not installed; install the tickets extra"
        ) from exc

    try:
        public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
        public_key.verify(signature, f"{parts[0]}.{parts[1]}".encode("ascii"))
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise _invalid() from exc


def _check_claims(claims, brief, audience, now):
    """Check audience, lifetime and brief binding; return the validated exp."""
    if claims["aud"] != audience:
        raise _invalid()
    iat = claims["iat"]
    exp = claims["exp"]
    if type(iat) is not int or type(exp) is not int:
        raise _invalid()
    if exp <= now:
        raise TicketExpired("dispatch ticket has expired")
    if exp - iat > 600 or exp < iat:
        raise _invalid()
    try:
        expected_digest = brief_digest(brief)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _invalid() from exc
    if claims["brief_sha256"] != expected_digest:
        raise TicketBriefMismatch("dispatch ticket is bound to a different brief")
    return exp


def verify_ticket(ticket, *, brief, public_keys, audience, now=None, replay) -> dict:
    now = time.time() if now is None else now
    parts = _ticket_parts(ticket)
    header, claims, signature = _decode_segments(parts)
    kid = _checked_header(header, claims, public_keys)
    _verify_signature(parts, signature, public_keys[kid])
    exp = _check_claims(claims, brief, audience, now)

    replay.consume(claims["jti"], exp, now)
    replay.prune(now)
    return claims
