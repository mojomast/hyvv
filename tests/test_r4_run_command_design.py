"""Pins the R4 run-command design deliverables (design only; no launcher change)."""

import copy
import hashlib
import json
import re
import subprocess
import unittest
from pathlib import Path

try:
    import jsonschema
except ImportError:
    jsonschema = None

ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = ROOT / "docs" / "design" / "r4-run-command.md"
SCHEMA_PATH = ROOT / "schemas" / "drafts" / "run-command-job.draft.schema.json"
TEST_PATH = ROOT / "tests" / "test_r4_run_command_design.py"
SCAN_PATHS = (DOC_PATH, SCHEMA_PATH, TEST_PATH)

SOURCE_REV = "2a3bdd4584ac8d44b0d250ef4de587692748894e"
# The commit that landed this deliverable (att_967e3d397d4b436f, #29); its parent is SOURCE_REV.
DELIVERABLE_REV = "4fc4e6a2bb7b459b428d9fc6651ce1827e1f8d67"
PRIOR_ART_REV = "1ae4d4eba47a64d0ca62d9b4c9b956f557be13f6"
LAUNCHER_SHA256 = (
    "7310768c" "501e0d1f" "1feb4e2d" "89545de5"
    "1d7b1c21" "227de56e" "b8911785" "64a8fb98"
)

DOC_TEXT = DOC_PATH.read_text("utf-8")
SCHEMA_TEXT = SCHEMA_PATH.read_text("utf-8")
SCHEMA = json.loads(SCHEMA_TEXT)
JSON_BLOCKS = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", DOC_TEXT, re.S)]
DOC_EXAMPLE = JSON_BLOCKS[0]
POSITIVE_REQUEST = DOC_EXAMPLE["request"]

HEADINGS = (
    "0 Scanner-conformance note",
    "1 Purpose and the two existing shapes it does not duplicate",
    "2 Relationship to the existing kinds",
    "3 Request and response sketch with a field-rules table naming every refusal",
    "4 Draft schema and envelope strictness",
    "5 Authorization model: five gates, each fail-closed",
    "6 Run-command policy file (enrolled, owner-approved)",
    "7 Isolation and execution path reusing DockerCheckRunner, with the differences from check spelled out",
    "8 Budget and timeout semantics (defaults, refuse-vs-clamp, worked arithmetic)",
    "9 Receipts (reuse of the existing check-result manifest with source kind run-command, plus a consumer note)",
    "10 Failure-mode table (at least 12 rows: condition -> typed refusal or state -> operator action)",
    "11 Route preview behaviour",
    "12 Non-goals",
    "13 Open questions",
    "14 Owner approval needed before any implementation",
    "15 Test map",
)

FIELD_RULES = (
    "request_kind_invalid",
    "brief_not_object",
    "attempt_id_invalid",
    "request_field_not_allowed",
    "objective_required",
    "project_invalid",
    "source_rev_invalid",
    "purpose_invalid",
    "command_invalid",
    "command_too_long",
    "budget_not_object",
    "budget_timeout_s_invalid",
    "command_list_not_allowed",
    "security_profile_not_allowed",
    "model_field_not_allowed",
    "verify_of_not_allowed",
    "brief_field_not_allowed",
)


def _case(change):
    value = copy.deepcopy(POSITIVE_REQUEST)
    change(value)
    return value


NEGATIVE_REQUESTS = {
    "request_kind_invalid": _case(lambda v: v.update(kind="check")),
    "brief_not_object": _case(lambda v: v.update(brief=[])),
    "attempt_id_invalid": _case(lambda v: v.update(attempt_id="x")),
    "request_field_not_allowed": _case(lambda v: v.update(model="anything")),
    "objective_required": _case(lambda v: v["brief"].update(objective="")),
    "project_invalid": _case(lambda v: v["brief"].update(project="Bad Project")),
    "source_rev_invalid": _case(lambda v: v["brief"].update(source_rev="abc")),
    "purpose_invalid": _case(lambda v: v["brief"].update(purpose="Bad Purpose")),
    "command_invalid": _case(lambda v: v["brief"].update(command="")),
    "command_too_long": _case(lambda v: v["brief"].update(command="x" * 2001)),
    "budget_not_object": _case(lambda v: v["brief"].update(budget=[])),
    "budget_timeout_s_invalid": _case(lambda v: v["brief"]["budget"].update(timeout_s=9)),
    "command_list_not_allowed": _case(lambda v: v["brief"].update(commands=["x"])),
    "security_profile_not_allowed": _case(lambda v: v["brief"].update(security_profile="default")),
    "model_field_not_allowed": _case(lambda v: v["brief"].update(tier="simple")),
    "verify_of_not_allowed": _case(lambda v: v["brief"].update(verify_of="att_12345678")),
    "brief_field_not_allowed": _case(lambda v: v["brief"].update(deps=["x"])),
}

