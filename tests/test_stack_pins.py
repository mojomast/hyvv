"""Relationships among external pins, resolved artifacts, CI schema and build identity.

These tests deliberately do not snapshot a release version or a repository
commit. They derive each value from its configured source (pyproject.toml,
uv.lock, config.example.json, the workflow files, the release-asset builder)
and assert that the evidence document and the workflows agree.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
LOCK = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
NAME = PYPROJECT["project"]["name"]
VERSION = PYPROJECT["project"]["version"]
EVIDENCE = (ROOT / "docs/STACK-PINS.md").read_text(encoding="utf-8")
WORKFLOWS = (".github/workflows/ci.yml", ".github/workflows/release.yml")

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ACTION_SPEC = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_release_assets", ROOT / "scripts/build_release_assets.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _action_specs(text: str) -> list[str]:
    found = re.findall(r"(?m)^\s*uses:\s*([^\s#]+)", text)
    return [spec.strip().strip("'\"") for spec in found]


def _documented_action_specs(text: str) -> set[str]:
    return set(re.findall(r"`(" + ACTION_SPEC.pattern + r")`", text))


def _block(text: str, key: str, indent: int = 0) -> str:
    pad = " " * indent
    match = re.search(
        rf"(?ms)^{re.escape(pad + key)}:[^\n]*\n(.*?)(?=^{re.escape(pad)}\S|\Z)",
        text,
    )
    if not match:
        raise AssertionError(f"missing YAML block {key!r} at indent {indent}")
    return match.group(1)


def _job_ids(text: str) -> list[str]:
    jobs = _block(text, "jobs")
    return re.findall(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", jobs)


def _eval_expr(expr: str, *, workflow: str, event_name: str, ref: str, pr_number: str | None):
    """Evaluate the small GitHub-expression subset used by concurrency."""
    value = expr.strip()
    properties = {
        "github.event.pull_request.number": repr(pr_number) if pr_number is not None else "None",
        "github.workflow": repr(workflow),
        "github.event_name": repr(event_name),
        "github.ref": repr(ref),
    }
    for token in sorted(properties, key=len, reverse=True):
        value = value.replace(token, properties[token])
    value = value.replace("||", " or ").replace("&&", " and ")
    return eval(value, {"__builtins__": {}}, {})


def _render(template: str, **context) -> str:
    def substitute(match: re.Match[str]) -> str:
        return str(_eval_expr(match.group(1), **context))

    return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", substitute, template.strip(), flags=re.S)


def _cancel(template: str, **context) -> bool:
    match = re.fullmatch(r"\s*\$\{\{\s*(.*?)\s*\}\}\s*", template.strip(), re.S)
    return bool(_eval_expr(match.group(1) if match else template, **context))


class WorkflowPins(unittest.TestCase):
    def test_every_action_use_is_a_full_commit_sha(self):
        for rel in WORKFLOWS:
            specs = _action_specs(_read(rel))
            self.assertTrue(specs, f"{rel} has no action pins")
            for spec in specs:
                _, _, revision = spec.rpartition("@")
                self.assertRegex(revision, FULL_SHA, f"{rel}: {spec} is not pinned to a full SHA")

    def test_workflow_pins_and_the_evidence_document_are_the_same_set(self):
        configured = set()
        for rel in WORKFLOWS:
            configured.update(_action_specs(_read(rel)))
        documented = _documented_action_specs(EVIDENCE)
        self.assertEqual(configured, documented)


class CIPipelineSchema(unittest.TestCase):
    def setUp(self):
        self.ci = _read(".github/workflows/ci.yml")
        self.triggers = _block(self.ci, "on")

    def test_feature_push_does_not_duplicate_the_pull_request_matrix(self):
        self.assertRegex(self.triggers, r"(?m)^  pull_request:")
        push = _block(self.triggers, "push", indent=2)
        branches_text = re.search(r"branches:\s*\[([^\]]*)\]", push).group(1)
        branches = [b.strip().strip("'\"") for b in branches_text.split(",") if b.strip()]
        self.assertIn("main", branches)
        self.assertFalse([b for b in branches if b.startswith("feat")])
        self.assertNotIn("feat/**", push)

    def test_merge_queue_candidates_run_the_same_schema(self):
        self.assertRegex(self.triggers, r"(?m)^  merge_group:")

    def test_superseded_candidates_cancel_but_main_does_not(self):
        concurrency = _block(self.ci, "concurrency")
        group = re.search(r"(?m)^  group:\s*(.+?)\s*$", concurrency).group(1)
        cancel = re.search(r"(?m)^  cancel-in-progress:\s*(.+?)\s*$", concurrency).group(1)

        main = dict(workflow="CI", event_name="push", ref="refs/heads/main", pr_number=None)
        pr = dict(workflow="CI", event_name="pull_request", ref="refs/pull/7/merge", pr_number="7")
        queue = dict(
            workflow="CI",
            event_name="merge_group",
            ref="refs/heads/gh-readonly-queue/main/pr-7-deadbeef",
            pr_number=None,
        )

        self.assertFalse(_cancel(cancel, **main))
        self.assertTrue(_cancel(cancel, **pr))
        self.assertTrue(_cancel(cancel, **queue))

        # New commits for one PR share a group; a main push is isolated.
        rerun = dict(pr, event_name="synchronize", ref="refs/pull/7/head")
        self.assertEqual(_render(group, **pr), _render(group, **rerun))
        self.assertNotEqual(_render(group, **main), _render(group, **pr))

    def test_required_contexts_and_gate_are_preserved(self):
        self.assertTrue(
            {"windows-provisioning", "test", "scan", "ci"} <= set(_job_ids(self.ci))
        )
        gate = _block(self.ci, "ci", indent=2)
        needs = re.search(r"needs:\s*\[([^\]]*)\]", gate).group(1)
        self.assertEqual({n.strip() for n in needs.split(",")}, {"test", "scan"})
        self.assertIn("if: always()", gate)
        self.assertIn("needs.test.result", gate)
        self.assertIn("needs.scan.result", gate)

    def test_main_branch_trigger_is_explicit(self):
        push = _block(self.triggers, "push", indent=2)
        self.assertIn("branches: [main]", push)


class ExternalPins(unittest.TestCase):
    def test_cua_configuration_resolves_to_every_locked_artifact(self):
        specifiers = PYPROJECT["project"]["optional-dependencies"]["cua"]
        self.assertEqual(len(specifiers), 1)
        specifier = specifiers[0]
        self.assertRegex(specifier, r"^cua==[0-9A-Za-z.+-]+$")
        version = specifier.split("==", 1)[1]

        packages = {pkg["name"]: pkg for pkg in LOCK["package"]}
        locked = packages["cua"]
        self.assertEqual(locked["version"], version)
        self.assertIn(f"cua=={version}", EVIDENCE)

        for wheel in locked["wheels"]:
            digest = wheel["hash"]
            self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
            self.assertIn(digest.split(":", 1)[1], EVIDENCE)

    def test_runtime_image_pin_is_a_full_digest(self):
        image = json.loads(_read("config.example.json"))["image"]
        match = re.search(r"@sha256:([0-9a-f]{64})$", image)
        self.assertIsNotNone(match, image)
        self.assertIn(image, EVIDENCE)

    def test_tool_pins_match_their_configured_revisions(self):
        dockerfile = _read("images/acceptance/Dockerfile")
        version = re.search(r"ARG GITLEAKS_VERSION=(\S+)", dockerfile).group(1)
        digest = re.search(r"ARG GITLEAKS_SHA256=([0-9a-f]{64})", dockerfile).group(1)
        self.assertIn(f"`{version}`", EVIDENCE)
        self.assertIn(f"sha256:{digest}", EVIDENCE)

        seccomp = hashlib.sha256(
            (ROOT / "images/acceptance/seccomp/moby-default-v28.4.0.json").read_bytes()
        ).hexdigest()
        self.assertIn(seccomp, _read("images/acceptance/README.md"))
        self.assertIn(seccomp, EVIDENCE)


class BuildIdentity(unittest.TestCase):
    def test_lock_and_runtime_agree_with_the_package_metadata(self):
        packages = {pkg["name"]: pkg for pkg in LOCK["package"]}
        self.assertEqual(packages[NAME]["version"], VERSION)

        import hyvv

        self.assertEqual(hyvv.__version__, VERSION)

        launcher_version = re.search(r'(?m)^VERSION = "([^"]+)"$', _read("launcher.py")).group(1)
        self.assertEqual(launcher_version, VERSION)

    def test_resolved_artifact_names_drive_the_workflow_contract(self):
        sdist, wheel = _load_builder().expected_dist_names(NAME, VERSION)
        self.assertTrue(sdist.endswith(".tar.gz"))
        self.assertTrue(wheel.endswith(".whl"))

        for rel in WORKFLOWS:
            workflow = _read(rel)
            self.assertIn("scripts/build_release_assets.py", workflow)
            self.assertIn("dist/*.tar.gz", workflow)
            self.assertIn("dist/*.whl", workflow)

        release = _read(".github/workflows/release.yml")
        self.assertIn("packages-dir: dist", release)
        self.assertIn("dist/*.tar.gz dist/*.whl release-assets/*", release)

    def test_evidence_records_the_generated_identity_without_a_self_pin(self):
        sdist, wheel = _load_builder().expected_dist_names(NAME, VERSION)
        for value in (NAME, VERSION, sdist, wheel, f"orbit-launcher/{VERSION}"):
            self.assertIn(value, EVIDENCE)

        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if head:
            self.assertNotIn(head, EVIDENCE)
        self.assertIn("No commit hash", EVIDENCE)


if __name__ == "__main__":
    unittest.main()
