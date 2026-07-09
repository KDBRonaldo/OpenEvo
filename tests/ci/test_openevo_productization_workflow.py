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
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
    )
    scanned_roots = (
        "src/",
        "tests/",
        "scripts/",
        "examples/",
        "desktop/",
        "web/",
        "docs/architecture/",
        "docs/user/",
        "docs/maintainer/",
        "README.md",
        "AGENTS.md",
    )
    archived_roots = (
        "docs/maintainer/development-history/",
        "docs/maintainer/productization/",
        "docs/dev/",
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
        } and path.name not in {"Dockerfile", "Containerfile"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for marker in forbidden:
            if marker in text:
                matches.append((raw_path, marker))

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
    try:
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_text.write_text("from polar import legacy\n", encoding="utf-8")
        bad_path.write_bytes(b"legacy path marker")

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
    finally:
        bad_text.unlink(missing_ok=True)
        bad_path.unlink(missing_ok=True)
        (bad_dir / "assets").rmdir()
        bad_dir.rmdir()


def test_release_facing_docs_present_only_desktop_and_core_backend() -> None:
    release_docs = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "docs" / "architecture" / "README.md",
    ]
    forbidden_phrases = (
        "three public surfaces",
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


def test_internal_process_history_is_not_under_public_superpowers_docs() -> None:
    assert not (REPO_ROOT / "docs" / "superpowers").exists()
    assert (REPO_ROOT / "docs" / "maintainer" / "productization" / "spec.md").exists()
    assert (
        REPO_ROOT / "docs" / "maintainer" / "productization" / "implementation-plan.md"
    ).exists()
    assert (REPO_ROOT / "docs" / "maintainer" / "development-history").exists()
