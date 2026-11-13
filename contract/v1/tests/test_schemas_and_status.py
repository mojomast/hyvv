"""Schema fixtures, version capabilities, and launcher implementation status."""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from contract.v1.reference import usage_unknown_template  # noqa: E402

SCHEMA_DIR = ROOT / "contract/v1/schemas"


def _load(name: str):
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def _required_present(schema: dict, instance: dict) -> list[str]:
    return [k for k in schema.get("required", []) if k not in instance]


class UsageSchema(unittest.TestCase):
    def test_usage_template_has_every_required_key(self):
        schema = _load("usage.json")
        rec = usage_unknown_template()
        self.assertEqual(rec["schema_version"], "bee.usage/v1")
        missing = _required_present(schema, rec)
        self.assertEqual(missing, [])
        # Unknown usage must use null, never 0, for the token counters in the template.
        self.assertIsNone(rec["input_tokens"])
        self.assertIsNone(rec["output_tokens"])
        self.assertEqual(rec["usage_status"], "unknown")
        self.assertEqual(set(rec), set(schema["required"]))

    def test_usage_schema_forbids_prompt_like_properties(self):
        schema = _load("usage.json")
        self.assertFalse(schema.get("additionalProperties", True))
        for banned in ("prompt", "objective", "completion", "hostname", "url"):
            self.assertNotIn(banned, schema["properties"])


class VersionSchema(unittest.TestCase):
    def test_version_example_advertises_required_capabilities(self):
        schema = _load("version.json")
        example = {
            "api_version": "v1",
            "version": "0.1.15",
            "capabilities": ["artifact_fetch", "priority_routing", "go_fallback", "leases"],
        }
        self.assertEqual(_required_present(schema, example), [])
        self.assertEqual(example["api_version"], schema["properties"]["api_version"]["const"])
        allowed = set(schema["properties"]["capabilities"]["items"]["enum"])
        self.assertEqual(allowed, {"artifact_fetch", "priority_routing", "go_fallback", "leases"})
        self.assertTrue(set(example["capabilities"]) <= allowed)

    def test_leases_are_specified_and_versioned(self):
        text = (ROOT / "contract/v1/openapi.yaml").read_text(encoding="utf-8")
        self.assertIn("/v1/leases:", text)
        self.assertIn("capacity_exhausted", text)
        self.assertIn("Retry-After", text)
        self.assertIn("x-capability: leases", text)
        self.assertIn("x-hyvv-status: specified", text)
        for name in ("lease.json", "lease-create.json", "lease-renew.json", "lease-list.json"):
            schema = _load(name)
            self.assertIn("$id", schema)
        # Not implemented in launcher v0.1.15.
        src = (ROOT / "launcher.py").read_text(encoding="utf-8")
        handler = src.split("def make_handler", 1)[-1]
        self.assertNotIn('["v1", "leases"]', handler)

    def test_openapi_labels_version_and_artifact_routes(self):

        text = (ROOT / "contract/v1/openapi.yaml").read_text(encoding="utf-8")
        self.assertIn("/v1/version:", text)
        self.assertIn("/v1/attempts/{attempt_id}/artifact:", text)
        self.assertIn("artifact_fetch", text)
        self.assertIn("hyvv.worker-artifact/1", text)
        self.assertIn("x-hyvv-status: reference", text)


class SemverPolicy(unittest.TestCase):
    def test_semver_policy_is_documented(self):
        text = (ROOT / "contract/v1/SEMVER.md").read_text(encoding="utf-8")
        self.assertIn("additive", text.lower())
        self.assertIn("/v2", text)
        self.assertIn("alongside", text.lower())


class LauncherStatus(unittest.TestCase):
    def test_launcher_v0115_has_version_but_no_artifact_route(self):
        src = (ROOT / "launcher.py").read_text(encoding="utf-8")
        self.assertIn('VERSION = "0.1.15"', src)
        # The live 0.1.15 handler has health and /v1/version, not /artifact.
        self.assertIn('["v1", "health"]', src)
        self.assertIn('["v1", "version"]', src)
        self.assertNotIn('"artifact"', src.split("def make_handler", 1)[-1])
        self.assertIn("is_high_priority", src)
        self.assertIn("falling back", src)

    def test_readme_declares_the_name_is_a_vocabulary(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("vocabulary", text.lower())
        self.assertIn("not a dependency on bundlebees", text.lower())


if __name__ == "__main__":
    unittest.main()
