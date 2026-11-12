"""Conformance tests for GET /v1/attempts/{attempt_id}/artifact.

Reuses OA1 acceptance criteria AC-001 through AC-009 and the error codes
verbatim. These tests run against contract/v1/reference.py because
artifact_fetch is planned and is not in launcher v0.1.15.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from contract.v1.privatefs import WELL_KNOWN_BROAD_SIDS, acl_sids, current_user_sid  # noqa: E402
from contract.v1.reference import (  # noqa: E402
    DEFAULT_MAX_BYTES,
    ERROR_HTTP,
    ArtifactStore,
    usage_unknown_template,
)

CODES = json.loads((ROOT / "contract/v1/error_codes.json").read_text(encoding="utf-8"))
OWNER = "coordinator-a"
OTHER = "coordinator-b"
# Per-coordinator keys (test values, not real). The host keeps only their hashes.
OWNER_KEY = "test-key-coordinator-a-not-real-0001"
OTHER_KEY = "test-key-coordinator-b-not-real-0002"
PRINCIPALS = {OWNER: OWNER_KEY, OTHER: OTHER_KEY}
# Capabilities as the creating coordinator would hold them (test-side only; the store keeps hashes).
ISSUED: dict[str, str] = {}
_DEFAULT = object()
REV = "47f2fc93565a53a767c3ff2d498ebf604fd0b4f9"
PATCH = "diff --git a/foo.py b/foo.py\n+hello\n"


def _manifest(attempt_id="att_deadbeefcafebabe", source_rev=REV, patch=PATCH, files=None, usage=None, extra_observed=None):
    files = files or [{"path": "foo.py", "sha256": "a" * 64, "change": "changed", "size": 5, "in_assigned_scope": True}]
    observed = {
        "local_base_rev": "abc1234",
        "tool_calls": 3,
        "stop_reason": "end_turn",
        "files": files,
        "usage": {"prompt_tokens": 10},  # legacy blob, not bee.usage/v1
    }
    if extra_observed:
        observed.update(extra_observed)
    return {
        "schema": "orbit.local-worker.result/v1",
        "attempt_id": attempt_id,
        "source": {"project": "demo", "repo_rev": source_rev, "harness": "opencode", "launcher": "0.1.15"},
        "observed": observed,
        "patch": {"diff": patch, "files": files},
        "worker_claims": {"summary": "did the work"},
        "note": "worker_claims are self-reported",
    }


class _CoordinatorView(ArtifactStore):
    """Records capabilities the way their creator would receive them, for the tests."""

    def add_attempt(self, attempt_id, **kwargs):
        cap = super().add_attempt(attempt_id, **kwargs)
        if cap:
            ISSUED[attempt_id] = cap
        return cap


def _store(tmp, **kwargs):
    kwargs.setdefault("principals", PRINCIPALS)
    return _CoordinatorView(root=tmp, **kwargs)


def _get(store, attempt_id, bearer=OWNER_KEY, capability=_DEFAULT):
    if capability is _DEFAULT:
        capability = ISSUED.get(attempt_id)
    return store.get_artifact(attempt_id, bearer=bearer, capability=capability)


def assert_owner_only(tc: unittest.TestCase, path: Path) -> None:
    """POSIX: mode 0600. Windows: the ACL names only the current user (no groups, no other users)."""
    if os.name == "nt":
        sids = acl_sids(path)
        tc.assertTrue(sids, "file has no ACL entries")
        tc.assertEqual(set(sids), {current_user_sid()}, f"non-owner principals on a private file: {sorted(set(sids) - {current_user_sid()})}")
        for sid in WELL_KNOWN_BROAD_SIDS:
            tc.assertNotIn(sid, sids)
    else:
        tc.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


def _json(body: bytes):
    return json.loads(body.decode("utf-8"))


class AC001SucceededArtifact(unittest.TestCase):
    def test_AC_001_succeeded_attempt_patch_sha256_coordinator_source_rev_and_observed_metadata_are_returned(self):
        """AC-001 succeeded attempt: patch, its sha256, coordinator source_rev and observed metadata are returned."""
        with tempfile.TemporaryDirectory() as tmp:
            s = _store(tmp)
            mid = "att_deadbeefcafebabe"
            usage = usage_unknown_template(
                key=f"attempt:{mid}",
                attempt_id=mid,
                usage_status="known",
                input_tokens=10,
                cache_read_tokens=0,
                cache_write_tokens=0,
                output_tokens=4,
                reasoning_tokens=0,
                provider="opencode",
                route="go",
                go_allowance={"raw": None, "pct_used": None, "pct_source": None, "allowance_usd": None},
            )
            man = _manifest(mid)
            man["usage"] = usage
            s.add_attempt(mid, coordinator=OWNER, state="succeeded", source_rev=REV, result=man)
            status, body = _get(s, mid)
            self.assertEqual(status, 200)
            art = _json(body)
            self.assertEqual(art["schema"], "hyvv.worker-artifact/1")
            self.assertEqual(art["attempt_id"], mid)
            self.assertEqual(art["state"], "succeeded")
            self.assertEqual(art["source_rev"], REV)
            self.assertEqual(art["patch"], PATCH)
            self.assertEqual(art["patch_sha256"], hashlib.sha256(PATCH.encode()).hexdigest())
            self.assertEqual(len(art["result_sha256"]), 64)
            self.assertEqual(art["observed"]["base_rev"], "abc1234")
            self.assertEqual(art["observed"]["tool_calls"], 3)
            self.assertEqual(art["observed"]["stop_reason"], "end_turn")
            self.assertEqual(art["observed"]["files"][0]["path"], "foo.py")
            self.assertFalse(art["observed"]["scope_flags"]["scope_violation"])
            self.assertEqual(art["usage"]["schema_version"], "bee.usage/v1")
            self.assertEqual(art["worker_claims_untrusted"]["summary"], "did the work")
            dumped = json.dumps(art)
            self.assertNotRegex(dumped, r"100\.\d+\.\d+\.\d+")
            self.assertNotIn("/results/", dumped)
            self.assertNotIn("result_file", dumped)
            self.assertNotIn("workdir", dumped)


class AC002Auth(unittest.TestCase):
    def test_AC_002_only_the_coordinator_key_on_loopback_and_only_the_owning_coordinator_may_read_an_artifact(self):
        """AC-002 only the coordinator key on loopback, and only the owning coordinator, may read an artifact."""
        with tempfile.TemporaryDirectory() as tmp:
            s = _store(tmp)
            mid = "att_auth0000000001"
            s.add_attempt(mid, coordinator=OWNER, state="succeeded", source_rev=REV, result=_manifest(mid))
            st, body = _get(s, mid, bearer="wrong-token")
            self.assertEqual(st, 401)
            self.assertEqual(_json(body)["code"], "unauthorized")
            self.assertEqual(_json(body)["error"], "unauthorized")
            st, body = _get(s, mid, bearer=None)
            self.assertEqual(st, 401)
            self.assertEqual(_json(body)["code"], "unauthorized")
            # Another authenticated coordinator, even holding the right capability, is not the owner.
            st, body = _get(s, mid, bearer=OTHER_KEY)
            self.assertEqual(st, 403)
            self.assertEqual(_json(body)["code"], "not_owner")
            self.assertEqual(_json(body)["error"], "not_owner")
            st, body = _get(s, mid)
            self.assertEqual(st, 200)


class AC010Capability(unittest.TestCase):
    """AC-010 artifact reads need the attempt's own scoped capability, issued once to its creator."""

    def _two(self, tmp, **kw):
        s = _store(tmp, **kw)
        a, b = "att_capa000000000a", "att_capb000000000b"
        s.add_attempt(a, coordinator=OWNER, state="succeeded", source_rev=REV, result=_manifest(a))
        s.add_attempt(b, coordinator=OWNER, state="succeeded", source_rev=REV, result=_manifest(b))
        return s, a, b

    def test_AC_010a_missing_capability_is_401(self):
        with tempfile.TemporaryDirectory() as tmp:
            s, a, _ = self._two(tmp)
            st, body = _get(s, a, capability=None)
            self.assertEqual((st, _json(body)["code"]), (401, "capability_required"))

    def test_AC_010b_wrong_capability_is_403(self):
        with tempfile.TemporaryDirectory() as tmp:
            s, a, _ = self._two(tmp)
            st, body = _get(s, a, capability="hyvvcap1_" + "x" * 43)
            self.assertEqual((st, _json(body)["code"]), (403, "capability_invalid"))

    def test_AC_010c_another_attempts_capability_is_403(self):
        with tempfile.TemporaryDirectory() as tmp:
            s, a, b = self._two(tmp)
            st, body = _get(s, a, capability=ISSUED[b])
            self.assertEqual((st, _json(body)["code"]), (403, "capability_invalid"))
            st, _ = _get(s, b)
            self.assertEqual(st, 200)

    def test_AC_010d_revoked_capability_cannot_be_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            s, a, _ = self._two(tmp)
            cap = ISSUED[a]
            self.assertEqual(_get(s, a, capability=cap)[0], 200)
            st, _ = s.revoke_capability(a, bearer=OTHER_KEY)
            self.assertEqual(st, 403, "only the owner may revoke")
            st, _ = s.revoke_capability(a, bearer=OWNER_KEY)
            self.assertEqual(st, 200)
            st, body = _get(s, a, capability=cap)
            self.assertEqual((st, _json(body)["code"]), (403, "capability_revoked"))
            # Revocation survives a restart.
            s2 = _store(tmp)
            s2.add_attempt(a, coordinator=OWNER, state="succeeded", source_rev=REV)
            st, body = _get(s2, a, capability=cap)
            self.assertEqual((st, _json(body)["code"]), (403, "capability_revoked"))

    def test_AC_010e_expired_capability_cannot_be_replayed(self):
        now = [1000.0]
        with tempfile.TemporaryDirectory() as tmp:
            s, a, _ = self._two(tmp, capability_ttl_s=60, clock=lambda: now[0])
            self.assertEqual(_get(s, a)[0], 200)
            now[0] += 61
            st, body = _get(s, a)
            self.assertEqual((st, _json(body)["code"]), (403, "capability_expired"))

    def test_AC_010f_capability_returned_once_to_the_creator_and_only_hashed_at_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = ArtifactStore(root=tmp, principals=PRINCIPALS)
            mid = "att_create00000001"
            st, body = s.create_attempt(mid, bearer="not-a-key", state="queued", source_rev=REV)
            self.assertEqual(st, 401)
            st, body = s.create_attempt(mid, bearer=OWNER_KEY, state="queued", source_rev=REV)
            self.assertEqual(st, 201)
            cap = body["artifact_capability"]
            self.assertTrue(cap.startswith("hyvvcap1_"))
            self.assertEqual(s.attempts[mid]["coordinator"], OWNER, "owner comes from the key, not a request field")
            st, again = s.create_attempt(mid, bearer=OWNER_KEY, state="queued", source_rev=REV)
            self.assertEqual(st, 200)
            self.assertNotIn("artifact_capability", again)
            st, _ = s.create_attempt(mid, bearer=OTHER_KEY, state="queued", source_rev=REV)
            self.assertEqual(st, 403)
            for blob in (json.dumps(s.list_attempts()), json.dumps(s.get_attempt(mid)), "\n".join(s.log_lines())):
                self.assertNotIn(cap, blob)
            at_rest = "".join(p.read_text(encoding="utf-8", errors="replace") for p in Path(tmp).rglob("*") if p.is_file())
            self.assertNotIn(cap, at_rest)
            self.assertNotIn(OWNER_KEY, at_rest)
            self.assertIn(hashlib.sha256(cap.encode()).hexdigest(), s.cap_path.read_text(encoding="utf-8"))
            assert_owner_only(self, s.cap_path)

    def test_AC_010g_no_caller_supplied_coordinator(self):
        import inspect
        params = inspect.signature(ArtifactStore.get_artifact).parameters
        self.assertNotIn("coordinator", params)
        self.assertIn("capability", params)


    def test_AC_010h_shared_token_plus_claimed_name_cannot_read(self):
        """Audit repro: a holder of some valid key who claims another coordinator's name gets nothing."""
        import inspect
        self.assertNotIn("token", inspect.signature(ArtifactStore.__init__).parameters, "no shared host token for artifacts")
        with tempfile.TemporaryDirectory() as tmp:
            s, a, _ = self._two(tmp)
            with self.assertRaises(TypeError):
                s.get_artifact(a, bearer=OTHER_KEY, capability=ISSUED[a], coordinator=OWNER)
            # Another principal, even holding the owner's capability, is refused.
            st, body = _get(s, a, bearer=OTHER_KEY, capability=ISSUED[a])
            self.assertEqual((st, _json(body)["code"]), (403, "not_owner"))
            self.assertNotIn(b"+hello", body)
            # Unknown keys are 401.
            st, body = _get(s, a, bearer="shared-host-token-not-a-coordinator-key")
            self.assertEqual(st, 401)
            self.assertNotIn(b"+hello", body)

    def test_AC_010i_pins_file_holds_digests_not_raw_patches(self):
        """Audit repro: the pin file must not persist the raw patch; restart replay is still byte-identical."""
        with tempfile.TemporaryDirectory() as tmp:
            s, a, _ = self._two(tmp)
            st, first = _get(s, a)
            self.assertEqual(st, 200)
            self.assertIn(b"+hello", first)
            text = s.pin_path.read_text(encoding="utf-8")
            self.assertNotIn("+hello", text)
            self.assertNotIn("diff --git", text)
            data = json.loads(text)
            self.assertEqual(set(data), {"pins"})
            self.assertEqual(data["pins"][a]["body_sha256"], hashlib.sha256(first).hexdigest())
            assert_owner_only(self, s.pin_path)
            s2 = _store(tmp)
            s2.add_attempt(a, coordinator=OWNER, state="succeeded", source_rev=REV)
            st, again = _get(s2, a)
            self.assertEqual((st, again), (200, first))
            # A legacy pin file that still carries bodies is scrubbed on the next save.
            legacy = {"pins": {a: hashlib.sha256(b"x").hexdigest()}, "bodies": {a: "diff --git +hello"}}
            s.pin_path.write_text(json.dumps(legacy), encoding="utf-8")
            s3 = _store(tmp)
            self.assertNotIn("+hello", s.pin_path.read_text(encoding="utf-8"), "legacy bodies scrubbed on load")
            s3.add_attempt(a, coordinator=OWNER, state="succeeded", source_rev=REV)
            st, body = _get(s3, a)
            self.assertEqual((st, _json(body)["code"]), (409, "artifact_changed"))


