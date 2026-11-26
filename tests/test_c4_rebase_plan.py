"""RED for the C4 rebase plan after C2 (#83) lands.

C2 and C4 both branched from 5c2e4a4 and both add the same launcher default:
``cfg.setdefault("ticket_mode", "off")``. C2 adds it with the validation and
the ticket surface; C4's entire launcher hunk is that one line. After #83
merges, the rebase is mechanical only if the branch's launcher delta is still
exactly that line and the base already carries it -- then base's file wins
verbatim and the duplicate is dropped.

This module is the executable plan: it refuses a drifted launcher hunk, refuses
a base that lost the default, and prints the ordered steps an operator runs.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "c4_rebase_after_c2.py"
DEFAULT_LINE = '    cfg.setdefault("ticket_mode", "off")'


def plan_module():
    assert SCRIPT.is_file(), "scripts/c4_rebase_after_c2.py is missing"
    spec = importlib.util.spec_from_file_location("c4_rebase_after_c2", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def one_line_diff() -> str:
    return (
        "diff --git a/launcher.py b/launcher.py\n"
        "--- a/launcher.py\n"
        "+++ b/launcher.py\n"
        "@@ -1151,6 +1151,7 @@\n"
        "     cfg.setdefault(\"require_budget\", False)\n"
        '+    cfg.setdefault("ticket_mode", "off")\n'
        '     cfg.setdefault("data_dir", str(Path.home() / ".orbit-launcher"))\n'
    )


def test_the_plan_names_the_gate_branch_and_no_force_rule() -> None:
    steps = "\n".join(plan_module().rebase_plan())
    assert "#83" in steps
    assert "feat/wave-c-c4" in steps
    assert "without force" in steps.lower()


def test_the_plan_runs_both_approved_test_files() -> None:
    steps = "\n".join(plan_module().rebase_plan())
    assert "tests.test_ticket_verify" in steps
    assert "tests.test_ticket_surface" in steps


def test_the_launcher_hunk_accepts_exactly_the_one_default_line() -> None:
    assert plan_module().launcher_hunk_is_exactly_the_default(one_line_diff()) is True


def test_the_launcher_hunk_rejects_any_extra_change() -> None:
    module = plan_module()
    extra = one_line_diff().replace(
        '     cfg.setdefault("require_budget", False)\n',
        '     cfg.setdefault("require_budget", True)\n',
    )
    assert module.launcher_hunk_is_exactly_the_default(extra) is False
    removal = one_line_diff().replace(
        '     cfg.setdefault("data_dir", str(Path.home() / ".orbit-launcher"))\n',
        '',
    )
    assert module.launcher_hunk_is_exactly_the_default(removal) is False


def test_resolve_launcher_keeps_base_verbatim_when_it_carries_the_default() -> None:
    base = "def load(path):\n    cfg = {}\n" + DEFAULT_LINE + "\n    return cfg\n"
    assert plan_module().resolve_launcher(base) == base


def test_resolve_launcher_refuses_a_base_without_the_default() -> None:
    module = plan_module()
    try:
        module.resolve_launcher("def load(path):\n    return {}\n")
    except ValueError as exc:
        assert "ticket_mode" in str(exc)
    else:  # pragma: no cover - a silent pass is the defect
        raise AssertionError("a base without the default must be refused, not resolved")
