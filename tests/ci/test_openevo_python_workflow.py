from __future__ import annotations

from pathlib import Path


def test_openevo_python_workflow_runs_focused_regressions() -> None:
    workflow = Path(".github/workflows/openevo-python.yml")

    text = workflow.read_text(encoding="utf-8")

    assert "name: OpenEvo Python checks" in text
    assert '"src/openevo/**"' in text
    assert '"tests/openevo/**"' in text
    assert "python -m pip install -e ." in text
    assert "python -m pip install pytest ruff build twine" in text
    assert "ruff check src/openevo tests/openevo" in text
    assert "python -m pytest tests/ci/test_openevo_python_workflow.py tests/openevo -q" in text