class AC003Ids(unittest.TestCase):
    def test_AC_003_attempt_ids_are_validated_traversal_unknown_and_malformed_ids_are_refused(self):
        """AC-003 attempt ids are validated: traversal, unknown and malformed ids are refused."""
        with tempfile.TemporaryDirectory() as tmp:
            s = _store(tmp)
            for bad in ("../etc/passwd", "att_/../../secret", "short", "bad id", "att_" + "a" * 80, ""):
                st, body = _get(s, bad)
                self.assertEqual(st, 400, bad)
                self.assertEqual(_json(body)["code"], "invalid_attempt_id")
                self.assertEqual(_json(body)["error"], "invalid_attempt_id")
            st, body = _get(s, "att_missing00000001")
            self.assertEqual(st, 404)
            self.assertEqual(_json(body)["code"], "unknown_attempt")
            # Nothing outside the results dir should have been read or created.
            self.assertEqual(list(s.results.glob("*")), [])


class AC004States(unittest.TestCase):
    def test_AC_004_pending_disconnected_and_patchless_states_are_explicit(self):
        """AC-004 pending, disconnected and patchless states are explicit."""
        with tempfile.TemporaryDirectory() as tmp:
            s = _store(tmp)
            s.add_attempt("att_pending00000001", coordinator=OWNER, state="running", source_rev=REV)
            st, body = _get(s, "att_pending00000001")
            self.assertEqual(st, 409)
            self.assertEqual(_json(body)["code"], "result_pending")

            s.disconnected = True
            st, body = _get(s, "att_pending00000001")
            self.assertEqual(st, 503)
            self.assertEqual(_json(body)["code"], "worker_disconnected")
            self.assertTrue(_json(body)["retryable"])
            s.disconnected = False

            s.add_attempt("att_cancelled000001", coordinator=OWNER, state="cancelled", source_rev=REV)
            st, body = _get(s, "att_cancelled000001")
            self.assertEqual(st, 409)
            self.assertEqual(_json(body)["code"], "no_result_for_state")

            mid = "att_nullpatch000001"
            man = _manifest(mid, patch=None)
            man["patch"] = None
            s.add_attempt(mid, coordinator=OWNER, state="failed", source_rev=REV, result=man)
            st, body = _get(s, mid)
            self.assertEqual(st, 200)
            art = _json(body)
            self.assertIsNone(art["patch"])
            self.assertIsNone(art["patch_sha256"])


