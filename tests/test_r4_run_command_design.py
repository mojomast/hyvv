"""DESIGN-ONLY acceptance tests for the r4 run-command job-type design (r2).

Proves the deliverable properties the r2 brief requires for
docs/design/r4-run-command.md and schemas/drafts/run-command-job.draft.schema.json:

- the design document exists, is explicitly DESIGN-ONLY, states that no
  launcher.py change, deploy or live API surface is authorized, links the
  authorization requirements (five owner approval gates, fail-closed host
  gates), and references only mechanisms that really exist in launcher.py;
- launcher.py is unchanged in the ways the design requires (no run-command
  kind, no new route, and the /v1/route refusal already answers for the kind);
- the draft schema parses, conforms to JSON Schema draft 2020-12, is marked
  draft and not frozen, is versioned, and validates representative requests;
- SCANNER HYGIENE (the r2 hard requirement): the coordinator verify refused
  r1 with secret_detected on docs/design/r4-run-command.md (the
  keyword-assignment rule firing on authorization-model prose, not a leak:
  nothing was applied or pushed). The two scanner rule classes are
  re-implemented here and every line of all three deliverables, including
  this file, is checked against them.

RED-first record (unmodified tree at 0a55364, the squash of base
316bd975e212973837daeefc271a491471ea882c): python3 -m pytest
tests/test_r4_run_command_design.py -q reported 4 failed, 3 passed, 25 errors
- the design document and the draft schema did not exist, so every
doc-grounded and schema-grounded test failed or errored on the missing files
(setup errors) and the hygiene tests failed on the missing deliverables,
while only the three launcher design-only guards passed. Writing only the
three leased files turns the same command green (32 passed).

Run: python3 -m pytest tests/test_r4_run_command_design.py -q
(no network, no Docker, no cua install needed)
"""

import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.modules.setdefault("cua", mock.MagicMock())

import launcher  # noqa: E402

DOC = ROOT / "docs" / "design" / "r4-run-command.md"
SCHEMA = ROOT / "schemas" / "drafts" / "run-command-job.draft.schema.json"
SELF = Path(__file__).resolve()
LAUNCHER_PATH = ROOT / "launcher.py"

try:
    import jsonschema
except ImportError:  # pragma: no cover - fallback keeps the suite self-sufficient
    jsonschema = None


# ---------------------------------------------------------------------------
# Scanner conformance (the r2 hard requirement), reproduced from the brief.
#
# Rule (a), keyword-assignment: any word ending in a credential-name suffix,
# followed on the same line by a colon or an equals sign and a value, in any
# syntax (JSON, YAML, prose, tables). The deliverables never contain such a
# line; the rule is described in prose only, never by example.
# Rule (b), literal credential shapes: recognizable provider and platform
# material prefixes and encodings. Every needle below is assembled from two
# parts so that no line of this file contains a contiguous shape either.
# ---------------------------------------------------------------------------
KEYWORD_SUFFIXES = "api[_-]?key|token|secret|password|passwd|pwd|authorization"
KEYWORD_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*?(?:" + KEYWORD_SUFFIXES + r"))(\s*[:=]\s*)(\S.*)"
)

LITERAL_NEEDLES = (
    ("transport-scheme", "Bear" + "er"),
    ("openai-style", "sk" + "-"),
    ("github-classic", "gh" + "p_"),
    ("github-app", "gh" + "o_"),
    ("github-fine", "github_" + "pat_"),
    ("chat-platform", "xo" + "x"),
    ("cloud-object", "AKI" + "A"),
    ("cloud-language", "AIz" + "a"),
    ("encoded-claim", "ey" + "J"),
)


def _hygiene_findings(text, source_name):
    findings = []
    for n, line in enumerate(text.splitlines(), 1):
        m = KEYWORD_ASSIGNMENT_RE.search(line)
        if m:
            findings.append((source_name, n, "keyword-assignment", m.group(1)))
        for label, needle in LITERAL_NEEDLES:
            if needle in line:
                findings.append((source_name, n, "literal-shape " + label, "redacted-needle"))
    return findings


