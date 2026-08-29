from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "docs" / "user" / "macos-release-template.md"
WORKFLOW = ROOT / ".github" / "workflows" / "openevo-launcher-release.yml"
RENDERER = ROOT / "scripts" / "release" / "render_release_notes.py"


def _load_renderer():
    spec = importlib.util.spec_from_file_location("render_release_notes", RENDERER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_notes_make_the_supported_download_unambiguous() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")

    assert "Supported local platform" in text
    assert "macOS only" in text
    assert "EvoLab-macOS-{{VERSION}}.tar.gz" in text
    assert "Do not download" in text
    assert "Source code (zip)" in text
    assert "Windows" in text


def test_release_notes_cover_first_install_and_remote_authentication() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")

    for required in (
        "Python 3.11",
        "OpenSSH",
        "exact same SSH user",
        "command -v codex",
        "codex login --device-auth",
        "codex login status",
        "~/.ssh/config",
        "IdentityFile",
        "evolab webui",
        "Git, uv, Node.js, npm",
        "does not fetch EvoLab product source from GitHub",
        "Common problems",
        "{{CHANGELOG}}",
    ):
        assert required in text


def test_renderer_combines_fixed_guide_with_generated_changelog() -> None:
    renderer = _load_renderer()
    rendered = renderer.render_release_notes(
        template=TEMPLATE.read_text(encoding="utf-8"),
        version="0.2.1",
        changelog="* Fixed launcher startup",
    )

    assert "EvoLab-macOS-0.2.1.tar.gz" in rendered
    assert "* Fixed launcher startup" in rendered
    assert "{{VERSION}}" not in rendered
    assert "{{CHANGELOG}}" not in rendered


def test_release_workflow_publishes_one_custom_macos_asset() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    release_command = text.split('gh release create "$GITHUB_REF_NAME"', 1)[1]
    release_command = release_command.split('--repo "$GITHUB_REPOSITORY"', 1)[0]
    asset_lines = [line.strip().rstrip(" \\") for line in release_command.splitlines()]
    asset_lines = [line for line in asset_lines if line]

    assert asset_lines == ["EvoLab-macOS-*.tar.gz"]
    assert "macos-latest" in text
    assert "windows-latest" not in text
    assert "--notes-file" in text
    assert "--generate-notes" not in text
