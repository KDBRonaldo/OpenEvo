from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/check_pr_process.py"
    spec = importlib.util.spec_from_file_location("check_pr_process", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_accepts_issue_reference_and_docs_change() -> None:
    checker = _load_module()
    warnings = checker.find_process_warnings(
        "Fixes #123\n\nDocs updated in docs/dev/process.md",
        ["src/openevo/evolution/methods.py", "docs/dev/process.md"],
    )
    assert warnings == []


def test_warns_for_missing_issue_reference() -> None:
    checker = _load_module()
    warnings = checker.find_process_warnings(
        "Summary only\n\nNo docs needed: test-only change.",
        ["tests/evolution/test_worker_methods.py"],
    )
    assert len(warnings) == 1
    assert "Fixes/Closes/Resolves/Part of" in warnings[0]


def test_template_placeholders_do_not_count_as_process_overrides() -> None:
    checker = _load_module()
    warnings = checker.find_process_warnings(
        "\n".join(
            [
                "## Linked Issue",
                "Use `Fixes #...`, `Closes #...`, `Resolves #...`, or `Part of #...`.",
                "## Docs",
                "- Updated docs:",
                "- No docs needed:",
            ]
        ),
        ["src/openevo/evolution/methods.py"],
    )
    assert len(warnings) == 2


def test_process_overrides_require_explanation_text() -> None:
    checker = _load_module()
    assert not checker.has_issue_reference("No issue needed:")
    assert checker.has_issue_reference("No issue needed: mechanical local experiment.")


def test_release_scope_rejects_no_issue_override() -> None:
    checker = _load_module()
    warnings = checker.find_process_warnings(
        "No issue needed: small productization cleanup.\nNo docs needed: none.",
        ["src/openevo/backend/models.py"],
    )
    assert len(warnings) == 1
    assert "must link an issue" in warnings[0]


def test_release_scope_accepts_linked_issue() -> None:
    checker = _load_module()
    warnings = checker.find_process_warnings(
        "Part of #131\nNo docs needed: test-only contract probe.",
        ["benchmarks/terminal_bench/test_contract.py"],
    )
    assert warnings == []


def test_release_facing_docs_reject_no_issue_override() -> None:
    checker = _load_module()
    for path in (
        "README.md",
        "docs/architecture/openevo-desktop-release.md",
        "docs/core/backend-api.md",
        "docs/maintainer/repository-structure.md",
        "docs/maintainer/testing.md",
        "docs/user/first-run.md",
        "scripts/ci/check_openevo_release.py",
    ):
        warnings = checker.find_process_warnings(
            "No issue needed: small release-doc cleanup.\n"
            "No docs needed: this assertion isolates the issue-link rule.",
            [path],
        )
        assert len(warnings) == 1, path
        assert "must link an issue" in warnings[0]


def test_non_release_mechanical_change_allows_explained_override() -> None:
    checker = _load_module()
    warnings = checker.find_process_warnings(
        "No issue needed: mechanical fixture formatting.\nNo docs needed: no behavior change.",
        ["tests/fixtures/example.json"],
    )
    assert warnings == []


def test_warns_for_non_docs_change_without_docs_or_explanation() -> None:
    checker = _load_module()
    warnings = checker.find_process_warnings(
        "Part of #123",
        ["src/openevo/evolution/methods.py"],
    )
    assert len(warnings) == 1
    assert "Non-documentation changes" in warnings[0]


def test_docs_only_change_does_not_need_docs_explanation() -> None:
    checker = _load_module()
    warnings = checker.find_process_warnings(
        "Resolves #123",
        ["AGENTS.md", ".github/pull_request_template.md"],
    )
    assert warnings == []


def test_workflow_changes_are_not_docs_like() -> None:
    checker = _load_module()
    warnings = checker.find_process_warnings(
        "Fixes #123",
        [".github/workflows/issue-labels.yml"],
    )
    assert len(warnings) == 1
    assert "Non-documentation changes" in warnings[0]


def test_issue_templates_are_docs_like() -> None:
    checker = _load_module()
    warnings = checker.find_process_warnings(
        "Fixes #123",
        [".github/ISSUE_TEMPLATE/change_request.yml"],
    )
    assert warnings == []


def test_pr_process_workflow_runs_checker_in_strict_mode() -> None:
    text = Path(".github/workflows/pr-process.yml").read_text(encoding="utf-8")
    assert "name: issue and docs gate" in text
    assert "scripts/ci/check_pr_process.py" in text
    assert "--pr-body-file pr_body.md" in text
    assert "--changed-files-file changed_files.txt" in text
    assert "--strict" in text


def test_pr_process_docs_describe_blocking_ci_gate() -> None:
    text = Path("docs/architecture/pr-process-checks.md").read_text(encoding="utf-8")
    assert "blocking CI gate" in text
    assert "warning-only" in text
    assert "exits successfully by default" not in text