class AC005Missing(unittest.TestCase):
    def test_AC_005_missing_results_are_refetched_once_from_the_launcher_still_missing_and_unreachable_are_explicit(self):
        """AC-005 missing results are refetched once from the launcher (restart recovery); still-missing and unreachable are explicit.

        On the hyvv host itself a missing result file is 404 result_missing.
        An adapter (Orbit) may refetch once; if the host is down that is
        503 worker_disconnected.
        """
        with tempfile.TemporaryDirectory() as tmp:
            s = _store(tmp)
            mid = "att_missingresult01"
            s.add_attempt(mid, coordinator=OWNER, state="succeeded", source_rev=REV, result=_manifest(mid))
            (s.results / f"{mid}.result.json").unlink()
            st, body = _get(s, mid)
            self.assertEqual(st, 404)
            self.assertEqual(_json(body)["code"], "result_missing")
            s.disconnected = True
            st, body = _get(s, mid)
            self.assertEqual(st, 503)
            self.assertEqual(_json(body)["code"], "worker_disconnected")
            self.assertTrue(_json(body).get("retryable"))


class AC006Unsafe(unittest.TestCase):
    def test_AC_006_malformed_oversized_and_symlinked_result_files_are_refused(self):
        """AC-006 malformed, oversized and symlinked result files are refused."""
        with tempfile.TemporaryDirectory() as tmp:
            s = _store(tmp)
            mid = "att_malformed000001"
            s.add_attempt(mid, coordinator=OWNER, state="succeeded", source_rev=REV, result_bytes=b"not-json{")
            st, body = _get(s, mid)
            self.assertEqual(st, 502)
            self.assertEqual(_json(body)["code"], "artifact_malformed")

            mid = "att_symlink00000001"
            try:
                s.add_attempt(mid, coordinator=OWNER, state="succeeded", source_rev=REV, symlink=True)
            except OSError as exc:  # Windows without SeCreateSymbolicLinkPrivilege (CI runners have it)
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("symlink sub-case needs the create-symlink privilege on this Windows host")
                raise
            st, body = _get(s, mid)
            self.assertEqual(st, 409)
            self.assertEqual(_json(body)["code"], "artifact_unsafe")
            self.assertNotIn("secret", body.decode())

    def test_AC_006b_oversized_artifacts_are_refused_before_parsing(self):
        """AC-006b oversized artifacts are refused before parsing."""
        with tempfile.TemporaryDirectory() as tmp:
            s = _store(tmp, max_bytes=64)
            mid = "att_oversized000001"
            s.add_attempt(mid, coordinator=OWNER, state="succeeded", source_rev=REV, result_bytes=b"x" * 128)
            st, body = _get(s, mid)
            self.assertEqual(st, 413)
            self.assertEqual(_json(body)["code"], "artifact_oversized")
            self.assertEqual(ERROR_HTTP["artifact_oversized"], 413)
            self.assertEqual(CODES["default_artifact_max_bytes"], DEFAULT_MAX_BYTES)
            self.assertEqual(DEFAULT_MAX_BYTES, 8 * 1024 * 1024)


