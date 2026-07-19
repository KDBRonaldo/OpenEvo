from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN = (
    REPO_ROOT
    / "docs"
    / "maintainer"
    / "productization"
    / "implementation-plan.md"
)
RELEASE_SMOKE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "openevo-release-smoke.yml"


def test_plan_uses_focused_branch_and_pr_workflow() -> None:
    text = PLAN.read_text(encoding="utf-8")

    assert "#131" in text
    assert "Base branch: `stable`" in text
    assert "focused branches" in text
    assert "ivowang <ziyiwang@ieee.org>" in text
    assert "gpt-5.6-sol" in text


def test_plan_covers_productization_workstreams_and_release_gates() -> None:
    text = PLAN.read_text(encoding="utf-8")
    assert re.findall(r"^## ([A-E])\.", text, flags=re.MULTILINE) == [
        "A",
        "B",
        "C",
        "D",
        "E",
    ]
    normalized = " ".join(text.split()).lower()
    required_markers = (
        "g7",
        "self-contained daemon bundle",
        "packaged macos application",
        "immediate execution order",
    )
    missing = [marker for marker in required_markers if marker not in normalized]
    assert missing == []


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
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
    )
    scanned_roots = (
        "src/",
        "scripts/",
        "examples/",
        "assets/",
        "desktop/",
        "web/",
        "docs/architecture/",
        "docs/core/",
        "docs/user/",
        "docs/maintainer/",
        ".github/",
        "README.md",
        "AGENTS.md",
        "pyproject.toml",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
    )
    archived_roots = (
        "docs/maintainer/development-history/",
        "docs/maintainer/productization/",
        "docs/dev/",
    )
    ignored_paths = {
        "scripts/ci/audit_openevo_identity.py",
        "tests/ci/test_openevo_productization_workflow.py",
        "tests/ci/test_openevo_productization_identity.py",
    }
    forbidden = (
        "POL" + "AR_",
        "/pol" + "ar/session",
        "pol" + "ar.session_completed",
        "Polar",
        "polar_",
        "polar/",
        "polar:",
        ".pol" + "ar_evolution",
        "pol" + "ar-evolution",
        "polar-",
        "polar.",
        "uv run " + "polar",
        "polar serve_",
        "polar dashboard",
        '"polar' + '_gateway"',
        "from polar",
        "import polar",
        "Polar server",
        "Polar servers",
        ".openevo" + ".evolution",
    )
    forbidden_paths = (
        "README.polar.md",
        "assets/polar",
        "docs/superpowers",
        "docs/report",
        "polar_config.yaml",
        "polar-system-overview",
        "openevo-dev-kit",
        "slime_polar_async",
        "polar_stars",
    )
    forbidden_patterns = tuple(
        re.compile(pattern)
        for pattern in (
            r"\bpolar[A-Za-z0-9_]+",
            r"\b[A-Za-z0-9_]+polar[A-Za-z0-9_]*",
            r"\bPOLAR[A-Z0-9_]*\b",
        )
    )
    matches: list[tuple[str, str]] = []
    for raw_path in result.stdout.decode().split("\0"):
        if not raw_path or raw_path in ignored_paths:
            continue
        if not raw_path.startswith(scanned_roots):
            continue
        if raw_path.startswith(archived_roots):
            continue
        for marker in forbidden_paths:
            if marker in raw_path:
                matches.append((raw_path, marker))
        path = REPO_ROOT / raw_path
        if not path.exists():
            continue
        if path.suffix not in {
            ".md",
            ".py",
            ".toml",
            ".yaml",
            ".yml",
            ".json",
            ".html",
            ".css",
            ".js",
            ".lock",
            ".txt",
            ".sh",
            ".ts",
            ".tsx",
            ".svg",
        } and path.name not in {"Dockerfile", "Containerfile", ".env.openevo-desktop"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for marker in forbidden:
            if marker in text:
                matches.append((raw_path, marker))
        for pattern in forbidden_patterns:
            if pattern.search(text):
                matches.append((raw_path, pattern.pattern))

    assert matches == []


def test_identity_audit_current_tree_is_clean() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/ci/audit_openevo_identity.py"],
        cwd=REPO_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        text=True,
    )
    report = json.loads(result.stdout)

    assert result.returncode == 0
    assert report["active_matches"] == []
    assert report["src_polar_exists"] is False
    assert report["src_legacy_evolution_exists"] is False
    assert report["web_exists"] is False


def test_identity_audit_fails_on_active_legacy_markers() -> None:
    bad_dir = REPO_ROOT / "examples" / "__openevo_identity_audit_fixture"
    bad_text = bad_dir / "Dockerfile"
    bad_path = bad_dir / "assets" / "polar_bad.bin"
    bad_svg = bad_dir / "diagram.svg"
    try:
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_text.write_text("from polar import legacy\n", encoding="utf-8")
        bad_path.write_bytes(b"legacy path marker")
        bad_svg.write_text("<svg><text>Polar</text></svg>\n", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, "scripts/ci/audit_openevo_identity.py"],
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            text=True,
        )
        report = json.loads(result.stdout)

        assert result.returncode == 1
        assert {
            "path": "examples/__openevo_identity_audit_fixture/Dockerfile",
            "marker": "from polar",
        } in report["active_matches"]
        assert {
            "path": "examples/__openevo_identity_audit_fixture/assets/polar_bad.bin",
            "marker": "assets/polar",
        } in report["active_matches"]
        assert {
            "path": "examples/__openevo_identity_audit_fixture/diagram.svg",
            "marker": "Polar",
        } in report["active_matches"]
    finally:
        bad_text.unlink(missing_ok=True)
        bad_path.unlink(missing_ok=True)
        bad_svg.unlink(missing_ok=True)
        (bad_dir / "assets").rmdir()
        bad_dir.rmdir()


