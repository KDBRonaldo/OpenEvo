from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts/ci/issue_form_labels.py"
    spec = importlib.util.spec_from_file_location("issue_form_labels", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_extracts_primary_and_secondary_issue_form_labels() -> None:
    labels = _load_module().extract_issue_form_labels(
        """
### Primary label

bug

### Secondary labels

documentation, enhancement

### Background and trigger

Something broke.
"""
    )

    assert labels == ["bug", "documentation", "enhancement"]


def test_ignores_invalid_and_duplicate_secondary_labels() -> None:
    labels = _load_module().extract_issue_form_labels(
        """
### Primary label

enhancement

### Secondary labels

enhancement, made-up-label, question
"""
    )

    assert labels == ["enhancement", "question"]


def test_computes_allowed_labels_to_remove_when_issue_form_changes() -> None:
    module = _load_module()

    labels = module.labels_to_remove(
        current_labels=["bug", "documentation", "external", "question"],
        selected_labels=["documentation", "question"],
    )

    assert labels == ["bug"]


def test_builds_label_update_plan_when_form_clears_managed_labels() -> None:
    module = _load_module()

    plan = module.build_label_update_plan(
        issue_body="""
### Primary label

None

### Secondary labels

made-up-label
""",
        current_labels=[{"name": "bug"}, {"name": "external"}],
    )

    assert plan == {"labels_to_add": [], "labels_to_remove": ["bug"]}


def test_cli_outputs_add_and_remove_labels(tmp_path, capsys) -> None:
    module = _load_module()
    issue_body = tmp_path / "issue_body.md"
    github_output = tmp_path / "github_output.txt"
    issue_body.write_text(
        """
### Primary label

documentation

### Secondary labels

question
""",
        encoding="utf-8",
    )

    result = module.main(
        [
            "--issue-body-file",
            str(issue_body),
            "--current-labels-json",
            json.dumps([{"name": "bug"}, {"name": "external"}]),
            "--github-output",
            str(github_output),
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        "labels_to_add": ["documentation", "question"],
        "labels_to_remove": ["bug"],
    }
    assert github_output.read_text(encoding="utf-8").splitlines() == [
        'labels=["documentation", "question"]',
        'labels_to_add=["documentation", "question"]',
        'labels_to_remove=["bug"]',
    ]