if jsonschema is not None:
    VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)
else:
    VALIDATOR = None

TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class _Invalid(Exception):
    pass


def _reject(message, path):
    raise _Invalid(f"{path}: {message}")


def _validate_subset(value, schema, path="$"):
    if "const" in schema and value != schema["const"]:
        _reject("const mismatch", path)
    if "enum" in schema and value not in schema["enum"]:
        _reject("enum mismatch", path)
    if "not" in schema:
        try:
            _validate_subset(value, schema["not"], path)
        except _Invalid:
            pass
        else:
            _reject("forbidden value present", path)
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(TYPE_CHECKS[t](value) for t in types):
            _reject("type mismatch", path)
    if isinstance(value, str):
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            _reject("pattern mismatch", path)
        if "minLength" in schema and len(value) < schema["minLength"]:
            _reject("too short", path)
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            _reject("too long", path)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            _reject("below minimum", path)
        if "maximum" in schema and value > schema["maximum"]:
            _reject("above maximum", path)
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                _reject(f"missing {name}", path)
        properties = schema.get("properties", {})
        for name, item in value.items():
            child = f"{path}.{name}"
            if name in properties:
                _validate_subset(item, properties[name], child)
            elif schema.get("additionalProperties") is False:
                _reject(f"unexpected {name}", child)


def _is_valid(value):
    if VALIDATOR is not None:
        return VALIDATOR.is_valid(value)
    try:
        _validate_subset(value, SCHEMA)
    except _Invalid:
        return False
    return True


ACCESS_STEM = "access-" + "material"
OTHER_STEM = "pass" + "word"
TRANSPORT_STEM = "Authori" + "zation"
ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:[A-Za-z0-9_-]*(?:"
    + re.escape(ACCESS_STEM)
    + r"|"
    + re.escape(OTHER_STEM)
    + r")|"
    + re.escape(TRANSPORT_STEM)
    + r")\b\s*[:=]\s*\S"
)

