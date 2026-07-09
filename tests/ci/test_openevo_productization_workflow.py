from __future__ import annotations

import re
import subprocess
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


def test_current_product_surface_uses_openevo_runtime_identity() -> None:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
    )
    scanned_roots = (
        "src/",
        "tests/",
        "scripts/",
        "examples/",
        "web/",
        "docs/architecture/",
        "README.md",
        "AGENTS.md",
    )
    ignored_paths = {
        "scripts/ci/audit_openevo_identity.py",
        "tests/ci/test_openevo_productization_workflow.py",
    }
    forbidden = (
        "POL" + "AR_",
        "/pol" + "ar/session",
        "pol" + "ar.session_completed",
        ".pol" + "ar_evolution",
        "pol" + "ar-evolution",
        "uv run " + "polar",
        "polar serve_",
        "polar dashboard",
        "from polar",
        "import polar",
        "Polar server",
        "Polar servers",
        ".openevo" + ".evolution",
    )
    matches: list[tuple[str, str]] = []
    for raw_path in result.stdout.decode().split("\0"):
        if not raw_path or raw_path in ignored_paths:
            continue
        if not raw_path.startswith(scanned_roots):
            continue
        path = REPO_ROOT / raw_path
        if path.suffix not in {
            ".md",
            ".py",
            ".toml",
            ".yaml",
            ".yml",
            ".json",
            ".sh",
            ".ts",
            ".tsx",
        }:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for marker in forbidden:
            if marker in text:
                matches.append((raw_path, marker))

    assert matches == []
