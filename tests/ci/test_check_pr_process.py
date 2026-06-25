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
        ["src/polar_evolution/methods.py", "docs/dev/process.md"],
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
                "",
                "Use `Fixes #...`, `Closes #...`, `Resolves #...`, or `Part of #...`.",
                "",
                "## Docs",
                "",
                "- Updated docs:",
                "- No docs needed:",
                "",
                "## Checklist",
                "",
                "- [ ] Linked issue is present, or `No issue needed:` is explained.",
                "- [ ] Docs are updated, or `No docs needed:` is explained.",
            ]
        ),
        ["src/polar_evolution/methods.py"],
    )

    assert len(warnings) == 2
    assert "Fixes/Closes/Resolves/Part of" in warnings[0]
    assert "Non-documentation changes" in warnings[1]


def test_no_issue_override_requires_explanation_text() -> None:
    checker = _load_module()

    assert not checker.has_issue_reference("No issue needed:")
    assert checker.has_issue_reference("No issue needed: mechanical local experiment.")


def test_warns_for_non_docs_change_without_docs_or_explanation() -> None:
    checker = _load_module()

    warnings = checker.find_process_warnings(
        "Part of #123",
        ["src/polar_evolution/methods.py"],
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
