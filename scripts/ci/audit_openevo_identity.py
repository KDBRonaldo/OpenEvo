from __future__ import annotations

import json
import subprocess
import sys
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
    ".rs",
    ".sh",
}
MARKERS = (
    "src/" + "polar",
    "src/" + "polar" + "_evolution",
    "POL" + "AR_",
    "/pol" + "ar/session",
    "pol" + "ar.session_completed",
    "polar-" + "evolution",
    "uv run " + "polar",
    "polar serve_",
    "polar dashboard",
    "from polar",
    "import polar",
)
ACTIVE_PREFIXES = (
    "src/",
    "tests/",
    "scripts/",
    "examples/",
    "web/",
    "docs/architecture/",
    "README.md",
    "AGENTS.md",
)
ARCHIVED_PREFIXES = (
    "docs/superpowers/",
    "docs/dev/",
    "README.polar.md",
)
IGNORED_PATHS = {
    "scripts/ci/audit_openevo_identity.py",
    "tests/ci/test_openevo_productization_workflow.py",
}


def _tracked_text_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
    )
    files: list[Path] = []
    for raw_path in result.stdout.decode().split("\0"):
        if not raw_path:
            continue
        path = REPO_ROOT / raw_path
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        files.append(path)
    return sorted(files)


def audit() -> dict[str, object]:
    matches: list[dict[str, str]] = []
    active_matches: list[dict[str, str]] = []
    archived_matches: list[dict[str, str]] = []
    for path in _tracked_text_files():
        relative = path.relative_to(REPO_ROOT)
        relative_text = str(relative)
        if relative_text in IGNORED_PATHS:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for marker in MARKERS:
            if marker in text:
                match = {"path": str(relative), "marker": marker}
                matches.append(match)
                if relative_text.startswith(ARCHIVED_PREFIXES):
                    archived_matches.append(match)
                elif relative_text.startswith(ACTIVE_PREFIXES):
                    active_matches.append(match)
    matches.sort(key=lambda match: (match["path"], match["marker"]))
    active_matches.sort(key=lambda match: (match["path"], match["marker"]))
    archived_matches.sort(key=lambda match: (match["path"], match["marker"]))
    return {
        "src_polar_exists": (REPO_ROOT / "src" / "polar").exists(),
        "src_legacy_evolution_exists": (
            REPO_ROOT / "src" / ("polar" + "_evolution")
        ).exists(),
        "web_exists": (REPO_ROOT / "web").exists(),
        "desktop_exists": (REPO_ROOT / "desktop").exists(),
        "active_matches": active_matches,
        "archived_matches": archived_matches,
        "matches": matches,
    }


def main() -> int:
    report = audit()
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