def test_release_facing_docs_present_only_desktop_and_daemon() -> None:
    release_docs = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "docs" / "architecture" / "README.md",
        REPO_ROOT / "docs" / "core" / "backend-api.md",
        REPO_ROOT / "docs" / "maintainer" / "repository-structure.md",
    ]
    forbidden_phrases = (
        "three public surfaces",
        "OpenEvo Core Backend",
        "OpenEvo Dev Kit",
        "Dev Kit",
        "DevKit",
    )
    offenders: list[str] = []
    for path in release_docs:
        text = path.read_text(encoding="utf-8")
        for phrase in forbidden_phrases:
            if phrase in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {phrase}")

    assert offenders == []
    assert all(
        "OpenEvo Daemon" in path.read_text(encoding="utf-8")
        for path in release_docs
    )


def test_ordinary_user_docs_do_not_require_remote_shell_operations() -> None:
    user_docs = [
        REPO_ROOT / "docs" / "user" / "README.md",
        REPO_ROOT / "docs" / "user" / "desktop-quickstart.md",
        REPO_ROOT / "docs" / "user" / "remote-server-setup.md",
        REPO_ROOT / "docs" / "user" / "troubleshooting.md",
    ]
    forbidden = (
        "ssh-add",
        "codex login status",
        "codex --version",
        "openevo-backend",
        "Restart OpenEvo Daemon",
        "Stop the active Daemon",
    )
    offenders = [
        f"{path.relative_to(REPO_ROOT)}: {marker}"
        for path in user_docs
        for marker in forbidden
        if marker in path.read_text(encoding="utf-8")
    ]
    assert offenders == []

    combined = "\n".join(path.read_text(encoding="utf-8") for path in user_docs)
    assert "OpenEvo Desktop is the only application an ordinary user operates" in combined
    assert "operate the remote Daemon manually" in combined
    assert "server administrator" in combined


def test_ordinary_science_examples_use_desktop_managed_workspaces() -> None:
    local_folder = (
        REPO_ROOT / "examples" / "science-with-local-folder" / "README.md"
    ).read_text(encoding="utf-8")
    self_deployed = (
        REPO_ROOT / "examples" / "self-deployed-model" / "README.md"
    ).read_text(encoding="utf-8")

    assert "Choose a folder on the Mac" in local_folder
    assert "Daemon-managed" in local_folder
    assert "experiment.yaml" not in local_folder
    assert "Core Backend" not in local_folder
    assert "points at a user workspace folder on the remote server" not in local_folder

    assert "unavailable in the current Preview" in self_deployed
    assert "OpenEvo Daemon" in self_deployed
    assert "Core Backend" not in self_deployed
    assert "users will not SSH to the server" in self_deployed


def test_release_repository_metadata_is_present() -> None:
    required_paths = [
        REPO_ROOT / "CHANGELOG.md",
        REPO_ROOT / "CONTRIBUTING.md",
        REPO_ROOT / "SECURITY.md",
        REPO_ROOT / "docs" / "user" / "desktop-quickstart.md",
        REPO_ROOT / "docs" / "user" / "remote-server-setup.md",
        REPO_ROOT / "docs" / "user" / "proxy-and-network.md",
        REPO_ROOT / "docs" / "user" / "troubleshooting.md",
        REPO_ROOT / "docs" / "core" / "backend-api.md",
        REPO_ROOT / "docs" / "maintainer" / "release-process.md",
        REPO_ROOT / "docs" / "maintainer" / "testing.md",
        REPO_ROOT / "docs" / "maintainer" / "repository-structure.md",
    ]

    missing = [str(path.relative_to(REPO_ROOT)) for path in required_paths if not path.exists()]

    assert missing == []

    ignored: list[str] = []
    for path in required_paths:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path.relative_to(REPO_ROOT))],
            cwd=REPO_ROOT,
            check=False,
        )
        if result.returncode == 0:
            ignored.append(str(path.relative_to(REPO_ROOT)))

    assert ignored == []


def test_release_smoke_runs_for_productization_inputs() -> None:
    text = RELEASE_SMOKE_WORKFLOW.read_text(encoding="utf-8")
    required_path_filters = (
        '- "README.md"',
        '- "AGENTS.md"',
        '- "docs/user/**"',
        '- "docs/maintainer/**"',
        '- "examples/**"',
        '- "scripts/ci/**"',
        '- "tests/**"',
    )

    missing = [path_filter for path_filter in required_path_filters if path_filter not in text]

    assert missing == []
    assert "tests/ci/test_productization_spec.py" in text


def test_internal_process_history_is_not_under_public_superpowers_docs() -> None:
    assert not (REPO_ROOT / "docs" / "superpowers").exists()
    assert (REPO_ROOT / "docs" / "maintainer" / "productization" / "spec.md").exists()
    assert (
        REPO_ROOT / "docs" / "maintainer" / "productization" / "implementation-plan.md"
    ).exists()
    assert (REPO_ROOT / "docs" / "maintainer" / "development-history").exists()