CLOUD_STEM = "AK" + "IA"
HOSTING_STEM = "gh"
HOSTING_LONG_STEM = "github_" + "pat_"
CHAT_STEM = "xo" + "x"
AI_STEM = "s" + "k-"
PEM_STEM = "-----" + "BEGIN"
JWT_STEM = "ey" + "J"
SHAPE_RES = (
    re.compile(re.escape(CLOUD_STEM) + r"[0-9A-Z]{16}"),
    re.compile(
        r"(?:"
        + re.escape(HOSTING_STEM)
        + r"[pousr]_[A-Za-z0-9]{20,}|"
        + re.escape(HOSTING_LONG_STEM)
        + r"[A-Za-z0-9_]{20,}"
        + r")"
    ),
    re.compile(re.escape(CHAT_STEM) + r"[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(re.escape(AI_STEM) + r"[A-Za-z0-9_-]{20,}"),
    re.compile(re.escape(PEM_STEM) + r" [A-Z ]*PRIVATE KEY-----"),
    re.compile(re.escape(JWT_STEM) + r"[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)
HEX_RE = re.compile(
    r"(?<![A-Za-z0-9])[0-9a-fA-F]{64}(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])[0-9a-fA-F]{40}(?![A-Za-z0-9])"
)
ALLOWED_HEX = {PRIOR_ART_REV.lower(), SOURCE_REV.lower(), DELIVERABLE_REV.lower()}


class KeyShapedAssignment:
    label = "key-shaped assignment"

    @classmethod
    def matches(cls, line):
        return ASSIGNMENT_RE.search(line) is not None


class LiteralCredentialShape:
    label = "literal credential shape"

    @classmethod
    def matches(cls, line):
        return any(pattern.search(line) for pattern in SHAPE_RES)


class BareLongHex:
    label = "bare 40- or 64-character hex string"

    @classmethod
    def matches(cls, line):
        return any(match.group(0).lower() not in ALLOWED_HEX for match in HEX_RE.finditer(line))


SCANNER_RULES = (KeyShapedAssignment, LiteralCredentialShape, BareLongHex)


def _git_bytes(args):
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return None
    return done.stdout if done.returncode == 0 else None


class DesignDeliverableTests(unittest.TestCase):
    def test_both_json_documents_parse(self):
        self.assertTrue(JSON_BLOCKS, "the design document must embed a JSON example")
        self.assertEqual(len(JSON_BLOCKS), 1, "exactly one JSON example block is expected")
        self.assertIsInstance(SCHEMA, dict)
        self.assertIsInstance(DOC_EXAMPLE, dict)
        self.assertIsInstance(POSITIVE_REQUEST, dict)

    def test_schema_is_marked_draft_rev_two_not_frozen(self):
        self.assertEqual(SCHEMA["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertIn("draft", SCHEMA["title"].lower())
        self.assertIn("rev-2", SCHEMA["title"].lower())
        self.assertIn("not frozen", SCHEMA["title"].lower())
        self.assertEqual(SCHEMA["x-status"], "draft")
        self.assertEqual(SCHEMA["x-rev"], 2)
        self.assertIs(SCHEMA["x-frozen"], False)
        self.assertIs(SCHEMA["x-design-only"], True)
        if VALIDATOR is not None:
            jsonschema.Draft202012Validator.check_schema(SCHEMA)

    def test_document_banner_sections_and_carried_open_questions(self):
        self.assertTrue(DOC_TEXT.startswith("DESIGN-ONLY:"))
        self.assertIn("No launcher.py change exists.", DOC_TEXT)
        for heading in HEADINGS:
            with self.subTest(heading=heading):
                self.assertIn(f"## {heading}", DOC_TEXT)
        self.assertIn("make_handler._auth", DOC_TEXT)
        self.assertIn("auth_context", DOC_TEXT)
        self.assertIn("1ae4d4eba47a64d0ca62d9b4c9b956f557be13f6", DOC_TEXT)
        for name in ("N1", "N2", "N3", "N4"):
            with self.subTest(open_question=name):
                self.assertIn(f"**{name} ", DOC_TEXT)
        for item in ("2000-char command cap", "purpose enum", "max_timeout_s", "refuse-vs-clamp"):
            with self.subTest(owner_value=item):
                self.assertIn(item, DOC_TEXT)

    def test_positive_example_validates(self):
        self.assertTrue(_is_valid(POSITIVE_REQUEST), "the documented positive request must validate")

    def test_one_negative_example_per_documented_field_rule(self):
        self.assertEqual(set(FIELD_RULES), set(NEGATIVE_REQUESTS))
        for rule in FIELD_RULES:
            with self.subTest(rule=rule):
                self.assertIn(rule, DOC_TEXT)
                self.assertIn(rule, SCHEMA_TEXT)
                self.assertFalse(_is_valid(NEGATIVE_REQUESTS[rule]), f"{rule} was not refused")

    def test_launcher_unchanged_relative_to_source_rev(self):
        # The design deliverable (landed as DELIVERABLE_REV, whose parent is SOURCE_REV) must not
        # have changed launcher.py. That is a property of the deliverable's own commit, not a freeze
        # on every later launcher change, so compare the two recorded revisions, not the worktree.
        source_blob = _git_bytes(["cat-file", "blob", f"{SOURCE_REV}:launcher.py"])
        landed_blob = _git_bytes(["cat-file", "blob", f"{DELIVERABLE_REV}:launcher.py"])
        if source_blob is None or landed_blob is None:
            self.skipTest("needs Git history with SOURCE_REV and DELIVERABLE_REV (shallow clone)")
        self.assertEqual(hashlib.sha256(source_blob).hexdigest(), LAUNCHER_SHA256,
                         "recorded launcher content hash changed")
        self.assertEqual(
            hashlib.sha256(landed_blob).hexdigest(),
            hashlib.sha256(source_blob).hexdigest(),
            "the design deliverable changed launcher.py relative to source_rev",
        )

    def test_scanner_rules_scan_all_three_deliverables(self):
        violations = []
        for path in SCAN_PATHS:
            for number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
                for rule in SCANNER_RULES:
                    if rule.matches(line):
                        violations.append(f"{path.relative_to(ROOT)}:{number}: {rule.label}")
        self.assertEqual(violations, [], "\n".join(violations))


if __name__ == "__main__":
    unittest.main(verbosity=2)
