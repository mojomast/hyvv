"""GET /v1/health carries the deployment receipt written by the admin apply-update.

The bundlebees doctor only treats the launcher's build identity as known when the health payload
has a ``deployment_receipt`` with a ``deployed_at`` marker and a build identity; a version string
alone is never deployment evidence. The launcher republishes only the validated receipt fields
(``deployed_at``, ``version``, ``rev``) -- never the file's other content or its path -- and an
unreadable or malformed receipt is reported as invalid, never repaired or guessed.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_health_models import L, get, serve

REV = "1fcf1039e67ec5fd07e2111cba1808f793c7b5bd"
GOOD = {"deployed_at": "2026-11-18T03:13:00Z", "version": L.VERSION, "rev": REV}


class _ReceiptCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lch-receipt-"))
        self.cfg = {"data_dir": str(self.tmp / "data"), "_config_path": str(self.tmp / "conf" / "config.json")}
        self.install = self.tmp / "pc-launcher"
        self.install.mkdir()
        p = mock.patch.object(L, "LAUNCHER_DIR", self.install)
        p.start(); self.addCleanup(p.stop)

    def write(self, obj, name="hyvv-deployed.json", where=None):
        p = (where or self.tmp / "conf") / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(obj if isinstance(obj, str) else json.dumps(obj), "utf-8")
        return p


class ReceiptReader(_ReceiptCase):
    def test_no_receipt_file_means_no_receipt_key(self):
        self.assertIsNone(L.deployment_receipt(self.cfg))

    def test_valid_receipt_beside_the_config_is_published_with_only_the_validated_fields(self):
        self.write({**GOOD, "token": "x" * 40, "path": "C:/Users/someone/pc-launcher", "note": "extra"}, where=self.tmp / "conf")
        self.assertEqual(L.deployment_receipt(self.cfg), GOOD)

    def test_data_dir_location_and_explicit_path_and_aliases(self):
        self.write({"applied_at": "2026-11-18T03:13:00+00:00", "version": L.VERSION, "sha": REV}, where=self.tmp / "data")
        self.assertEqual(L.deployment_receipt(self.cfg),
                         {"deployed_at": "2026-11-18T03:13:00Z", "version": L.VERSION, "rev": REV})
        other = self.write(GOOD, name="elsewhere.json")
        self.assertEqual(L.deployment_receipt({**self.cfg, "deployment_receipt_path": str(other)}), GOOD)

    def test_explicit_path_that_is_missing_is_invalid_not_a_fallback(self):
        self.write(GOOD, where=self.tmp / "conf")  # a default-location receipt must not stand in
        self.write(GOOD, where=self.install)  # ... nor the install-dir one
        r = L.deployment_receipt({**self.cfg, "deployment_receipt_path": str(self.tmp / "missing.json")})
        self.assertEqual(r, {"status": "invalid", "reason": "receipt_unreadable"})

    def test_malformed_receipts_are_reported_invalid(self):
        cases = {
            "not json": "receipt_unreadable",
            json.dumps([GOOD]): "receipt_malformed",
            json.dumps({"version": L.VERSION, "rev": REV}): "receipt_malformed",                 # no deployed_at
            json.dumps({"deployed_at": "yesterday", "version": L.VERSION}): "receipt_malformed",  # not a timestamp
            json.dumps({"deployed_at": "2026-11-18T03:13:00", "version": L.VERSION}): "receipt_malformed",  # no zone
            json.dumps({"deployed_at": GOOD["deployed_at"]}): "receipt_malformed",                # no identity
            json.dumps({"deployed_at": GOOD["deployed_at"], "version": "0.1.17-dirty"}): "receipt_malformed",
            json.dumps({"deployed_at": GOOD["deployed_at"], "rev": "1fcf1039"}): "receipt_malformed",  # short rev
        }
        for text, reason in cases.items():
            self.write(text)
            self.assertEqual(L.deployment_receipt(self.cfg), {"status": "invalid", "reason": reason}, text)

    def test_a_receipt_for_another_version_is_published_as_recorded(self):
        # The receipt is evidence, not a claim the launcher makes about itself: a mismatch is shown
        # as recorded so the consumer reports the disagreement.
        self.write({**GOOD, "version": "0.1.16"})
        self.assertEqual(L.deployment_receipt(self.cfg)["version"], "0.1.16")

    def test_a_describe_derived_build_identity_is_published(self):
        describe = f"v{L.VERSION}-25-g7f73b7c"
        self.write({"deployed_at": GOOD["deployed_at"], "version": f"{L.VERSION}+25.g7f73b7c",
                    "sha": REV, "describe": describe})
        self.assertEqual(L.deployment_receipt(self.cfg),
                         {"deployed_at": GOOD["deployed_at"], "version": f"{L.VERSION}+25.g7f73b7c",
                          "rev": REV, "describe": describe})

    def test_describe_alone_is_a_complete_identity(self):
        describe = f"v{L.VERSION}-25-g7f73b7c"
        self.write({"deployed_at": GOOD["deployed_at"], "describe": describe})
        self.assertEqual(L.deployment_receipt(self.cfg),
                         {"deployed_at": GOOD["deployed_at"], "describe": describe})

    def test_a_bad_describe_is_malformed(self):
        for bad in ("not a build", f"v{L.VERSION}-25", "0.1.19-25-gzzzzzzz"):
            self.write({"deployed_at": GOOD["deployed_at"], "describe": bad})
            self.assertEqual(L.deployment_receipt(self.cfg),
                             {"status": "invalid", "reason": "receipt_malformed"}, bad)

    def test_receipt_file_is_bounded(self):
        self.write({**GOOD, "pad": "x" * 70_000})
        self.assertEqual(L.deployment_receipt(self.cfg), {"status": "invalid", "reason": "receipt_unreadable"})


# Exactly the key set the PC apply-update writes (pc-launcher/hyvv-deployed.json, Nov 18 2026).
PC_RECEIPT = {
    "repo": "mojomast/hyvv",
    "sha": REV,
    "version": "0.1.17",
    "installed_at_utc": "2026-11-18T03:11:06Z",
    "files": {"launcher.py": "0" * 64, "README.md": "1" * 64},
}
PC_PUBLISHED = {"deployed_at": "2026-11-18T03:11:06Z", "version": "0.1.17", "rev": REV}


class InstallDirReceipt(_ReceiptCase):
    """The real PC layout: the receipt sits beside launcher.py, not beside config.json."""

    def test_install_dir_receipt_with_the_real_pc_keys(self):
        self.write(PC_RECEIPT, where=self.install)
        r = L.deployment_receipt(self.cfg)
        self.assertEqual(r, PC_PUBLISHED)
        self.assertNotIn("files", r); self.assertNotIn("repo", r)

    def test_install_dir_is_searched_last(self):
        self.write(PC_RECEIPT, where=self.install)
        self.write(GOOD, where=self.tmp / "data")
        self.assertEqual(L.deployment_receipt(self.cfg), GOOD)
        self.write({**GOOD, "deployed_at": "2026-11-19T00:00:00Z"}, where=self.tmp / "conf")
        self.assertEqual(L.deployment_receipt(self.cfg)["deployed_at"], "2026-11-19T00:00:00Z")

    def test_install_dir_is_used_without_any_config_or_data_dir(self):
        self.write(PC_RECEIPT, where=self.install)
        self.assertEqual(L.deployment_receipt({}), PC_PUBLISHED)

    def test_install_dir_receipt_stays_fail_closed(self):
        for bad in ({**PC_RECEIPT, "installed_at_utc": "2026-11-18T03:11:06"},
                    {**PC_RECEIPT, "installed_at_utc": "yesterday"},
                    {k: v for k, v in PC_RECEIPT.items() if k != "installed_at_utc"},
                    {**PC_RECEIPT, "sha": "1fcf103"},
                    {**PC_RECEIPT, "version": "0.1.17-dirty"}):
            with self.subTest(bad=bad):
                self.write(bad, where=self.install)
                self.assertEqual(L.deployment_receipt(self.cfg), {"status": "invalid", "reason": "receipt_malformed"})
        self.write("{not json", where=self.install)
        self.assertEqual(L.deployment_receipt(self.cfg), {"status": "invalid", "reason": "receipt_unreadable"})


class HealthReceipt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lch-health-receipt-")
        self.install = Path(tempfile.mkdtemp(prefix="lch-health-install-"))
        p = mock.patch.object(L, "LAUNCHER_DIR", self.install)
        p.start(); self.addCleanup(p.stop)

    def test_health_publishes_the_install_dir_receipt_of_the_real_pc_shape(self):
        (self.install / "hyvv-deployed.json").write_text(json.dumps(PC_RECEIPT), "utf-8")
        la, srv = serve(self.tmp)
        try:
            st, body = get(srv, "/v1/health")
            self.assertEqual(st, 200)
            self.assertEqual(body["deployment_receipt"], PC_PUBLISHED)
            blob = json.dumps(body["deployment_receipt"])
            self.assertNotIn(str(self.install), blob); self.assertNotIn("launcher.py", blob)
        finally:
            srv.shutdown(); la.loop.call_soon_threadsafe(la.loop.stop)

    def test_health_includes_the_receipt_the_doctor_can_validate(self):
        (Path(self.tmp) / "hyvv-deployed.json").write_text(json.dumps(GOOD), "utf-8")
        la, srv = serve(self.tmp)
        try:
            st, body = get(srv, "/v1/health")
            self.assertEqual(st, 200)
            self.assertEqual(body["deployment_receipt"], GOOD)
            # The receipt records the release; `version` is the build running. With no git metadata
            # and no baked describe, the fallback names the receipt's SHA and never the bare tag.
            self.assertEqual(body["release"], body["deployment_receipt"]["version"])
            self.assertEqual(body["build"]["source"], "receipt")
            self.assertTrue(body["version"].startswith(L.VERSION + "+g"), body["version"])
            self.assertNotIn(self.tmp, json.dumps(body["deployment_receipt"]))
        finally:
            srv.shutdown(); la.loop.call_soon_threadsafe(la.loop.stop)

    def test_health_without_a_receipt_has_no_receipt_key(self):
        la, srv = serve(self.tmp)
        try:
            st, body = get(srv, "/v1/health")
            self.assertEqual(st, 200)
            self.assertNotIn("deployment_receipt", body)
        finally:
            srv.shutdown(); la.loop.call_soon_threadsafe(la.loop.stop)

    def test_receipt_is_reread_so_an_apply_update_shows_without_a_restart(self):
        la, srv = serve(self.tmp)
        try:
            self.assertNotIn("deployment_receipt", get(srv, "/v1/health")[1])
            (Path(self.tmp) / "hyvv-deployed.json").write_text(json.dumps(GOOD), "utf-8")
            self.assertEqual(get(srv, "/v1/health")[1]["deployment_receipt"], GOOD)
        finally:
            srv.shutdown(); la.loop.call_soon_threadsafe(la.loop.stop)


if __name__ == "__main__":
    unittest.main()