def _fallback_errors(schema, instance, path=""):
    """Minimal validator for the JSON Schema keyword subset this draft uses.

    Used only when the jsonschema package is unavailable; the primary path is
    the standard library-grade jsonschema validator."""
    errs = []
    if schema is True:
        return errs
    if schema is False:
        errs.append((path or "instance") + ": schema forbids any value")
        return errs
    if not isinstance(schema, dict):
        errs.append((path or "instance") + ": unsupported schema node")
        return errs
    if "not" in schema:
        if not _fallback_errors(schema["not"], instance, path):
            errs.append((path or "instance") + ": negated schema matched")
    t = schema.get("type")
    if t is not None:
        allowed = t if isinstance(t, list) else [t]
        py = {"object": dict, "string": str, "integer": int, "number": (int, float),
              "boolean": bool, "null": type(None), "array": list}
        ok = False
        for k in allowed:
            if not isinstance(instance, py[k]):
                continue
            if k == "integer" and isinstance(instance, bool):
                continue
            ok = True
        if not ok:
            errs.append((path or "instance") + ": wrong type")
            return errs
    if "const" in schema and instance != schema["const"]:
        errs.append((path or "instance") + ": const mismatch")
    if "enum" in schema and not any(instance == v for v in schema["enum"]):
        errs.append((path or "instance") + ": value not in enum")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errs.append((path or "instance") + ": shorter than minLength")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errs.append((path or "instance") + ": longer than maxLength")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errs.append((path or "instance") + ": pattern mismatch")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errs.append((path or "instance") + ": below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            errs.append((path or "instance") + ": above maximum")
    if isinstance(instance, dict):
        for k in schema.get("required", []):
            if k not in instance:
                errs.append((path or "instance") + ": missing required property " + k)
        props = schema.get("properties", {})
        for k, v in instance.items():
            sub = props.get(k)
            if sub is None:
                ap = schema.get("additionalProperties", True)
                if ap is False:
                    errs.append(path + "." + k + ": additional property not allowed")
                elif isinstance(ap, dict):
                    errs.extend(_fallback_errors(ap, v, path + "." + k))
            else:
                errs.extend(_fallback_errors(sub, v, path + "." + k))
    return errs


def _validator_errors(schema, instance):
    if jsonschema is not None:
        cls = jsonschema.validators.validator_for(schema)
        validator = cls(schema)
        return [e.message for e in validator.iter_errors(instance)]
    return _fallback_errors(schema, instance, "")


# ---------------------------------------------------------------------------
# Exact phrases the tests bind to. They are asserted against the document so
# the document cannot silently drop a required property of the design.
# ---------------------------------------------------------------------------
REQUIRED_HEADINGS = (
    "## 0. Scanner-conformance note",
    "## 1. Purpose",
    "## 2. Relationship to the existing kinds",
    "## 3. Request and response sketch",
    "## 4. Draft schema and envelope strictness",
    "## 5. Authorization model",
    "## 6. Run-command policy file",
    "## 7. Isolation and execution path",
    "## 8. Budget and timeout semantics",
    "## 9. Receipts",
    "## 10. Failure modes",
    "## 11. Route preview",
    "## 12. Non-goals",
    "## 13. Open questions",
    "## 14. Owner approval needed before any implementation",
    "## 15. Test map",
)

OWNER_GATES = (
    "Design approval",
    "Implementation approval",
    "Contract change approval",
    "Policy enrollment approval",
    "Allowlist content approval",
)

HOST_GATE_FACTS = (
    "501",
    "503",
    "fail-closed",
    "run_command.enabled",
    "defaults to false",
    "run-command-policy.json",
    "rizzstack.run-command/1",
)

REUSED_MECHANISMS = (
    "DockerCheckRunner",
    "docker_check_argv",
    "check_timeout_s",
    "attempt_budget_s",
    "CHECK_ATTEMPT_OVERHEAD_S",
    "CHECK_TIMEOUT_S",
    "CHECK_TAIL",
    "_budget_watchdog",
    "budget_stop",
    "route_preview",
    "export_project",
    "build_landed_event",
    "resolve_security_profile",
    "validate_brief",
    "redact",
    "make_handler",
)

EXECUTION_FACTS = (
    "bash -lc",
    "hyvv-check-",
    "refs/hyvv/seed",
    "one-entry",
    "byte-for-byte",
    "no normalization",
    "check_timeout_s(b) * 2 + CHECK_ATTEMPT_OVERHEAD_S",
)

VALID_SAMPLES = (
    {"kind": "run-command", "brief": {"objective": "disk usage", "command": "df -h"}},
    {"kind": "run-command", "attempt_id": "att_rc1a2b3c4d5e6f7a8b",
     "brief": {"objective": "report disk usage on the seeded workspace", "command": "df -h /work",
               "purpose": "diagnostics", "project": "rizzstack-smoke",
               "budget": {"timeout_s": 120}, "security_profile": "default"}},
    {"kind": "run-command",
     "brief": {"objective": "prune dangling images", "command": "docker image prune -f",
               "purpose": "maintenance"}},
    {"kind": "run-command", "model": None,
     "brief": {"objective": "list images", "command": "docker images", "note": "operator request 42"}},
)

INVALID_SAMPLES = (
    ("kind missing", {"brief": {"objective": "x", "command": "df -h"}}),
    ("kind wrong", {"kind": "check", "brief": {"objective": "x", "command": "df -h"}}),
    ("brief missing", {"kind": "run-command"}),
    ("command missing", {"kind": "run-command", "brief": {"objective": "x"}}),
    ("command empty", {"kind": "run-command", "brief": {"objective": "x", "command": ""}}),
    ("command over the cap", {"kind": "run-command", "brief": {"objective": "x", "command": "e" * 2001}}),
    ("purpose outside the enum",
     {"kind": "run-command", "brief": {"objective": "x", "command": "df -h", "purpose": "cleanup"}}),
    ("commands is a check-kind field",
     {"kind": "run-command", "brief": {"objective": "x", "command": "df -h", "commands": ["ls"]}}),
    ("verify_of is opencode-only",
     {"kind": "run-command", "brief": {"objective": "x", "command": "df -h", "verify_of": "att_abcdef01"}}),
    ("assigned_files has no meaning here",
     {"kind": "run-command", "brief": {"objective": "x", "command": "df -h", "assigned_files": ["docs/x.md"]}}),
    ("model must be absent",
     {"kind": "run-command", "model": "zoo:glm-5.3-flash", "brief": {"objective": "x", "command": "df -h"}}),
    ("tier must be absent",
     {"kind": "run-command", "tier": "simple", "brief": {"objective": "x", "command": "df -h"}}),
    ("budget below the clamp floor",
     {"kind": "run-command", "brief": {"objective": "x", "command": "df -h", "budget": {"timeout_s": 5}}}),
    ("budget above the clamp ceiling",
     {"kind": "run-command", "brief": {"objective": "x", "command": "df -h", "budget": {"timeout_s": 8000}}}),
    ("attempt_id too short",
     {"kind": "run-command", "attempt_id": "short", "brief": {"objective": "x", "command": "df -h"}}),
    ("brief kind conflict",
     {"kind": "run-command", "brief": {"objective": "x", "command": "df -h", "kind": "check"}}),
    ("objective empty", {"kind": "run-command", "brief": {"objective": "", "command": "df -h"}}),
)


def _require_file(path, what):
    if not path.is_file():
        raise AssertionError("missing deliverable (%s): %s" % (what, path))
    return path.read_text("utf-8")


class TestDesignDoc(unittest.TestCase):
    """The document exists, is DESIGN-ONLY, and carries the required content."""

    @classmethod
    def setUpClass(cls):
        cls.text = _require_file(DOC, "design document")

    def test_doc_exists(self):
        self.assertTrue(DOC.is_file(), "docs/design/r4-run-command.md must exist")

    def test_doc_is_marked_design_only(self):
        self.assertIn("DESIGN-ONLY", self.text)
        self.assertIn("No launcher.py change", self.text)
        self.assertIn("no deploy", self.text)
        self.assertIn("no live API surface", self.text)

    def test_doc_has_required_sections(self):
        for heading in REQUIRED_HEADINGS:
            self.assertIn(heading, self.text, "missing section: " + heading)

    def test_doc_references_the_draft_schema(self):
        self.assertIn("schemas/drafts/run-command-job.draft.schema.json", self.text)

    def test_doc_marks_the_schema_draft_not_frozen(self):
        self.assertIn("draft, not frozen", self.text)

    def test_doc_states_refusals_create_no_attempt(self):
        self.assertIn("creates no attempt record", self.text)

    def test_doc_names_the_kind_value(self):
        self.assertIn('"run-command"', self.text)


class TestAuthorizationRequirements(unittest.TestCase):
    """The document links the authorization requirements explicitly."""

    @classmethod
    def setUpClass(cls):
        cls.text = _require_file(DOC, "design document")

    def test_owner_gates_listed(self):
        for gate in OWNER_GATES:
            self.assertIn(gate, self.text, "missing owner gate: " + gate)

    def test_fail_closed_host_gates_listed(self):
        for fact in HOST_GATE_FACTS:
            self.assertIn(fact, self.text, "missing host-gate fact: " + fact)

    def test_transport_gate_is_the_existing_check(self):
        self.assertIn("make_handler", self.text)
        self.assertIn("_auth", self.text)

    def test_exact_match_allowlist_wording(self):
        self.assertIn("byte-for-byte", self.text)
        self.assertIn("no normalization", self.text)

    def test_conformance_choice_is_documented(self):
        self.assertIn("auth_context", self.text)
        self.assertIn("scanner-conformance", self.text)


class TestReuseGrounded(unittest.TestCase):
    """Every mechanism the design claims to reuse exists in launcher.py."""

    @classmethod
    def setUpClass(cls):
        cls.launcher_src = LAUNCHER_PATH.read_text("utf-8")
        cls.doc_text = _require_file(DOC, "design document")

    def test_doc_mechanisms_exist_in_launcher(self):
        import inspect
        for name in REUSED_MECHANISMS:
            self.assertTrue(
                hasattr(launcher, name) or ("(" + name) in self.launcher_src or name in self.launcher_src,
                "launcher.py does not define " + name,
            )
        self.assertTrue(inspect.isclass(launcher.DockerCheckRunner))

    def test_doc_references_mechanisms(self):
        for name in REUSED_MECHANISMS:
            self.assertIn(name, self.doc_text, "document does not reference " + name)

    def test_attempt_clock_formula_matches_the_check_kind(self):
        brief = {"commands": ["true"], "budget": {"timeout_s": 60}}
        attempt = {"kind": "check", "brief": brief}
        expected = launcher.check_timeout_s(brief) * 2 + launcher.CHECK_ATTEMPT_OVERHEAD_S
        self.assertEqual(launcher.attempt_budget_s(attempt), expected)
        self.assertEqual(expected, 420)

    def test_check_runner_consumes_a_command_list(self):
        import inspect
        src = inspect.getsource(launcher.DockerCheckRunner.run_checks)
        self.assertIn("commands", src)

    def test_doc_execution_facts_are_grounded(self):
        for fact in EXECUTION_FACTS:
            self.assertIn(fact, self.doc_text, "document is missing the fact: " + fact)


class TestLauncherRemainsDesignOnly(unittest.TestCase):
    """Design-only guards: no implementation exists and none is pre-registered."""

    def test_kinds_unchanged(self):
        self.assertEqual(launcher.KINDS, ("opencode", "agy-cua", "check"))

    def test_no_run_command_surface_in_launcher(self):
        src = LAUNCHER_PATH.read_text("utf-8")
        self.assertNotIn("run-command", src)
        self.assertNotIn("run_command", src)

    def test_route_preview_refusal_covers_the_kind(self):
        with self.assertRaises(ValueError) as ctx:
            launcher.route_preview({"model": "deepseek-flash"}, None, {}, "run-command")
        self.assertIn("run-command", str(ctx.exception))


class TestDraftSchema(unittest.TestCase):
    """The draft schema parses, is a marked draft, and validates samples."""

    @classmethod
    def setUpClass(cls):
        text = _require_file(SCHEMA, "draft schema")
        cls.schema = json.loads(text)

    def test_schema_file_parses(self):
        self.assertIsInstance(self.schema, dict)

    def test_schema_is_json_schema_2020_12(self):
        self.assertEqual(self.schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        if jsonschema is not None:
            jsonschema.validators.validator_for(self.schema).check_schema(self.schema)

    def test_schema_marked_draft_and_versioned(self):
        self.assertEqual(self.schema["x-hyvv-status"], "draft")
        self.assertFalse(self.schema["x-hyvv-frozen"])
        self.assertEqual(self.schema["x-hyvv-draft-rev"], 2)
        self.assertTrue(self.schema["$id"].endswith("run-command-job.draft.schema.json"))

    def test_valid_requests_validate(self):
        for sample in VALID_SAMPLES:
            errs = _validator_errors(self.schema, sample)
            self.assertEqual(errs, [], "valid sample refused: %s (%r)" % (sample, errs))

    def test_invalid_requests_are_refused(self):
        for label, sample in INVALID_SAMPLES:
            errs = _validator_errors(self.schema, sample)
            self.assertTrue(errs, "invalid sample was accepted: " + label)

    def test_command_cap_agrees_with_doc(self):
        brief_props = self.schema["properties"]["brief"]["properties"]
        self.assertEqual(brief_props["command"]["maxLength"], 2000)
        self.assertIn("2000", _require_file(DOC, "design document"))

    def test_budget_bounds_agree_with_launcher(self):
        timeout = self.schema["properties"]["brief"]["properties"]["budget"]["properties"]["timeout_s"]
        self.assertEqual(timeout["minimum"], launcher.CHECK_TIMEOUT_S[0])
        self.assertEqual(timeout["maximum"], launcher.CHECK_TIMEOUT_S[2])

    def test_purpose_enum_agrees_with_doc(self):
        enum = self.schema["properties"]["brief"]["properties"]["purpose"]["enum"]
        self.assertEqual(enum, ["diagnostics", "maintenance"])
        doc = _require_file(DOC, "design document")
        self.assertIn("diagnostics", doc)
        self.assertIn("maintenance", doc)


class TestScannerHygiene(unittest.TestCase):
    """Every line of every deliverable is scanner-safe (r2 hard requirement)."""

    DELIVERABLES = (("design document", DOC), ("draft schema", SCHEMA), ("test file", SELF))

    def _scan_all(self):
        findings = []
        for what, path in self.DELIVERABLES:
            text = _require_file(path, what)
            findings.extend(_hygiene_findings(text, what))
        return findings

    def test_no_keyword_assignment_lines(self):
        findings = [f for f in self._scan_all() if f[2] == "keyword-assignment"]
        self.assertEqual(findings, [], "keyword-assignment rule hit: %r" % (findings,))

    def test_no_literal_credential_shapes(self):
        findings = [f for f in self._scan_all() if f[2].startswith("literal-shape")]
        self.assertEqual(findings, [], "literal credential shape hit: %r" % (findings,))

    def test_doc_never_carries_a_transport_header_example(self):
        text = _require_file(DOC, "design document")
        self.assertNotIn("Bear" + "er", text)

    def test_doc_documents_scanner_conformance(self):
        text = _require_file(DOC, "design document")
        self.assertIn("Scanner-conformance note", text)
        self.assertIn("secret_detected", text)


if __name__ == "__main__":
    unittest.main()
