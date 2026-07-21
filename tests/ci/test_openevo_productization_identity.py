from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".ts",
    ".tsx",
    ".svg",
    ".rs",
    ".sh",
    ".html",
    ".css",
    ".js",
    ".lock",
    ".txt",
}
TEXT_FILENAMES = {"Dockerfile", "Containerfile", ".env.openevo-desktop"}
ACTIVE_PREFIXES = (
    "src/",
    "scripts/",
    "examples/",
    "assets/",
    "desktop/",
    "web/",
    "docs/architecture/",
    ".github/",
    "docs/core/",
    "docs/user/",
    "docs/maintainer/",
    "README.md",
    "AGENTS.md",
    "pyproject.toml",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
)
ARCHIVED_PREFIXES = (
    "docs/maintainer/development-history/",
    "docs/maintainer/productization/",
    "docs/dev/",
)
ALLOWED_TEST_PATHS = {
    "scripts/ci/audit_openevo_identity.py",
    "tests/ci/test_openevo_productization_workflow.py",
    "tests/ci/test_openevo_productization_identity.py",
}
FORBIDDEN_TEXT_MARKERS = (
    "POL" + "AR_",
    "/pol" + "ar/session",
    "pol" + "ar.session_completed",
    "Polar",
    "polar_",
    "polar/",
    "polar:",
    "polar-",
    "polar.",
    ".pol" + "ar_evolution",
    "uv run " + "polar",
    "polar serve_",
    "polar dashboard",
    '"polar' + '_gateway"',
    "from polar",
    "import polar",
)
FORBIDDEN_PATH_MARKERS = (
    "README.polar.md",
    "assets/polar",
    "docs/superpowers",
    "docs/report",
    "polar_config.yaml",
    "polar-system-overview",
    "openevo-dev-kit",
    "slime_polar_async",
    "polar_stars",
    "polar_",
)
FORBIDDEN_TEXT_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bpolar[A-Za-z0-9_]+",
        r"\b[A-Za-z0-9_]+polar[A-Za-z0-9_]*",
        r"\bPOLAR[A-Z0-9_]*\b",
    )
)
CJK_TEXT_PATTERN = re.compile(r"[\u3400-\u9fff]")


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
    )
    return [
        REPO_ROOT / raw_path
        for raw_path in result.stdout.decode().split("\0")
        if raw_path and (REPO_ROOT / raw_path).is_file()
    ]


def _is_text_file(path: Path) -> bool:
    return path.suffix in TEXT_SUFFIXES or path.name in TEXT_FILENAMES


def _is_release_surface_path(relative_path: str) -> bool:
    return relative_path.startswith(ACTIVE_PREFIXES) and not relative_path.startswith(
        ARCHIVED_PREFIXES
    )


def test_no_legacy_polar_packages_remain() -> None:
    assert not (REPO_ROOT / "src" / "polar").exists()
    assert not (REPO_ROOT / "src" / "polar_evolution").exists()


def test_only_backend_maintenance_scripts_are_public() -> None:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)

    scripts = pyproject.get("project", {}).get("scripts", {})

    assert "polar" not in scripts
    assert "polar-evolution" not in scripts
    assert "openevo" not in scripts
    assert scripts == {
        "openevo-backend": "openevo.backend.launcher:main",
        "openevo-core-service": "openevo.backend.service:main",
    }


def test_no_public_polar_runtime_identity_remains() -> None:
    offenders: list[str] = []
    for path in _tracked_files():
        relative_path = path.relative_to(REPO_ROOT).as_posix()
        if relative_path in ALLOWED_TEST_PATHS:
            continue
        if not _is_release_surface_path(relative_path):
            continue
        for marker in FORBIDDEN_PATH_MARKERS:
            if marker in relative_path:
                offenders.append(f"{relative_path}: path contains {marker}")
        if not _is_text_file(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for marker in FORBIDDEN_TEXT_MARKERS:
            if marker in text:
                offenders.append(f"{relative_path}: {marker}")
        for pattern in FORBIDDEN_TEXT_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{relative_path}: {pattern.pattern}")

    assert offenders == []


def test_identity_audit_reports_clean_active_surface() -> None:
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
    assert report["desktop_exists"] is True


def test_desktop_is_top_level_product_surface() -> None:
    assert not (REPO_ROOT / "web").exists()
    assert (REPO_ROOT / "desktop" / "src-tauri").is_dir()
    assert (REPO_ROOT / "desktop" / "src").is_dir()
    assert (REPO_ROOT / "desktop" / "sidecar").is_dir()
    assert not (REPO_ROOT / "src" / "openevo" / "desktop").exists()


def test_desktop_release_ui_copy_is_english() -> None:
    offenders: list[str] = []
    for path in (REPO_ROOT / "desktop" / "src").rglob("*"):
        if not path.is_file() or path.suffix not in {".ts", ".tsx"}:
            continue
        if ".test." in path.name:
            continue
        text = path.read_text(encoding="utf-8")
        if CJK_TEXT_PATTERN.search(text):
            offenders.append(path.relative_to(REPO_ROOT).as_posix())

    assert offenders == []