class AC007Binding(unittest.TestCase):
    def test_AC_007_artifacts_are_bound_to_their_attempt_and_source_rev_and_substitution_after_first_read_is_refused(self):
        """AC-007 artifacts are bound to their attempt and source_rev, and substitution after first read is refused."""
        with tempfile.TemporaryDirectory() as tmp:
            s = _store(tmp)
            mid = "att_bound0000000001"
            s.add_attempt(mid, coordinator=OWNER, state="succeeded", source_rev=REV, result=_manifest(mid))
            st, first = _get(s, mid)
            self.assertEqual(st, 200)
            st, second = _get(s, mid)
            self.assertEqual(st, 200)
            self.assertEqual(first, second)  # byte-identical

            other = _manifest("att_someoneelse0001", source_rev=REV)
            path = s.results / f"{mid}.result.json"
            path.write_bytes(json.dumps(other).encode())
            st, body = _get(s, mid)
            # The bytes changed, so either artifact_changed (pin) or artifact_mismatch (id).
            self.assertEqual(st, 409)
            self.assertIn(_json(body)["code"], ("artifact_changed", "artifact_mismatch"))

            mid2 = "att_revmismatch0001"
            s.add_attempt(
                mid2, coordinator=OWNER, state="succeeded", source_rev=REV,
                result=_manifest(mid2, source_rev="0" * 40),
            )
            st, body = _get(s, mid2)
            self.assertEqual(st, 409)
            self.assertEqual(_json(body)["code"], "artifact_mismatch")


