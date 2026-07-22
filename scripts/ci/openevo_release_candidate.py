#!/usr/bin/env python3
"""Create and verify one exact unsigned OpenEvo Desktop release candidate."""

from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any
from urllib.parse import quote, urlsplit
from zipfile import BadZipFile, ZipFile


MANIFEST_NAME = "release-candidate.json"
CORE_DESCRIPTOR_NAME = "core-install-artifact.json"
CHECKSUMS_NAME = "SHA256SUMS"
MANAGED_RUNTIME_SOURCE_NAME = "managed-runtime-source.json"
PLAYWRIGHT_EVIDENCE_NAME = "playwright-candidate-evidence.json"
PLAYWRIGHT_REPORT_NAME = "playwright-report.json"
PACKAGED_WEB_MANIFEST_NAME = "packaged-web-manifest.json"
DAEMON_BUNDLE_NAME = "openevo-daemon-linux-x86_64"
DAEMON_MANIFEST_NAME = "openevo-daemon-bundle.json"
DAEMON_MOUNTED_EVIDENCE_NAME = "daemon-mounted-resource.json"
DAEMON_COPY_EVIDENCE_NAME = "daemon-copy-resource.json"
RELEASE_ASSETS_RESOURCE_ROOT = "Contents/Resources/openevo-release-assets"
RELEASE_ASSETS_MANIFEST_NAME = "release-assets.json"
MINIMUM_MACOS_VERSION = "12.0"
RUST_TOOLCHAIN_VERSION = "1.95.0"
TAURI_EXECUTABLE_NAME = "openevo-desktop"
CORE_PYTHON_REQUIRES = ">=3.11"
CORE_SUPPORTED_PLATFORMS = ("linux-x86_64",)
MANAGED_RUNTIME_REPOSITORY = "CompLifeLab-ZJU/OpenEvo"
MANAGED_RUNTIME_RELEASE_ID = 356072935
MANAGED_RUNTIME_RELEASE_TAG = "openevo-managed-runtime-assets-v0.1.1"
MANAGED_RUNTIME_ASSET_ID = 481361975
MANAGED_RUNTIME_ARCHIVE_NAME = "openevo-science-runtime-0.1.1-linux-amd64.tar.gz"
MANAGED_RUNTIME_ARCHIVE_SIZE = 352_236_726
MANAGED_RUNTIME_ARCHIVE_SHA256 = "ad9c5ebd69b5785b94dd52dc077d93ababfa9cf8cbcbf92940f60bee48a91149"
MANAGED_RUNTIME_CONFIG_ID = (
    "sha256:0e5783e7839fe06d2df14d7a431c90f0982ca2099ef33bfa4c9e5933149bf5f2"
)
MANAGED_RUNTIME_OCI_INDEX_ID = (
    "sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b"
)
MANAGED_RUNTIME_ALIAS = "openevo/science-runtime:0.1.1"
MANAGED_RUNTIME_LABEL = "io.openevo.managed-runtime"
MANAGED_RUNTIME_LABEL_VALUE = "true"
MANAGED_RUNTIME_PLATFORM = "linux-amd64"
MANAGED_RUNTIME_EXECUTION_MODE = "codex_subscription_transcript"
REPO_ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE_TARGETS = {
    "aarch64": "aarch64-apple-darwin",
    "x64": "x86_64-apple-darwin",
}
ARCHITECTURE_SLICES = {"aarch64": "arm64", "x64": "x86_64"}
SOURCE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
OWNERSHIP_TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")
CHROMIUM_VERSION_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}")
PLAYWRIGHT_VIEWPORTS = {
    "release-packaged-1440": {"height": 900, "width": 1440},
    "release-packaged-1024": {"height": 768, "width": 1024},
    "release-packaged-760": {"height": 600, "width": 760},
}
PLAYWRIGHT_REQUIRED_CASES = frozenset(
    (
        project,
        file,
        title,
    )
    for projects, file, title in (
        (
            (
                "release-packaged-1440",
                "release-packaged-1024",
                "release-packaged-760",
            ),
            "release-readonly.pw.ts",
            "first launch uses the release sidecar composition and keeps demo navigation non-mutating",
        ),
    )
    for project in projects
)
REQUIRED_INPUT_ROLES = (
    ("desktop_dmg", None),
    ("core_wheel", None),
    ("framework_lock", "framework-lock.json"),
    ("daemon_bundle", DAEMON_BUNDLE_NAME),
    ("daemon_manifest", DAEMON_MANIFEST_NAME),
    ("daemon_mounted_resource", DAEMON_MOUNTED_EVIDENCE_NAME),
    ("daemon_copy_resource", DAEMON_COPY_EVIDENCE_NAME),
    ("release_notes", "release-notes.md"),
    ("dependency_inventory", "dependency-inventory.json"),
    ("license_inventory", "license-inventory.json"),
    ("security_audit", "security-audit.json"),
    ("python_requirements", "python-requirements.txt"),
    ("app_bundle_smoke", "app-bundle-smoke.json"),
    ("dmg_copy_smoke", "dmg-copy-smoke.json"),
    ("managed_runtime_source", MANAGED_RUNTIME_SOURCE_NAME),
    ("playwright_evidence", PLAYWRIGHT_EVIDENCE_NAME),
    ("playwright_report", PLAYWRIGHT_REPORT_NAME),
    ("packaged_web_manifest", PACKAGED_WEB_MANIFEST_NAME),
)
FINAL_ROLES = tuple(role for role, _name in REQUIRED_INPUT_ROLES) + (
    "core_descriptor",
    "checksums",
)
DRAFT_RELEASE_METADATA_KEYS = {
    "apiUrl",
    "body",
    "isDraft",
    "isPrerelease",
    "name",
    "tagName",
    "targetCommitish",
    "url",
}
PREVIEW_RELEASE_METADATA_KEYS = {
    "assets",
    "body",
    "draft",
    "html_url",
    "id",
    "immutable",
    "name",
    "prerelease",
    "tag_name",
    "target_commitish",
}
PREVIEW_RELEASE_ASSET_KEYS = {
    "digest",
    "id",
    "name",
    "size",
    "state",
}
PREVIEW_RELEASE_SNAPSHOT_KEYS = {
    "assets",
    "body",
    "candidate_workflow",
    "draft",
    "manifest_sha256",
    "prerelease",
    "release_id",
    "repository",
    "schema_version",
    "source_commit",
    "tag",
    "target_commitish",
    "title",
}
CANDIDATE_WORKFLOW_RUN_KEYS = {
    "conclusion",
    "event",
    "head_branch",
    "head_sha",
    "id",
    "path",
    "repository",
    "run_attempt",
    "status",
}


