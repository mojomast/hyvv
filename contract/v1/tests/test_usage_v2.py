"""bee.usage/v2: the per-attempt usage record with open provider labels.

v1 stays frozen byte for byte. v2 keeps every v1 field and meaning; only
provider, route and fallback change from closed enums to provider_label tokens,
so Nous, Kimi, Zoo and future routes are recorded as themselves instead of
unknown.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

SCHEMA_DIR = ROOT / "contract/v1/schemas"
V1_SHA256 = "3ef1f8b498a698ca9a016853094d90e724edbfe407f0c726d75e18689e88063a"
LABEL_FIELDS = ("provider", "route", "fallback")
GOOD = ("nous", "kimi", "zoo", "go", "deepseek", "opencode", "openrouter", "agy", "free", "unknown")
BAD = ("https://api.example", "Nous", "nous portal", "", "a" * 65, "../x")


def _load(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def _accepts(spec: dict, value) -> bool:
    if value is None:
        types = spec.get("type")
        return isinstance(types, list) and "null" in types
    if not isinstance(value, str):
        return False
    return re.fullmatch(spec["pattern"], value) is not None


class UsageV1Frozen(unittest.TestCase):
    def test_v1_schema_is_byte_identical(self):
        digest = hashlib.sha256((SCHEMA_DIR / "usage.json").read_bytes()).hexdigest()
        self.assertEqual(digest, V1_SHA256)


class UsageV2Schema(unittest.TestCase):
    def setUp(self):
        self.v1 = _load("usage.json")
        self.v2 = _load("usage.v2.json")

    def test_discriminator_and_id(self):
        self.assertEqual(self.v2["properties"]["schema_version"]["const"], "bee.usage/v2")
        self.assertTrue(self.v2["$id"].endswith("/contract/v1/schemas/usage.v2.json"))
        self.assertFalse(self.v2.get("additionalProperties", True))

    def test_same_fields_and_required_set_as_v1(self):
        self.assertEqual(set(self.v2["properties"]), set(self.v1["properties"]))
        self.assertEqual(self.v2["required"], self.v1["required"])

    def test_only_label_fields_and_discriminator_change(self):
        changed = {k for k in self.v1["properties"] if self.v1["properties"][k] != self.v2["properties"][k]}
        self.assertEqual(changed, {"schema_version", *LABEL_FIELDS})

    def test_label_fields_accept_open_provider_labels(self):
        for field in LABEL_FIELDS:
            spec = self.v2["properties"][field]
            for value in GOOD:
                with self.subTest(field=field, value=value):
                    self.assertTrue(_accepts(spec, value))

    def test_label_fields_refuse_urls_case_and_spaces(self):
        for field in LABEL_FIELDS:
            spec = self.v2["properties"][field]
            for value in BAD:
                with self.subTest(field=field, value=value):
                    self.assertFalse(_accepts(spec, value))

    def test_only_fallback_may_be_null(self):
        self.assertTrue(_accepts(self.v2["properties"]["fallback"], None))
        self.assertFalse(_accepts(self.v2["properties"]["provider"], None))
        self.assertFalse(_accepts(self.v2["properties"]["route"], None))

    def test_forbids_prompt_like_properties(self):
        for banned in ("prompt", "objective", "completion", "hostname", "url"):
            self.assertNotIn(banned, self.v2["properties"])


class UsageV2Template(unittest.TestCase):
    def test_template_has_every_required_key_and_unknowns_are_null(self):
        from contract.v1.reference import usage_unknown_template_v2

        schema = _load("usage.v2.json")
        rec = usage_unknown_template_v2(provider="nous", route="nous")
        self.assertEqual(rec["schema_version"], "bee.usage/v2")
        self.assertEqual(set(rec), set(schema["required"]))
        self.assertIsNone(rec["input_tokens"])
        self.assertIsNone(rec["output_tokens"])
        self.assertEqual(rec["usage_status"], "unknown")
        self.assertEqual(rec["provider"], "nous")

    def test_v1_template_is_unchanged(self):
        from contract.v1.reference import usage_unknown_template

        self.assertEqual(usage_unknown_template()["schema_version"], "bee.usage/v1")


class UsageV2Status(unittest.TestCase):
    def test_status_lists_v2_as_specified(self):
        status = json.loads((ROOT / "contract/v1/STATUS.json").read_text(encoding="utf-8"))
        self.assertEqual(status["features"].get("bee.usage/v2 records (open provider labels)"), "specified")


if __name__ == "__main__":
    unittest.main()