class AC008Restart(unittest.TestCase):
    def test_AC_008_the_first_read_hash_survives_an_Orbit_restart_idempotent_substitution_still_detected(self):
        """AC-008 the first-read hash survives an Orbit restart (idempotent, substitution still detected)."""
        with tempfile.TemporaryDirectory() as tmp:
            s = _store(tmp)
            mid = "att_restart00000001"
            man = _manifest(mid)
            s.add_attempt(mid, coordinator=OWNER, state="succeeded", source_rev=REV, result=man)
            st, first = _get(s, mid)
            self.assertEqual(st, 200)
            pinned = _json(first)["result_sha256"]

            s2 = _store(tmp)  # new process, same data dir
            s2.add_attempt(mid, coordinator=OWNER, state="succeeded", source_rev=REV)  # metadata only; file still there
            st, again = _get(s2, mid)
            self.assertEqual(st, 200)
            self.assertEqual(again, first)
            self.assertEqual(_json(again)["result_sha256"], pinned)

            # Substitute the result file after the restart; the pin must still refuse it.
            changed = _manifest(mid, patch=PATCH + "+extra\n")
            (s2.results / f"{mid}.result.json").write_text(json.dumps(changed), encoding="utf-8")
            st, body = _get(s2, mid)
            self.assertEqual(st, 409)
            self.assertEqual(_json(body)["code"], "artifact_changed")