class CandidateError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _canonical_json(payload: object) -> bytes:
    return (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _write_new(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as stream:
            stream.write(payload)
    except FileExistsError as exc:
        raise CandidateError(f"Refusing to replace existing candidate file: {path.name}") from exc


def _write_private_new(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise CandidateError(f"Refusing to replace existing candidate file: {path.name}") from exc
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _load_json(path: Path) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in pairs:
            if key in payload:
                raise CandidateError(
                    f"Candidate JSON contains a duplicate key in {path.name}: {key}"
                )
            payload[key] = value
        return payload

    try:
        if path.stat().st_size > 1024 * 1024:
            raise CandidateError(f"Candidate JSON exceeds 1 MiB: {path.name}")
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateError(f"Candidate JSON is unreadable: {path.name}") from exc


def _require_digest(value: object, subject: str) -> str:
    if type(value) is not str or DIGEST_PATTERN.fullmatch(value) is None:
        raise CandidateError(f"{subject} must be a lowercase SHA-256 digest")
    return value


def _require_safe_basename(value: object, subject: str) -> str:
    if (
        type(value) is not str
        or not value
        or Path(value).name != value
        or value in {".", ".."}
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise CandidateError(f"{subject} must be one safe filename")
    return value


def _managed_runtime_source_evidence() -> dict[str, object]:
    return {
        "asset": {
            "api_digest": f"sha256:{MANAGED_RUNTIME_ARCHIVE_SHA256}",
            "byte_size": MANAGED_RUNTIME_ARCHIVE_SIZE,
            "download_sha256": MANAGED_RUNTIME_ARCHIVE_SHA256,
            "id": MANAGED_RUNTIME_ASSET_ID,
            "name": MANAGED_RUNTIME_ARCHIVE_NAME,
        },
        "image": {
            "config_id": MANAGED_RUNTIME_CONFIG_ID,
            "oci_index_digest": MANAGED_RUNTIME_OCI_INDEX_ID,
        },
        "release": {
            "id": MANAGED_RUNTIME_RELEASE_ID,
            "is_draft": False,
            "is_prerelease": True,
            "tag": MANAGED_RUNTIME_RELEASE_TAG,
        },
        "repository": MANAGED_RUNTIME_REPOSITORY,
        "schema_version": 1,
    }


def _managed_runtime_manifest() -> dict[str, object]:
    source = _managed_runtime_source_evidence()
    asset = source["asset"]
    assert isinstance(asset, dict)
    return {
        "archive": {
            "byte_size": asset["byte_size"],
            "filename": asset["name"],
            "sha256": asset["download_sha256"],
        },
        "asset": {
            "api_digest": asset["api_digest"],
            "id": asset["id"],
        },
        "capability": {
            "capture_mode": "transcript",
            "execution_mode": MANAGED_RUNTIME_EXECUTION_MODE,
            "harness_id": "codex",
            "token_level_metrics_available": False,
        },
        "image": {
            "config_id": MANAGED_RUNTIME_CONFIG_ID,
            "loaded_image_id": MANAGED_RUNTIME_OCI_INDEX_ID,
            "managed_label": {
                MANAGED_RUNTIME_LABEL: MANAGED_RUNTIME_LABEL_VALUE,
            },
            "platform": MANAGED_RUNTIME_PLATFORM,
            "runtime_alias": MANAGED_RUNTIME_ALIAS,
        },
        "release": source["release"],
    }


def _validate_packaged_web_manifest(path: Path) -> dict[str, object]:
    manifest = _load_json(path)
    if type(manifest) is not dict or set(manifest) != {
        "build_digest",
        "files",
        "schema_version",
    }:
        raise CandidateError("packaged web manifest does not use the closed schema")
    files = manifest.get("files")
    if manifest.get("schema_version") != "1" or type(files) is not list or not files:
        raise CandidateError("packaged web manifest is invalid")
    normalized_files: list[dict[str, object]] = []
    paths: set[str] = set()
    for entry in files:
        if type(entry) is not dict or set(entry) != {"byte_size", "path", "sha256"}:
            raise CandidateError("packaged web manifest file entry is invalid")
        relative_path = entry.get("path")
        byte_size = entry.get("byte_size")
        if (
            type(relative_path) is not str
            or not relative_path
            or not relative_path.isascii()
            or relative_path.startswith("/")
            or "\\" in relative_path
            or any(part in {"", ".", ".."} for part in relative_path.split("/"))
            or relative_path in paths
        ):
            raise CandidateError("packaged web manifest paths are invalid")
        if type(byte_size) is not int or byte_size < 1:
            raise CandidateError("packaged web manifest file size is invalid")
        digest = _require_digest(entry.get("sha256"), "packaged web file digest")
        paths.add(relative_path)
        normalized_files.append({"path": relative_path, "sha256": digest, "byte_size": byte_size})
    if normalized_files != sorted(normalized_files, key=lambda entry: str(entry["path"])):
        raise CandidateError("packaged web manifest file order is not canonical")
    expected_build_digest = hashlib.sha256(
        json.dumps(
            normalized_files,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if manifest.get("build_digest") != expected_build_digest:
        raise CandidateError("packaged web build digest is invalid")
    return manifest


def _playwright_test_results(report_path: Path) -> list[dict[str, object]]:
    report = _load_json(report_path)
    if type(report) is not dict:
        raise CandidateError("Playwright report must be a JSON object")
    config = report.get("config")
    suites = report.get("suites")
    stats = report.get("stats")
    errors = report.get("errors")
    if (
        type(config) is not dict
        or type(suites) is not list
        or not suites
        or type(stats) is not dict
        or errors != []
    ):
        raise CandidateError("Playwright report is incomplete or contains errors")
    projects = config.get("projects")
    if type(projects) is not list:
        raise CandidateError("Playwright report project inventory is invalid")
    project_ids: list[str] = []
    for project in projects:
        if type(project) is not dict:
            raise CandidateError("Playwright report project entry is invalid")
        project_id = project.get("name")
        if (
            type(project_id) is not str
            or project.get("id") not in {None, project_id}
            or project_id not in PLAYWRIGHT_VIEWPORTS
            or project_id in project_ids
        ):
            raise CandidateError("Playwright report project identity is invalid")
        project_ids.append(project_id)
    if set(project_ids) != set(PLAYWRIGHT_VIEWPORTS):
        raise CandidateError("Playwright report does not cover the closed viewport matrix")

    results: list[dict[str, object]] = []
    seen: set[tuple[str, int, str, str]] = set()
    for suite in suites:
        if type(suite) is not dict or type(suite.get("specs")) is not list:
            raise CandidateError("Playwright report suite is invalid")
        for spec in suite["specs"]:
            if type(spec) is not dict:
                raise CandidateError("Playwright report test specification is invalid")
            file = spec.get("file")
            line = spec.get("line")
            title = spec.get("title")
            tests = spec.get("tests")
            if (
                spec.get("ok") is not True
                or type(file) is not str
                or not file
                or not file.isascii()
                or Path(file).name != file
                or not file.endswith(".pw.ts")
                or type(line) is not int
                or line < 1
                or type(title) is not str
                or not title
                or len(title) > 512
                or type(tests) is not list
                or len(tests) != 1
            ):
                raise CandidateError(
                    "Playwright test specification did not pass the closed contract"
                )
            test = tests[0]
            if type(test) is not dict:
                raise CandidateError("Playwright test result is invalid")
            project_id = test.get("projectName")
            attempts = test.get("results")
            if (
                type(project_id) is not str
                or project_id not in PLAYWRIGHT_VIEWPORTS
                or test.get("projectId") not in {None, project_id}
                or test.get("expectedStatus") != "passed"
                or test.get("status") != "expected"
                or test.get("annotations") != []
                or type(attempts) is not list
                or len(attempts) != 1
                or type(attempts[0]) is not dict
                or attempts[0].get("status") != "passed"
                or attempts[0].get("retry") != 0
            ):
                raise CandidateError("Playwright test result is not a first-attempt pass")
            identity = (file, line, title, project_id)
            if identity in seen:
                raise CandidateError("Playwright report contains a duplicate test result")
            seen.add(identity)
            results.append(
                {
                    "file": file,
                    "line": line,
                    "project": project_id,
                    "retry": 0,
                    "status": "passed",
                    "title": title,
                    "viewport": PLAYWRIGHT_VIEWPORTS[project_id],
                }
            )
    results.sort(
        key=lambda entry: (
            str(entry["file"]),
            int(entry["line"]),
            str(entry["title"]),
            str(entry["project"]),
        )
    )
    if not results:
        raise CandidateError("Playwright report contains no test results")
    observed_cases = {
        (str(entry["project"]), str(entry["file"]), str(entry["title"])) for entry in results
    }
    if observed_cases != PLAYWRIGHT_REQUIRED_CASES:
        raise CandidateError("Playwright report does not cover the exact candidate test matrix")
    expected_count = len(results)
    if (
        stats.get("expected") != expected_count
        or stats.get("skipped") != 0
        or stats.get("unexpected") != 0
        or stats.get("flaky") != 0
    ):
        raise CandidateError("Playwright aggregate status is not a complete pass")
    return results


def _sanitized_playwright_report(
    tests: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "composition": "packaged_web",
        "projects": [
            {
                "name": name,
                "viewport": PLAYWRIGHT_VIEWPORTS[name],
            }
            for name in sorted(PLAYWRIGHT_VIEWPORTS)
        ],
        "provider_kind": "desktop_sidecar",
        "schema_version": 2,
        "simulator": False,
        "status": "passed",
        "summary": {
            "expected": len(PLAYWRIGHT_REQUIRED_CASES),
            "flaky": 0,
            "skipped": 0,
            "unexpected": 0,
        },
        "tests": tests,
    }


def _validate_sanitized_playwright_report(
    report_path: Path,
) -> list[dict[str, object]]:
    report = _load_json(report_path)
    if type(report) is not dict or set(report) != {
        "composition",
        "projects",
        "provider_kind",
        "schema_version",
        "simulator",
        "status",
        "summary",
        "tests",
    }:
        raise CandidateError("sanitized Playwright report does not use the closed schema")
    if report_path.read_bytes() != _canonical_json(report):
        raise CandidateError("sanitized Playwright report is not canonical")
    expected_projects = [
        {"name": name, "viewport": PLAYWRIGHT_VIEWPORTS[name]}
        for name in sorted(PLAYWRIGHT_VIEWPORTS)
    ]
    if (
        report.get("schema_version") != 2
        or report.get("simulator") is not False
        or report.get("provider_kind") != "desktop_sidecar"
        or report.get("composition") != "packaged_web"
        or report.get("status") != "passed"
        or report.get("projects") != expected_projects
        or report.get("summary")
        != {
            "expected": len(PLAYWRIGHT_REQUIRED_CASES),
            "flaky": 0,
            "skipped": 0,
            "unexpected": 0,
        }
    ):
        raise CandidateError("sanitized Playwright report identity or status is invalid")
    tests = report.get("tests")
    if type(tests) is not list or len(tests) != len(PLAYWRIGHT_REQUIRED_CASES):
        raise CandidateError("sanitized Playwright report test inventory is incomplete")
    normalized: list[dict[str, object]] = []
    for entry in tests:
        if type(entry) is not dict or set(entry) != {
            "file",
            "line",
            "project",
            "retry",
            "status",
            "title",
            "viewport",
        }:
            raise CandidateError("sanitized Playwright test entry is invalid")
        project = entry.get("project")
        file = entry.get("file")
        line = entry.get("line")
        title = entry.get("title")
        if (
            type(project) is not str
            or project not in PLAYWRIGHT_VIEWPORTS
            or type(file) is not str
            or not file.isascii()
            or Path(file).name != file
            or not file.endswith(".pw.ts")
            or type(line) is not int
            or line < 1
            or type(title) is not str
            or not title
            or len(title) > 512
            or entry.get("retry") != 0
            or entry.get("status") != "passed"
            or entry.get("viewport") != PLAYWRIGHT_VIEWPORTS[project]
        ):
            raise CandidateError("sanitized Playwright test entry is invalid")
        normalized.append(entry)
    expected_order = sorted(
        normalized,
        key=lambda entry: (
            str(entry["file"]),
            int(entry["line"]),
            str(entry["title"]),
            str(entry["project"]),
        ),
    )
    observed_cases = {
        (str(entry["project"]), str(entry["file"]), str(entry["title"])) for entry in normalized
    }
    if normalized != expected_order or observed_cases != PLAYWRIGHT_REQUIRED_CASES:
        raise CandidateError("sanitized Playwright report does not cover the exact test matrix")
    return normalized


def _expected_playwright_evidence(
    *,
    report_path: Path,
    packaged_web_manifest_path: Path,
    source_commit: str,
    run_id: int,
    run_attempt: int,
    browser_version: str,
) -> dict[str, object]:
    if SOURCE_COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise CandidateError("Playwright source commit must be one full lowercase Git commit")
    if type(run_id) is not int or run_id < 1 or type(run_attempt) is not int or run_attempt < 1:
        raise CandidateError("Playwright run identity must contain positive integers")
    if (
        type(browser_version) is not str
        or CHROMIUM_VERSION_PATTERN.fullmatch(browser_version) is None
    ):
        raise CandidateError("Playwright Chromium version is invalid")
    manifest = _validate_packaged_web_manifest(packaged_web_manifest_path)
    return {
        "browser": {"name": "chromium", "version": browser_version},
        "composition": "packaged_web",
        "packaged_web": {
            "build_digest": manifest["build_digest"],
            "manifest": _file_entry(
                "packaged_web_manifest",
                packaged_web_manifest_path,
            ),
        },
        "provider_kind": "desktop_sidecar",
        "report": _file_entry("playwright_report", report_path),
        "run": {"attempt": run_attempt, "id": run_id},
        "schema_version": 2,
        "simulator": False,
        "source_commit": source_commit,
        "status": "passed",
        "tests": _validate_sanitized_playwright_report(report_path),
    }


def write_playwright_candidate_evidence(
    path: Path,
    *,
    raw_report_path: Path,
    sanitized_report_path: Path,
    packaged_web_manifest_path: Path,
    source_commit: str,
    run_id: int,
    run_attempt: int,
    browser_version: str,
) -> None:
    tests = _playwright_test_results(raw_report_path)
    _write_new(
        sanitized_report_path,
        _canonical_json(_sanitized_playwright_report(tests)),
    )
    evidence = _expected_playwright_evidence(
        report_path=sanitized_report_path,
        packaged_web_manifest_path=packaged_web_manifest_path,
        source_commit=source_commit,
        run_id=run_id,
        run_attempt=run_attempt,
        browser_version=browser_version,
    )
    _write_new(path, _canonical_json(evidence))


def _validate_playwright_candidate_evidence(
    evidence_path: Path,
    *,
    report_path: Path,
    packaged_web_manifest_path: Path,
    expected_source_commit: str | None = None,
    expected_run_id: int | None = None,
    expected_run_attempt: int | None = None,
) -> None:
    evidence = _load_json(evidence_path)
    if type(evidence) is not dict or set(evidence) != {
        "browser",
        "composition",
        "packaged_web",
        "provider_kind",
        "report",
        "run",
        "schema_version",
        "simulator",
        "source_commit",
        "status",
        "tests",
    }:
        raise CandidateError("Playwright evidence does not use the closed candidate schema")
    browser = evidence.get("browser")
    run = evidence.get("run")
    if (
        evidence.get("schema_version") != 2
        or evidence.get("simulator") is not False
        or evidence.get("provider_kind") != "desktop_sidecar"
        or evidence.get("composition") != "packaged_web"
        or evidence.get("status") != "passed"
        or type(browser) is not dict
        or set(browser) != {"name", "version"}
        or browser.get("name") != "chromium"
        or type(run) is not dict
        or set(run) != {"attempt", "id"}
    ):
        raise CandidateError("Playwright evidence identity or status is invalid")
    source_commit = evidence.get("source_commit")
    if (
        type(source_commit) is not str
        or SOURCE_COMMIT_PATTERN.fullmatch(source_commit) is None
        or expected_source_commit is not None
        and source_commit != expected_source_commit
        or expected_run_id is not None
        and run.get("id") != expected_run_id
        or expected_run_attempt is not None
        and run.get("attempt") != expected_run_attempt
    ):
        raise CandidateError("Playwright evidence is bound to a different candidate run")
    expected = _expected_playwright_evidence(
        report_path=report_path,
        packaged_web_manifest_path=packaged_web_manifest_path,
        source_commit=source_commit,
        run_id=run.get("id"),
        run_attempt=run.get("attempt"),
        browser_version=browser.get("version"),
    )
    if evidence != expected or evidence_path.read_bytes() != _canonical_json(evidence):
        raise CandidateError(
            "Playwright evidence does not match its report and packaged web build"
        )


def render_candidate_release_notes(
    *,
    source_commit: str,
    version: str,
    architecture: str,
) -> str:
    if SOURCE_COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise CandidateError("source_commit must be one full lowercase Git commit")
    if not version or any(character.isspace() for character in version):
        raise CandidateError("candidate version is invalid")
    if architecture not in ARCHITECTURE_TARGETS:
        raise CandidateError("candidate architecture must be an actual supported architecture")
    return "\n".join(
        (
            f"# OpenEvo Desktop {version} Preview",
            "",
            f"Source commit: {source_commit}",
            f"Architecture: {architecture}",
            f"Minimum macOS: {MINIMUM_MACOS_VERSION}",
            "",
            "This Preview is Developer ID unsigned and not notarized. Its app bundle is ad-hoc signed for integrity, and the documented browser-quarantine removal path is validated by the packaging workflow.",
            "",
            "## Supported Workflows",
            "",
            "Codex subscription transcript mode: packaged and declared in this Preview.",
            "Candidate-bound real Codex Subscription science E2E: required before public Preview publication.",
            "A public Preview carrying these notes has passed the separate signed publication gate for a real remote OpenEvo Daemon, subscription-authenticated Codex, transcript capture, two science sessions, evolution artifacts, cross-session artifact reuse, and Desktop renderer observability. A candidate that has not passed that gate is not public.",
            "Self-Deployed Reference mode: unavailable in this Preview.",
            "The shipped Desktop release authority blocks saving or running that mode; its Core-side reference architecture is not a Desktop product claim.",
            f"Managed Science runtime archive: {MANAGED_RUNTIME_ARCHIVE_NAME}.",
            f"Managed Science runtime archive size: {MANAGED_RUNTIME_ARCHIVE_SIZE}.",
            f"Managed Science runtime archive SHA-256: {MANAGED_RUNTIME_ARCHIVE_SHA256}.",
            f"Managed Science runtime source asset ID: {MANAGED_RUNTIME_ASSET_ID}.",
            f"Managed Science runtime loaded image ID: {MANAGED_RUNTIME_OCI_INDEX_ID}.",
            "",
            "## Known Limitations",
            "",
            "Parameter evolution is not included in this Preview.",
            "PyPI is not used for this release.",
            "Only the declared architecture was built.",
            "The interactive Privacy & Security allow flow is not automated; command-line quarantine removal is validated.",
            "This unsigned Preview does not satisfy the benchmark, full secret-canary/privacy, Developer ID signing, notarization, or final External Beta gates. The public Preview science E2E claim is limited to the separately signed publication evidence described above.",
            "",
            "## Validation Results",
            "",
            "Benchmark gates completed by this Preview: 0 of 3.",
            "Textual-memory pass@1 rescue count: pending.",
            "Trajectory-to-skill pass@1 rescue count: pending.",
            "Agent-system pass@1 rescue count: pending.",
            "No benchmark performance claim is made by this Preview.",
            "The packaging workflow validates the exact Core wheel, Preview DMG, mounted app and detached copy, packaged macOS sidecar, embedded subscription Science runtime, declared subscription capability, packaged Desktop web build, packaged release-composition Playwright interaction result, source evidence, dependency evidence, and downloaded release assets. Public publication additionally requires signed cross-platform science-run evidence bound to those candidate identities.",
            "",
            "## Security And Privacy",
            "",
            "No analytics, crash reporting, telemetry, or diagnostics upload is enabled by default.",
            "Credential-canary verification for release assets: pending.",
            "This workflow does not claim credential-free assets until the separate secret-canary gate passes.",
            "Diagnostics sharing is explicit. Science data can be sent to the remote server and harness or model provider selected by the user. The full secret-canary/privacy release gate remains pending.",
            "",
            "## Install, Upgrade, And Uninstall",
            "",
            'Install: copy OpenEvo Desktop to Applications, run `xattr -dr com.apple.quarantine "/Applications/OpenEvo Desktop.app"`, then open it. This workflow validates synthetic browser quarantine, that documented removal command, the ad-hoc app signature, and launch; the interactive Privacy & Security UI remains unvalidated.',
            "Upgrade: this Preview has no automatic updater; quit the app and replace it with a newer reviewed DMG. OpenEvo Daemon upgrade compatibility is not proven by this packaging-only Preview.",
            "Uninstall: quit OpenEvo Desktop and remove it from Applications. Current local Desktop data under ~/Library/Application Support/org.openevo.desktop, including run-retry recovery state, is retained unless deleted separately. Legacy Preview data under ~/.openevo/desktop is preserved without being read and is also retained unless deleted separately. OpenEvo Daemon state, task data, model downloads, and runtime caches are also retained.",
            "",
        )
    )


def write_candidate_release_notes(
    path: Path,
    *,
    source_commit: str,
    version: str,
    architecture: str,
) -> None:
    payload = render_candidate_release_notes(
        source_commit=source_commit,
        version=version,
        architecture=architecture,
    )
    _write_new(path, payload.encode("utf-8"))


def render_draft_release_body(*, release_notes: str, ownership_token: str) -> str:
    if OWNERSHIP_TOKEN_PATTERN.fullmatch(ownership_token) is None:
        raise CandidateError("draft ownership token must be 128-bit lowercase hex")
    if not release_notes:
        raise CandidateError("draft release notes must not be empty")
    return release_notes.rstrip("\n") + f"\n\n<!-- openevo-draft-owner:{ownership_token} -->\n"


def write_draft_release_body(
    path: Path,
    *,
    release_notes: Path,
    ownership_token: str,
) -> None:
    try:
        notes = release_notes.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CandidateError("Release notes are unreadable") from exc
    body = render_draft_release_body(
        release_notes=notes,
        ownership_token=ownership_token,
    )
    _write_new(path, body.encode("utf-8"))


def assert_release_tag_absent(inventory_path: Path, *, expected_tag: str) -> None:
    if not expected_tag or "\n" in expected_tag or "\r" in expected_tag:
        raise CandidateError("Expected release tag is invalid")
    try:
        lines = inventory_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise CandidateError("GitHub release inventory is unreadable") from exc
    for line_number, line in enumerate(lines, start=1):
        try:
            tag = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CandidateError(
                f"GitHub release inventory line {line_number} is invalid"
            ) from exc
        if type(tag) is not str or not tag:
            raise CandidateError(f"GitHub release inventory line {line_number} is not a tag name")
        if tag == expected_tag:
            raise CandidateError(f"GitHub release already exists: {expected_tag}")


def _single_match(root: Path, pattern: str, subject: str) -> Path:
    matches = sorted(root.glob(pattern))
    if len(matches) != 1 or not _is_regular_file(matches[0]):
        raise CandidateError(f"Candidate must contain exactly one {subject}; found {len(matches)}")
    return matches[0]


def _validate_wheel(wheel: Path, *, version: str) -> None:
    try:
        with ZipFile(wheel) as archive:
            names = archive.namelist()
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            entry_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
            if len(metadata_names) != 1 or len(entry_names) != 1:
                raise CandidateError("Core wheel metadata or entry points are incomplete")
            metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
            entry_points = archive.read(entry_names[0]).decode("utf-8")
    except (BadZipFile, OSError, UnicodeDecodeError) as exc:
        raise CandidateError("Core wheel is unreadable") from exc
    if metadata.get("Name") != "openevo" or metadata.get("Version") != version:
        raise CandidateError("Core wheel name/version does not match the candidate")
    if metadata.get("Requires-Python") != CORE_PYTHON_REQUIRES:
        raise CandidateError("Core wheel Python compatibility does not match the release contract")
    required = (
        "openevo-backend = openevo.backend.launcher:main",
        "openevo-core-service = openevo.backend.service:main",
    )
    if any(entry not in entry_points for entry in required):
        raise CandidateError("Core wheel does not expose the required backend launchers")


def _validate_framework_lock(lock_path: Path, wheel: Path, *, version: str) -> dict[str, object]:
    lock = _load_json(lock_path)
    required_keys = {
        "schema_version",
        "distribution",
        "distribution_version",
        "distribution_digest",
        "wheel_filename",
    }
    if type(lock) is not dict or set(lock) != required_keys:
        raise CandidateError("framework-lock.json does not use the closed release schema")
    if (
        lock.get("schema_version") != "1"
        or lock.get("distribution") != "openevo"
        or lock.get("distribution_version") != version
        or lock.get("wheel_filename") != wheel.name
        or lock.get("distribution_digest") != _sha256(wheel)
    ):
        raise CandidateError("framework-lock.json does not bind the exact Core wheel")
    return lock


def validate_daemon_release_inputs(
    *,
    bundle: Path,
    manifest_path: Path,
    wheel: Path,
    framework_lock: Path,
    source_commit: str,
    registry_digest: str,
) -> dict[str, object]:
    if not _is_regular_file(bundle) or bundle.name != DAEMON_BUNDLE_NAME:
        raise CandidateError("Daemon bundle must be the canonical regular release binary")
    if not _is_regular_file(manifest_path) or manifest_path.name != DAEMON_MANIFEST_NAME:
        raise CandidateError("Daemon manifest must be the canonical regular release manifest")
    _require_digest(registry_digest, "Daemon registry digest")
    manifest = _load_json(manifest_path)
    expected_keys = {
        "artifact",
        "build_environment_distributions",
        "core",
        "dependency_lock",
        "platform",
        "release",
        "runtime",
        "schema_version",
        "smoke",
    }
    if type(manifest) is not dict or set(manifest) != expected_keys:
        raise CandidateError("Daemon manifest does not use the closed release schema")
    if manifest_path.read_bytes() != _canonical_json(manifest):
        raise CandidateError("Daemon manifest is not canonical")

    artifact = manifest.get("artifact")
    if (
        type(artifact) is not dict
        or set(artifact) != {"filename", "sha256", "size"}
        or artifact.get("filename") != DAEMON_BUNDLE_NAME
        or artifact.get("sha256") != _sha256(bundle)
        or artifact.get("size") != bundle.stat().st_size
        or type(artifact.get("size")) is not int
        or artifact["size"] < 1
    ):
        raise CandidateError("Daemon manifest does not bind the exact bundle")

    version = _wheel_version(wheel)
    _validate_wheel(wheel, version=version)
    lock = _validate_framework_lock(
        framework_lock,
        wheel,
        version=version,
    )
    core = manifest.get("core")
    if (
        type(core) is not dict
        or set(core) != {"framework_lock", "registry_digest", "wheel"}
        or core.get("framework_lock")
        != {
            "filename": framework_lock.name,
            "sha256": _sha256(framework_lock),
        }
        or core.get("registry_digest") != registry_digest
        or core.get("wheel")
        != {
            "filename": wheel.name,
            "sha256": _sha256(wheel),
            "size": wheel.stat().st_size,
            "version": lock["distribution_version"],
        }
    ):
        raise CandidateError("Daemon manifest does not bind the candidate Core wheel and lock")

    dependency_lock = manifest.get("dependency_lock")
    uv_lock = REPO_ROOT / "uv.lock"
    if dependency_lock != {"filename": "uv.lock", "sha256": _sha256(uv_lock)}:
        raise CandidateError("Daemon manifest does not bind the checkout dependency lock")
    if manifest.get("platform") != {"architecture": "x86_64", "system": "linux"}:
        raise CandidateError("Daemon manifest platform is not Linux x86_64")
    release = manifest.get("release")
    if (
        type(release) is not dict
        or set(release) != {"identity", "source_commit"}
        or release.get("source_commit") != source_commit
    ):
        raise CandidateError("Daemon manifest does not bind the candidate source commit")
    _require_digest(release.get("identity"), "Daemon release identity")
    runtime = manifest.get("runtime")
    if (
        type(runtime) is not dict
        or set(runtime) != {"format", "python", "system_python_required", "target_pypi_required"}
        or runtime.get("format") != "pyinstaller-onefile"
        or runtime.get("system_python_required") is not False
        or runtime.get("target_pypi_required") is not False
        or type(runtime.get("python")) is not dict
        or set(runtime["python"]) != {"implementation", "version"}
        or runtime["python"].get("implementation") != "CPython"
        or type(runtime["python"].get("version")) is not str
        or not runtime["python"]["version"]
    ):
        raise CandidateError("Daemon runtime contract is invalid")
    if manifest.get("schema_version") != 1 or manifest.get("smoke") != {
        "backend_readiness": "passed",
        "controlled_exit": "passed",
        "identity": "passed",
    }:
        raise CandidateError("Daemon release smoke evidence is incomplete")
    distributions = manifest.get("build_environment_distributions")
    if (
        type(distributions) is not list
        or not distributions
        or any(
            type(item) is not dict
            or set(item) != {"name", "version"}
            or type(item["name"]) is not str
            or not item["name"]
            or type(item["version"]) is not str
            or not item["version"]
            for item in distributions
        )
        or distributions != sorted(distributions, key=lambda item: (item["name"], item["version"]))
        or len(distributions) != len({(item["name"], item["version"]) for item in distributions})
    ):
        raise CandidateError("Daemon build distribution inventory is invalid")
    return manifest


def _wheel_version(wheel: Path) -> str:
    try:
        with ZipFile(wheel) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise CandidateError("Core wheel metadata is incomplete")
            metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
    except (BadZipFile, OSError, UnicodeDecodeError) as exc:
        raise CandidateError("Core wheel is unreadable") from exc
    version = metadata.get("Version")
    if type(version) is not str or not version:
        raise CandidateError("Core wheel version is invalid")
    return version


def _validate_daemon_resource_evidence(
    path: Path,
    *,
    launch_origin: str,
    dmg_path: Path,
    wheel_entry: dict[str, object],
    framework_lock_entry: dict[str, object],
    bundle_entry: dict[str, object],
    manifest_entry: dict[str, object],
    source_commit: str,
) -> None:
    evidence = _load_json(path)
    if type(evidence) is not dict or set(evidence) != {
        "launch_origin",
        "release_assets",
        "schema_version",
        "source_dmg",
    }:
        raise CandidateError(f"{path.name} does not use the closed release asset resource schema")
    if evidence.get("schema_version") != 2 or evidence.get("launch_origin") != launch_origin:
        raise CandidateError(f"{path.name} release asset resource origin is invalid")
    if evidence.get("source_dmg") != {
        "filename": dmg_path.name,
        "sha256": _sha256(dmg_path),
    }:
        raise CandidateError(f"{path.name} does not bind the exact source DMG")
    release_assets = evidence.get("release_assets")
    if type(release_assets) is not dict or set(release_assets) != {"files", "manifest"}:
        raise CandidateError(f"{path.name} release asset inventory is invalid")
    expected_files = [
        {
            "relative_path": "core/framework-lock.json",
            "sha256": framework_lock_entry["sha256"],
            "byte_size": framework_lock_entry["byte_size"],
        },
        {
            "relative_path": f"core/{wheel_entry['filename']}",
            "sha256": wheel_entry["sha256"],
            "byte_size": wheel_entry["byte_size"],
        },
        {
            "relative_path": f"daemon/{DAEMON_MANIFEST_NAME}",
            "sha256": manifest_entry["sha256"],
            "byte_size": manifest_entry["byte_size"],
        },
        {
            "relative_path": f"daemon/{DAEMON_BUNDLE_NAME}",
            "sha256": bundle_entry["sha256"],
            "byte_size": bundle_entry["byte_size"],
        },
        {
            "relative_path": f"runtime/{MANAGED_RUNTIME_ARCHIVE_NAME}",
            "sha256": MANAGED_RUNTIME_ARCHIVE_SHA256,
            "byte_size": MANAGED_RUNTIME_ARCHIVE_SIZE,
        },
    ]
    if expected_files != sorted(expected_files, key=lambda entry: entry["relative_path"]):
        raise CandidateError("release asset inventory ordering is invalid")
    if release_assets.get("files") != [
        {**entry, "relative_path": f"{RELEASE_ASSETS_RESOURCE_ROOT}/{entry['relative_path']}"}
        for entry in expected_files
    ]:
        raise CandidateError(f"{path.name} does not bind the exact packaged release assets")
    expected_manifest = _canonical_json(
        {"files": expected_files, "schema_version": 1, "source_commit": source_commit}
    )
    expected_manifest_entry = {
        "byte_size": len(expected_manifest),
        "relative_path": f"{RELEASE_ASSETS_RESOURCE_ROOT}/{RELEASE_ASSETS_MANIFEST_NAME}",
        "sha256": hashlib.sha256(expected_manifest).hexdigest(),
    }
    if release_assets.get("manifest") != expected_manifest_entry:
        raise CandidateError(
            f"{path.name} does not bind the exact packaged release asset manifest"
        )


def _validate_mach_o_observation(payload: object, *, subject: str) -> list[str]:
    if type(payload) is not dict or set(payload) != {"file_output", "slices"}:
        raise CandidateError(f"{subject} Mach-O evidence does not use the closed schema")
    file_output = payload.get("file_output")
    slices = payload.get("slices")
    if (
        type(file_output) is not str
        or not file_output
        or len(file_output) > 512
        or "Mach-O" not in file_output
        or any(ord(character) < 32 or ord(character) == 127 for character in file_output)
    ):
        raise CandidateError(f"{subject} file evidence is not Mach-O")
    if (
        type(slices) is not list
        or not slices
        or any(
            type(value) is not str or value not in ARCHITECTURE_SLICES.values() for value in slices
        )
        or slices != sorted(set(slices))
    ):
        raise CandidateError(f"{subject} Mach-O slices are invalid")
    return slices


def _validate_evidence(
    root: Path,
    *,
    architecture: str,
    dmg_path: Path,
) -> dict[str, list[str]]:
    dependency = _load_json(root / "dependency-inventory.json")
    license_inventory = _load_json(root / "license-inventory.json")
    security = _load_json(root / "security-audit.json")
    ecosystems = {"python", "npm", "cargo"}
    if type(dependency) is not dict or set(dependency) != {"schema_version", "ecosystems"}:
        raise CandidateError("dependency inventory does not use the closed evidence schema")
    dependency_ecosystems = dependency.get("ecosystems")
    if dependency.get("schema_version") != 2 or type(dependency_ecosystems) is not dict:
        raise CandidateError("dependency inventory schema version is invalid")
    if set(dependency_ecosystems) != ecosystems:
        raise CandidateError("dependency inventory must cover Python, npm, and Cargo")
    for ecosystem, entry in dependency_ecosystems.items():
        if type(entry) is not dict or set(entry) != {"lockfile_sha256", "packages"}:
            raise CandidateError(f"dependency evidence is invalid for {ecosystem}")
        _require_digest(entry.get("lockfile_sha256"), f"{ecosystem} lockfile digest")
        if type(entry.get("packages")) is not int or entry["packages"] < 1:
            raise CandidateError(f"dependency evidence is empty for {ecosystem}")
    lockfiles = {
        "python": REPO_ROOT / "uv.lock",
        "npm": REPO_ROOT / "desktop/package-lock.json",
        "cargo": REPO_ROOT / "desktop/src-tauri/Cargo.lock",
    }
    for ecosystem, path in lockfiles.items():
        if path.is_file() and dependency_ecosystems[ecosystem]["lockfile_sha256"] != _sha256(path):
            raise CandidateError(
                f"dependency evidence does not bind the checkout {ecosystem} lockfile"
            )

    if type(license_inventory) is not dict or set(license_inventory) != {
        "schema_version",
        "project_license_sha256",
        "ecosystems",
    }:
        raise CandidateError("license inventory does not use the closed evidence schema")
    license_ecosystems = license_inventory.get("ecosystems")
    if license_inventory.get("schema_version") != 1 or type(license_ecosystems) is not dict:
        raise CandidateError("license inventory schema version is invalid")
    _require_digest(license_inventory.get("project_license_sha256"), "project license digest")
    project_license = REPO_ROOT / "LICENSE"
    if project_license.is_file() and license_inventory.get("project_license_sha256") != _sha256(
        project_license
    ):
        raise CandidateError("license evidence does not bind the checkout LICENSE")
    if set(license_ecosystems) != ecosystems:
        raise CandidateError("license inventory must cover Python, npm, and Cargo")
    for ecosystem, entry in license_ecosystems.items():
        if type(entry) is not dict or set(entry) != {"packages", "unresolved"}:
            raise CandidateError(f"license evidence is invalid for {ecosystem}")
        if type(entry.get("packages")) is not int or entry["packages"] < 1:
            raise CandidateError(f"license evidence is empty for {ecosystem}")
        if entry.get("unresolved") != 0:
            raise CandidateError(f"license evidence has unresolved packages for {ecosystem}")

    expected_audits = {"npm-audit-high", "pip-audit", "cargo-audit"}
    if type(security) is not dict or set(security) != {"schema_version", "audits"}:
        raise CandidateError("security evidence does not use the closed evidence schema")
    audits = security.get("audits")
    if security.get("schema_version") != 2 or type(audits) is not dict:
        raise CandidateError("security evidence schema version is invalid")
    if set(audits) != expected_audits:
        raise CandidateError("security evidence must cover npm, Python, and Cargo")
    for audit, entry in audits.items():
        expected_keys = {"status", "vulnerabilities"}
        if audit == "pip-audit":
            expected_keys |= {"audited_packages", "requirements_sha256"}
        if type(entry) is not dict or set(entry) != expected_keys:
            raise CandidateError(f"security evidence is invalid for {audit}")
        if entry.get("status") != "passed" or entry.get("vulnerabilities") != 0:
            raise CandidateError(f"security evidence did not pass for {audit}")
    requirements_path = root / "python-requirements.txt"
    pip_evidence = audits["pip-audit"]
    _require_digest(pip_evidence.get("requirements_sha256"), "Python requirements digest")
    if (
        not _is_regular_file(requirements_path)
        or pip_evidence.get("requirements_sha256") != _sha256(requirements_path)
        or pip_evidence.get("audited_packages") != dependency_ecosystems["python"]["packages"]
    ):
        raise CandidateError("pip-audit evidence does not bind the exported Python requirements")

    smoke_keys = {
        "schema_version",
        "native_executable",
        "bundled_external_bin",
        "renderer_ready",
        "sidecar_ready",
        "bundled_external_bin_resolved",
        "native_listener_fd_handoff",
        "native_executable_fd_handoff",
        "process_group_cleanup",
        "mach_o",
        "launch_origin",
        "source_dmg",
        "binary_sha256",
    }
    smoke_payloads: list[dict[str, object]] = []
    expected_dmg_identity = {"filename": dmg_path.name, "sha256": _sha256(dmg_path)}
    smoke_contracts = (
        ("app-bundle-smoke.json", "mounted_dmg"),
        ("dmg-copy-smoke.json", "detached_copy"),
    )
    for filename, launch_origin in smoke_contracts:
        smoke = _load_json(root / filename)
        if type(smoke) is not dict or set(smoke) != smoke_keys:
            raise CandidateError(f"{filename} does not use the closed smoke evidence schema")
        if smoke.get("schema_version") != 3:
            raise CandidateError(f"{filename} schema version is invalid")
        if smoke.get("launch_origin") != launch_origin:
            raise CandidateError(f"{filename} launch origin is invalid")
        if smoke.get("source_dmg") != expected_dmg_identity:
            raise CandidateError(f"{filename} does not bind the exact source DMG")
        binary_sha256 = smoke.get("binary_sha256")
        if type(binary_sha256) is not dict or set(binary_sha256) != {
            "native_executable",
            "bundled_external_bin",
        }:
            raise CandidateError(f"{filename} binary digests do not use the closed schema")
        for binary, digest in binary_sha256.items():
            _require_digest(digest, f"{filename} {binary} digest")
        if smoke.get("native_executable") != TAURI_EXECUTABLE_NAME:
            raise CandidateError(f"{filename} did not launch the Tauri executable")
        if smoke.get("bundled_external_bin") != "openevo-desktop-sidecar":
            raise CandidateError(f"{filename} did not resolve the packaged externalBin")
        boolean_keys = smoke_keys - {
            "schema_version",
            "native_executable",
            "bundled_external_bin",
            "mach_o",
            "launch_origin",
            "source_dmg",
            "binary_sha256",
        }
        if any(smoke.get(key) is not True for key in boolean_keys):
            raise CandidateError(f"{filename} contains failing native evidence")
        mach_o = smoke.get("mach_o")
        if type(mach_o) is not dict or set(mach_o) != {
            "native_executable",
            "bundled_external_bin",
        }:
            raise CandidateError(f"{filename} Mach-O evidence does not use the closed schema")
        for binary in ("native_executable", "bundled_external_bin"):
            _validate_mach_o_observation(mach_o[binary], subject=f"{filename} {binary}")
        smoke_payloads.append(smoke)
    if smoke_payloads[0]["mach_o"] != smoke_payloads[1]["mach_o"]:
        raise CandidateError("Mounted-DMG app and detached-copy Mach-O evidence do not match")
    if smoke_payloads[0]["binary_sha256"] != smoke_payloads[1]["binary_sha256"]:
        raise CandidateError("Mounted-DMG app and detached-copy binary digests do not match")
    expected_slice = ARCHITECTURE_SLICES[architecture]
    mach_o = smoke_payloads[0]["mach_o"]
    assert isinstance(mach_o, dict)
    native_architectures: dict[str, list[str]] = {}
    for binary in ("native_executable", "bundled_external_bin"):
        observation = mach_o[binary]
        assert isinstance(observation, dict)
        slices = observation["slices"]
        assert isinstance(slices, list)
        if slices != [expected_slice]:
            raise CandidateError("Packaged Mach-O slices do not match the candidate architecture")
        native_architectures[binary] = slices
    return native_architectures


def _validate_core_compatibility(
    payload: object,
    *,
    expected_platform: str | None = None,
) -> None:
    if type(payload) is not dict or set(payload) != {"python_requires", "supported_platforms"}:
        raise CandidateError("Core install compatibility does not use the closed release schema")
    if payload.get("python_requires") != CORE_PYTHON_REQUIRES or payload.get(
        "supported_platforms"
    ) != list(CORE_SUPPORTED_PLATFORMS):
        raise CandidateError("Core install compatibility is unsupported")
    if expected_platform is not None and expected_platform not in CORE_SUPPORTED_PLATFORMS:
        raise CandidateError("Expected Core platform is not supported by the candidate")


def _validate_release_notes(
    path: Path,
    *,
    source_commit: str,
    version: str,
    architecture: str,
) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CandidateError("Release notes are unreadable") from exc
    expected = render_candidate_release_notes(
        source_commit=source_commit,
        version=version,
        architecture=architecture,
    )
    if text != expected:
        raise CandidateError("Release notes do not match the canonical packaging draft")


def _validate_draft_release_metadata(
    metadata_path: Path,
    *,
    release_notes: Path,
    expected_tag: str,
    expected_target: str,
    expected_title: str,
    expected_repository: str,
    expected_owner: str,
) -> int:
    metadata = _load_json(metadata_path)
    if type(metadata) is not dict or set(metadata) != DRAFT_RELEASE_METADATA_KEYS:
        raise CandidateError("Draft release metadata does not use the closed review schema")
    try:
        body = release_notes.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CandidateError("Release notes are unreadable") from exc
    expected_body = render_draft_release_body(
        release_notes=body,
        ownership_token=expected_owner,
    )
    release_body = metadata.get("body")
    if type(release_body) is not str or release_body.rstrip("\n") != expected_body.rstrip("\n"):
        raise CandidateError("Draft release body does not match the candidate release notes")
    expected = {
        "isDraft": True,
        "isPrerelease": True,
        "name": expected_title,
        "tagName": expected_tag,
        "targetCommitish": expected_target,
    }
    if any(metadata.get(field) != value for field, value in expected.items()):
        raise CandidateError("Draft release identity or state does not match the candidate")
    url = metadata.get("url")
    repository_parts = expected_repository.split("/")
    if (
        len(repository_parts) != 2
        or not all(repository_parts)
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
            for character in expected_repository.replace("/", "")
        )
    ):
        raise CandidateError("Expected GitHub repository is invalid")
    api_url = metadata.get("apiUrl")
    if type(api_url) is not str:
        raise CandidateError("Draft release API URL is invalid")
    parsed_api_url = urlsplit(api_url)
    expected_api_prefix = f"/repos/{expected_repository}/releases/"
    release_id_text = parsed_api_url.path[len(expected_api_prefix) :]
    if (
        parsed_api_url.scheme != "https"
        or parsed_api_url.netloc.casefold() != "api.github.com"
        or not parsed_api_url.path.startswith(expected_api_prefix)
        or not release_id_text.isascii()
        or not release_id_text.isdigit()
        or release_id_text.startswith("0")
        or parsed_api_url.query
        or parsed_api_url.fragment
    ):
        raise CandidateError("Draft release API URL does not bind an immutable release ID")
    if type(url) is not str:
        raise CandidateError("Draft release URL is invalid")
    parsed_url = urlsplit(url)
    expected_prefix = f"/{expected_repository}/releases/tag/"
    release_slug = parsed_url.path[len(expected_prefix) :]
    if (
        parsed_url.scheme != "https"
        or parsed_url.netloc.casefold() != "github.com"
        or not parsed_url.path.startswith(expected_prefix)
        or not release_slug
        or "/" in release_slug
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise CandidateError("Draft release URL does not belong to the expected repository")
    return int(release_id_text)


def validated_draft_release_id(
    metadata_path: Path,
    *,
    release_notes: Path,
    expected_tag: str,
    expected_target: str,
    expected_title: str,
    expected_repository: str,
    expected_owner: str,
) -> int:
    return _validate_draft_release_metadata(
        metadata_path,
        release_notes=release_notes,
        expected_tag=expected_tag,
        expected_target=expected_target,
        expected_title=expected_title,
        expected_repository=expected_repository,
        expected_owner=expected_owner,
    )


def validate_draft_release_metadata(
    metadata_path: Path,
    *,
    release_notes: Path,
    expected_tag: str,
    expected_target: str,
    expected_title: str,
    expected_repository: str,
    expected_owner: str,
) -> list[str]:
    try:
        validated_draft_release_id(
            metadata_path,
            release_notes=release_notes,
            expected_tag=expected_tag,
            expected_target=expected_target,
            expected_title=expected_title,
            expected_repository=expected_repository,
            expected_owner=expected_owner,
        )
    except CandidateError as exc:
        return [str(exc)]
    return []


def _file_entry(role: str, path: Path) -> dict[str, object]:
    return {
        "byte_size": path.stat().st_size,
        "filename": path.name,
        "role": role,
        "sha256": _sha256(path),
    }


def _candidate_paths(root: Path, *, version: str, architecture: str) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for role, filename in REQUIRED_INPUT_ROLES:
        if role == "desktop_dmg":
            path = _single_match(
                root,
                f"OpenEvo-Desktop-{version}-{architecture}.dmg",
                "architecture-specific Desktop DMG",
            )
        elif role == "core_wheel":
            path = _single_match(root, f"openevo-{version}-*.whl", "Core wheel")
        else:
            assert filename is not None
            path = root / filename
            if not _is_regular_file(path):
                raise CandidateError(f"Candidate input is missing: {filename}")
        paths[role] = path
    return paths


def create_candidate_manifest(
    candidate_root: Path,
    *,
    source_commit: str,
    version: str,
    architecture: str,
    rust_target: str,
    registry_digest: str,
) -> Path:
    root = candidate_root.resolve()
    if not root.is_dir():
        raise CandidateError("Candidate root must be a directory")
    if SOURCE_COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise CandidateError("source_commit must be one full lowercase Git commit")
    if architecture not in ARCHITECTURE_TARGETS:
        raise CandidateError(
            "candidate architecture must be the actual host architecture, not universal"
        )
    if ARCHITECTURE_TARGETS[architecture] != rust_target:
        raise CandidateError("candidate architecture does not match the actual Rust host target")
    _require_digest(registry_digest, "registry digest")
    if not version or any(character.isspace() for character in version):
        raise CandidateError("candidate version is invalid")
    generated = [root / name for name in (CORE_DESCRIPTOR_NAME, CHECKSUMS_NAME, MANIFEST_NAME)]
    if any(path.exists() for path in generated):
        raise CandidateError("candidate generated files must not already exist")

    paths = _candidate_paths(root, version=version, architecture=architecture)
    wheel = paths["core_wheel"]
    _validate_wheel(wheel, version=version)
    _validate_framework_lock(paths["framework_lock"], wheel, version=version)
    daemon_manifest = validate_daemon_release_inputs(
        bundle=paths["daemon_bundle"],
        manifest_path=paths["daemon_manifest"],
        wheel=wheel,
        framework_lock=paths["framework_lock"],
        source_commit=source_commit,
        registry_digest=registry_digest,
    )
    native_architectures = _validate_evidence(
        root,
        architecture=architecture,
        dmg_path=paths["desktop_dmg"],
    )
    _validate_daemon_resource_evidence(
        paths["daemon_mounted_resource"],
        launch_origin="mounted_dmg",
        dmg_path=paths["desktop_dmg"],
        wheel_entry=_file_entry("core_wheel", wheel),
        framework_lock_entry=_file_entry("framework_lock", paths["framework_lock"]),
        bundle_entry=_file_entry("daemon_bundle", paths["daemon_bundle"]),
        manifest_entry=_file_entry("daemon_manifest", paths["daemon_manifest"]),
        source_commit=source_commit,
    )
    _validate_daemon_resource_evidence(
        paths["daemon_copy_resource"],
        launch_origin="detached_copy",
        dmg_path=paths["desktop_dmg"],
        wheel_entry=_file_entry("core_wheel", wheel),
        framework_lock_entry=_file_entry("framework_lock", paths["framework_lock"]),
        bundle_entry=_file_entry("daemon_bundle", paths["daemon_bundle"]),
        manifest_entry=_file_entry("daemon_manifest", paths["daemon_manifest"]),
        source_commit=source_commit,
    )
    if _load_json(paths["managed_runtime_source"]) != _managed_runtime_source_evidence():
        raise CandidateError("managed runtime source evidence is invalid")
    _validate_playwright_candidate_evidence(
        paths["playwright_evidence"],
        report_path=paths["playwright_report"],
        packaged_web_manifest_path=paths["packaged_web_manifest"],
        expected_source_commit=source_commit,
    )
    _validate_release_notes(
        paths["release_notes"],
        source_commit=source_commit,
        version=version,
        architecture=architecture,
    )

    descriptor = {
        "artifact": _file_entry("core_wheel", wheel),
        "compatibility": {
            "python_requires": CORE_PYTHON_REQUIRES,
            "supported_platforms": list(CORE_SUPPORTED_PLATFORMS),
        },
        "framework_lock": _file_entry("framework_lock", paths["framework_lock"]),
        "registry_digest": registry_digest,
        "schema_version": 2,
        "source_commit": source_commit,
        "version": version,
    }
    descriptor_path = root / CORE_DESCRIPTOR_NAME
    _write_new(descriptor_path, _canonical_json(descriptor))
    paths["core_descriptor"] = descriptor_path

    checksum_subjects = sorted(paths.values(), key=lambda path: path.name)
    checksum_payload = "".join(f"{_sha256(path)}  {path.name}\n" for path in checksum_subjects)
    checksums_path = root / CHECKSUMS_NAME
    _write_new(checksums_path, checksum_payload.encode("ascii"))
    paths["checksums"] = checksums_path

    files = [_file_entry(role, paths[role]) for role in FINAL_ROLES]
    manifest = {
        "core": {
            "descriptor_filename": CORE_DESCRIPTOR_NAME,
            "descriptor_sha256": _sha256(descriptor_path),
            "registry_digest": registry_digest,
        },
        "daemon": {
            "artifact_filename": paths["daemon_bundle"].name,
            "artifact_sha256": _sha256(paths["daemon_bundle"]),
            "manifest_filename": paths["daemon_manifest"].name,
            "manifest_sha256": _sha256(paths["daemon_manifest"]),
            "release_identity": daemon_manifest["release"]["identity"],
        },
        "files": files,
        "macos": {
            "architecture": architecture,
            "minimum_system_version": MINIMUM_MACOS_VERSION,
            "native_architectures": native_architectures,
            "rust_target": rust_target,
            "rust_toolchain": RUST_TOOLCHAIN_VERSION,
        },
        "managed_runtime": _managed_runtime_manifest(),
        "release": {
            "app_bundle_signature": "adhoc",
            "channel": "unsigned-preview",
            "developer_id_signed": False,
            "notarized": False,
            "quarantine_removal_tested": True,
        },
        "schema_version": 6,
        "source_commit": source_commit,
        "version": version,
    }
    manifest_path = root / MANIFEST_NAME
    _write_new(manifest_path, _canonical_json(manifest))
    errors = validate_candidate_manifest(manifest_path, expected_source_commit=source_commit)
    if errors:
        raise CandidateError("generated candidate failed validation: " + "; ".join(errors))
    return manifest_path


def _validate_candidate_manifest(
    manifest_path: Path,
    *,
    expected_source_commit: str | None,
    expected_core_platform: str | None,
) -> None:
    if not _is_regular_file(manifest_path):
        raise CandidateError("candidate manifest must be a regular non-symlink file")
    root = manifest_path.absolute().parent
    manifest = _load_json(manifest_path)
    required_keys = {
        "schema_version",
        "source_commit",
        "version",
        "release",
        "macos",
        "managed_runtime",
        "core",
        "daemon",
        "files",
    }
    if type(manifest) is not dict or set(manifest) != required_keys:
        raise CandidateError("candidate manifest does not use the closed release schema")
    source_commit = manifest.get("source_commit")
    version = manifest.get("version")
    release = manifest.get("release")
    macos = manifest.get("macos")
    managed_runtime = manifest.get("managed_runtime")
    core = manifest.get("core")
    daemon = manifest.get("daemon")
    files = manifest.get("files")
    if manifest.get("schema_version") != 6:
        raise CandidateError("candidate manifest schema version is invalid")
    if type(source_commit) is not str or SOURCE_COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise CandidateError("candidate source commit is invalid")
    if expected_source_commit is not None and source_commit != expected_source_commit:
        raise CandidateError("candidate source commit does not match the expected checkout")
    if type(version) is not str or not version:
        raise CandidateError("candidate version is invalid")
    if release != {
        "app_bundle_signature": "adhoc",
        "channel": "unsigned-preview",
        "developer_id_signed": False,
        "notarized": False,
        "quarantine_removal_tested": True,
    }:
        raise CandidateError(
            "candidate release signature, notarization, or quarantine evidence is invalid"
        )
    if type(macos) is not dict or set(macos) != {
        "architecture",
        "minimum_system_version",
        "native_architectures",
        "rust_target",
        "rust_toolchain",
    }:
        raise CandidateError("candidate macOS identity is invalid")
    architecture = macos.get("architecture")
    if architecture not in ARCHITECTURE_TARGETS:
        raise CandidateError("candidate declares an unsupported or unbuilt architecture")
    if (
        macos.get("rust_target") != ARCHITECTURE_TARGETS[architecture]
        or macos.get("minimum_system_version") != MINIMUM_MACOS_VERSION
        or macos.get("rust_toolchain") != RUST_TOOLCHAIN_VERSION
    ):
        raise CandidateError("candidate macOS target identity is inconsistent")
    native_architectures = macos.get("native_architectures")
    expected_slice = ARCHITECTURE_SLICES[architecture]
    if native_architectures != {
        "native_executable": [expected_slice],
        "bundled_external_bin": [expected_slice],
    }:
        raise CandidateError("candidate manifest does not bind the packaged Mach-O slices")
    if managed_runtime != _managed_runtime_manifest():
        raise CandidateError("candidate managed runtime identity is invalid")
    if type(files) is not list or len(files) != len(FINAL_ROLES):
        raise CandidateError("candidate file inventory is incomplete")
    by_role: dict[str, dict[str, object]] = {}
    filenames: set[str] = set()
    for entry in files:
        if type(entry) is not dict or set(entry) != {"role", "filename", "byte_size", "sha256"}:
            raise CandidateError("candidate file entry is invalid")
        role = entry.get("role")
        if type(role) is not str or role not in FINAL_ROLES or role in by_role:
            raise CandidateError("candidate file roles are invalid or duplicated")
        filename = _require_safe_basename(entry.get("filename"), f"{role} filename")
        if filename in filenames:
            raise CandidateError("candidate filenames must be unique")
        filenames.add(filename)
        byte_size = entry.get("byte_size")
        if type(byte_size) is not int or byte_size < 1:
            raise CandidateError(f"candidate file size is invalid for {filename}")
        _require_digest(entry.get("sha256"), f"{filename} digest")
        path = root / filename
        if (
            not _is_regular_file(path)
            or path.stat().st_size != byte_size
            or _sha256(path) != entry["sha256"]
        ):
            raise CandidateError(f"candidate file digest mismatch: {filename}")
        by_role[role] = entry
    if tuple(by_role) != FINAL_ROLES:
        raise CandidateError("candidate file role order is not canonical")
    entries = list(root.iterdir())
    if any(not _is_regular_file(path) for path in entries):
        raise CandidateError("candidate directory contains a non-regular or symbolic-link entry")
    actual_inventory = {path.name for path in entries}
    if actual_inventory != filenames | {MANIFEST_NAME}:
        raise CandidateError("candidate directory inventory does not exactly match the manifest")

    if type(core) is not dict or set(core) != {
        "descriptor_filename",
        "descriptor_sha256",
        "registry_digest",
    }:
        raise CandidateError("candidate Core identity is invalid")
    _require_digest(core.get("registry_digest"), "candidate registry digest")
    if (
        core.get("descriptor_filename") != by_role["core_descriptor"]["filename"]
        or core.get("descriptor_sha256") != by_role["core_descriptor"]["sha256"]
    ):
        raise CandidateError("candidate Core descriptor identity is inconsistent")
    if type(daemon) is not dict or set(daemon) != {
        "artifact_filename",
        "artifact_sha256",
        "manifest_filename",
        "manifest_sha256",
        "release_identity",
    }:
        raise CandidateError("candidate Daemon identity is invalid")
    if (
        daemon.get("artifact_filename") != by_role["daemon_bundle"]["filename"]
        or daemon.get("artifact_sha256") != by_role["daemon_bundle"]["sha256"]
        or daemon.get("manifest_filename") != by_role["daemon_manifest"]["filename"]
        or daemon.get("manifest_sha256") != by_role["daemon_manifest"]["sha256"]
    ):
        raise CandidateError("candidate Daemon file identity is inconsistent")
    _require_digest(daemon.get("release_identity"), "candidate Daemon release identity")

    descriptor_path = root / str(by_role["core_descriptor"]["filename"])
    descriptor = _load_json(descriptor_path)
    if type(descriptor) is not dict or set(descriptor) != {
        "schema_version",
        "source_commit",
        "version",
        "registry_digest",
        "compatibility",
        "artifact",
        "framework_lock",
    }:
        raise CandidateError("Core install descriptor does not use the closed release schema")
    if (
        descriptor.get("schema_version") != 2
        or descriptor.get("source_commit") != source_commit
        or descriptor.get("version") != version
        or descriptor.get("registry_digest") != core.get("registry_digest")
        or descriptor.get("artifact") != by_role["core_wheel"]
        or descriptor.get("framework_lock") != by_role["framework_lock"]
    ):
        raise CandidateError("Core install descriptor does not bind the candidate wheel and lock")
    _validate_core_compatibility(
        descriptor.get("compatibility"),
        expected_platform=expected_core_platform,
    )

    wheel_path = root / str(by_role["core_wheel"]["filename"])
    lock_path = root / str(by_role["framework_lock"]["filename"])
    _validate_wheel(wheel_path, version=version)
    _validate_framework_lock(lock_path, wheel_path, version=version)
    daemon_manifest = validate_daemon_release_inputs(
        bundle=root / str(by_role["daemon_bundle"]["filename"]),
        manifest_path=root / str(by_role["daemon_manifest"]["filename"]),
        wheel=wheel_path,
        framework_lock=lock_path,
        source_commit=source_commit,
        registry_digest=str(core["registry_digest"]),
    )
    if daemon.get("release_identity") != daemon_manifest["release"]["identity"]:
        raise CandidateError("candidate Daemon release identity is inconsistent")
    observed_native_architectures = _validate_evidence(
        root,
        architecture=architecture,
        dmg_path=root / str(by_role["desktop_dmg"]["filename"]),
    )
    if observed_native_architectures != native_architectures:
        raise CandidateError("candidate manifest Mach-O slices do not match native evidence")
    _validate_daemon_resource_evidence(
        root / str(by_role["daemon_mounted_resource"]["filename"]),
        launch_origin="mounted_dmg",
        dmg_path=root / str(by_role["desktop_dmg"]["filename"]),
        wheel_entry=by_role["core_wheel"],
        framework_lock_entry=by_role["framework_lock"],
        bundle_entry=by_role["daemon_bundle"],
        manifest_entry=by_role["daemon_manifest"],
        source_commit=str(manifest["source_commit"]),
    )
    _validate_daemon_resource_evidence(
        root / str(by_role["daemon_copy_resource"]["filename"]),
        launch_origin="detached_copy",
        dmg_path=root / str(by_role["desktop_dmg"]["filename"]),
        wheel_entry=by_role["core_wheel"],
        framework_lock_entry=by_role["framework_lock"],
        bundle_entry=by_role["daemon_bundle"],
        manifest_entry=by_role["daemon_manifest"],
        source_commit=str(manifest["source_commit"]),
    )
    if (
        _load_json(root / str(by_role["managed_runtime_source"]["filename"]))
        != _managed_runtime_source_evidence()
    ):
        raise CandidateError("managed runtime source evidence is invalid")
    _validate_playwright_candidate_evidence(
        root / str(by_role["playwright_evidence"]["filename"]),
        report_path=root / str(by_role["playwright_report"]["filename"]),
        packaged_web_manifest_path=root / str(by_role["packaged_web_manifest"]["filename"]),
        expected_source_commit=source_commit,
    )
    _validate_release_notes(
        root / str(by_role["release_notes"]["filename"]),
        source_commit=source_commit,
        version=version,
        architecture=str(architecture),
    )

    checksums_path = root / str(by_role["checksums"]["filename"])
    expected_subjects = sorted(
        (root / str(entry["filename"]) for role, entry in by_role.items() if role != "checksums"),
        key=lambda path: path.name,
    )
    expected_checksums = "".join(f"{_sha256(path)}  {path.name}\n" for path in expected_subjects)
    try:
        checksums = checksums_path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise CandidateError("SHA256SUMS is unreadable") from exc
    if checksums != expected_checksums:
        raise CandidateError("SHA256SUMS is not the canonical exact candidate checksum set")


def validate_candidate_manifest(
    manifest_path: Path,
    *,
    expected_source_commit: str | None = None,
    expected_core_platform: str | None = None,
) -> list[str]:
    try:
        _validate_candidate_manifest(
            manifest_path,
            expected_source_commit=expected_source_commit,
            expected_core_platform=expected_core_platform,
        )
    except CandidateError as exc:
        return [str(exc)]
    return []


def _require_positive_int(value: object, subject: str) -> int:
    if type(value) is not int or value < 1:
        raise CandidateError(f"{subject} must be a positive integer")
    return value


def _validate_repository(repository: str) -> None:
    parts = repository.split("/")
    if (
        len(parts) != 2
        or not all(parts)
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
            for character in repository.replace("/", "")
        )
    ):
        raise CandidateError("Expected GitHub repository is invalid")


def _normalized_preview_metadata(
    metadata_path: Path,
    *,
    expected_repository: str,
    expected_release_id: int,
    expected_tag: str,
    expected_target: str,
    expected_title: str,
    expected_draft: bool,
) -> dict[str, object]:
    _validate_repository(expected_repository)
    _require_positive_int(expected_release_id, "Expected release ID")
    if SOURCE_COMMIT_PATTERN.fullmatch(expected_target) is None:
        raise CandidateError("Expected Preview target must be one full lowercase Git commit")
    metadata = _load_json(metadata_path)
    if type(metadata) is not dict or set(metadata) != PREVIEW_RELEASE_METADATA_KEYS:
        raise CandidateError("Preview release metadata does not use the closed REST schema")
    if (
        metadata.get("id") != expected_release_id
        or metadata.get("tag_name") != expected_tag
        or metadata.get("target_commitish") != expected_target
        or metadata.get("name") != expected_title
        or metadata.get("draft") is not expected_draft
        or metadata.get("immutable") is not (not expected_draft)
        or metadata.get("prerelease") is not True
        or type(metadata.get("body")) is not str
    ):
        raise CandidateError("Preview release identity, metadata, or visibility is invalid")

    html_url = metadata.get("html_url")
    if type(html_url) is not str:
        raise CandidateError("Preview release HTML URL is invalid")
    parsed_url = urlsplit(html_url)
    expected_prefix = f"/{expected_repository}/releases/tag/"
    slug = parsed_url.path[len(expected_prefix) :]
    if (
        parsed_url.scheme != "https"
        or parsed_url.netloc.casefold() != "github.com"
        or not parsed_url.path.startswith(expected_prefix)
        or not slug
        or "/" in slug
        or parsed_url.query
        or parsed_url.fragment
        or not expected_draft
        and slug != quote(expected_tag, safe="._-")
    ):
        raise CandidateError("Preview release HTML URL is invalid")

    assets = metadata.get("assets")
    if type(assets) is not list or not assets:
        raise CandidateError("Preview release asset inventory is empty")
    normalized_assets: list[dict[str, object]] = []
    asset_ids: set[int] = set()
    names: set[str] = set()
    for asset in assets:
        if type(asset) is not dict or set(asset) != PREVIEW_RELEASE_ASSET_KEYS:
            raise CandidateError("Preview release asset metadata is not closed")
        asset_id = _require_positive_int(asset.get("id"), "Preview release asset ID")
        name = _require_safe_basename(asset.get("name"), "Preview release asset name")
        size = asset.get("size")
        digest = asset.get("digest")
        if (
            asset_id in asset_ids
            or name in names
            or type(size) is not int
            or size < 1
            or asset.get("state") != "uploaded"
            or type(digest) is not str
            or not digest.startswith("sha256:")
            or DIGEST_PATTERN.fullmatch(digest.removeprefix("sha256:")) is None
        ):
            raise CandidateError("Preview release asset identity or upload state is invalid")
        asset_ids.add(asset_id)
        names.add(name)
        normalized_assets.append(
            {
                "id": asset_id,
                "name": name,
                "sha256": digest.removeprefix("sha256:"),
                "size": size,
            }
        )
    normalized_assets.sort(key=lambda entry: str(entry["name"]))
    return {
        "assets": normalized_assets,
        "body": metadata["body"],
        "draft": expected_draft,
        "prerelease": True,
        "release_id": expected_release_id,
        "target_commitish": expected_target,
        "title": expected_title,
    }


def _load_preview_snapshot(path: Path) -> dict[str, object]:
    snapshot = _load_json(path)
    if (
        type(snapshot) is not dict
        or set(snapshot) != PREVIEW_RELEASE_SNAPSHOT_KEYS
        or snapshot.get("schema_version") != 1
        or path.read_bytes() != _canonical_json(snapshot)
    ):
        raise CandidateError("Preview release snapshot does not use the canonical closed schema")
    _validate_repository(str(snapshot.get("repository")))
    _require_positive_int(snapshot.get("release_id"), "Preview snapshot release ID")
    if (
        type(snapshot.get("tag")) is not str
        or not snapshot["tag"]
        or type(snapshot.get("source_commit")) is not str
        or SOURCE_COMMIT_PATTERN.fullmatch(snapshot["source_commit"]) is None
        or snapshot.get("target_commitish") != snapshot.get("source_commit")
        or type(snapshot.get("manifest_sha256")) is not str
        or DIGEST_PATTERN.fullmatch(snapshot["manifest_sha256"]) is None
        or type(snapshot.get("title")) is not str
        or not snapshot["title"]
        or type(snapshot.get("body")) is not str
        or snapshot.get("draft") is not True
        or snapshot.get("prerelease") is not True
    ):
        raise CandidateError("Preview release snapshot identity is invalid")
    workflow = snapshot.get("candidate_workflow")
    if type(workflow) is not dict or set(workflow) != {"run_attempt", "run_id"}:
        raise CandidateError("Preview release snapshot workflow identity is invalid")
    _require_positive_int(workflow.get("run_id"), "Preview snapshot workflow run ID")
    _require_positive_int(workflow.get("run_attempt"), "Preview snapshot workflow run attempt")
    assets = snapshot.get("assets")
    if type(assets) is not list or not assets:
        raise CandidateError("Preview release snapshot asset inventory is empty")
    expected_order = sorted(assets, key=lambda entry: str(entry.get("name", "")))
    if assets != expected_order:
        raise CandidateError("Preview release snapshot assets are not canonically ordered")
    ids: set[int] = set()
    names: set[str] = set()
    for asset in assets:
        if type(asset) is not dict or set(asset) != {"id", "name", "sha256", "size"}:
            raise CandidateError("Preview release snapshot asset entry is invalid")
        asset_id = _require_positive_int(asset.get("id"), "Preview snapshot asset ID")
        name = _require_safe_basename(asset.get("name"), "Preview snapshot asset name")
        if (
            asset_id in ids
            or name in names
            or type(asset.get("size")) is not int
            or asset["size"] < 1
            or type(asset.get("sha256")) is not str
            or DIGEST_PATTERN.fullmatch(asset["sha256"]) is None
        ):
            raise CandidateError("Preview release snapshot asset identity is invalid")
        ids.add(asset_id)
        names.add(name)
    return snapshot


def validate_preview_release_snapshot_identity(
    snapshot_path: Path,
    *,
    expected_repository: str,
    expected_release_id: int,
    expected_tag: str,
    expected_source_commit: str,
    expected_manifest_sha256: str,
    expected_run_id: int,
    expected_run_attempt: int,
) -> None:
    _validate_repository(expected_repository)
    _require_positive_int(expected_release_id, "Expected release ID")
    _require_positive_int(expected_run_id, "Expected candidate workflow run ID")
    _require_positive_int(expected_run_attempt, "Expected candidate workflow run attempt")
    if SOURCE_COMMIT_PATTERN.fullmatch(expected_source_commit) is None:
        raise CandidateError("Expected Preview source commit is invalid")
    _require_digest(
        expected_manifest_sha256,
        "Expected release-candidate manifest digest",
    )
    snapshot = _load_preview_snapshot(snapshot_path)
    expected_identity = {
        "draft": True,
        "manifest_sha256": expected_manifest_sha256,
        "prerelease": True,
        "release_id": expected_release_id,
        "repository": expected_repository,
        "source_commit": expected_source_commit,
        "tag": expected_tag,
        "target_commitish": expected_source_commit,
    }
    if any(snapshot.get(key) != value for key, value in expected_identity.items()):
        raise CandidateError("Preview release snapshot identity does not match publication inputs")
    if snapshot.get("candidate_workflow") != {
        "run_attempt": expected_run_attempt,
        "run_id": expected_run_id,
    }:
        raise CandidateError(
            "Preview release snapshot workflow identity does not match publication inputs"
        )


def _candidate_preview_identity(
    candidate_root: Path,
    *,
    expected_source_commit: str,
    expected_manifest_sha256: str,
    expected_run_id: int,
    expected_run_attempt: int,
) -> tuple[dict[str, object], list[dict[str, object]], Path]:
    manifest_path = candidate_root / MANIFEST_NAME
    if not _is_regular_file(manifest_path):
        raise CandidateError("Downloaded Preview is missing release-candidate.json")
    if _sha256(manifest_path) != _require_digest(
        expected_manifest_sha256,
        "Expected release-candidate manifest digest",
    ):
        raise CandidateError(
            "release-candidate manifest digest does not match the expected digest"
        )
    errors = validate_candidate_manifest(
        manifest_path,
        expected_source_commit=expected_source_commit,
    )
    if errors:
        raise CandidateError("; ".join(errors))
    manifest = _load_json(manifest_path)
    files = manifest["files"]
    by_role = {entry["role"]: entry for entry in files}
    evidence = _load_json(candidate_root / by_role["playwright_evidence"]["filename"])
    if evidence.get("run") != {"attempt": expected_run_attempt, "id": expected_run_id}:
        raise CandidateError("Candidate Playwright evidence belongs to another workflow run")
    expected_assets = [
        {
            "name": entry["filename"],
            "sha256": entry["sha256"],
            "size": entry["byte_size"],
        }
        for entry in files
    ]
    expected_assets.append(
        {
            "name": MANIFEST_NAME,
            "sha256": expected_manifest_sha256,
            "size": manifest_path.stat().st_size,
        }
    )
    expected_assets.sort(key=lambda entry: str(entry["name"]))
    return manifest, expected_assets, candidate_root / by_role["release_notes"]["filename"]


def _validate_preview_body(body: str, release_notes: Path) -> None:
    try:
        notes = release_notes.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CandidateError("Release notes are unreadable") from exc
    prefix = notes.rstrip("\n") + "\n\n<!-- openevo-draft-owner:"
    suffix = " -->"
    normalized = body.rstrip("\n")
    if not normalized.startswith(prefix) or not normalized.endswith(suffix):
        raise CandidateError("Preview release body does not match the canonical candidate notes")
    token = normalized[len(prefix) : -len(suffix)]
    expected = render_draft_release_body(
        release_notes=notes,
        ownership_token=token,
    )
    if normalized != expected.rstrip("\n"):
        raise CandidateError("Preview release body does not match the canonical candidate notes")


def write_preview_release_snapshot(
    output: Path,
    *,
    metadata_path: Path,
    candidate_root: Path,
    baseline_path: Path | None,
    expected_repository: str,
    expected_release_id: int,
    expected_tag: str,
    expected_source_commit: str,
    expected_manifest_sha256: str,
    expected_run_id: int,
    expected_run_attempt: int,
    expected_draft: bool,
) -> None:
    manifest, expected_assets, release_notes = _candidate_preview_identity(
        candidate_root,
        expected_source_commit=expected_source_commit,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_run_id=expected_run_id,
        expected_run_attempt=expected_run_attempt,
    )
    expected_title = f"OpenEvo Desktop {manifest['version']} Preview"
    normalized = _normalized_preview_metadata(
        metadata_path,
        expected_repository=expected_repository,
        expected_release_id=expected_release_id,
        expected_tag=expected_tag,
        expected_target=expected_source_commit,
        expected_title=expected_title,
        expected_draft=expected_draft,
    )
    observed_assets = normalized["assets"]
    assert isinstance(observed_assets, list)
    if [
        {key: asset[key] for key in ("name", "sha256", "size")} for asset in observed_assets
    ] != expected_assets:
        raise CandidateError("Preview release assets do not exactly match the candidate manifest")
    for asset in observed_assets:
        path = candidate_root / str(asset["name"])
        if (
            not _is_regular_file(path)
            or path.stat().st_size != asset["size"]
            or _sha256(path) != asset["sha256"]
        ):
            raise CandidateError(f"Downloaded Preview asset is invalid: {asset['name']}")
    body = str(normalized["body"])
    _validate_preview_body(body, release_notes)
    snapshot = {
        "assets": observed_assets,
        "body": body,
        "candidate_workflow": {
            "run_attempt": expected_run_attempt,
            "run_id": expected_run_id,
        },
        "draft": expected_draft,
        "manifest_sha256": expected_manifest_sha256,
        "prerelease": True,
        "release_id": expected_release_id,
        "repository": expected_repository,
        "schema_version": 1,
        "source_commit": expected_source_commit,
        "tag": expected_tag,
        "target_commitish": expected_source_commit,
        "title": expected_title,
    }
    if baseline_path is not None:
        baseline = _load_preview_snapshot(baseline_path)
        expected = dict(baseline)
        expected["draft"] = expected_draft
        if snapshot != expected:
            raise CandidateError(
                "Preview release metadata or assets changed after draft validation"
            )
    _write_private_new(output, _canonical_json(snapshot))


def write_preview_asset_plan(
    output: Path,
    *,
    metadata_path: Path,
    baseline_path: Path,
    expected_draft: bool,
) -> None:
    baseline = _load_preview_snapshot(baseline_path)
    normalized = _normalized_preview_metadata(
        metadata_path,
        expected_repository=str(baseline["repository"]),
        expected_release_id=int(baseline["release_id"]),
        expected_tag=str(baseline["tag"]),
        expected_target=str(baseline["source_commit"]),
        expected_title=str(baseline["title"]),
        expected_draft=expected_draft,
    )
    expected = dict(baseline)
    expected["draft"] = expected_draft
    for field in (
        "assets",
        "body",
        "draft",
        "prerelease",
        "release_id",
        "target_commitish",
        "title",
    ):
        if normalized[field] != expected[field]:
            raise CandidateError("Preview release metadata or asset identities changed")
    plan = "".join(f"{asset['id']}\t{asset['name']}\n" for asset in normalized["assets"])
    _write_private_new(output, plan.encode("utf-8"))


def assert_release_id_inventory(
    inventory_path: Path,
    *,
    expected_tag: str,
    expected_release_id: int,
) -> None:
    _require_positive_int(expected_release_id, "Expected release ID")
    try:
        lines = inventory_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise CandidateError("GitHub release ID inventory is unreadable") from exc
    matches: list[int] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CandidateError(
                f"GitHub release ID inventory line {line_number} is invalid"
            ) from exc
        if (
            type(entry) is not dict
            or set(entry) != {"id", "tag_name"}
            or type(entry.get("tag_name")) is not str
        ):
            raise CandidateError(f"GitHub release ID inventory line {line_number} is invalid")
        release_id = _require_positive_int(entry.get("id"), "GitHub release inventory ID")
        if entry["tag_name"] == expected_tag:
            matches.append(release_id)
    if matches != [expected_release_id]:
        raise CandidateError("Candidate tag does not resolve to the expected numeric release ID")


def validate_candidate_workflow_run(
    metadata_path: Path,
    *,
    expected_repository: str,
    expected_run_id: int,
    expected_run_attempt: int,
    expected_source_commit: str,
) -> None:
    _validate_repository(expected_repository)
    _require_positive_int(expected_run_id, "Expected candidate workflow run ID")
    _require_positive_int(expected_run_attempt, "Expected candidate workflow run attempt")
    if SOURCE_COMMIT_PATTERN.fullmatch(expected_source_commit) is None:
        raise CandidateError("Expected candidate workflow source commit is invalid")
    metadata = _load_json(metadata_path)
    if type(metadata) is not dict or set(metadata) != CANDIDATE_WORKFLOW_RUN_KEYS:
        raise CandidateError("Candidate workflow run metadata does not use the closed schema")
    expected = {
        "conclusion": "success",
        "event": "workflow_dispatch",
        "head_branch": "stable",
        "head_sha": expected_source_commit,
        "id": expected_run_id,
        "path": ".github/workflows/openevo-desktop-candidate.yml",
        "repository": expected_repository,
        "run_attempt": expected_run_attempt,
        "status": "completed",
    }
    if metadata != expected:
        raise CandidateError("Candidate workflow run identity or result is invalid")


def validate_published_tag_target(
    inventory_path: Path,
    *,
    expected_tag: str,
    expected_source_commit: str,
) -> None:
    if SOURCE_COMMIT_PATTERN.fullmatch(expected_source_commit) is None:
        raise CandidateError("Expected published source commit is invalid")
    try:
        lines = inventory_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise CandidateError("Published tag inventory is unreadable") from exc
    expected_line = f"{expected_source_commit}\trefs/tags/{expected_tag}"
    if lines != [expected_line]:
        raise CandidateError("Published Preview tag does not point to the expected source commit")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    write_notes = subparsers.add_parser("write-notes")
    write_notes.add_argument("output", type=Path)
    write_notes.add_argument("--source-commit", required=True)
    write_notes.add_argument("--version", required=True)
    write_notes.add_argument("--architecture", required=True)
    write_playwright = subparsers.add_parser("write-playwright-evidence")
    write_playwright.add_argument("output", type=Path)
    write_playwright.add_argument("--raw-report", type=Path, required=True)
    write_playwright.add_argument("--sanitized-report-output", type=Path, required=True)
    write_playwright.add_argument("--packaged-web-manifest", type=Path, required=True)
    write_playwright.add_argument("--source-commit", required=True)
    write_playwright.add_argument("--run-id", type=int, required=True)
    write_playwright.add_argument("--run-attempt", type=int, required=True)
    write_playwright.add_argument("--browser-version", required=True)
    write_draft_body = subparsers.add_parser("write-draft-body")
    write_draft_body.add_argument("output", type=Path)
    write_draft_body.add_argument("--release-notes", type=Path, required=True)
    write_draft_body.add_argument("--ownership-token", required=True)
    assert_release_absent = subparsers.add_parser("assert-release-absent")
    assert_release_absent.add_argument("inventory", type=Path)
    assert_release_absent.add_argument("--expected-tag", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("candidate_root", type=Path)
    create.add_argument("--source-commit", required=True)
    create.add_argument("--version", required=True)
    create.add_argument("--architecture", required=True)
    create.add_argument("--rust-target", required=True)
    create.add_argument("--registry-digest", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("manifest", type=Path)
    validate.add_argument("--expected-source-commit")
    validate.add_argument("--expected-core-platform")
    validate_playwright = subparsers.add_parser("validate-playwright-evidence")
    validate_playwright.add_argument("evidence", type=Path)
    validate_playwright.add_argument("--report", type=Path, required=True)
    validate_playwright.add_argument("--packaged-web-manifest", type=Path, required=True)
    validate_playwright.add_argument("--expected-source-commit")
    validate_playwright.add_argument("--expected-run-id", type=int)
    validate_playwright.add_argument("--expected-run-attempt", type=int)
    validate_draft = subparsers.add_parser("validate-draft")
    validate_draft.add_argument("metadata", type=Path)
    validate_draft.add_argument("--release-notes", type=Path, required=True)
    validate_draft.add_argument("--expected-tag", required=True)
    validate_draft.add_argument("--expected-target", required=True)
    validate_draft.add_argument("--expected-title", required=True)
    validate_draft.add_argument("--expected-repository", required=True)
    validate_draft.add_argument("--expected-owner", required=True)
    validate_draft.add_argument("--release-id-output", type=Path)
    snapshot_preview = subparsers.add_parser("snapshot-preview")
    snapshot_preview.add_argument("output", type=Path)
    snapshot_preview.add_argument("--metadata", type=Path, required=True)
    snapshot_preview.add_argument("--candidate-root", type=Path, required=True)
    snapshot_preview.add_argument("--baseline", type=Path)
    snapshot_preview.add_argument("--expected-repository", required=True)
    snapshot_preview.add_argument("--expected-release-id", type=int, required=True)
    snapshot_preview.add_argument("--expected-tag", required=True)
    snapshot_preview.add_argument("--expected-source-commit", required=True)
    snapshot_preview.add_argument("--expected-manifest-sha256", required=True)
    snapshot_preview.add_argument("--expected-run-id", type=int, required=True)
    snapshot_preview.add_argument("--expected-run-attempt", type=int, required=True)
    snapshot_preview.add_argument("--state", choices=("draft", "public"), required=True)
    asset_plan = subparsers.add_parser("write-preview-asset-plan")
    asset_plan.add_argument("output", type=Path)
    asset_plan.add_argument("--metadata", type=Path, required=True)
    asset_plan.add_argument("--baseline", type=Path, required=True)
    asset_plan.add_argument("--state", choices=("draft", "public"), required=True)
    validate_snapshot = subparsers.add_parser("validate-preview-snapshot")
    validate_snapshot.add_argument("snapshot", type=Path)
    validate_snapshot.add_argument("--expected-repository", required=True)
    validate_snapshot.add_argument("--expected-release-id", type=int, required=True)
    validate_snapshot.add_argument("--expected-tag", required=True)
    validate_snapshot.add_argument("--expected-source-commit", required=True)
    validate_snapshot.add_argument("--expected-manifest-sha256", required=True)
    validate_snapshot.add_argument("--expected-run-id", type=int, required=True)
    validate_snapshot.add_argument("--expected-run-attempt", type=int, required=True)
    assert_release_id = subparsers.add_parser("assert-release-id")
    assert_release_id.add_argument("inventory", type=Path)
    assert_release_id.add_argument("--expected-tag", required=True)
    assert_release_id.add_argument("--expected-release-id", type=int, required=True)
    validate_tag = subparsers.add_parser("validate-published-tag")
    validate_tag.add_argument("inventory", type=Path)
    validate_tag.add_argument("--expected-tag", required=True)
    validate_tag.add_argument("--expected-source-commit", required=True)
    validate_run = subparsers.add_parser("validate-candidate-run")
    validate_run.add_argument("metadata", type=Path)
    validate_run.add_argument("--expected-repository", required=True)
    validate_run.add_argument("--expected-run-id", type=int, required=True)
    validate_run.add_argument("--expected-run-attempt", type=int, required=True)
    validate_run.add_argument("--expected-source-commit", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "write-notes":
            write_candidate_release_notes(
                args.output,
                source_commit=args.source_commit,
                version=args.version,
                architecture=args.architecture,
            )
            print(args.output)
            return 0
        if args.command == "write-playwright-evidence":
            write_playwright_candidate_evidence(
                args.output,
                raw_report_path=args.raw_report,
                sanitized_report_path=args.sanitized_report_output,
                packaged_web_manifest_path=args.packaged_web_manifest,
                source_commit=args.source_commit,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                browser_version=args.browser_version,
            )
            print(args.output)
            return 0
        if args.command == "write-draft-body":
            write_draft_release_body(
                args.output,
                release_notes=args.release_notes,
                ownership_token=args.ownership_token,
            )
            print(args.output)
            return 0
        if args.command == "assert-release-absent":
            assert_release_tag_absent(
                args.inventory,
                expected_tag=args.expected_tag,
            )
            print(f"GitHub release tag is absent: {args.expected_tag}")
            return 0
        if args.command == "create":
            path = create_candidate_manifest(
                args.candidate_root,
                source_commit=args.source_commit,
                version=args.version,
                architecture=args.architecture,
                rust_target=args.rust_target,
                registry_digest=args.registry_digest,
            )
            print(path)
            return 0
        if args.command == "validate-playwright-evidence":
            _validate_playwright_candidate_evidence(
                args.evidence,
                report_path=args.report,
                packaged_web_manifest_path=args.packaged_web_manifest,
                expected_source_commit=args.expected_source_commit,
                expected_run_id=args.expected_run_id,
                expected_run_attempt=args.expected_run_attempt,
            )
            print(f"OpenEvo Playwright evidence validation passed: {args.evidence}")
            return 0
        if args.command == "validate-draft":
            release_id = validated_draft_release_id(
                args.metadata,
                release_notes=args.release_notes,
                expected_tag=args.expected_tag,
                expected_target=args.expected_target,
                expected_title=args.expected_title,
                expected_repository=args.expected_repository,
                expected_owner=args.expected_owner,
            )
            if args.release_id_output is not None:
                _write_private_new(
                    args.release_id_output,
                    f"{release_id}\n".encode("ascii"),
                )
            print(f"OpenEvo draft release metadata validation passed: {args.metadata}")
            return 0
        if args.command == "snapshot-preview":
            write_preview_release_snapshot(
                args.output,
                metadata_path=args.metadata,
                candidate_root=args.candidate_root,
                baseline_path=args.baseline,
                expected_repository=args.expected_repository,
                expected_release_id=args.expected_release_id,
                expected_tag=args.expected_tag,
                expected_source_commit=args.expected_source_commit,
                expected_manifest_sha256=args.expected_manifest_sha256,
                expected_run_id=args.expected_run_id,
                expected_run_attempt=args.expected_run_attempt,
                expected_draft=args.state == "draft",
            )
            print(args.output)
            return 0
        if args.command == "write-preview-asset-plan":
            write_preview_asset_plan(
                args.output,
                metadata_path=args.metadata,
                baseline_path=args.baseline,
                expected_draft=args.state == "draft",
            )
            print(args.output)
            return 0
        if args.command == "validate-preview-snapshot":
            validate_preview_release_snapshot_identity(
                args.snapshot,
                expected_repository=args.expected_repository,
                expected_release_id=args.expected_release_id,
                expected_tag=args.expected_tag,
                expected_source_commit=args.expected_source_commit,
                expected_manifest_sha256=args.expected_manifest_sha256,
                expected_run_id=args.expected_run_id,
                expected_run_attempt=args.expected_run_attempt,
            )
            print(f"OpenEvo Preview snapshot validation passed: {args.snapshot}")
            return 0
        if args.command == "assert-release-id":
            assert_release_id_inventory(
                args.inventory,
                expected_tag=args.expected_tag,
                expected_release_id=args.expected_release_id,
            )
            print(f"GitHub release ID is validated: {args.expected_release_id}")
            return 0
        if args.command == "validate-published-tag":
            validate_published_tag_target(
                args.inventory,
                expected_tag=args.expected_tag,
                expected_source_commit=args.expected_source_commit,
            )
            print(f"Published tag target is validated: {args.expected_tag}")
            return 0
        if args.command == "validate-candidate-run":
            validate_candidate_workflow_run(
                args.metadata,
                expected_repository=args.expected_repository,
                expected_run_id=args.expected_run_id,
                expected_run_attempt=args.expected_run_attempt,
                expected_source_commit=args.expected_source_commit,
            )
            print(f"Candidate workflow run is validated: {args.expected_run_id}")
            return 0
        errors = validate_candidate_manifest(
            args.manifest,
            expected_source_commit=args.expected_source_commit,
            expected_core_platform=args.expected_core_platform,
        )
        if errors:
            raise CandidateError("; ".join(errors))
        print(f"OpenEvo candidate validation passed: {args.manifest}")
        return 0
    except CandidateError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
