from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN = (
    REPO_ROOT
    / "docs"
    / "superpowers"
    / "plans"
    / "2026-07-09-openevo-productization-implementation.md"
)


def _bash_blocks(text: str) -> str:
    return "\n".join(re.findall(r"```bash\n(.*?)\n```", text, flags=re.DOTALL))


def test_plan_uses_phase_branch_pr_workflow() -> None:
    text = PLAN.read_text(encoding="utf-8")
    bash = _bash_blocks(text)
    assert "git push openevo " + "stable" not in bash
    assert "git push -u openevo HEAD" in bash
    pr_commands = re.findall(
        r'gh pr create --base stable --head "\$\(git branch --show-current\)"',
        bash,
    )
    assert len(pr_commands) >= 9
    assert "Part of #121" in bash


def test_plan_does_not_commit_known_failing_tests() -> None:
    text = PLAN.read_text(encoding="utf-8")
    forbidden = (
        "Commit " + "failing",
        "known " + "failing",
        "Expected: fail because " + "legacy",
        "OPENEVO_PRODUCTIZATION_" + "STRICT",
        "pytest.mark." + "xfail",
    )
    for marker in forbidden:
        assert marker not in text