class AC009NoLeak(unittest.TestCase):
    def test_AC_009_raw_patches_stay_out_of_list_get_responses_and_logs_result_files_are_private(self):
        """AC-009 raw patches stay out of list/get responses and logs; result files are private."""
        with tempfile.TemporaryDirectory() as tmp:
            s = _store(tmp)
            mid = "att_noleak000000001"
            s.add_attempt(mid, coordinator=OWNER, state="succeeded", source_rev=REV, result=_manifest(mid))
            st, body = _get(s, mid)
            self.assertEqual(st, 200)
            listing = json.dumps(s.list_attempts())
            got = json.dumps(s.get_attempt(mid))
            logs = "\n".join(s.log_lines())
            for blob in (listing, got, logs):
                self.assertNotIn(PATCH, blob)
                self.assertNotIn("+hello", blob)
                self.assertNotIn(str(s.results), blob)
                self.assertNotIn("/results/", blob)
            self.assertIn(mid, logs)
            self.assertIn("patch_sha256=", logs)
            path = s.results / f"{mid}.result.json"
            assert_owner_only(self, path)
            assert_owner_only(self, s.pin_path)


class ErrorCodeTable(unittest.TestCase):
    def test_error_codes_match_the_published_table(self):
        for http, codes in CODES["http"].items():
            for code in codes:
                self.assertEqual(ERROR_HTTP[code], int(http), code)
        self.assertEqual(CODES["retryable"], ["worker_disconnected"])
        published = {c for codes in CODES["http"].values() for c in codes}
        self.assertEqual(published, set(ERROR_HTTP), "reference and published error tables must match both ways")


if __name__ == "__main__":
    unittest.main()
