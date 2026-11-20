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

from tests.test_health_models import L, get, serve

REV = "1fcf1039e67ec5fd07e2111cba1808f793c7b5bd"
GOOD = {"deployed_at": "2026-11-18T03:13:00Z", "version": L.VERSION, "rev": REV}


class ReceiptReader(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lch-receipt-"))
        self.cfg = {"data_dir": str(self.tmp / "data"), "_config_path": str(self.tmp / "config.json")}

    def write(self, obj, name="hyvv-deployed.json", where=None):
        p = (where or self.tmp) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(obj if isinstance(obj, str) else json.dumps(obj), "utf-8")
        return p

    def test_no_receipt_file_means_no_receipt_key(self):
        self.assertIsNone(L.deployment_receipt(self.cfg))

    def test_valid_receipt_beside_the_config_is_published_with_only_the_validated_fields(self):
        self.write({**GOOD, "token": "x" * 40, "path": "C:/Users/someone/pc-launcher", "note": "extra"})
        self.assertEqual(L.deployment_receipt(self.cfg), GOOD)

    def test_data_dir_location_and_explicit_path_and_aliases(self):
        self.write({"applied_at": "2026-11-18T03:13:00+00:00", "version": L.VERSION, "sha": REV}, where=self.tmp / "data")
        self.assertEqual(L.deployment_receipt(self.cfg),
                         {"deployed_at": "2026-11-18T03:13:00Z", "version": L.VERSION, "rev": REV})
        other = self.write(GOOD, name="elsewhere.json")
        self.assertEqual(L.deployment_receipt({**self.cfg, "deployment_receipt_path": str(other)}), GOOD)

    def test_explicit_path_that_is_missing_is_invalid_not_a_fallback(self):
        self.write(GOOD)  # a default-location receipt must not stand in for the configured one
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

    def test_receipt_file_is_bounded(self):
        self.write({**GOOD, "pad": "x" * 70_000})
        self.assertEqual(L.deployment_receipt(self.cfg), {"status": "invalid", "reason": "receipt_unreadable"})


class HealthReceipt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lch-health-receipt-")

    def test_health_includes_the_receipt_the_doctor_can_validate(self):
        (Path(self.tmp) / "hyvv-deployed.json").write_text(json.dumps(GOOD), "utf-8")
        la, srv = serve(self.tmp)
        try:
            st, body = get(srv, "/v1/health")
            self.assertEqual(st, 200)
            self.assertEqual(body["deployment_receipt"], GOOD)
            self.assertEqual(body["version"], body["deployment_receipt"]["version"])
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
