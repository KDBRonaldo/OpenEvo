#!/usr/bin/env python3
"""Development-only loopback daemon for real Codex turns and document evolution.

This is intentionally not the release OpenEvo Daemon and must never be exposed directly to a
network. It reuses the real document-reflector implementations without claiming the sealed
release orchestration contract. Bind it to loopback and reach it only through an SSH tunnel.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import zipfile
from urllib.parse import parse_qs, quote, urlsplit
from xml.etree import ElementTree
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator

from pypdf import PdfReader
from pydantic import ValidationError


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if os.fspath(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(SOURCE_ROOT))

from openevo.backend.evolution_runtime import codex_development_runtime_adapter  # noqa: E402
from openevo.backend.harness_adapter import (  # noqa: E402
    CodexHarnessAdapter,
    HarnessCancellation,
    HarnessRunCancelled,
    HarnessRunError,
)
from openevo.backend.contracts.v2 import models as core_v2  # noqa: E402
try:  # package import in tests; direct import when launched as a script
    from scripts.dev.development_agent_v2_contract import (  # noqa: E402
        DevelopmentArtifactPageV2,
        DevelopmentArtifactV2,
        DevelopmentEvolutionJobPageV2,
        DevelopmentEvolutionJobRetryV2,
        DevelopmentEvolutionJobV2,
        DevelopmentEvolutionRunApplyV2,
        DevelopmentEvolutionRunCreateV2,
        DevelopmentEvolutionRunPageV2,
        DevelopmentEvolutionRunV2,
        DevelopmentTaskObservationPageV2,
        DevelopmentTaskObservationV2,
        DevelopmentTaskTimelinePageV2,
        DevelopmentWorkspaceDeleteV2,
        DevelopmentWorkspaceMutationV2,
        DevelopmentWorkspacePageV2,
    )
except ModuleNotFoundError:  # pragma: no cover - exercised by the remote launcher
    from development_agent_v2_contract import (  # type: ignore[no-redef]  # noqa: E402
        DevelopmentArtifactPageV2,
        DevelopmentArtifactV2,
        DevelopmentEvolutionJobPageV2,
        DevelopmentEvolutionJobRetryV2,
        DevelopmentEvolutionJobV2,
        DevelopmentEvolutionRunApplyV2,
        DevelopmentEvolutionRunCreateV2,
        DevelopmentEvolutionRunPageV2,
        DevelopmentEvolutionRunV2,
        DevelopmentTaskObservationPageV2,
        DevelopmentTaskObservationV2,
        DevelopmentTaskTimelinePageV2,
        DevelopmentWorkspaceDeleteV2,
        DevelopmentWorkspaceMutationV2,
        DevelopmentWorkspacePageV2,
    )


MAX_REQUEST_BYTES = 256 * 1024
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_WORKSPACE_ENTRIES = 1_000
MAX_WORKSPACE_TEXT_FILE_BYTES = 256 * 1024
MAX_WORKSPACE_TEXT_BYTES = 2 * 1024 * 1024
MAX_WORKSPACE_PDF_FILE_BYTES = 16 * 1024 * 1024
MAX_WORKSPACE_PDF_PAGES = 200
MAX_WORKSPACE_DOCUMENT_FILE_BYTES = 32 * 1024 * 1024
MAX_WORKSPACE_ARCHIVE_ENTRIES = 2_000
MAX_WORKSPACE_ARCHIVE_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_WORKSPACE_ARCHIVE_MEMBER_BYTES = 8 * 1024 * 1024
MAX_WORKSPACE_MUTATIONS = 64
MAX_WORKSPACE_WRITE_FILE_BYTES = 192 * 1024
MAX_WORKSPACE_WRITE_BYTES = 256 * 1024
MAX_WORKSPACE_UPLOAD_FILE_BYTES = 32 * 1024 * 1024
MAX_WORKSPACE_DOWNLOAD_FILE_BYTES = 64 * 1024 * 1024
MAX_WORKSPACE_TOTAL_BYTES = 512 * 1024 * 1024
MAX_AGENT_WORKSPACE_CONTEXT_BYTES = 512 * 1024
MAX_DEVELOPMENT_STATE_EVENTS = 4_096
MAX_DEVELOPMENT_EVENT_PAGE = 100
MAX_DEVELOPMENT_EVENT_WAIT_SECONDS = 10.0
DEFAULT_TIMEOUT_SECONDS = 300
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ALLOWED_REQUEST_FIELDS = {
    "schema_version",
    "project_id",
    "project_name",
    "task_title",
    "instruction",
}
ALLOWED_PROJECT_FIELDS = {"schema_version", "project_id", "display_name", "config"}
ALLOWED_PROJECT_UPDATE_FIELDS = {"schema_version", "display_name", "config"}
PROJECT_PATH_PATTERN = re.compile(r"^/openevo-dev-agent/v1/projects/([^/]+)$")
ACTIVATE_PATH_PATTERN = re.compile(r"^/openevo-dev-agent/v1/projects/([^/]+)/activate$")
SESSION_PATH_PATTERN = re.compile(r"^/openevo-dev-agent/v1/sessions/([^/]+)$")
SESSION_CANCEL_PATH_PATTERN = re.compile(
    r"^/openevo-dev-agent/v1/sessions/([^/]+)/cancel$"
)
EVOLUTION_JOB_RETRY_PATH_PATTERN = re.compile(
    r"^/openevo-dev-agent/v1/evolution-jobs/([^/]+)/retry$"
)
EVOLUTION_RUN_APPLY_PATH_PATTERN = re.compile(
    r"^/openevo-dev-agent/v1/evolution-runs/([^/]+)/apply$"
)
WORKSPACE_FILES_PATH_PATTERN = re.compile(
    r"^/openevo-dev-agent/v1/projects/([^/]+)/workspace/files$"
)
DEVELOPMENT_EVENTS_PATH = "/openevo-dev-agent/v1/events"
DAEMON_V2_TASKS_PATH = "/v2/tasks"
DAEMON_V2_TASK_PATH_PATTERN = re.compile(r"^/v2/tasks/([^/]+)$")
DAEMON_V2_TASK_LOGS_PATH_PATTERN = re.compile(r"^/v2/tasks/([^/]+)/logs$")
DAEMON_V2_TASK_TIMELINE_PATH_PATTERN = re.compile(r"^/v2/tasks/([^/]+)/timeline$")
DAEMON_V2_TASK_ARTIFACTS_PATH_PATTERN = re.compile(r"^/v2/tasks/([^/]+)/artifacts$")
DAEMON_V2_ARTIFACT_CONTENT_PATH_PATTERN = re.compile(
    r"^/v2/artifacts/([^/]+)/content$"
)
DAEMON_V2_ARTIFACT_PATH_PATTERN = re.compile(r"^/v2/artifacts/([^/]+)$")
DAEMON_V2_DEVELOPMENT_ARTIFACTS_PATH = "/v2/development/artifacts"
DAEMON_V2_DEVELOPMENT_ARTIFACT_PATH_PATTERN = re.compile(
    r"^/v2/development/artifacts/([^/]+)$"
)
DAEMON_V2_DEVELOPMENT_EVOLUTION_RUNS_PATH = "/v2/development/evolution-runs"
DAEMON_V2_DEVELOPMENT_EVOLUTION_JOBS_PATH = "/v2/development/evolution-jobs"
DAEMON_V2_DEVELOPMENT_EVOLUTION_JOB_RETRY_PATH_PATTERN = re.compile(
    r"^/v2/development/evolution-jobs/([^/]+)/retry$"
)
DAEMON_V2_DEVELOPMENT_EVOLUTION_JOB_PATH_PATTERN = re.compile(
    r"^/v2/development/evolution-jobs/([^/]+)$"
)
DAEMON_V2_DEVELOPMENT_EVOLUTION_RUN_APPLY_PATH_PATTERN = re.compile(
    r"^/v2/development/evolution-runs/([^/]+)/apply$"
)
DAEMON_V2_DEVELOPMENT_EVOLUTION_RUN_PATH_PATTERN = re.compile(
    r"^/v2/development/evolution-runs/([^/]+)$"
)
DAEMON_V2_WORKSPACE_PATH_PATTERN = re.compile(
    r"^/v2/projects/([^/]+)/workspace$"
)
DAEMON_V2_WORKSPACE_FILES_PATH_PATTERN = re.compile(
    r"^/v2/projects/([^/]+)/workspace/files$"
)
MAX_DAEMON_V2_LOG_PAGE = 100
MAX_DAEMON_V2_TASK_PAGE = 100
MAX_DAEMON_V2_WORKSPACE_PAGE = 100
MAX_DAEMON_V2_ARTIFACT_PAGE = 100
MAX_DAEMON_V2_DEVELOPMENT_ARTIFACT_PAGE = 5
MAX_DAEMON_V2_EVOLUTION_RUN_PAGE = 25
MAX_DAEMON_V2_EVOLUTION_JOB_PAGE = 25
MAX_DAEMON_V2_LOG_TEXT = 16_384


class RequestError(ValueError):
    pass


class AgentRunError(RuntimeError):
    pass


class EvolutionRunError(RuntimeError):
    pass


class StateConflictError(RuntimeError):
    pass


class EventCursorExpiredError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def development_registry_snapshot() -> Any:
    """Build the explicit unverified catalog used only by this development bridge."""

    from openevo.evolution.framework.builtins import (
        ImplementationDistributionIdentity,
        build_builtin_registry,
    )

    identity = ImplementationDistributionIdentity(
        distribution="openevo",
        distribution_version="0.1.10.dev0",
        distribution_digest=hashlib.sha256(
            b"openevo-development-catalog-v1"
        ).hexdigest(),
    )
    return build_builtin_registry(identity)


def selected_document_evolution(config: object) -> list[dict[str, Any]]:
    if not isinstance(config, dict):
        return []
    evolution = config.get("evolution")
    targets = evolution.get("targets") if isinstance(evolution, dict) else None
    if not isinstance(targets, dict):
        return []
    selected: list[dict[str, Any]] = []
    for target_id, selection in sorted(targets.items()):
        if not isinstance(target_id, str) or not ID_PATTERN.fullmatch(target_id):
            continue
        if not isinstance(selection, dict) or selection.get("enabled") is not True:
            continue
        method = selection.get("method")
        method_config = selection.get("config", {})
        if not isinstance(method, str) or not ID_PATTERN.fullmatch(method):
            continue
        if not isinstance(method_config, dict):
            continue
        selected.append({
            "target_id": target_id,
            "method": method,
            "config": method_config,
        })
    return selected


def normalize_selected_evolution(value: object) -> list[dict[str, Any]]:
    """Upgrade pre-capability session selections to the generic selection shape."""

    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for selection in value:
        if not isinstance(selection, dict):
            continue
        target_id = selection.get("target_id")
        method = selection.get("method")
        config = selection.get("config", {})
        if not isinstance(target_id, str) or not ID_PATTERN.fullmatch(target_id):
            continue
        if not isinstance(method, str) or not ID_PATTERN.fullmatch(method):
            continue
        if not isinstance(config, dict):
            config = {}
        normalized.append({
            "target_id": target_id,
            "method": method,
            "config": config,
        })
    return normalized


def validate_evolution_run_request(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RequestError("request body must be a JSON object")
    unknown = set(payload) - {"schema_version", "project_id", "session_ids", "selections"}
    if unknown:
        raise RequestError(f"unknown request fields: {', '.join(sorted(unknown))}")
    if payload.get("schema_version") != "1":
        raise RequestError("schema_version must be '1'")
    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not ID_PATTERN.fullmatch(project_id):
        raise RequestError("project_id is invalid")
    session_ids = payload.get("session_ids")
    if not isinstance(session_ids, list) or not session_ids or len(session_ids) > 128:
        raise RequestError("session_ids must contain between 1 and 128 sessions")
    if any(not isinstance(value, str) or not ID_PATTERN.fullmatch(value) for value in session_ids):
        raise RequestError("session_ids contains an invalid session id")
    if len(set(session_ids)) != len(session_ids):
        raise RequestError("session_ids must not contain duplicates")
    selections = normalize_selected_evolution(payload.get("selections"))
    if not selections:
        raise RequestError("selections must contain at least one enabled Evolution method")
    if len(selections) != len(payload.get("selections", [])):
        raise RequestError("selections contains an invalid Evolution method")
    if len({selection["target_id"] for selection in selections}) != len(selections):
        raise RequestError("selections must contain at most one method per target")
    return {
        "project_id": project_id,
        "session_ids": session_ids,
        "selections": selections,
    }


def validate_project_request(payload: object, *, updating: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RequestError("request body must be a JSON object")
    allowed = ALLOWED_PROJECT_UPDATE_FIELDS if updating else ALLOWED_PROJECT_FIELDS
    unknown = set(payload) - allowed
    if unknown:
        raise RequestError(f"unknown request fields: {', '.join(sorted(unknown))}")
    if payload.get("schema_version") != "1":
        raise RequestError("schema_version must be '1'")
    result: dict[str, Any] = {}
    if not updating:
        project_id = payload.get("project_id")
        if not isinstance(project_id, str) or not ID_PATTERN.fullmatch(project_id):
            raise RequestError("project_id is invalid")
        result["project_id"] = project_id
    display_name = payload.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 200:
        raise RequestError("display_name must be a non-empty string of at most 200 characters")
    config = payload.get("config")
    if not isinstance(config, dict):
        raise RequestError("config must be a JSON object")
    if len(canonical_json(config).encode("utf-8")) > 192 * 1024:
        raise RequestError("config is too large")
    result["display_name"] = display_name.strip()
    result["config"] = config
    return result


class ProjectWorkspaceStore:
    """Own persistent per-project scratch directories and bounded readable projections."""

    def __init__(self, root: Path) -> None:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("development workspace root must be a real directory")
        self.root = root.resolve(strict=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass

    def ensure_project(self, project_id: str) -> Path:
        path = self._project_path(project_id)
        path.mkdir(mode=0o700, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError("project workspace must be a real directory")
        try:
            path.chmod(0o700)
        except OSError:
            pass
        return path

    def project_path(self, project_id: str) -> Path:
        path = self.ensure_project(project_id)
        if path.resolve(strict=True).parent != self.root:
            raise RuntimeError("project workspace escaped the managed root")
        return path

    def snapshot(self, project_id: str) -> dict[str, Any]:
        project_root = self.project_path(project_id)
        entries: list[dict[str, Any]] = []
        remaining_text_bytes = MAX_WORKSPACE_TEXT_BYTES
        truncated = False

        def walk(directory: Path, relative_directory: Path) -> None:
            nonlocal remaining_text_bytes, truncated
            try:
                children = sorted(os.scandir(directory), key=lambda entry: entry.name)
            except OSError:
                truncated = True
                return
            for child in children:
                if len(entries) >= MAX_WORKSPACE_ENTRIES:
                    truncated = True
                    return
                if child.name in {".git", ".openevo"}:
                    continue
                relative = relative_directory / child.name
                relative_text = relative.as_posix()
                try:
                    stat_result = child.stat(follow_symlinks=False)
                except OSError:
                    entries.append(self._unreadable_entry(relative_text))
                    continue
                modified_at = datetime.fromtimestamp(
                    stat_result.st_mtime, timezone.utc
                ).isoformat().replace("+00:00", "Z")
                if child.is_symlink():
                    entries.append({
                        "path": relative_text,
                        "kind": "symlink",
                        "byte_size": 0,
                        "content_sha256": None,
                        "media_type": None,
                        "content": None,
                        "modified_at": modified_at,
                    })
                    continue
                if child.is_dir(follow_symlinks=False):
                    entries.append({
                        "path": relative_text,
                        "kind": "directory",
                        "byte_size": 0,
                        "content_sha256": None,
                        "media_type": None,
                        "content": None,
                        "modified_at": modified_at,
                    })
                    walk(Path(child.path), relative)
                    if truncated:
                        return
                    continue
                if not child.is_file(follow_symlinks=False):
                    entries.append(self._unreadable_entry(relative_text, modified_at))
                    continue
                size = stat_result.st_size
                content: str | None = None
                digest: str | None = None
                media_type = mimetypes.guess_type(child.name)[0] or "application/octet-stream"
                suffix = Path(child.name).suffix.lower()
                if (
                    media_type == "application/pdf"
                    and size <= MAX_WORKSPACE_PDF_FILE_BYTES
                    and remaining_text_bytes > 0
                ):
                    content = self._extract_pdf_text(
                        Path(child.path),
                        min(MAX_WORKSPACE_TEXT_FILE_BYTES, remaining_text_bytes),
                    )
                    if content is not None:
                        remaining_text_bytes -= len(content.encode("utf-8"))
                elif (
                    suffix in {".docx", ".pptx", ".xlsx", ".xlsm"}
                    and size <= MAX_WORKSPACE_DOCUMENT_FILE_BYTES
                    and remaining_text_bytes > 0
                ):
                    content = self._extract_ooxml_text(
                        Path(child.path),
                        min(MAX_WORKSPACE_TEXT_FILE_BYTES, remaining_text_bytes),
                    )
                    if content is not None:
                        remaining_text_bytes -= len(content.encode("utf-8"))
                elif (
                    suffix in {".zip", ".whl"}
                    and size <= MAX_WORKSPACE_DOCUMENT_FILE_BYTES
                    and remaining_text_bytes > 0
                ):
                    content = self._extract_zip_listing(
                        Path(child.path),
                        min(MAX_WORKSPACE_TEXT_FILE_BYTES, remaining_text_bytes),
                    )
                    if content is not None:
                        remaining_text_bytes -= len(content.encode("utf-8"))
                elif size <= MAX_WORKSPACE_TEXT_FILE_BYTES and size <= remaining_text_bytes:
                    try:
                        payload = Path(child.path).read_bytes()
                        if len(payload) != size:
                            raise OSError("workspace file changed while being read")
                        digest = hashlib.sha256(payload).hexdigest()
                        if b"\x00" not in payload:
                            content = payload.decode("utf-8")
                            remaining_text_bytes -= len(payload)
                            if media_type == "application/octet-stream":
                                media_type = "text/plain"
                    except (OSError, UnicodeDecodeError):
                        content = None
                entries.append({
                    "path": relative_text,
                    "kind": "file",
                    "byte_size": size,
                    "content_sha256": digest,
                    "media_type": media_type,
                    "content": content,
                    "modified_at": modified_at,
                })

        walk(project_root, Path())
        return {
            "project_id": project_id,
            "entries": entries,
            "truncated": truncated,
        }

    def authoritative_snapshot_v2(self, project_id: str) -> dict[str, Any]:
        """Return a digest-complete snapshot for the development daemon v2 boundary."""

        snapshot = self.snapshot(project_id)
        project_root = self.project_path(project_id)
        entries: list[dict[str, Any]] = []
        for raw in snapshot["entries"]:
            entry = dict(raw)
            if entry["kind"] == "file":
                original_digest = entry["content_sha256"]
                digest, modified_at = self._file_sha256_v2(
                    project_root,
                    entry["path"],
                    expected_size=entry["byte_size"],
                )
                if original_digest is not None and original_digest != digest:
                    raise RequestError("workspace file changed while it was inventoried")
                entry["content_sha256"] = digest
                entry["modified_at"] = modified_at
            entries.append(entry)
        authority = {
            "project_id": project_id,
            "entries": entries,
            "truncated": snapshot["truncated"],
        }
        return {
            **authority,
            "manifest_sha256": hashlib.sha256(
                canonical_json(authority).encode("utf-8")
            ).hexdigest(),
        }

    @classmethod
    def _file_sha256_v2(
        cls,
        project_root: Path,
        relative_path: str,
        *,
        expected_size: int,
    ) -> tuple[str, str]:
        path = cls._workspace_path(project_root, relative_path, actor="inventory")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise RequestError("workspace file changed while it was inventoried") from exc
        digest = hashlib.sha256()
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size != expected_size
            ):
                raise RequestError("workspace inventory only accepts single-link regular files")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            after = os.fstat(descriptor)
            try:
                bound = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise RequestError("workspace file changed while it was inventoried") from exc
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or after.st_dev != bound.st_dev
                or after.st_ino != bound.st_ino
                or not stat.S_ISREG(bound.st_mode)
            ):
                raise RequestError("workspace file changed while it was inventoried")
        finally:
            os.close(descriptor)
        modified_at = datetime.fromtimestamp(
            after.st_mtime, timezone.utc
        ).isoformat().replace("+00:00", "Z")
        return digest.hexdigest(), modified_at

    @staticmethod
    def _extract_pdf_text(path: Path, byte_limit: int) -> str | None:
        """Return a bounded text projection for a text-based PDF.

        The original PDF remains the authoritative workspace file. This projection only lets
        the read-only harness reason over its text without receiving host filesystem access.
        """

        if byte_limit <= 0:
            return None
        try:
            reader = PdfReader(path, strict=False)
        except Exception:
            return None
        chunks = [f"[Text extracted from PDF: {path.name}]\n"]
        consumed = len(chunks[0].encode("utf-8"))
        truncated = len(reader.pages) > MAX_WORKSPACE_PDF_PAGES
        for page_number, page in enumerate(reader.pages[:MAX_WORKSPACE_PDF_PAGES], start=1):
            try:
                page_text = page.extract_text() or ""
            except Exception:
                continue
            if not page_text.strip():
                continue
            section = f"\n--- Page {page_number} ---\n{page_text.strip()}\n"
            encoded = section.encode("utf-8")
            if consumed + len(encoded) > byte_limit:
                available = max(0, byte_limit - consumed)
                if available:
                    chunks.append(encoded[:available].decode("utf-8", errors="ignore"))
                truncated = True
                break
            chunks.append(section)
            consumed += len(encoded)
        if len(chunks) == 1:
            return None
        if truncated:
            marker = "\n[PDF text projection truncated by OpenEvo.]\n"
            encoded_marker = marker.encode("utf-8")
            rendered = "".join(chunks)
            rendered_bytes = rendered.encode("utf-8")
            if len(encoded_marker) <= byte_limit:
                rendered = (
                    rendered_bytes[: byte_limit - len(encoded_marker)]
                    .decode("utf-8", errors="ignore")
                    + marker
                )
            return rendered
        return "".join(chunks)

    @staticmethod
    def _bounded_projection(
        header: str,
        sections: list[str],
        byte_limit: int,
        *,
        truncated: bool = False,
    ) -> str | None:
        if not sections or byte_limit <= 0:
            return None
        marker = "\n[Document projection truncated by OpenEvo.]\n"
        rendered = header + "".join(sections)
        encoded = rendered.encode("utf-8")
        if len(encoded) <= byte_limit and not truncated:
            return rendered
        marker_bytes = marker.encode("utf-8")
        if len(marker_bytes) >= byte_limit:
            return encoded[:byte_limit].decode("utf-8", errors="ignore")
        return (
            encoded[: byte_limit - len(marker_bytes)].decode("utf-8", errors="ignore")
            + marker
        )

    @staticmethod
    def _safe_archive(path: Path) -> tuple[zipfile.ZipFile, list[zipfile.ZipInfo]]:
        archive = zipfile.ZipFile(path)
        infos = archive.infolist()
        if len(infos) > MAX_WORKSPACE_ARCHIVE_ENTRIES:
            archive.close()
            raise ValueError("archive has too many entries")
        expanded = 0
        for info in infos:
            if info.is_dir():
                continue
            expanded += info.file_size
            if expanded > MAX_WORKSPACE_ARCHIVE_EXPANDED_BYTES:
                archive.close()
                raise ValueError("archive expands beyond the document budget")
            if info.file_size > MAX_WORKSPACE_ARCHIVE_MEMBER_BYTES:
                continue
            if info.compress_size and info.file_size > info.compress_size * 200:
                archive.close()
                raise ValueError("archive member has an unsafe compression ratio")
        return archive, infos

    @classmethod
    def _extract_ooxml_text(cls, path: Path, byte_limit: int) -> str | None:
        """Project common Office Open XML formats into bounded plain text."""

        try:
            archive, infos = cls._safe_archive(path)
        except (OSError, ValueError, zipfile.BadZipFile):
            return None
        suffix = path.suffix.lower()
        sections: list[str] = []
        truncated = False
        try:
            if suffix in {".xlsx", ".xlsm"}:
                return cls._extract_spreadsheet_xml(
                    path.name, archive, infos, byte_limit
                )
            names = [info.filename for info in infos if not info.is_dir()]
            if suffix == ".docx":
                selected = [
                    name
                    for name in names
                    if name == "word/document.xml"
                    or re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)
                    or name in {"word/footnotes.xml", "word/endnotes.xml"}
                ]
            else:
                selected = [
                    name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
                ]
                selected.sort(key=lambda value: int(re.search(r"(\d+)", value).group(1)))
            info_by_name = {info.filename: info for info in infos}
            for name in selected:
                info = info_by_name[name]
                if info.file_size > MAX_WORKSPACE_ARCHIVE_MEMBER_BYTES:
                    truncated = True
                    continue
                try:
                    root = ElementTree.fromstring(archive.read(info))
                except (KeyError, OSError, ElementTree.ParseError):
                    continue
                values = [
                    element.text.strip()
                    for element in root.iter()
                    if element.tag.rsplit("}", 1)[-1] == "t"
                    and element.text
                    and element.text.strip()
                ]
                if values:
                    sections.append(f"\n--- {name} ---\n" + "\n".join(values) + "\n")
        finally:
            archive.close()
        return cls._bounded_projection(
            f"[Text extracted from {suffix[1:].upper()}: {path.name}]\n",
            sections,
            byte_limit,
            truncated=truncated,
        )

    @classmethod
    def _extract_spreadsheet_xml(
        cls,
        file_name: str,
        archive: zipfile.ZipFile,
        infos: list[zipfile.ZipInfo],
        byte_limit: int,
    ) -> str | None:
        info_by_name = {info.filename: info for info in infos}
        shared_strings: list[str] = []
        shared_info = info_by_name.get("xl/sharedStrings.xml")
        if shared_info is not None and shared_info.file_size <= MAX_WORKSPACE_ARCHIVE_MEMBER_BYTES:
            try:
                root = ElementTree.fromstring(archive.read(shared_info))
                for item in root.iter():
                    if item.tag.rsplit("}", 1)[-1] != "si":
                        continue
                    shared_strings.append("".join(
                        node.text or ""
                        for node in item.iter()
                        if node.tag.rsplit("}", 1)[-1] == "t"
                    ))
            except (OSError, ElementTree.ParseError):
                shared_strings = []
        sheet_names = sorted(
            (
                info.filename
                for info in infos
                if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", info.filename)
            ),
            key=lambda value: int(re.search(r"(\d+)", value).group(1)),
        )
        sections: list[str] = []
        truncated = False
        for name in sheet_names:
            info = info_by_name[name]
            if info.file_size > MAX_WORKSPACE_ARCHIVE_MEMBER_BYTES:
                truncated = True
                continue
            try:
                root = ElementTree.fromstring(archive.read(info))
            except (OSError, ElementTree.ParseError):
                continue
            cells: list[str] = []
            for cell in root.iter():
                if cell.tag.rsplit("}", 1)[-1] != "c":
                    continue
                coordinate = cell.attrib.get("r", "?")
                cell_type = cell.attrib.get("t")
                raw_value = next(
                    (
                        node.text
                        for node in cell
                        if node.tag.rsplit("}", 1)[-1] == "v" and node.text is not None
                    ),
                    None,
                )
                inline = "".join(
                    node.text or ""
                    for node in cell.iter()
                    if node.tag.rsplit("}", 1)[-1] == "t"
                )
                value = inline or raw_value or ""
                if cell_type == "s" and raw_value is not None:
                    try:
                        value = shared_strings[int(raw_value)]
                    except (IndexError, ValueError):
                        value = raw_value
                if value:
                    cells.append(f"{coordinate}={value}")
            if cells:
                sections.append(f"\n--- {name} ---\n" + "\n".join(cells) + "\n")
        return cls._bounded_projection(
            f"[Cells extracted from spreadsheet: {file_name}]\n",
            sections,
            byte_limit,
            truncated=truncated,
        )

    @classmethod
    def _extract_zip_listing(cls, path: Path, byte_limit: int) -> str | None:
        try:
            archive, infos = cls._safe_archive(path)
        except (OSError, ValueError, zipfile.BadZipFile):
            return None
        try:
            sections = [
                f"{info.filename}\t{info.file_size} bytes\n"
                for info in infos
                if not info.is_dir()
            ]
        finally:
            archive.close()
        return cls._bounded_projection(
            f"[Safe archive listing: {path.name}]\n",
            sections,
            byte_limit,
        )

    def apply_mutations(self, project_id: str, mutations: object) -> None:
        """Apply a bounded Codex file plan without giving Codex host filesystem access."""

        if not isinstance(mutations, dict) or set(mutations) != {"file_writes", "delete_paths"}:
            raise AgentRunError("Codex returned an invalid workspace mutation plan")
        file_writes = mutations.get("file_writes")
        delete_paths = mutations.get("delete_paths")
        if not isinstance(file_writes, list) or not isinstance(delete_paths, list):
            raise AgentRunError("Codex returned an invalid workspace mutation plan")
        if len(file_writes) + len(delete_paths) > MAX_WORKSPACE_MUTATIONS:
            raise AgentRunError("Codex requested too many workspace mutations")

        project_root = self.project_path(project_id)
        normalized_writes: list[tuple[Path, bytes]] = []
        normalized_deletes: list[Path] = []
        seen: set[str] = set()
        total_bytes = 0
        for write in file_writes:
            if not isinstance(write, dict) or set(write) != {"path", "content"}:
                raise AgentRunError("Codex returned an invalid file write")
            path = self._mutation_path(project_root, write.get("path"))
            content = write.get("content")
            if not isinstance(content, str):
                raise AgentRunError("Codex returned a non-text file write")
            payload = content.encode("utf-8")
            if len(payload) > MAX_WORKSPACE_WRITE_FILE_BYTES:
                raise AgentRunError("Codex requested a workspace file that is too large")
            total_bytes += len(payload)
            if total_bytes > MAX_WORKSPACE_WRITE_BYTES:
                raise AgentRunError("Codex requested too much workspace output")
            identity = path.relative_to(project_root).as_posix()
            if identity in seen:
                raise AgentRunError("Codex requested duplicate workspace mutations")
            seen.add(identity)
            normalized_writes.append((path, payload))
        for value in delete_paths:
            path = self._mutation_path(project_root, value)
            identity = path.relative_to(project_root).as_posix()
            if identity in seen:
                raise AgentRunError("Codex requested duplicate workspace mutations")
            seen.add(identity)
            normalized_deletes.append(path)

        for path in normalized_deletes:
            if path.is_symlink():
                raise AgentRunError("Codex cannot delete workspace symlinks")
            if path.exists():
                if not path.is_file():
                    raise AgentRunError("Codex can only delete regular workspace files")
                path.unlink()
        for path, payload in normalized_writes:
            try:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                resolved_parent = path.parent.resolve(strict=True)
            except OSError as exc:
                raise AgentRunError(f"could not prepare workspace directory: {exc}") from exc
            if resolved_parent != project_root and project_root not in resolved_parent.parents:
                raise AgentRunError("Codex workspace write escaped the managed project")
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise AgentRunError("Codex can only replace regular workspace files")
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".openevo-write-",
                dir=resolved_parent,
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary_path.chmod(0o600)
                os.replace(temporary_path, path)
            except OSError as exc:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise AgentRunError(f"could not write workspace file: {exc}") from exc

    def upload_file(
        self,
        project_id: str,
        relative_path: object,
        payload: bytes,
        *,
        overwrite: bool,
    ) -> dict[str, Any]:
        """Atomically store one user-selected file inside a managed project workspace."""

        if len(payload) > MAX_WORKSPACE_UPLOAD_FILE_BYTES:
            raise RequestError(
                f"uploaded file exceeds the {MAX_WORKSPACE_UPLOAD_FILE_BYTES // (1024 * 1024)} MiB limit"
            )
        project_root = self.project_path(project_id)
        path = self._workspace_path(project_root, relative_path, actor="upload")
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            resolved_parent = path.parent.resolve(strict=True)
        except OSError as exc:
            raise RequestError(f"could not prepare the upload directory: {exc}") from exc
        if resolved_parent != project_root and project_root not in resolved_parent.parents:
            raise RequestError("upload path escaped the managed project workspace")
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RequestError("uploads may only replace regular workspace files")
        if path.exists() and not overwrite:
            raise StateConflictError("a workspace file already exists at this path")

        replaced_size = path.stat().st_size if path.exists() else 0
        if self._workspace_size(project_root) - replaced_size + len(payload) > MAX_WORKSPACE_TOTAL_BYTES:
            raise RequestError(
                f"project workspace exceeds the {MAX_WORKSPACE_TOTAL_BYTES // (1024 * 1024)} MiB limit"
            )

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".openevo-upload-",
            dir=resolved_parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.chmod(0o600)
            os.replace(temporary_path, path)
        except OSError as exc:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise RequestError(f"could not store the uploaded file: {exc}") from exc

        snapshot = self.snapshot(project_id)
        identity = path.relative_to(project_root).as_posix()
        return next(
            entry for entry in snapshot["entries"]
            if entry["kind"] == "file" and entry["path"] == identity
        )

    def read_file(self, project_id: str, relative_path: object) -> tuple[bytes, str, str]:
        """Read one bounded regular workspace file for an authenticated download."""

        project_root = self.project_path(project_id)
        path = self._workspace_path(project_root, relative_path, actor="download")
        try:
            resolved_parent = path.parent.resolve(strict=True)
        except OSError as exc:
            raise KeyError(relative_path) from exc
        if resolved_parent != project_root and project_root not in resolved_parent.parents:
            raise RequestError("download path escaped the managed project workspace")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise KeyError(relative_path) from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise RequestError("workspace downloads require a single-link regular file")
            if before.st_size > MAX_WORKSPACE_DOWNLOAD_FILE_BYTES:
                raise RequestError(
                    f"workspace file exceeds the {MAX_WORKSPACE_DOWNLOAD_FILE_BYTES // (1024 * 1024)} MiB download limit"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                payload = stream.read(MAX_WORKSPACE_DOWNLOAD_FILE_BYTES + 1)
            after = os.fstat(descriptor)
            try:
                bound = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise RequestError("workspace file changed while it was being read") from exc
            if (
                len(payload) != before.st_size
                or len(payload) > MAX_WORKSPACE_DOWNLOAD_FILE_BYTES
                or before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or after.st_dev != bound.st_dev
                or after.st_ino != bound.st_ino
                or not stat.S_ISREG(bound.st_mode)
            ):
                raise RequestError("workspace file changed while it was being read")
        finally:
            os.close(descriptor)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return payload, media_type, path.name

    def delete_file(self, project_id: str, relative_path: object) -> str:
        """Delete one regular file without following links or removing directories."""

        project_root = self.project_path(project_id)
        path = self._workspace_path(project_root, relative_path, actor="delete")
        try:
            resolved_parent = path.parent.resolve(strict=True)
        except OSError as exc:
            raise KeyError(relative_path) from exc
        if resolved_parent != project_root and project_root not in resolved_parent.parents:
            raise RequestError("delete path escaped the managed project workspace")
        if path.is_symlink() or not path.is_file():
            raise KeyError(relative_path)
        identity = path.relative_to(project_root).as_posix()
        try:
            path.unlink()
        except OSError as exc:
            raise RequestError(f"could not delete the workspace file: {exc}") from exc
        parent = path.parent
        while parent != project_root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return identity

    @staticmethod
    def _workspace_size(project_root: Path) -> int:
        total = 0
        entry_count = 0
        for directory, directory_names, file_names in os.walk(project_root, followlinks=False):
            directory_names[:] = [
                name for name in directory_names
                if name not in {".git", ".openevo"}
                and not (Path(directory) / name).is_symlink()
            ]
            for name in file_names:
                entry_count += 1
                if entry_count > MAX_WORKSPACE_ENTRIES * 10:
                    raise RequestError("project workspace contains too many files")
                candidate = Path(directory) / name
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                total += candidate.stat().st_size
                if total > MAX_WORKSPACE_TOTAL_BYTES:
                    return total
        return total

    @staticmethod
    def _workspace_path(project_root: Path, value: object, *, actor: str) -> Path:
        if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value:
            raise RequestError(f"{actor} workspace path is invalid")
        relative = PurePosixPath(value)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise RequestError(f"{actor} workspace path is unsafe")
        if relative.parts[0] in {".git", ".openevo"}:
            raise RequestError(f"{actor} cannot access reserved workspace paths")
        return project_root.joinpath(*relative.parts)

    @staticmethod
    def _mutation_path(project_root: Path, value: object) -> Path:
        if not isinstance(value, str) or not value or len(value) > 512 or "\\" in value:
            raise AgentRunError("Codex returned an invalid workspace path")
        relative = PurePosixPath(value)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise AgentRunError("Codex returned an unsafe workspace path")
        if relative.parts[0] in {".git", ".openevo"}:
            raise AgentRunError("Codex cannot mutate reserved workspace paths")
        return project_root.joinpath(*relative.parts)

    @staticmethod
    def changes(
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> list[dict[str, Any]]:
        before_files = {
            entry["path"]: entry
            for entry in before["entries"]
            if entry["kind"] == "file"
        }
        after_files = {
            entry["path"]: entry
            for entry in after["entries"]
            if entry["kind"] == "file"
        }
        changes: list[dict[str, Any]] = []
        for path in sorted(set(before_files) | set(after_files)):
            old = before_files.get(path)
            new = after_files.get(path)
            if old is not None and new is not None and all(
                old.get(field) == new.get(field)
                for field in ("byte_size", "content_sha256", "modified_at")
            ):
                continue
            change_type = "created" if old is None else "deleted" if new is None else "modified"
            old_content = old.get("content") if old else None
            new_content = new.get("content") if new else None
            diff_lines: list[dict[str, str]] = []
            if isinstance(old_content, str) or isinstance(new_content, str):
                for line in difflib.unified_diff(
                    (old_content or "").splitlines(),
                    (new_content or "").splitlines(),
                    lineterm="",
                ):
                    if line.startswith(("---", "+++", "@@")):
                        continue
                    kind = "added" if line.startswith("+") else "removed" if line.startswith("-") else "context"
                    diff_lines.append({"kind": kind, "text": line[1:] if line[:1] in "+- " else line})
                    if len(diff_lines) >= 400:
                        break
            current = new or old
            changes.append({
                "path": path,
                "change_type": change_type,
                "byte_size": current["byte_size"],
                "media_type": current.get("media_type"),
                "content": new_content,
                "previous_path": path if old is not None else None,
                "diff_lines": diff_lines,
            })
        return changes

    def _project_path(self, project_id: str) -> Path:
        if not ID_PATTERN.fullmatch(project_id):
            raise RuntimeError("project_id is invalid")
        path = self.root / project_id
        if path.parent != self.root:
            raise RuntimeError("project workspace escaped the managed root")
        return path

    @staticmethod
    def _unreadable_entry(path: str, modified_at: str | None = None) -> dict[str, Any]:
        return {
            "path": path,
            "kind": "unreadable",
            "byte_size": 0,
            "content_sha256": None,
            "media_type": None,
            "content": None,
            "modified_at": modified_at or utc_now(),
        }


class DevelopmentStateStore:
    """Small SQLite authority for the development-only Project/Session loop."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._event_condition = threading.Condition(self._lock)
        self.workspaces = ProjectWorkspaceStore(path.parent / "workspaces")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS development_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS development_state_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    event_type TEXT NOT NULL CHECK (event_type = 'state_changed'),
                    payload_sha256 TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS development_state_events_project_sequence
                    ON development_state_events(project_id, sequence);
                CREATE TABLE IF NOT EXISTS development_projects (
                    project_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS development_sessions (
                    session_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES development_projects(project_id),
                    task_title TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    response TEXT,
                    model TEXT,
                    state TEXT NOT NULL CHECK (state IN ('running', 'completed', 'failed')),
                    duration_ms INTEGER,
                    logs_json TEXT NOT NULL,
                    selected_evolution_json TEXT NOT NULL DEFAULT '[]',
                    evolution_errors_json TEXT NOT NULL DEFAULT '[]',
                    workspace_changes_json TEXT NOT NULL DEFAULT '[]',
                    context_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                    runtime_activation_json TEXT NOT NULL DEFAULT 'null',
                    cancellation_requested INTEGER NOT NULL DEFAULT 0
                        CHECK (cancellation_requested IN (0, 1)),
                    terminal_kind TEXT CHECK (terminal_kind IN ('failed', 'cancelled')),
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS development_sessions_project_created
                    ON development_sessions(project_id, created_at, session_id);
                CREATE TABLE IF NOT EXISTS development_task_logs_v2 (
                    task_id TEXT NOT NULL REFERENCES development_sessions(session_id),
                    sequence INTEGER NOT NULL CHECK (sequence > 0),
                    occurred_at TEXT NOT NULL,
                    stream TEXT NOT NULL CHECK (
                        stream IN ('system', 'stdout', 'stderr', 'transcript')
                    ),
                    message TEXT NOT NULL,
                    PRIMARY KEY(task_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS development_task_timeline_v2 (
                    task_id TEXT NOT NULL REFERENCES development_sessions(session_id),
                    sequence INTEGER NOT NULL CHECK (sequence > 0),
                    event_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL REFERENCES development_projects(project_id),
                    event_type TEXT NOT NULL CHECK (
                        event_type IN ('task_admitted', 'attempt_appended', 'dataset_sealed')
                    ),
                    dataset_id TEXT,
                    dataset_sha256 TEXT,
                    occurred_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, sequence),
                    CHECK (
                        (event_type = 'dataset_sealed' AND dataset_id IS NOT NULL
                         AND dataset_sha256 IS NOT NULL)
                        OR
                        (event_type != 'dataset_sealed' AND dataset_id IS NULL
                         AND dataset_sha256 IS NULL)
                    )
                );
                CREATE TABLE IF NOT EXISTS development_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES development_projects(project_id),
                    session_id TEXT NOT NULL UNIQUE REFERENCES development_sessions(session_id),
                    artifact_type TEXT NOT NULL CHECK (artifact_type = 'text_memory'),
                    method TEXT NOT NULL CHECK (method = 'text_memory_reflector'),
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    previous_artifact_id TEXT REFERENCES development_artifacts(artifact_id),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS development_artifacts_project_created
                    ON development_artifacts(project_id, created_at, artifact_id);
                CREATE TABLE IF NOT EXISTS development_document_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES development_projects(project_id),
                    session_id TEXT NOT NULL REFERENCES development_sessions(session_id),
                    artifact_type TEXT NOT NULL CHECK (
                        artifact_type IN ('text_memory', 'skill_bundle', 'agent_system')
                    ),
                    method TEXT NOT NULL CHECK (
                        method IN (
                            'text_memory_reflector',
                            'skill_bundle_reflector',
                            'agent_system_reflector'
                        )
                    ),
                    content_path TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    previous_artifact_id TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, artifact_type)
                );
                CREATE INDEX IF NOT EXISTS development_document_artifacts_project_created
                    ON development_document_artifacts(project_id, artifact_type, created_at, artifact_id);
                CREATE TABLE IF NOT EXISTS development_evolution_artifacts_v2 (
                    artifact_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES development_projects(project_id),
                    session_id TEXT NOT NULL REFERENCES development_sessions(session_id),
                    run_id TEXT,
                    target_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    method_id TEXT NOT NULL,
                    renderer_kind TEXT NOT NULL CHECK (
                        renderer_kind IN ('markdown', 'file_bundle', 'structured_summary', 'adapter')
                    ),
                    documents_json TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    previous_artifact_id TEXT,
                    promoted INTEGER NOT NULL DEFAULT 1 CHECK (promoted IN (0, 1)),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS development_evolution_artifacts_v2_project_created
                    ON development_evolution_artifacts_v2(
                        project_id, target_id, created_at, artifact_id
                    );
                CREATE TABLE IF NOT EXISTS development_evolution_runs (
                    run_id TEXT PRIMARY KEY,
                    action_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL REFERENCES development_projects(project_id),
                    source_session_ids_json TEXT NOT NULL,
                    selections_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('running', 'candidate_ready', 'applied', 'failed')
                    ),
                    artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS development_evolution_runs_project_created
                    ON development_evolution_runs(project_id, created_at, run_id);
                CREATE TABLE IF NOT EXISTS development_evolution_jobs (
                    job_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES development_sessions(session_id),
                    run_id TEXT,
                    target_id TEXT NOT NULL,
                    method_id TEXT NOT NULL,
                    requested_method_id TEXT NOT NULL,
                    resolver_input_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                    previous_artifact_id TEXT,
                    config_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'completed', 'failed')),
                    artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, target_id)
                );
                CREATE INDEX IF NOT EXISTS development_evolution_jobs_session
                    ON development_evolution_jobs(session_id, created_at, job_id);
                CREATE TABLE IF NOT EXISTS development_evolution_job_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    action_id TEXT,
                    job_id TEXT NOT NULL REFERENCES development_evolution_jobs(job_id),
                    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
                    state TEXT NOT NULL CHECK (
                        state IN ('queued', 'running', 'completed', 'failed', 'cancelled')
                    ),
                    stage TEXT NOT NULL,
                    artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                    error_code TEXT,
                    error_message TEXT,
                    logs_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(job_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS development_evolution_attempts_job
                    ON development_evolution_job_attempts(job_id, ordinal);
                CREATE TABLE IF NOT EXISTS development_dataset_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES development_projects(project_id),
                    session_id TEXT NOT NULL UNIQUE REFERENCES development_sessions(session_id),
                    uri TEXT NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS development_dataset_artifacts_project_created
                    ON development_dataset_artifacts(project_id, created_at, artifact_id);
                """
            )
            session_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(development_sessions)")
            }
            if "selected_evolution_json" not in session_columns:
                connection.execute(
                    "ALTER TABLE development_sessions "
                    "ADD COLUMN selected_evolution_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "evolution_errors_json" not in session_columns:
                connection.execute(
                    "ALTER TABLE development_sessions "
                    "ADD COLUMN evolution_errors_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "workspace_changes_json" not in session_columns:
                connection.execute(
                    "ALTER TABLE development_sessions "
                    "ADD COLUMN workspace_changes_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "context_artifact_ids_json" not in session_columns:
                connection.execute(
                    "ALTER TABLE development_sessions "
                    "ADD COLUMN context_artifact_ids_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "runtime_activation_json" not in session_columns:
                connection.execute(
                    "ALTER TABLE development_sessions "
                    "ADD COLUMN runtime_activation_json TEXT NOT NULL DEFAULT 'null'"
                )
            if "cancellation_requested" not in session_columns:
                connection.execute(
                    "ALTER TABLE development_sessions "
                    "ADD COLUMN cancellation_requested INTEGER NOT NULL DEFAULT 0 "
                    "CHECK (cancellation_requested IN (0, 1))"
                )
            if "terminal_kind" not in session_columns:
                connection.execute(
                    "ALTER TABLE development_sessions "
                    "ADD COLUMN terminal_kind TEXT "
                    "CHECK (terminal_kind IN ('failed', 'cancelled'))"
                )
            connection.execute(
                "UPDATE development_sessions SET runtime_activation_json = 'null' "
                "WHERE runtime_activation_json = '{}'"
            )
            artifact_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(development_evolution_artifacts_v2)"
                )
            }
            if "promoted" not in artifact_columns:
                connection.execute(
                    "ALTER TABLE development_evolution_artifacts_v2 "
                    "ADD COLUMN promoted INTEGER NOT NULL DEFAULT 1 "
                    "CHECK (promoted IN (0, 1))"
                )
            if "run_id" not in artifact_columns:
                connection.execute(
                    "ALTER TABLE development_evolution_artifacts_v2 ADD COLUMN run_id TEXT"
                )
            evolution_run_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(development_evolution_runs)"
                )
            }
            if "action_id" not in evolution_run_columns:
                connection.execute(
                    "ALTER TABLE development_evolution_runs ADD COLUMN action_id TEXT"
                )
                connection.execute(
                    "UPDATE development_evolution_runs "
                    "SET action_id = 'legacy-' || run_id WHERE action_id IS NULL"
                )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "development_evolution_runs_action_id "
                "ON development_evolution_runs(action_id)"
            )
            job_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(development_evolution_jobs)"
                )
            }
            if "previous_artifact_id" not in job_columns:
                connection.execute(
                    "ALTER TABLE development_evolution_jobs "
                    "ADD COLUMN previous_artifact_id TEXT"
                )
            if "requested_method_id" not in job_columns:
                connection.execute(
                    "ALTER TABLE development_evolution_jobs "
                    "ADD COLUMN requested_method_id TEXT"
                )
                connection.execute(
                    "UPDATE development_evolution_jobs "
                    "SET requested_method_id = method_id "
                    "WHERE requested_method_id IS NULL"
                )
            if "resolver_input_artifact_ids_json" not in job_columns:
                connection.execute(
                    "ALTER TABLE development_evolution_jobs "
                    "ADD COLUMN resolver_input_artifact_ids_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "run_id" not in job_columns:
                connection.execute(
                    "ALTER TABLE development_evolution_jobs ADD COLUMN run_id TEXT"
                )
            job_table_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'development_evolution_jobs'"
            ).fetchone()["sql"]
            if "UNIQUE(session_id, target_id)" in job_table_sql:
                connection.executescript(
                    """
                    CREATE TABLE development_evolution_jobs_rebuilt (
                        job_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES development_sessions(session_id),
                        run_id TEXT,
                        target_id TEXT NOT NULL,
                        method_id TEXT NOT NULL,
                        requested_method_id TEXT NOT NULL,
                        resolver_input_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                        previous_artifact_id TEXT,
                        config_json TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'completed', 'failed')),
                        artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                        error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(run_id, target_id)
                    );
                    INSERT INTO development_evolution_jobs_rebuilt
                    SELECT job_id, session_id, run_id, target_id, method_id,
                           requested_method_id, resolver_input_artifact_ids_json,
                           previous_artifact_id, config_json, state,
                           artifact_ids_json, error, created_at, updated_at
                    FROM development_evolution_jobs;
                    CREATE TABLE development_evolution_job_attempts_rebuilt (
                        attempt_id TEXT PRIMARY KEY,
                        action_id TEXT,
                        job_id TEXT NOT NULL REFERENCES development_evolution_jobs_rebuilt(job_id),
                        ordinal INTEGER NOT NULL CHECK (ordinal > 0),
                        state TEXT NOT NULL CHECK (
                            state IN ('queued', 'running', 'completed', 'failed', 'cancelled')
                        ),
                        stage TEXT NOT NULL,
                        artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                        error_code TEXT,
                        error_message TEXT,
                        logs_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        completed_at TEXT,
                        updated_at TEXT NOT NULL,
                        UNIQUE(job_id, ordinal)
                    );
                    INSERT INTO development_evolution_job_attempts_rebuilt
                    SELECT attempt_id, NULL, job_id, ordinal, state, stage,
                           artifact_ids_json, error_code, error_message, logs_json,
                           created_at, started_at, completed_at, updated_at
                    FROM development_evolution_job_attempts;
                    DROP TABLE development_evolution_job_attempts;
                    DROP TABLE development_evolution_jobs;
                    ALTER TABLE development_evolution_jobs_rebuilt
                        RENAME TO development_evolution_jobs;
                    ALTER TABLE development_evolution_job_attempts_rebuilt
                        RENAME TO development_evolution_job_attempts;
                    CREATE INDEX development_evolution_jobs_session
                        ON development_evolution_jobs(session_id, created_at, job_id);
                    CREATE INDEX development_evolution_attempts_job
                        ON development_evolution_job_attempts(job_id, ordinal);
                    """
                )
            attempt_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(development_evolution_job_attempts)"
                )
            }
            if "action_id" not in attempt_columns:
                connection.execute(
                    "ALTER TABLE development_evolution_job_attempts ADD COLUMN action_id TEXT"
                )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "development_evolution_attempts_action_id "
                "ON development_evolution_job_attempts(action_id) "
                "WHERE action_id IS NOT NULL"
            )
            for row in connection.execute(
                "SELECT session_id, selected_evolution_json FROM development_sessions"
            ).fetchall():
                stored = json.loads(row["selected_evolution_json"])
                normalized = normalize_selected_evolution(stored)
                normalized_json = canonical_json(normalized)
                if normalized_json != row["selected_evolution_json"]:
                    connection.execute(
                        "UPDATE development_sessions SET selected_evolution_json = ? "
                        "WHERE session_id = ?",
                        (normalized_json, row["session_id"]),
                    )
            connection.execute(
                """
                INSERT OR IGNORE INTO development_document_artifacts(
                    artifact_id, project_id, session_id, artifact_type, method, content_path,
                    content, content_sha256, byte_size, previous_artifact_id, created_at
                )
                SELECT artifact_id, project_id, session_id, artifact_type, method, 'memory.md',
                       content, content_sha256, byte_size, previous_artifact_id, created_at
                FROM development_artifacts
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO development_evolution_artifacts_v2(
                    artifact_id, project_id, session_id, target_id, artifact_type,
                    method_id, renderer_kind, documents_json, manifest_json,
                    content_sha256, byte_size, previous_artifact_id, created_at
                )
                SELECT artifact_id, project_id, session_id, artifact_type, artifact_type,
                       method,
                       CASE artifact_type WHEN 'skill_bundle' THEN 'file_bundle' ELSE 'markdown' END,
                       json_array(json_object('path', content_path, 'media_type', 'text/markdown',
                                              'content', content)),
                       json_object('content_path', content_path),
                       content_sha256, byte_size, previous_artifact_id, created_at
                FROM development_document_artifacts
                """
            )
            restarted_at = utc_now()
            connection.execute(
                """
                UPDATE development_evolution_jobs
                SET state = 'failed', error = ?, updated_at = ?
                WHERE state IN ('queued', 'running')
                """,
                ("Development daemon restarted before this evolution job completed.", restarted_at),
            )
            connection.execute(
                """
                UPDATE development_evolution_job_attempts
                SET state = 'failed', error_code = 'daemon_restarted',
                    error_message = ?, completed_at = ?, updated_at = ?
                WHERE state IN ('queued', 'running')
                """,
                (
                    "Development daemon restarted before this evolution attempt completed.",
                    restarted_at,
                    restarted_at,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO development_evolution_job_attempts(
                    attempt_id, job_id, ordinal, state, stage, artifact_ids_json,
                    error_code, error_message, logs_json, created_at, started_at,
                    completed_at, updated_at
                )
                SELECT job_id || '-attempt-1', job_id, 1,
                       CASE state WHEN 'completed' THEN 'completed' ELSE 'failed' END,
                       CASE state WHEN 'completed' THEN 'completed' ELSE 'unknown' END,
                       artifact_ids_json,
                       CASE WHEN state = 'completed' THEN NULL ELSE 'legacy_failure' END,
                       error,
                       '[]', created_at, created_at,
                       CASE WHEN state IN ('completed', 'failed') THEN updated_at ELSE NULL END,
                       updated_at
                FROM development_evolution_jobs
                """
            )
            interrupted_sessions = connection.execute(
                "SELECT session_id FROM development_sessions WHERE state = 'running'"
            ).fetchall()
            connection.execute(
                """
                UPDATE development_sessions
                SET state = 'failed', terminal_kind = 'failed', error = ?, updated_at = ?
                WHERE state = 'running'
                """,
                ("Development daemon restarted before this session completed.", restarted_at),
            )
            self._backfill_task_journals(connection)
            for interrupted in interrupted_sessions:
                self._append_task_log_v2(
                    connection,
                    task_id=interrupted["session_id"],
                    stream="system",
                    message="Session failed: Development daemon restarted before this session completed.",
                    occurred_at=restarted_at,
                )
            project_ids = [
                row["project_id"]
                for row in connection.execute("SELECT project_id FROM development_projects")
            ]
        for project_id in project_ids:
            self.workspaces.ensure_project(project_id)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def _connection(self, *, emit_event: bool = True) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        emitted = False
        try:
            with connection:
                initial_changes = connection.total_changes
                yield connection
                if emit_event and connection.total_changes > initial_changes:
                    emitted = self._append_state_event(connection)
        finally:
            connection.close()
        if emitted:
            with self._event_condition:
                self._event_condition.notify_all()

    @staticmethod
    def _append_state_event(
        connection: sqlite3.Connection,
        *,
        project_id: str | None = None,
    ) -> bool:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'development_state_events'"
        ).fetchone()
        if table is None:
            return False
        if project_id is None:
            active = connection.execute(
                "SELECT value FROM development_metadata WHERE key = 'active_project_id'"
            ).fetchone()
            project_id = active["value"] if active is not None else None
        if not project_id:
            latest = connection.execute(
                "SELECT project_id FROM development_projects "
                "ORDER BY updated_at DESC, project_id DESC LIMIT 1"
            ).fetchone()
            project_id = latest["project_id"] if latest is not None else None
        if not project_id:
            return False
        occurred_at = utc_now()
        event_id = f"development-event-{secrets.token_hex(16)}"
        payload_sha256 = hashlib.sha256(
            canonical_json(
                {
                    "event_id": event_id,
                    "event_type": "state_changed",
                    "occurred_at": occurred_at,
                    "project_id": project_id,
                }
            ).encode("utf-8")
        ).hexdigest()
        connection.execute(
            "INSERT INTO development_state_events("
            "event_id, project_id, event_type, payload_sha256, occurred_at"
            ") VALUES (?, ?, 'state_changed', ?, ?)",
            (event_id, project_id, payload_sha256, occurred_at),
        )
        connection.execute(
            "DELETE FROM development_state_events WHERE sequence < ("
            "SELECT sequence FROM development_state_events "
            "ORDER BY sequence DESC LIMIT 1 OFFSET ?)",
            (MAX_DEVELOPMENT_STATE_EVENTS - 1,),
        )
        return True

    def _emit_project_event(self, project_id: str) -> None:
        with self._event_condition:
            with self._connection(emit_event=False) as connection:
                emitted = self._append_state_event(connection, project_id=project_id)
            if emitted:
                self._event_condition.notify_all()

    def read_events(
        self,
        *,
        after_sequence: int | None,
        limit: int,
        wait_seconds: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + wait_seconds
        with self._event_condition:
            while True:
                with self._connection(emit_event=False) as connection:
                    bounds = connection.execute(
                        "SELECT MIN(sequence) AS earliest, MAX(sequence) AS latest "
                        "FROM development_state_events"
                    ).fetchone()
                    earliest = bounds["earliest"] if bounds is not None else None
                    latest = bounds["latest"] if bounds is not None else None
                    latest_sequence = int(latest or 0)
                    if after_sequence is None:
                        return {
                            "schema_version": "1",
                            "events": [],
                            "latest_sequence": latest_sequence,
                            "has_more": False,
                        }
                    if after_sequence > latest_sequence:
                        raise EventCursorExpiredError("event cursor is ahead of daemon authority")
                    if earliest is not None and after_sequence < int(earliest) - 1:
                        raise EventCursorExpiredError("event cursor is outside the replay window")
                    rows = connection.execute(
                        "SELECT * FROM development_state_events WHERE sequence > ? "
                        "ORDER BY sequence LIMIT ?",
                        (after_sequence, limit + 1),
                    ).fetchall()
                has_more = len(rows) > limit
                page = rows[:limit]
                if page:
                    return {
                        "schema_version": "1",
                        "events": [self._event_record(row) for row in page],
                        "latest_sequence": latest_sequence,
                        "has_more": has_more,
                    }
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {
                        "schema_version": "1",
                        "events": [],
                        "latest_sequence": latest_sequence,
                        "has_more": False,
                    }
                self._event_condition.wait(remaining)

    def snapshot(self) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            projects = [
                {
                    "project_id": row["project_id"],
                    "display_name": row["display_name"],
                    "config": json.loads(row["config_json"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in connection.execute(
                    "SELECT * FROM development_projects ORDER BY created_at, project_id"
                )
            ]
            evidence_session_ids = {
                row["session_id"]
                for row in connection.execute(
                    "SELECT session_id FROM development_dataset_artifacts"
                )
            }
            sessions = [
                self._session_record(
                    row,
                    evolution_evidence_ready=row["session_id"] in evidence_session_ids,
                )
                for row in connection.execute(
                    "SELECT * FROM development_sessions ORDER BY created_at, session_id"
                )
            ]
            artifacts = [self._artifact_record(row) for row in connection.execute(
                "SELECT * FROM development_evolution_artifacts_v2 ORDER BY created_at, artifact_id"
            )]
            attempt_rows = connection.execute(
                "SELECT * FROM development_evolution_job_attempts ORDER BY job_id, ordinal"
            ).fetchall()
            attempts_by_job: dict[str, list[dict[str, Any]]] = {}
            for attempt_row in attempt_rows:
                attempts_by_job.setdefault(attempt_row["job_id"], []).append(
                    self._attempt_record(attempt_row)
                )
            jobs = [
                self._job_record(row, attempts_by_job.get(row["job_id"], []))
                for row in connection.execute(
                    "SELECT * FROM development_evolution_jobs ORDER BY created_at, job_id"
                )
            ]
            evolution_runs = [
                self._evolution_run_record(row)
                for row in connection.execute(
                    "SELECT * FROM development_evolution_runs ORDER BY created_at, run_id"
                )
            ]
            active_row = connection.execute(
                "SELECT value FROM development_metadata WHERE key = 'active_project_id'"
            ).fetchone()
        active_project_id = active_row["value"] if active_row else None
        if active_project_id not in {project["project_id"] for project in projects}:
            active_project_id = projects[-1]["project_id"] if projects else None
        return {
            "schema_version": "1",
            "active_project_id": active_project_id,
            "projects": projects,
            "sessions": sessions,
            "artifacts": artifacts,
            "evolution_jobs": jobs,
            "evolution_runs": evolution_runs,
            "workspaces": [
                self.workspaces.snapshot(project["project_id"])
                for project in projects
            ],
        }

    def create_project(self, request: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connection() as connection:
            try:
                connection.execute(
                    "INSERT INTO development_projects VALUES (?, ?, ?, ?, ?)",
                    (
                        request["project_id"],
                        request["display_name"],
                        canonical_json(request["config"]),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StateConflictError("project_id already exists") from exc
            self._set_active(connection, request["project_id"])
        self.workspaces.ensure_project(request["project_id"])
        return {**request, "created_at": now, "updated_at": now}

    def workspace_path(self, project_id: str) -> Path:
        with self._lock, self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM development_projects WHERE project_id = ?", (project_id,)
            ).fetchone() is None:
                raise KeyError(project_id)
        return self.workspaces.project_path(project_id)

    def workspace_snapshot(self, project_id: str) -> dict[str, Any]:
        self.workspace_path(project_id)
        return self.workspaces.snapshot(project_id)

    def workspace_page_v2(
        self,
        project_id: str,
        *,
        after_path: str | None,
        expected_manifest_sha256: str | None,
        limit: int,
    ) -> DevelopmentWorkspacePageV2:
        self.workspace_path(project_id)
        authority = self.workspaces.authoritative_snapshot_v2(project_id)
        manifest_sha256 = authority["manifest_sha256"]
        if (
            expected_manifest_sha256 is not None
            and expected_manifest_sha256 != manifest_sha256
        ):
            raise StateConflictError("workspace changed while its inventory was paged")
        entries = authority["entries"]
        start = 0
        if after_path is not None:
            positions = [
                index for index, entry in enumerate(entries)
                if entry["path"] == after_path
            ]
            if len(positions) != 1:
                raise RequestError("workspace cursor is not part of this inventory")
            start = positions[0] + 1
        selected = entries[start:start + limit]
        has_more = start + len(selected) < len(entries)
        return DevelopmentWorkspacePageV2.model_validate({
            "schema_version": "2",
            "project_id": project_id,
            "manifest_sha256": manifest_sha256,
            "items": [
                {"schema_version": "2", **entry}
                for entry in selected
            ],
            "next_cursor": selected[-1]["path"] if selected and has_more else None,
            "has_more": has_more,
            "truncated": authority["truncated"],
        })

    def workspace_mutation_v2(
        self,
        project_id: str,
        relative_path: str,
    ) -> DevelopmentWorkspaceMutationV2:
        authority = self.workspaces.authoritative_snapshot_v2(project_id)
        entry = next(
            (
                candidate for candidate in authority["entries"]
                if candidate["kind"] == "file" and candidate["path"] == relative_path
            ),
            None,
        )
        if entry is None:
            raise KeyError(relative_path)
        return DevelopmentWorkspaceMutationV2.model_validate({
            "schema_version": "2",
            "project_id": project_id,
            "manifest_sha256": authority["manifest_sha256"],
            "entry": {"schema_version": "2", **entry},
        })

    def apply_workspace_mutations(self, project_id: str, mutations: object) -> None:
        self.workspace_path(project_id)
        self.workspaces.apply_mutations(project_id, mutations)
        self._emit_project_event(project_id)

    def upload_workspace_file(
        self,
        project_id: str,
        relative_path: object,
        payload: bytes,
        *,
        overwrite: bool,
    ) -> dict[str, Any]:
        self.workspace_path(project_id)
        result = self.workspaces.upload_file(
            project_id,
            relative_path,
            payload,
            overwrite=overwrite,
        )
        self._emit_project_event(project_id)
        return result

    def download_workspace_file(
        self,
        project_id: str,
        relative_path: object,
    ) -> tuple[bytes, str, str]:
        self.workspace_path(project_id)
        return self.workspaces.read_file(project_id, relative_path)

    def delete_workspace_file(self, project_id: str, relative_path: object) -> str:
        self.workspace_path(project_id)
        deleted_path = self.workspaces.delete_file(project_id, relative_path)
        self._emit_project_event(project_id)
        return deleted_path

    def workspace_delete_v2(
        self,
        project_id: str,
        deleted_path: str,
    ) -> DevelopmentWorkspaceDeleteV2:
        authority = self.workspaces.authoritative_snapshot_v2(project_id)
        return DevelopmentWorkspaceDeleteV2.model_validate({
            "schema_version": "2",
            "project_id": project_id,
            "manifest_sha256": authority["manifest_sha256"],
            "deleted_path": deleted_path,
        })

    def update_project(self, project_id: str, request: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE development_projects
                SET display_name = ?, config_json = ?, updated_at = ?
                WHERE project_id = ?
                """,
                (request["display_name"], canonical_json(request["config"]), now, project_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(project_id)
            created = connection.execute(
                "SELECT created_at FROM development_projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        return {"project_id": project_id, **request, "created_at": created["created_at"], "updated_at": now}

    def activate_project(self, project_id: str) -> None:
        with self._lock, self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM development_projects WHERE project_id = ?", (project_id,)
            ).fetchone() is None:
                raise KeyError(project_id)
            self._set_active(connection, project_id)

    @staticmethod
    def _append_task_log_v2(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        stream: str,
        message: object,
        occurred_at: str | None = None,
    ) -> None:
        if not isinstance(message, str) or not message:
            return
        timestamp = occurred_at or utc_now()
        for offset in range(0, len(message), MAX_DAEMON_V2_LOG_TEXT):
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence "
                "FROM development_task_logs_v2 WHERE task_id = ?",
                (task_id,),
            ).fetchone()["next_sequence"]
            connection.execute(
                "INSERT INTO development_task_logs_v2("
                "task_id, sequence, occurred_at, stream, message"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    task_id,
                    sequence,
                    timestamp,
                    stream,
                    message[offset : offset + MAX_DAEMON_V2_LOG_TEXT],
                ),
            )

    @staticmethod
    def _append_task_timeline_v2(
        connection: sqlite3.Connection,
        *,
        task_id: str,
        project_id: str,
        event_type: str,
        occurred_at: str,
        dataset_id: str | None = None,
        dataset_sha256: str | None = None,
    ) -> None:
        existing = connection.execute(
            "SELECT 1 FROM development_task_timeline_v2 "
            "WHERE task_id = ? AND event_type = ?",
            (task_id, event_type),
        ).fetchone()
        if existing is not None:
            return
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence "
            "FROM development_task_timeline_v2 WHERE task_id = ?",
            (task_id,),
        ).fetchone()["next_sequence"]
        connection.execute(
            "INSERT INTO development_task_timeline_v2("
            "task_id, sequence, event_id, project_id, event_type, dataset_id, "
            "dataset_sha256, occurred_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                sequence,
                f"development-task-event-{secrets.token_hex(16)}",
                project_id,
                event_type,
                dataset_id,
                dataset_sha256,
                occurred_at,
            ),
        )

    @classmethod
    def _backfill_task_journals(cls, connection: sqlite3.Connection) -> None:
        for row in connection.execute(
            "SELECT * FROM development_sessions ORDER BY created_at, session_id"
        ).fetchall():
            task_id = row["session_id"]
            if connection.execute(
                "SELECT 1 FROM development_task_timeline_v2 WHERE task_id = ? LIMIT 1",
                (task_id,),
            ).fetchone() is None:
                cls._append_task_timeline_v2(
                    connection,
                    task_id=task_id,
                    project_id=row["project_id"],
                    event_type="task_admitted",
                    occurred_at=row["created_at"],
                )
                cls._append_task_timeline_v2(
                    connection,
                    task_id=task_id,
                    project_id=row["project_id"],
                    event_type="attempt_appended",
                    occurred_at=row["created_at"],
                )
                dataset = connection.execute(
                    "SELECT artifact_id, uri, name, created_at "
                    "FROM development_dataset_artifacts WHERE session_id = ?",
                    (task_id,),
                ).fetchone()
                if dataset is not None:
                    dataset_sha256 = hashlib.sha256(
                        canonical_json({
                            "artifact_id": dataset["artifact_id"],
                            "name": dataset["name"],
                            "uri": dataset["uri"],
                        }).encode("utf-8")
                    ).hexdigest()
                    cls._append_task_timeline_v2(
                        connection,
                        task_id=task_id,
                        project_id=row["project_id"],
                        event_type="dataset_sealed",
                        occurred_at=dataset["created_at"],
                        dataset_id=dataset["artifact_id"],
                        dataset_sha256=dataset_sha256,
                    )
            if connection.execute(
                "SELECT 1 FROM development_task_logs_v2 WHERE task_id = ? LIMIT 1",
                (task_id,),
            ).fetchone() is None:
                for message in json.loads(row["logs_json"]):
                    cls._append_task_log_v2(
                        connection,
                        task_id=task_id,
                        stream="system",
                        message=message,
                        occurred_at=row["updated_at"],
                    )
                cls._append_task_log_v2(
                    connection,
                    task_id=task_id,
                    stream="transcript",
                    message=row["response"],
                    occurred_at=row["updated_at"],
                )
                cls._append_task_log_v2(
                    connection,
                    task_id=task_id,
                    stream="system",
                    message=row["error"],
                    occurred_at=row["updated_at"],
                )

    @staticmethod
    def _set_active(connection: sqlite3.Connection, project_id: str) -> None:
        connection.execute(
            """
            INSERT INTO development_metadata(key, value) VALUES ('active_project_id', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (project_id,),
        )

    def start_session(self, session_id: str, request: dict[str, str]) -> None:
        now = utc_now()
        with self._lock, self._connection() as connection:
            project = connection.execute(
                "SELECT display_name, config_json FROM development_projects WHERE project_id = ?",
                (request["project_id"],),
            ).fetchone()
            if project is None:
                raise KeyError(request["project_id"])
            if project["display_name"] != request["project_name"]:
                raise StateConflictError("project_name does not match the persisted project")
            context_rows = connection.execute(
                """
                SELECT artifact.artifact_id
                FROM development_evolution_artifacts_v2 AS artifact
                JOIN (
                    SELECT target_id, MAX(created_at || artifact_id) AS latest
                    FROM development_evolution_artifacts_v2
                    WHERE project_id = ? AND artifact_type != 'report' AND promoted = 1
                    GROUP BY target_id
                ) AS selected
                  ON selected.target_id = artifact.target_id
                 AND selected.latest = artifact.created_at || artifact.artifact_id
                WHERE artifact.project_id = ?
                ORDER BY artifact.target_id
                """,
                (request["project_id"], request["project_id"]),
            ).fetchall()
            context_artifact_ids = [row["artifact_id"] for row in context_rows]
            connection.execute(
                """
                INSERT INTO development_sessions(
                    session_id, project_id, task_title, instruction, response, model,
                    state, duration_ms, logs_json, selected_evolution_json,
                    evolution_errors_json, workspace_changes_json, context_artifact_ids_json,
                    error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, 'running', NULL, ?, ?, '[]', '[]', ?, NULL, ?, ?)
                """,
                (
                    session_id,
                    request["project_id"],
                    request["task_title"],
                    request["instruction"],
                    canonical_json(["Remote development daemon admitted the session."]),
                    "[]",
                    canonical_json(context_artifact_ids),
                    now,
                    now,
                ),
            )
            self._append_task_log_v2(
                connection,
                task_id=session_id,
                stream="system",
                message="Remote development daemon admitted the session.",
                occurred_at=now,
            )
            self._append_task_timeline_v2(
                connection,
                task_id=session_id,
                project_id=request["project_id"],
                event_type="task_admitted",
                occurred_at=now,
            )
            self._append_task_timeline_v2(
                connection,
                task_id=session_id,
                project_id=request["project_id"],
                event_type="attempt_appended",
                occurred_at=now,
            )

    def complete_session(self, session_id: str, result: dict[str, Any]) -> None:
        now = utc_now()
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT logs_json, cancellation_requested FROM development_sessions "
                "WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            if row["cancellation_requested"]:
                raise HarnessRunCancelled("Session cancelled by user")
            existing_logs = json.loads(row["logs_json"])
            merged_logs = [*existing_logs]
            appended_logs: list[str] = []
            for message in result["logs"]:
                if message not in merged_logs:
                    merged_logs.append(message)
                    appended_logs.append(message)
            connection.execute(
                """
                UPDATE development_sessions
                SET response = ?, model = ?, state = 'completed', duration_ms = ?,
                    logs_json = ?, workspace_changes_json = ?, runtime_activation_json = ?,
                    cancellation_requested = 0, terminal_kind = NULL,
                    error = NULL, updated_at = ?
                WHERE session_id = ?
                """,
                (
                    result["response"],
                    result["model"],
                    result["duration_ms"],
                    canonical_json(merged_logs),
                    canonical_json(result.get("workspace_changes", [])),
                    canonical_json(result.get("runtime_activation")),
                    now,
                    session_id,
                ),
            )
            for message in appended_logs:
                self._append_task_log_v2(
                    connection,
                    task_id=session_id,
                    stream="system",
                    message=message,
                    occurred_at=now,
                )
            self._append_task_log_v2(
                connection,
                task_id=session_id,
                stream="transcript",
                message=result["response"],
                occurred_at=now,
            )

    def append_session_log(self, session_id: str, message: str) -> list[str]:
        now = utc_now()
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT logs_json FROM development_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            logs = json.loads(row["logs_json"])
            logs.append(message)
            connection.execute(
                "UPDATE development_sessions SET logs_json = ?, updated_at = ? WHERE session_id = ?",
                (canonical_json(logs), now, session_id),
            )
            self._append_task_log_v2(
                connection,
                task_id=session_id,
                stream="system",
                message=message,
                occurred_at=now,
            )
        return logs

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM development_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            evidence_ready = connection.execute(
                "SELECT 1 FROM development_dataset_artifacts WHERE session_id = ?",
                (session_id,),
            ).fetchone() is not None
        if row is None:
            raise KeyError(session_id)
        return self._session_record(row, evolution_evidence_ready=evidence_ready)

    def task_observations_v2(
        self,
        *,
        project_id: str | None = None,
        after_task_id: str | None = None,
        limit: int = MAX_DAEMON_V2_TASK_PAGE,
    ) -> DevelopmentTaskObservationPageV2:
        with self._lock, self._connection() as connection:
            if project_id is None:
                rows = connection.execute(
                    "SELECT * FROM development_sessions ORDER BY created_at, session_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM development_sessions WHERE project_id = ? "
                    "ORDER BY created_at, session_id",
                    (project_id,),
                ).fetchall()
        observations = [self._task_observation_v2(row) for row in rows]
        start = 0
        if after_task_id is not None:
            try:
                start = next(
                    index + 1
                    for index, observation in enumerate(observations)
                    if observation.task_id == after_task_id
                )
            except StopIteration as exc:
                raise RequestError("task cursor is not part of this collection") from exc
        page = observations[start : start + limit]
        has_more = start + len(page) < len(observations)
        return DevelopmentTaskObservationPageV2(
            items=page,
            next_cursor=page[-1].task_id if has_more and page else None,
            has_more=has_more,
        )

    def task_observation_v2(self, task_id: str) -> DevelopmentTaskObservationV2:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM development_sessions WHERE session_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task_observation_v2(row)

    def task_logs_v2(
        self,
        task_id: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> core_v2.LogPageV2:
        with self._lock, self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM development_sessions WHERE session_id = ?", (task_id,)
            ).fetchone() is None:
                raise KeyError(task_id)
            latest_sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS latest_sequence "
                "FROM development_task_logs_v2 WHERE task_id = ?",
                (task_id,),
            ).fetchone()["latest_sequence"]
            if after_sequence > latest_sequence:
                raise RequestError("log cursor is beyond the authoritative journal")
            rows = connection.execute(
                "SELECT sequence, occurred_at, stream, message "
                "FROM development_task_logs_v2 "
                "WHERE task_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
                (task_id, after_sequence, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        page = [core_v2.LogEntryV2.model_validate(dict(row)) for row in rows[:limit]]
        return core_v2.LogPageV2(
            items=page,
            next_cursor=str(page[-1].sequence) if has_more and page else None,
            has_more=has_more,
        )

    def task_timeline_v2(
        self,
        task_id: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> DevelopmentTaskTimelinePageV2:
        with self._lock, self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM development_sessions WHERE session_id = ?", (task_id,)
            ).fetchone() is None:
                raise KeyError(task_id)
            latest_sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS latest_sequence "
                "FROM development_task_timeline_v2 WHERE task_id = ?",
                (task_id,),
            ).fetchone()["latest_sequence"]
            if after_sequence > latest_sequence:
                raise RequestError("timeline cursor is beyond the authoritative journal")
            rows = connection.execute(
                "SELECT sequence, event_id, occurred_at, project_id, task_id, event_type, "
                "dataset_id, dataset_sha256 FROM development_task_timeline_v2 "
                "WHERE task_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
                (task_id, after_sequence, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        items: list[dict[str, Any]] = []
        for row in rows[:limit]:
            item = dict(row)
            if item["event_type"] != "dataset_sealed":
                item.pop("dataset_id")
                item.pop("dataset_sha256")
            items.append({"schema_version": "2", **item})
        next_cursor = str(items[-1]["sequence"]) if has_more and items else None
        return DevelopmentTaskTimelinePageV2.model_validate({
            "schema_version": "2",
            "items": items,
            "next_cursor": next_cursor,
            "has_more": has_more,
        })

    def cancellation_requested(self, session_id: str) -> bool:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT cancellation_requested FROM development_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return bool(row["cancellation_requested"])

    def request_session_cancellation(self, session_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT state, logs_json FROM development_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            if row["state"] != "running":
                raise StateConflictError("session is already terminal")
            logs = json.loads(row["logs_json"])
            message = "Cancellation requested; stopping the active harness process."
            if message not in logs:
                logs.append(message)
                self._append_task_log_v2(
                    connection,
                    task_id=session_id,
                    stream="system",
                    message=message,
                    occurred_at=now,
                )
            connection.execute(
                "UPDATE development_sessions "
                "SET cancellation_requested = 1, logs_json = ?, updated_at = ? "
                "WHERE session_id = ? AND state = 'running'",
                (canonical_json(logs), now, session_id),
            )
            updated = connection.execute(
                "SELECT * FROM development_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self._session_record(updated)

    def cancel_session(
        self,
        session_id: str,
        workspace_changes: list[dict[str, Any]] | None = None,
    ) -> None:
        now = utc_now()
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT logs_json FROM development_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            logs = json.loads(row["logs_json"])
            message = "Session cancelled by user."
            if message not in logs:
                logs.append(message)
                self._append_task_log_v2(
                    connection,
                    task_id=session_id,
                    stream="system",
                    message=message,
                    occurred_at=now,
                )
            connection.execute(
                """
                UPDATE development_sessions
                SET state = 'failed', cancellation_requested = 1,
                    terminal_kind = 'cancelled', logs_json = ?, workspace_changes_json = ?,
                    error = NULL, updated_at = ?
                WHERE session_id = ? AND state = 'running'
                """,
                (canonical_json(logs), canonical_json(workspace_changes or []), now, session_id),
            )

    def record_evolution_errors(
        self,
        session_id: str,
        errors: list[dict[str, str]],
    ) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE development_sessions SET evolution_errors_json = ?, updated_at = ? "
                "WHERE session_id = ?",
                (canonical_json(errors), utc_now(), session_id),
            )

    def set_evolution_error(
        self,
        session_id: str,
        *,
        target_id: str,
        method: str,
        message: str | None,
    ) -> None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT evolution_errors_json FROM development_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            errors = [
                item
                for item in json.loads(row["evolution_errors_json"])
                if item.get("target_id") != target_id
            ]
            if message is not None:
                errors.append({
                    "target_id": target_id,
                    "method": method,
                    "message": message,
                })
            connection.execute(
                "UPDATE development_sessions SET evolution_errors_json = ?, updated_at = ? "
                "WHERE session_id = ?",
                (canonical_json(errors), utc_now(), session_id),
            )

    def latest_artifact(self, project_id: str, target_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM development_evolution_artifacts_v2
                WHERE project_id = ? AND target_id = ?
                  AND artifact_type != 'report' AND promoted = 1
                ORDER BY created_at DESC, artifact_id DESC
                LIMIT 1
                """,
                (project_id, target_id),
            ).fetchone()
        return None if row is None else self._artifact_record(row)

    def project_config(self, project_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT config_json FROM development_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return json.loads(row["config_json"])

    def project(self, project_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM development_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return {
            "project_id": row["project_id"],
            "display_name": row["display_name"],
            "config": json.loads(row["config_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def latest_memory(self, project_id: str) -> dict[str, Any] | None:
        return self.latest_artifact(project_id, "text_memory")

    def latest_context_artifacts(self, project_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT artifact.*
                FROM development_evolution_artifacts_v2 AS artifact
                JOIN (
                    SELECT target_id, MAX(created_at || artifact_id) AS latest
                    FROM development_evolution_artifacts_v2
                    WHERE project_id = ? AND artifact_type != 'report' AND promoted = 1
                    GROUP BY target_id
                ) AS selected
                  ON selected.target_id = artifact.target_id
                 AND selected.latest = artifact.created_at || artifact.artifact_id
                WHERE artifact.project_id = ?
                ORDER BY artifact.target_id
                """,
                (project_id, project_id),
            ).fetchall()
        return [self._artifact_record(row) for row in rows]

    def record_dataset_artifact(
        self,
        *,
        artifact_id: str,
        project_id: str,
        session_id: str,
        uri: str,
        name: str,
        manifest_sha256: str | None = None,
    ) -> None:
        now = utc_now()
        effective_manifest_sha256 = manifest_sha256 or hashlib.sha256(
            canonical_json({
                "artifact_id": artifact_id,
                "name": name,
                "uri": uri,
            }).encode("utf-8")
        ).hexdigest()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO development_dataset_artifacts(
                    artifact_id, project_id, session_id, uri, name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    artifact_id = excluded.artifact_id,
                    project_id = excluded.project_id,
                    uri = excluded.uri,
                    name = excluded.name
                """,
                (artifact_id, project_id, session_id, uri, name, now),
            )
            self._append_task_timeline_v2(
                connection,
                task_id=session_id,
                project_id=project_id,
                event_type="dataset_sealed",
                occurred_at=now,
                dataset_id=artifact_id,
                dataset_sha256=effective_manifest_sha256,
            )

    def dataset_artifacts(self, project_id: str) -> list[dict[str, str]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT artifact_id, project_id, session_id, uri, name, created_at
                FROM development_dataset_artifacts
                WHERE project_id = ?
                ORDER BY created_at, artifact_id
                """,
                (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def completed_sessions(self) -> list[dict[str, Any]]:
        """Return successful Sessions that can be sealed as transcript evidence."""

        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM development_sessions "
                "WHERE state = 'completed' AND response IS NOT NULL "
                "ORDER BY created_at, session_id"
            ).fetchall()
        return [self._session_record(row) for row in rows]

    def start_evolution_run(
        self,
        run_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        session_ids = request["session_ids"]
        action_id = request.get("action_id", f"legacy-{run_id}")
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if existing is not None:
                record = self._evolution_run_record(existing)
                if (
                    record["project_id"] != request["project_id"]
                    or record["source_session_ids"] != session_ids
                    or record["selections"] != request["selections"]
                ):
                    raise StateConflictError(
                        "Evolution action_id is already bound to another request"
                    )
                return record
            if connection.execute(
                "SELECT 1 FROM development_projects WHERE project_id = ?",
                (request["project_id"],),
            ).fetchone() is None:
                raise KeyError(request["project_id"])
            rows = connection.execute(
                f"SELECT session_id, project_id, state FROM development_sessions "
                f"WHERE session_id IN ({','.join('?' for _ in session_ids)})",
                tuple(session_ids),
            ).fetchall()
            by_id = {row["session_id"]: row for row in rows}
            if set(by_id) != set(session_ids):
                raise RequestError("one or more selected Sessions do not exist")
            if any(row["project_id"] != request["project_id"] for row in rows):
                raise RequestError("all selected Sessions must belong to the active Project")
            if any(row["state"] != "completed" for row in rows):
                raise StateConflictError("only completed Sessions can be used as Evolution evidence")
            missing_dataset = connection.execute(
                f"SELECT session_id FROM development_dataset_artifacts "
                f"WHERE session_id IN ({','.join('?' for _ in session_ids)})",
                tuple(session_ids),
            ).fetchall()
            available_session_ids = {row["session_id"] for row in missing_dataset}
            if available_session_ids != set(session_ids):
                unavailable = [
                    session_id for session_id in session_ids
                    if session_id not in available_session_ids
                ]
                raise StateConflictError(
                    "Sessions unavailable as Evolution evidence: " + ", ".join(unavailable)
                )
            running = connection.execute(
                "SELECT 1 FROM development_evolution_runs "
                "WHERE project_id = ? AND state = 'running' LIMIT 1",
                (request["project_id"],),
            ).fetchone()
            if running is not None:
                raise StateConflictError("another Evolution Run is already running for this Project")
            connection.execute(
                """
                INSERT INTO development_evolution_runs(
                    run_id, action_id, project_id, source_session_ids_json, selections_json,
                    state, artifact_ids_json, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'running', '[]', NULL, ?, ?)
                """,
                (
                    run_id,
                    action_id,
                    request["project_id"],
                    canonical_json(session_ids),
                    canonical_json(request["selections"]),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._evolution_run_record(row)

    def evolution_run_for_action(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any] | None:
        action_id = request.get("action_id")
        if action_id is None:
            return None
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE action_id = ?",
                (action_id,),
            ).fetchone()
        if row is None:
            return None
        record = self._evolution_run_record(row)
        if (
            record["project_id"] != request["project_id"]
            or record["source_session_ids"] != request["session_ids"]
            or record["selections"] != request["selections"]
        ):
            raise StateConflictError(
                "Evolution action_id is already bound to another request"
            )
        return record

    def evolution_run_v2(self, run_id: str) -> DevelopmentEvolutionRunV2:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._development_evolution_run_v2(
            self._evolution_run_record(row, include_action_id=True)
        )

    def evolution_run_page_v2(
        self,
        *,
        project_id: str,
        after_run_id: str | None,
        limit: int,
    ) -> DevelopmentEvolutionRunPageV2:
        with self._lock, self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM development_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone() is None:
                raise KeyError(project_id)
            parameters: list[object] = [project_id]
            cursor_clause = ""
            if after_run_id is not None:
                cursor = connection.execute(
                    "SELECT created_at, run_id FROM development_evolution_runs "
                    "WHERE project_id = ? AND run_id = ?",
                    (project_id, after_run_id),
                ).fetchone()
                if cursor is None:
                    raise RequestError("Evolution Run cursor is not part of this Project")
                cursor_clause = (
                    "AND (created_at > ? OR (created_at = ? AND run_id > ?)) "
                )
                parameters.extend(
                    [cursor["created_at"], cursor["created_at"], cursor["run_id"]]
                )
            rows = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE project_id = ? "
                + cursor_clause
                + "ORDER BY created_at, run_id LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        items = [
            self._development_evolution_run_v2(
                self._evolution_run_record(row, include_action_id=True)
            )
            for row in selected
        ]
        return DevelopmentEvolutionRunPageV2(
            items=items,
            next_cursor=items[-1].run_id if has_more and items else None,
            has_more=has_more,
        )

    def finish_evolution_run(
        self,
        run_id: str,
        *,
        artifact_ids: list[str],
        error: str | None,
    ) -> dict[str, Any]:
        now = utc_now()
        effective_error = error
        if effective_error is None and not artifact_ids:
            effective_error = "Evolution Run produced no candidate artifacts"
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE development_evolution_runs SET state = ?, artifact_ids_json = ?, "
                "error = ?, updated_at = ? WHERE run_id = ? AND state = 'running'",
                (
                    "failed" if effective_error is not None else "candidate_ready",
                    canonical_json(artifact_ids),
                    effective_error,
                    now,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise StateConflictError("Evolution Run is no longer running")
            row = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._evolution_run_record(row)

    def apply_evolution_run(self, run_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["state"] == "applied":
                return self._evolution_run_record(row)
            if row["state"] != "candidate_ready":
                raise StateConflictError("only a candidate-ready Evolution Run can be applied")
            artifact_ids = json.loads(row["artifact_ids_json"])
            if not artifact_ids:
                raise StateConflictError("Evolution Run produced no candidate artifacts")
            artifacts = connection.execute(
                f"SELECT artifact_id, project_id, target_id, artifact_type "
                f"FROM development_evolution_artifacts_v2 "
                f"WHERE artifact_id IN ({','.join('?' for _ in artifact_ids)})",
                tuple(artifact_ids),
            ).fetchall()
            if {item["artifact_id"] for item in artifacts} != set(artifact_ids):
                raise StateConflictError("Evolution Run candidate artifacts are incomplete")
            if any(item["project_id"] != row["project_id"] for item in artifacts):
                raise StateConflictError("Evolution Run candidate belongs to another Project")
            runtime_artifacts = [
                item for item in artifacts if item["artifact_type"] != "report"
            ]
            if runtime_artifacts:
                target_ids = sorted({item["target_id"] for item in runtime_artifacts})
                runtime_artifact_ids = [item["artifact_id"] for item in runtime_artifacts]
                connection.execute(
                    f"UPDATE development_evolution_artifacts_v2 SET promoted = 0 "
                    f"WHERE project_id = ? AND target_id IN "
                    f"({','.join('?' for _ in target_ids)})",
                    (row["project_id"], *target_ids),
                )
                connection.execute(
                    f"UPDATE development_evolution_artifacts_v2 SET promoted = 1 "
                    f"WHERE artifact_id IN "
                    f"({','.join('?' for _ in runtime_artifact_ids)})",
                    tuple(runtime_artifact_ids),
                )
            connection.execute(
                "UPDATE development_evolution_runs SET state = 'applied', updated_at = ? "
                "WHERE run_id = ?",
                (now, run_id),
            )
            updated = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._evolution_run_record(updated)

    def start_evolution_job(
        self,
        *,
        job_id: str,
        session_id: str,
        run_id: str | None = None,
        target_id: str,
        method_id: str,
        requested_method_id: str,
        resolver_input_artifact_ids: list[str],
        previous_artifact_id: str | None,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        attempt_id = f"{job_id}-attempt-1"
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO development_evolution_jobs(
                    job_id, session_id, run_id, target_id, method_id, requested_method_id,
                    resolver_input_artifact_ids_json, previous_artifact_id, config_json, state,
                    artifact_ids_json, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', '[]', NULL, ?, ?)
                """,
                (
                    job_id,
                    session_id,
                    run_id,
                    target_id,
                    method_id,
                    requested_method_id,
                    canonical_json(resolver_input_artifact_ids),
                    previous_artifact_id,
                    canonical_json(config),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO development_evolution_job_attempts(
                    attempt_id, job_id, ordinal, state, stage, artifact_ids_json,
                    error_code, error_message, logs_json, created_at, started_at,
                    completed_at, updated_at
                ) VALUES (?, ?, 1, 'running', 'input_resolution', '[]', NULL, NULL,
                          ?, ?, ?, NULL, ?)
                """,
                (
                    attempt_id,
                    job_id,
                    canonical_json(["Resolving the fixed Evolution Job inputs."]),
                    now,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM development_evolution_job_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return self._attempt_record(row)

    def evolution_retry_for_action(
        self,
        job_id: str,
        action_id: str,
    ) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            bound = connection.execute(
                "SELECT job_id FROM development_evolution_job_attempts WHERE action_id = ?",
                (action_id,),
            ).fetchone()
        if bound is None:
            return None
        if bound["job_id"] != job_id:
            raise StateConflictError(
                "Evolution retry action_id is already bound to another Job"
            )
        return self.get_evolution_job(job_id)

    def start_evolution_retry(
        self,
        job_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        job, attempt, _created = self.start_evolution_retry_v2(
            job_id,
            f"legacy-retry-{secrets.token_hex(16)}",
        )
        return job, attempt

    def start_evolution_retry_v2(
        self,
        job_id: str,
        action_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        now = utc_now()
        with self._lock, self._connection() as connection:
            bound = connection.execute(
                "SELECT * FROM development_evolution_job_attempts WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if bound is not None:
                if bound["job_id"] != job_id:
                    raise StateConflictError(
                        "Evolution retry action_id is already bound to another Job"
                    )
                job = connection.execute(
                    "SELECT * FROM development_evolution_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                attempts = [
                    self._attempt_record(attempt)
                    for attempt in connection.execute(
                        "SELECT * FROM development_evolution_job_attempts "
                        "WHERE job_id = ? ORDER BY ordinal",
                        (job_id,),
                    )
                ]
                return self._job_record(job, attempts), self._attempt_record(bound), False
            job_row = connection.execute(
                """
                SELECT job.*, session.state AS session_state, session.project_id
                FROM development_evolution_jobs AS job
                JOIN development_sessions AS session ON session.session_id = job.session_id
                WHERE job.job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if job_row is None:
                raise KeyError(job_id)
            if job_row["state"] != "failed":
                raise StateConflictError("only a failed Evolution Job can be retried")
            if job_row["session_state"] != "completed":
                raise StateConflictError("the parent Session must be completed before retry")
            running = connection.execute(
                """
                SELECT 1
                FROM development_evolution_jobs AS candidate
                JOIN development_sessions AS session
                  ON session.session_id = candidate.session_id
                WHERE session.project_id = ? AND candidate.state IN ('queued', 'running')
                LIMIT 1
                """,
                (job_row["project_id"],),
            ).fetchone()
            if running is not None:
                raise StateConflictError("another Evolution Job is already running for this project")
            ordinal = connection.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 AS ordinal "
                "FROM development_evolution_job_attempts WHERE job_id = ?",
                (job_id,),
            ).fetchone()["ordinal"]
            attempt_id = f"{job_id}-attempt-{ordinal}"
            connection.execute(
                """
                INSERT INTO development_evolution_job_attempts(
                    attempt_id, action_id, job_id, ordinal, state, stage, artifact_ids_json,
                    error_code, error_message, logs_json, created_at, started_at,
                    completed_at, updated_at
                ) VALUES (?, ?, ?, ?, 'running', 'input_resolution', '[]', NULL, NULL,
                          ?, ?, ?, NULL, ?)
                """,
                (
                    attempt_id,
                    action_id,
                    job_id,
                    ordinal,
                    canonical_json(["Retry admitted with the original fixed inputs."]),
                    now,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE development_evolution_jobs "
                "SET state = 'running', artifact_ids_json = '[]', error = NULL, updated_at = ? "
                "WHERE job_id = ?",
                (now, job_id),
            )
            if job_row["run_id"] is not None:
                connection.execute(
                    "UPDATE development_evolution_runs SET state = 'running', error = NULL, "
                    "updated_at = ? WHERE run_id = ? AND state = 'failed'",
                    (now, job_row["run_id"]),
                )
            job = connection.execute(
                "SELECT * FROM development_evolution_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            attempt = connection.execute(
                "SELECT * FROM development_evolution_job_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return (
            self._job_record(job, [self._attempt_record(attempt)]),
            self._attempt_record(attempt),
            True,
        )

    def reconcile_evolution_run(self, run_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connection() as connection:
            run = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            jobs = connection.execute(
                "SELECT state, artifact_ids_json, error FROM development_evolution_jobs "
                "WHERE run_id = ? ORDER BY created_at, job_id",
                (run_id,),
            ).fetchall()
            artifact_ids = [
                artifact_id
                for job in jobs
                for artifact_id in json.loads(job["artifact_ids_json"])
            ]
            errors = [job["error"] for job in jobs if job["error"]]
            if jobs and all(job["state"] == "completed" for job in jobs):
                state = "candidate_ready"
                error = None
            elif any(job["state"] == "failed" for job in jobs):
                state = "failed"
                error = "; ".join(errors) or "one or more Evolution methods failed"
            else:
                state = "running"
                error = None
            connection.execute(
                "UPDATE development_evolution_runs SET state = ?, artifact_ids_json = ?, "
                "error = ?, updated_at = ? WHERE run_id = ? AND state != 'applied'",
                (state, canonical_json(artifact_ids), error, now, run_id),
            )
            updated = connection.execute(
                "SELECT * FROM development_evolution_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._evolution_run_record(updated)

    def update_evolution_attempt(self, attempt_id: str, *, stage: str, message: str) -> None:
        now = utc_now()
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT logs_json, state FROM development_evolution_job_attempts "
                "WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            if row["state"] != "running":
                raise StateConflictError("Evolution attempt is already terminal")
            logs = json.loads(row["logs_json"])
            logs.append(message)
            connection.execute(
                "UPDATE development_evolution_job_attempts "
                "SET stage = ?, logs_json = ?, updated_at = ? WHERE attempt_id = ?",
                (stage, canonical_json(logs), now, attempt_id),
            )

    def finish_evolution_job(
        self,
        job_id: str,
        *,
        attempt_id: str | None = None,
        artifact_ids: list[str] | None = None,
        error: str | None = None,
        error_stage: str | None = None,
        error_code: str | None = None,
    ) -> None:
        now = utc_now()
        with self._lock, self._connection() as connection:
            if attempt_id is None:
                attempt_row = connection.execute(
                    "SELECT attempt_id FROM development_evolution_job_attempts "
                    "WHERE job_id = ? AND state = 'running' ORDER BY ordinal DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
                attempt_id = None if attempt_row is None else attempt_row["attempt_id"]
            connection.execute(
                """
                UPDATE development_evolution_jobs
                SET state = ?, artifact_ids_json = ?, error = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    "failed" if error is not None else "completed",
                    canonical_json(artifact_ids or []),
                    error,
                    now,
                    job_id,
                ),
            )
            if attempt_id is not None:
                attempt_row = connection.execute(
                    "SELECT logs_json FROM development_evolution_job_attempts WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()
                if attempt_row is None:
                    raise KeyError(attempt_id)
                logs = json.loads(attempt_row["logs_json"])
                logs.append(
                    "Evolution attempt failed." if error is not None
                    else "Evolution attempt completed and published its outputs."
                )
                connection.execute(
                    """
                    UPDATE development_evolution_job_attempts
                    SET state = ?, stage = ?, artifact_ids_json = ?, error_code = ?,
                        error_message = ?, logs_json = ?, completed_at = ?, updated_at = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        "failed" if error is not None else "completed",
                        error_stage or ("failed" if error is not None else "completed"),
                        canonical_json(artifact_ids or []),
                        error_code,
                        error,
                        canonical_json(logs),
                        now,
                        now,
                        attempt_id,
                    ),
                )

    def get_evolution_job(self, job_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM development_evolution_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            attempts = [
                self._attempt_record(attempt)
                for attempt in connection.execute(
                    "SELECT * FROM development_evolution_job_attempts "
                    "WHERE job_id = ? ORDER BY ordinal",
                    (job_id,),
                )
            ]
        return self._job_record(row, attempts)

    def evolution_job_v2(self, job_id: str) -> DevelopmentEvolutionJobV2:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT job.*, session.project_id "
                "FROM development_evolution_jobs AS job "
                "JOIN development_sessions AS session "
                "ON session.session_id = job.session_id "
                "WHERE job.job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            attempts = [
                self._attempt_record(attempt, include_action_id=True)
                for attempt in connection.execute(
                    "SELECT * FROM development_evolution_job_attempts "
                    "WHERE job_id = ? ORDER BY ordinal",
                    (job_id,),
                )
            ]
        return self._development_evolution_job_v2(
            self._job_record(row, attempts),
            project_id=row["project_id"],
        )

    def evolution_job_page_v2(
        self,
        *,
        project_id: str,
        after_job_id: str | None,
        limit: int,
    ) -> DevelopmentEvolutionJobPageV2:
        with self._lock, self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM development_projects WHERE project_id = ?",
                (project_id,),
            ).fetchone() is None:
                raise KeyError(project_id)
            parameters: list[object] = [project_id]
            cursor_clause = ""
            if after_job_id is not None:
                cursor = connection.execute(
                    "SELECT job.created_at, job.job_id "
                    "FROM development_evolution_jobs AS job "
                    "JOIN development_sessions AS session "
                    "ON session.session_id = job.session_id "
                    "WHERE session.project_id = ? AND job.job_id = ?",
                    (project_id, after_job_id),
                ).fetchone()
                if cursor is None:
                    raise RequestError("Evolution Job cursor is not part of this Project")
                cursor_clause = (
                    "AND (job.created_at > ? OR "
                    "(job.created_at = ? AND job.job_id > ?)) "
                )
                parameters.extend(
                    [cursor["created_at"], cursor["created_at"], cursor["job_id"]]
                )
            rows = connection.execute(
                "SELECT job.*, session.project_id "
                "FROM development_evolution_jobs AS job "
                "JOIN development_sessions AS session "
                "ON session.session_id = job.session_id "
                "WHERE session.project_id = ? "
                + cursor_clause
                + "ORDER BY job.created_at, job.job_id LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
            selected = rows[:limit]
            attempts_by_job: dict[str, list[dict[str, Any]]] = {}
            if selected:
                job_ids = [row["job_id"] for row in selected]
                for attempt in connection.execute(
                    "SELECT * FROM development_evolution_job_attempts "
                    f"WHERE job_id IN ({','.join('?' for _ in job_ids)}) "
                    "ORDER BY job_id, ordinal",
                    tuple(job_ids),
                ):
                    attempts_by_job.setdefault(attempt["job_id"], []).append(
                        self._attempt_record(attempt, include_action_id=True)
                    )
        has_more = len(rows) > limit
        items = [
            self._development_evolution_job_v2(
                self._job_record(row, attempts_by_job.get(row["job_id"], [])),
                project_id=row["project_id"],
            )
            for row in selected
        ]
        return DevelopmentEvolutionJobPageV2(
            items=items,
            next_cursor=items[-1].job_id if has_more and items else None,
            has_more=has_more,
        )

    def dataset_artifact(self, artifact_id: str) -> dict[str, str]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT artifact_id, project_id, session_id, uri, name, created_at "
                "FROM development_dataset_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return dict(row)

    def artifact(self, artifact_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM development_evolution_artifacts_v2 WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        return self._artifact_record(row)

    def artifact_observation_v2(self, artifact_id: str) -> core_v2.ArtifactV2:
        return self._core_artifact_v2(self.artifact(artifact_id))

    def artifact_content_observation_v2(
        self, artifact_id: str
    ) -> core_v2.ArtifactContentV2:
        record = self.artifact(artifact_id)
        documents = record["documents"]
        media_type = (
            documents[0]["media_type"] if documents else "application/octet-stream"
        )
        artifact = self._core_artifact_v2(record)
        return core_v2.ArtifactContentV2(
            artifact=artifact,
            media_type=media_type,
            content_sha256=record["content_sha256"],
            byte_size=record["byte_size"],
        )

    def artifact_page_v2(
        self,
        *,
        project_id: str | None,
        task_id: str | None,
        after_artifact_id: str | None,
        limit: int,
        development_detail: bool,
    ) -> core_v2.ArtifactPageV2 | DevelopmentArtifactPageV2:
        clauses: list[str] = []
        parameters: list[object] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            parameters.append(project_id)
        if task_id is not None:
            clauses.append("session_id = ?")
            parameters.append(task_id)
        where = " AND ".join(clauses) if clauses else "1 = 1"
        with self._lock, self._connection() as connection:
            if project_id is not None and connection.execute(
                "SELECT 1 FROM development_projects WHERE project_id = ?", (project_id,)
            ).fetchone() is None:
                raise KeyError(project_id)
            if task_id is not None and connection.execute(
                "SELECT 1 FROM development_sessions WHERE session_id = ?", (task_id,)
            ).fetchone() is None:
                raise KeyError(task_id)
            if after_artifact_id is not None:
                cursor = connection.execute(
                    f"SELECT created_at, artifact_id FROM development_evolution_artifacts_v2 "
                    f"WHERE {where} AND artifact_id = ?",
                    (*parameters, after_artifact_id),
                ).fetchone()
                if cursor is None:
                    raise RequestError("artifact cursor is not part of this collection")
                clauses.append("(created_at > ? OR (created_at = ? AND artifact_id > ?))")
                parameters.extend(
                    [cursor["created_at"], cursor["created_at"], cursor["artifact_id"]]
                )
                where = " AND ".join(clauses)
            rows = connection.execute(
                f"SELECT * FROM development_evolution_artifacts_v2 WHERE {where} "
                "ORDER BY created_at, artifact_id LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        records = [self._artifact_record(row) for row in selected]
        next_cursor = records[-1]["artifact_id"] if has_more and records else None
        if development_detail:
            return DevelopmentArtifactPageV2.model_validate({
                "schema_version": "2",
                "items": [self._development_artifact_v2(record) for record in records],
                "next_cursor": next_cursor,
                "has_more": has_more,
            })
        return core_v2.ArtifactPageV2(
            items=[self._core_artifact_v2(record) for record in records],
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def development_artifact_v2(self, artifact_id: str) -> DevelopmentArtifactV2:
        return DevelopmentArtifactV2.model_validate(
            self._development_artifact_v2(self.artifact(artifact_id))
        )

    def record_evolution_artifact(
        self,
        *,
        artifact_id: str,
        project_id: str,
        session_id: str,
        run_id: str | None = None,
        target_id: str,
        artifact_type: str,
        method_id: str,
        renderer_kind: str,
        documents: list[dict[str, str]],
        manifest: dict[str, Any],
        previous_artifact_id: str | None,
        promoted: bool,
    ) -> dict[str, Any]:
        encoded = canonical_json(documents).encode("utf-8")
        created_at = utc_now()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO development_evolution_artifacts_v2(
                    artifact_id, project_id, session_id, run_id, target_id, artifact_type,
                    method_id, renderer_kind, documents_json, manifest_json,
                    content_sha256, byte_size, previous_artifact_id, promoted, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    project_id,
                    session_id,
                    run_id,
                    target_id,
                    artifact_type,
                    method_id,
                    renderer_kind,
                    canonical_json(documents),
                    canonical_json(manifest),
                    hashlib.sha256(encoded).hexdigest(),
                    sum(len(document["content"].encode("utf-8")) for document in documents),
                    previous_artifact_id,
                    int(promoted),
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM development_evolution_artifacts_v2 WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("document evolution artifact was not persisted")
        return self._artifact_record(row)

    def fail_session(
        self,
        session_id: str,
        error: str,
        workspace_changes: list[dict[str, Any]] | None = None,
    ) -> None:
        now = utc_now()
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT logs_json FROM development_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            logs = json.loads(row["logs_json"])
            message = f"Session failed: {error}"
            logs.append(message)
            connection.execute(
                """
                UPDATE development_sessions
                SET state = 'failed', logs_json = ?, workspace_changes_json = ?,
                    terminal_kind = 'failed', error = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (canonical_json(logs), canonical_json(workspace_changes or []), error, now, session_id),
            )
            self._append_task_log_v2(
                connection,
                task_id=session_id,
                stream="system",
                message=message,
                occurred_at=now,
            )

    @staticmethod
    def _session_record(
        row: sqlite3.Row,
        *,
        evolution_evidence_ready: bool = False,
    ) -> dict[str, Any]:
        state = row["state"]
        if row["terminal_kind"] == "cancelled":
            state = "cancelled"
        elif state == "running" and row["cancellation_requested"]:
            state = "cancelling"
        return {
            "session_id": row["session_id"],
            "project_id": row["project_id"],
            "task_title": row["task_title"],
            "instruction": row["instruction"],
            "response": row["response"],
            "model": row["model"],
            "state": state,
            "duration_ms": row["duration_ms"],
            "logs": json.loads(row["logs_json"]),
            "selected_evolution": normalize_selected_evolution(
                json.loads(row["selected_evolution_json"])
            ),
            "evolution_errors": json.loads(row["evolution_errors_json"]),
            "workspace_changes": json.loads(row["workspace_changes_json"]),
            "context_artifact_ids": json.loads(row["context_artifact_ids_json"]),
            "runtime_activation": json.loads(row["runtime_activation_json"]),
            "evolution_evidence_ready": evolution_evidence_ready,
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _task_observation_v2(row: sqlite3.Row) -> DevelopmentTaskObservationV2:
        state = row["state"]
        if row["terminal_kind"] == "cancelled":
            state = "cancelled"
        elif state == "running" and row["cancellation_requested"]:
            state = "cancelling"
        elif state == "completed":
            state = "closed"
        return DevelopmentTaskObservationV2(
            task_id=row["session_id"],
            project_id=row["project_id"],
            state=state,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _artifact_record(row: sqlite3.Row) -> dict[str, Any]:
        documents = json.loads(row["documents_json"])
        primary = documents[0] if documents else None
        return {
            "artifact_id": row["artifact_id"],
            "project_id": row["project_id"],
            "session_id": row["session_id"],
            "run_id": row["run_id"],
            "target_id": row["target_id"],
            "artifact_type": row["artifact_type"],
            "method": row["method_id"],
            "renderer_kind": row["renderer_kind"],
            "documents": documents,
            "manifest": json.loads(row["manifest_json"]),
            "content_path": primary["path"] if primary else None,
            "content": primary["content"] if primary else None,
            "content_sha256": row["content_sha256"],
            "byte_size": row["byte_size"],
            "previous_artifact_id": row["previous_artifact_id"],
            "promoted": bool(row["promoted"]),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _core_artifact_v2(record: dict[str, Any]) -> core_v2.ArtifactV2:
        artifact_type = (
            "diagnostic" if record["artifact_type"] == "report"
            else record["artifact_type"]
        )
        return core_v2.ArtifactV2(
            artifact_id=record["artifact_id"],
            project_id=record["project_id"],
            artifact_type=artifact_type,
            manifest_sha256=record["content_sha256"],
            byte_size=record["byte_size"],
            created_at=record["created_at"],
        )

    @staticmethod
    def _development_artifact_v2(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "2",
            **record,
            "documents": [
                {"schema_version": "2", **document}
                for document in record["documents"]
            ],
        }

    @staticmethod
    def _event_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "sequence": row["sequence"],
            "event_id": row["event_id"],
            "project_id": row["project_id"],
            "event_type": row["event_type"],
            "payload_sha256": row["payload_sha256"],
            "occurred_at": row["occurred_at"],
        }

    @staticmethod
    def _job_record(
        row: sqlite3.Row,
        attempts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "job_id": row["job_id"],
            "session_id": row["session_id"],
            "run_id": row["run_id"],
            "target_id": row["target_id"],
            "method_id": row["method_id"],
            "requested_method_id": row["requested_method_id"],
            "resolver_input_artifact_ids": json.loads(
                row["resolver_input_artifact_ids_json"]
            ),
            "previous_artifact_id": row["previous_artifact_id"],
            "config": json.loads(row["config_json"]),
            "state": row["state"],
            "artifact_ids": json.loads(row["artifact_ids_json"]),
            "error": row["error"],
            "attempts": attempts or [],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _evolution_run_record(
        row: sqlite3.Row,
        *,
        include_action_id: bool = False,
    ) -> dict[str, Any]:
        record = {
            "run_id": row["run_id"],
            "project_id": row["project_id"],
            "source_session_ids": json.loads(row["source_session_ids_json"]),
            "selections": normalize_selected_evolution(json.loads(row["selections_json"])),
            "state": row["state"],
            "artifact_ids": json.loads(row["artifact_ids_json"]),
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_action_id:
            record["action_id"] = row["action_id"]
        return record

    @staticmethod
    def _development_evolution_run_v2(
        record: dict[str, Any],
    ) -> DevelopmentEvolutionRunV2:
        return DevelopmentEvolutionRunV2.model_validate({
            "schema_version": "2",
            "run_id": record["run_id"],
            "action_id": record["action_id"],
            "project_id": record["project_id"],
            "source_task_ids": record["source_session_ids"],
            "selections": [
                {"schema_version": "2", **selection}
                for selection in record["selections"]
            ],
            "state": record["state"],
            "artifact_ids": record["artifact_ids"],
            "error": record["error"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        })

    @staticmethod
    def _development_evolution_job_v2(
        record: dict[str, Any],
        *,
        project_id: str,
    ) -> DevelopmentEvolutionJobV2:
        return DevelopmentEvolutionJobV2.model_validate({
            "schema_version": "2",
            "job_id": record["job_id"],
            "project_id": project_id,
            "task_id": record["session_id"],
            "run_id": record["run_id"],
            "target_id": record["target_id"],
            "method_id": record["method_id"],
            "requested_method_id": record["requested_method_id"],
            "resolver_input_artifact_ids": record["resolver_input_artifact_ids"],
            "previous_artifact_id": record["previous_artifact_id"],
            "config": record["config"],
            "state": record["state"],
            "artifact_ids": record["artifact_ids"],
            "error": record["error"],
            "attempts": [
                {"schema_version": "2", **attempt}
                for attempt in record["attempts"]
            ],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        })

    @staticmethod
    def _attempt_record(
        row: sqlite3.Row,
        *,
        include_action_id: bool = False,
    ) -> dict[str, Any]:
        record = {
            "attempt_id": row["attempt_id"],
            "job_id": row["job_id"],
            "ordinal": row["ordinal"],
            "state": row["state"],
            "stage": row["stage"],
            "artifact_ids": json.loads(row["artifact_ids_json"]),
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "logs": json.loads(row["logs_json"]),
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "updated_at": row["updated_at"],
        }
        if include_action_id:
            record["action_id"] = row["action_id"]
        return record


def validate_request(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise RequestError("request body must be a JSON object")
    unknown = set(payload) - ALLOWED_REQUEST_FIELDS
    if unknown:
        raise RequestError(f"unknown request fields: {', '.join(sorted(unknown))}")
    if payload.get("schema_version") != "1":
        raise RequestError("schema_version must be '1'")

    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not ID_PATTERN.fullmatch(project_id):
        raise RequestError("project_id is invalid")

    result = {"project_id": project_id}
    for field, maximum in (("project_name", 200), ("task_title", 200), ("instruction", 32_000)):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise RequestError(f"{field} must be a non-empty string")
        if len(value) > maximum:
            raise RequestError(f"{field} is too long")
        result[field] = value.strip()
    return result


def extract_event_logs(stdout: str) -> list[str]:
    messages: list[str] = []
    for raw_line in stdout.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if isinstance(event_type, str) and event_type not in {"item.completed"}:
            messages.append(f"Codex event: {event_type}")
    return messages[-20:]


class _DevelopmentArtifactPayloads:
    """Bounded read service for DB-owned development artifact documents."""

    def __init__(self, documents: dict[str, dict[str, str]]) -> None:
        self._documents = documents

    def read_utf8_prefix(
        self,
        payload_handle: str,
        relative_path: str,
        *,
        max_chars: int,
        max_bytes: int,
    ) -> str:
        try:
            content = self._documents[payload_handle][relative_path]
        except KeyError as exc:
            raise ValueError("development artifact payload is unavailable") from exc
        clipped = content[:max_chars]
        encoded = clipped.encode("utf-8")
        if len(encoded) > max_bytes:
            clipped = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return clipped


class DevelopmentRuntimeContextMaterializer:
    """Project Core handler contributions into one isolated Codex runtime workspace.

    This is a development adapter, not the release artifact store/materializer. It deliberately
    consumes the same closed handler input/output contracts so target behavior is not inferred
    from a UI card or renderer kind.
    """

    def __init__(self, registry: Any | None = None) -> None:
        self._registry = registry or development_registry_snapshot()

    @staticmethod
    def _copy_workspace(source: Path, destination: Path) -> None:
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        entries = 0
        for candidate in sorted(source.rglob("*")):
            entries += 1
            if entries > MAX_WORKSPACE_ENTRIES:
                raise AgentRunError("persistent workspace exceeds the runtime entry limit")
            if candidate.is_symlink():
                raise AgentRunError("persistent workspace contains an unsupported symbolic link")
            relative = candidate.relative_to(source)
            target = destination / relative
            if candidate.is_dir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            elif candidate.is_file():
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(candidate, target)
            else:
                raise AgentRunError("persistent workspace contains an unsupported entry")

    @staticmethod
    def _scope_roots(runtime_workspace: Path) -> dict[str, Path]:
        return {
            "target_data": runtime_workspace / ".openevo" / "evolution",
            "harness_skills": runtime_workspace / ".agents" / "skills",
            "harness_instruction": runtime_workspace,
        }

    @staticmethod
    def _write_text(root: Path, relative_path: str, content: str) -> Path:
        from openevo.evolution.framework.contracts import validate_relative_path

        normalized = validate_relative_path(relative_path)
        destination = root.joinpath(*PurePosixPath(normalized).parts)
        root_resolved = root.resolve()
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.is_symlink() or root_resolved not in destination.resolve().parents:
            raise AgentRunError("runtime contribution escaped its destination scope")
        destination.write_text(content, encoding="utf-8")
        return destination

    @staticmethod
    def _codex_skill_entrypoint(skill_directory: str, content: str) -> str:
        """Add required Codex skill metadata without changing the stored artifact."""

        if content.startswith("---\n"):
            closing = content.find("\n---\n", 4)
            if closing != -1:
                header = content[4:closing]
                if re.search(r"(?m)^name:\s*\S+", header) and re.search(
                    r"(?m)^description:\s*\S+", header
                ):
                    return content
        normalized = re.sub(r"[^a-z0-9-]+", "-", skill_directory.lower()).strip("-")
        if not normalized or not normalized[0].isalpha():
            normalized = f"openevo-{normalized or 'evolved-skill'}"
        normalized = normalized[:64].rstrip("-")
        return (
            "---\n"
            f"name: {normalized}\n"
            "description: Apply this evolved OpenEvo workflow when the current task "
            "matches its instructions.\n"
            "---\n\n"
            f"{content.lstrip()}"
        )

    def _project(self, contexts: object) -> tuple[list[tuple[Any, Any]], dict[str, dict[str, str]]]:
        from openevo.evolution.framework.builtin_handlers import BUILTIN_HANDLER_REGISTRY
        from openevo.evolution.framework.contracts import EvolutionExecutionProfile
        from openevo.evolution.framework.handlers import (
            PayloadManifestEntry,
            RuntimeDestinationRoots,
            TargetHandlerInput,
            TargetHandlerServices,
            TrustedArtifactSnapshot,
            payload_tree_digest,
        )

        pairs: list[tuple[Any, Any]] = []
        payload_documents: dict[str, dict[str, str]] = {}
        if not isinstance(contexts, list):
            return pairs, payload_documents
        for rank, context in enumerate(contexts):
            if not isinstance(context, dict):
                raise AgentRunError("evolved context record is invalid")
            target_id = context.get("target_id")
            try:
                target = self._registry.targets[target_id]
                handler_descriptor = self._registry.target_handlers[target.handler_id]
                handler = BUILTIN_HANDLER_REGISTRY[target.handler_id]
            except (KeyError, TypeError) as exc:
                raise AgentRunError(f"evolved target {target_id!r} is not in the Core catalog") from exc
            if context.get("artifact_type") != target.artifact_type:
                raise AgentRunError(f"evolved target {target_id!r} has the wrong artifact type")
            raw_documents = context.get("documents")
            if not isinstance(raw_documents, list) or not raw_documents:
                raise AgentRunError(f"evolved target {target_id!r} has no readable payload")
            handle = f"development_payload_{rank}"
            document_map: dict[str, str] = {}
            entries: list[Any] = []
            for raw_document in raw_documents:
                if not isinstance(raw_document, dict):
                    raise AgentRunError("evolved artifact document is invalid")
                path = raw_document.get("path")
                content = raw_document.get("content")
                media_type = raw_document.get("media_type", "text/plain")
                if not isinstance(path, str) or not isinstance(content, str) or not isinstance(
                    media_type, str
                ):
                    raise AgentRunError("evolved artifact document is invalid")
                encoded = content.encode("utf-8")
                entry = PayloadManifestEntry(
                    relative_path=path,
                    media_type=media_type,
                    size_bytes=len(encoded),
                    sha256=hashlib.sha256(encoded).hexdigest(),
                )
                entries.append(entry)
                document_map[entry.relative_path] = content
            payload_documents[handle] = document_map
            manifest = context.get("manifest")
            if not isinstance(manifest, dict):
                raise AgentRunError("evolved artifact manifest is invalid")
            artifact_id = context.get("artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id:
                raise AgentRunError("evolved artifact identity is invalid")
            payload_entries = tuple(sorted(entries, key=lambda entry: entry.relative_path))
            snapshot = TrustedArtifactSnapshot(
                artifact_id=artifact_id,
                artifact_type=target.artifact_type,
                name=f"evolved {target.display_name}",
                uri_scheme="file",
                payload_handle=handle,
                payload_entries=payload_entries,
                payload_manifest_digest=payload_tree_digest(payload_entries),
                manifest_json=canonical_json(manifest),
                scores_json="{}",
                rank_index=0,
            )
            handler_input = TargetHandlerInput(
                target_id=target.id,
                handler_id=target.handler_id,
                execution_profile=EvolutionExecutionProfile(
                    execution_mode="subscription",
                    capture_mode="transcript",
                    harness_id="codex",
                ),
                # Handler contracts require canonical Linux runtime roots. The development
                # materializer maps these scopes into its private temporary workspace below.
                destination_roots=RuntimeDestinationRoots(
                    target_data="/openevo/session/evolution",
                    harness_skills="/openevo/session/evolution/skills",
                    harness_instruction="/workspace",
                ),
                ranked_artifacts=(snapshot,),
            )
            output = self._registry.validate_handler_output(
                handler(
                    handler_input,
                    TargetHandlerServices(
                        payloads=_DevelopmentArtifactPayloads(payload_documents)
                    ),
                ),
                handler_input=handler_input,
            )
            if output.handler_id != handler_descriptor.id:
                raise AgentRunError("Core target handler identity changed during projection")
            pairs.append((handler_input, output))
        return pairs, payload_documents

    def materialize(
        self,
        *,
        persistent_workspace: Path,
        runtime_workspace: Path,
        contexts: object,
    ) -> dict[str, Any]:
        from openevo.evolution.framework.contracts import (
            DestinationScope,
            EnvironmentValueKind,
        )
        from openevo.evolution.framework.contributions import (
            InlineTextPayloadContribution,
            StagedPayloadContribution,
        )
        from openevo.evolution.framework.runtime_controls import (
            AgentSystemRuntimeControlV1,
            validate_runtime_control,
        )

        self._copy_workspace(persistent_workspace, runtime_workspace)
        pairs, payload_documents = self._project(contexts)
        outputs = self._registry.validate_handler_outputs(pairs)
        scope_roots = self._scope_roots(runtime_workspace)
        artifact_handles = {
            handler_input.ranked_artifacts[0].artifact_id:
                handler_input.ranked_artifacts[0].payload_handle
            for handler_input, _output in pairs
        }
        contribution_paths: dict[str, Path] = {}
        instructions: list[str] = []
        activations: list[str] = []
        environment: dict[str, str] = {}
        runtime_controls: list[dict[str, Any]] = []

        for output in outputs:
            handler_descriptor = self._registry.target_handlers[output.handler_id]
            for instruction in output.instructions:
                section = instruction.text.strip()
                if handler_descriptor.instruction_preamble:
                    section = f"{handler_descriptor.instruction_preamble}\n{section}"
                instructions.append(section)
            for payload in output.staged_payloads:
                scope = payload.destination_scope.value
                root = scope_roots[scope]
                if isinstance(payload, InlineTextPayloadContribution):
                    contribution_paths[payload.contribution_id] = self._write_text(
                        root, payload.destination_relative_path, payload.text
                    )
                    if payload.destination_relative_path.startswith("runtime-controls/"):
                        try:
                            control = validate_runtime_control(json.loads(payload.text))
                        except (json.JSONDecodeError, ValueError) as exc:
                            raise AgentRunError(
                                "Core returned an invalid runtime-control contribution"
                            ) from exc
                        runtime_controls.append(control.model_dump(mode="json"))
                        activations.append(
                            f"{output.target_id}: {control.kind} runtime control v"
                            f"{control.contract_version} loaded"
                        )
                        if (
                            isinstance(control, AgentSystemRuntimeControlV1)
                            and control.spawn_plan is not None
                        ):
                            activations.append(
                                f"{output.target_id}: structured spawn plan staged for "
                                "the harness adapter"
                            )
                    continue
                if not isinstance(payload, StagedPayloadContribution):
                    raise AgentRunError("Core returned an unsupported payload contribution")
                source_handle = artifact_handles.get(payload.source_artifact_id)
                source = payload_documents.get(source_handle or "")
                if source is None:
                    raise AgentRunError("Core contribution source is unavailable")
                destination_root = root.joinpath(
                    *PurePosixPath(payload.destination_relative_path).parts
                )
                written: list[Path] = []
                if payload.source_relative_path == ".":
                    for source_path, content in source.items():
                        if (
                            payload.destination_scope is DestinationScope.HARNESS_SKILLS
                            and source_path == "SKILL.md"
                        ):
                            content = self._codex_skill_entrypoint(
                                payload.destination_relative_path,
                                content,
                            )
                        written.append(self._write_text(destination_root, source_path, content))
                    contribution_paths[payload.contribution_id] = destination_root
                else:
                    try:
                        content = source[payload.source_relative_path]
                    except KeyError as exc:
                        raise AgentRunError("Core contribution source file is unavailable") from exc
                    written.append(
                        self._write_text(
                            root, payload.destination_relative_path, content
                        )
                    )
                    contribution_paths[payload.contribution_id] = written[0]
            for binding in output.environment:
                if binding.value_kind is EnvironmentValueKind.SCOPE_ROOT:
                    if binding.destination_scope is None:
                        raise AgentRunError("Core returned an invalid scope-root binding")
                    environment[binding.name] = os.fspath(
                        scope_roots[binding.destination_scope.value]
                    )
                    continue
                paths = [
                    os.fspath(contribution_paths[contribution_id])
                    for contribution_id in binding.value_contribution_ids
                ]
                if binding.value_kind is EnvironmentValueKind.JSON_PATHS:
                    environment[binding.name] = canonical_json(paths)
                elif len(paths) == 1:
                    environment[binding.name] = paths[0]
                else:
                    raise AgentRunError("Core returned an invalid runtime path binding")
            if output.instructions:
                activations.append(f"{output.target_id}: instruction contribution loaded")
            if any(
                payload.destination_scope is DestinationScope.HARNESS_SKILLS
                for payload in output.staged_payloads
            ):
                activations.append(f"{output.target_id}: Codex skill bundle staged")
            if any(
                payload.destination_scope is DestinationScope.HARNESS_INSTRUCTION
                for payload in output.staged_payloads
            ):
                activations.append(f"{output.target_id}: native harness instruction staged")

        return {
            "workspace_path": runtime_workspace,
            "instruction_sections": instructions,
            "environment": environment,
            "activations": activations,
            "runtime_controls": runtime_controls,
        }


class CodexRunner:
    def __init__(self, codex_binary: str, timeout_seconds: int, model: str | None) -> None:
        self._adapter = CodexHarnessAdapter(
            codex_binary=codex_binary,
            timeout_seconds=timeout_seconds,
            model=model,
            context_materializer_factory=DevelopmentRuntimeContextMaterializer,
            runtime_control_adapter=codex_development_runtime_adapter(),
            extract_event_logs=extract_event_logs,
            max_capture_bytes=MAX_CAPTURE_BYTES,
            max_response_bytes=MAX_REQUEST_BYTES,
            max_workspace_context_bytes=MAX_AGENT_WORKSPACE_CONTEXT_BYTES,
        )

    @property
    def codex_binary(self) -> str:
        return self._adapter.codex_binary

    @property
    def model(self) -> str | None:
        return self._adapter.model

    def runtime_capabilities(self) -> dict[str, Any]:
        return self._adapter.runtime_capabilities()

    def check_ready(self) -> None:
        try:
            self._adapter.check_ready()
        except HarnessRunError as exc:
            raise AgentRunError(str(exc)) from exc

    def run(
        self,
        request: dict[str, Any],
        *,
        cancellation: HarnessCancellation | None = None,
        log: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        try:
            return self._adapter.run(request, cancellation=cancellation, log=log)
        except HarnessRunCancelled:
            raise
        except HarnessRunError as exc:
            raise AgentRunError(str(exc)) from exc

class DocumentEvolutionRunner:
    """Development adapter driven by Core framework descriptors instead of target switches."""

    def __init__(
        self,
        *,
        state_root: Path,
        codex_binary: str,
        model: str,
        timeout_seconds: int,
    ) -> None:
        self._artifact_root = state_root / "evolution-artifacts"
        self._artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._codex_binary = codex_binary
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._registry: Any | None = None
        self._capabilities: dict[str, Any] | None = None

    def check_ready(self) -> None:
        if sys.version_info < (3, 11):
            raise EvolutionRunError(
                "real document evolution requires Python 3.11 or newer; use `uv run python`"
            )
        try:
            from openevo.evolution.framework.builtins import (  # noqa: F401
                ImplementationDistributionIdentity,
                build_builtin_registry,
            )
            from openevo.evolution.framework.capabilities import (  # noqa: F401
                build_evolution_capabilities,
            )
            from openevo.evolution.methods import METHOD_REGISTRY  # noqa: F401
            from openevo.evolution.models import WorkerClaimedJob  # noqa: F401
        except (ImportError, ModuleNotFoundError) as exc:
            raise EvolutionRunError(
                "OpenEvo Python dependencies are unavailable; run this daemon with `uv run python`"
            ) from exc
        self._load_catalog()

    def _load_catalog(self) -> None:
        from openevo.evolution.framework.capabilities import build_evolution_capabilities
        from openevo.evolution.framework.contracts import EvolutionExecutionProfile

        self._registry = development_registry_snapshot()
        capability = build_evolution_capabilities(
            self._registry,
            profile=EvolutionExecutionProfile(
                execution_mode="subscription",
                capture_mode="transcript",
                harness_id="codex",
            ),
            audience="maintainer",
            core_version="development-catalog-unverified",
        ).model_dump(mode="json")
        # The development bridge currently executes the legacy worker ABI. Context-v1 methods
        # stay registered in Core but are not advertised as runnable by this bridge yet.
        for target in capability["targets"]:
            target["methods"] = [
                method
                for method in target["methods"]
                if self._registry.methods[method["method_id"]].invocation_abi.value
                == "legacy_worker_job_v1"
            ]
        self._capabilities = capability

    def capabilities(self) -> dict[str, Any]:
        if self._capabilities is None:
            self._load_catalog()
        return {
            "schema_version": "1",
            "authority": "development_catalog_unverified",
            "capabilities": self._capabilities,
        }

    def _descriptor(self, target_id: str, method_id: str) -> tuple[Any, Any, Any]:
        if self._registry is None:
            self._load_catalog()
        try:
            target = self._registry.targets[target_id]
            method = self._registry.methods[method_id]
            handler = self._registry.target_handlers[target.handler_id]
        except KeyError as exc:
            raise EvolutionRunError(f"unknown evolution selection {target_id}/{method_id}") from exc
        if method.target_id != target.id:
            raise EvolutionRunError(f"{method_id} does not belong to target {target_id}")
        if method.invocation_abi.value != "legacy_worker_job_v1":
            raise EvolutionRunError(
                f"{method_id} uses {method.invocation_abi.value}, which this development bridge "
                "does not execute yet"
            )
        return target, method, handler

    def _method_config(self, method: Any, requested: dict[str, Any]) -> dict[str, Any]:
        if self._registry is None:
            raise EvolutionRunError("evolution catalog is unavailable")
        config = dict(requested)
        for injection in method.project_config_injections:
            if injection.source.value == "reflector_llm":
                config[injection.field_name] = {
                    "provider": "codex_cli",
                    "model": self._model,
                    "timeout_seconds": self._timeout_seconds,
                }
            else:
                raise EvolutionRunError(
                    f"development bridge cannot provide {injection.source.value}"
                )
        normalized = self._registry.normalize_method_config(method.id, config)
        normalized.update({"promoted": True})
        return normalized

    @staticmethod
    def _materialize_previous(previous: dict[str, Any], root: Path) -> str:
        documents = previous.get("documents", [])
        if previous.get("renderer_kind") == "file_bundle":
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            for document in documents:
                destination = root / document["path"]
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                destination.write_text(document["content"], encoding="utf-8")
            return root.resolve().as_uri()
        root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        content = documents[0]["content"] if documents else ""
        root.write_text(content, encoding="utf-8")
        return root.resolve().as_uri()

    @staticmethod
    def _read_documents(uri: str, renderer_kind: str) -> list[dict[str, str]]:
        from urllib.parse import unquote, urlparse

        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return []
        path = Path(unquote(parsed.path))
        if renderer_kind == "adapter":
            return []
        candidates = [path] if path.is_file() else sorted(
            candidate for candidate in path.rglob("*") if candidate.is_file()
        )
        documents: list[dict[str, str]] = []
        for candidate in candidates[:128]:
            if candidate.stat().st_size > MAX_CAPTURE_BYTES:
                continue
            try:
                content = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            relative = candidate.name if path.is_file() else candidate.relative_to(path).as_posix()
            documents.append({
                "path": relative,
                "media_type": "text/markdown" if relative.lower().endswith(".md") else "text/plain",
                "content": content,
            })
        return documents

    def evolve(
        self,
        *,
        session_id: str,
        request: dict[str, str],
        result: dict[str, Any],
        store: DevelopmentStateStore,
        cancellation: HarnessCancellation | None = None,
    ) -> dict[str, Any]:
        if cancellation is not None:
            cancellation.raise_if_requested()
        config = store.project_config(request["project_id"])
        selected = selected_document_evolution(config)

        self.capture_session_dataset(
            session_id=session_id,
            request=request,
            result=result,
            store=store,
        )

        if not selected:
            return {"artifacts": [], "errors": []}
        prior_dataset_ids = [
            dataset["artifact_id"]
            for dataset in store.dataset_artifacts(request["project_id"])
            if dataset["session_id"] != session_id
        ]
        return self._run_selections(
            run_id=None,
            project_id=request["project_id"],
            project_name=request["project_name"],
            current_session_id=session_id,
            prior_dataset_ids=prior_dataset_ids,
            selections=selected,
            store=store,
            cancellation=cancellation,
            promote_outputs=True,
        )

    def capture_session_dataset(
        self,
        *,
        session_id: str,
        request: dict[str, str],
        result: dict[str, Any],
        store: DevelopmentStateStore,
    ) -> dict[str, Any]:
        """Seal one completed Session transcript without running Evolution."""

        dataset_dir = self._artifact_root / "datasets" / session_id
        dataset_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        records_path = dataset_dir / "records.jsonl"
        record = {
            "event_id": f"{session_id}-completed",
            "task_id": session_id,
            "session_id": session_id,
            "status": "COMPLETED",
            "reward": 1.0,
            "traces": [{
                "prompt_messages": [{"role": "user", "content": request["instruction"]}],
                "response_messages": [{"role": "assistant", "content": result["response"]}],
                "metadata": {
                    "capture_mode": "transcript",
                    "token_level_metrics_available": False,
                },
            }],
        }
        records_path.write_text(canonical_json(record) + "\n", encoding="utf-8")
        manifest_path = dataset_dir / "manifest.json"
        manifest_path.write_text(
            canonical_json({
                "dataset_id": f"dataset-{session_id}",
                "name": f"{request['task_title']} transcript",
                "records_path": records_path.name,
                "records_uri": records_path.resolve().as_uri(),
                "event_count": 1,
                "capture_mode": "transcript",
                "token_level_metrics_available": False,
            }),
            encoding="utf-8",
        )

        dataset_input: dict[str, Any] = {
            "artifact_id": f"dataset-{session_id}",
            "type": "dataset",
            "uri": manifest_path.resolve().as_uri(),
            "name": f"{request['task_title']} transcript",
        }
        store.record_dataset_artifact(
            artifact_id=dataset_input["artifact_id"],
            project_id=request["project_id"],
            session_id=session_id,
            uri=dataset_input["uri"],
            name=dataset_input["name"],
            manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        )
        return dataset_input

    def seal_completed_session_datasets(
        self,
        store: DevelopmentStateStore,
    ) -> list[str]:
        """Rebuild durable transcript datasets for completed current or legacy Sessions."""

        failures: list[str] = []
        for session in store.completed_sessions():
            try:
                self.capture_session_dataset(
                    session_id=session["session_id"],
                    request={
                        "project_id": session["project_id"],
                        "task_title": session["task_title"],
                        "instruction": session["instruction"],
                    },
                    result={"response": session["response"]},
                    store=store,
                )
            except Exception as exc:
                failures.append(f"{session['session_id']}: {exc}")
        return failures

    def evolve_run(
        self,
        *,
        run: dict[str, Any],
        store: DevelopmentStateStore,
    ) -> dict[str, Any]:
        """Build unapplied candidates from an explicit, multi-Session evidence set."""

        session_ids = list(run["source_session_ids"])
        current_session_id = session_ids[-1]
        project = store.project(run["project_id"])
        return self._run_selections(
            run_id=run["run_id"],
            project_id=run["project_id"],
            project_name=project["display_name"],
            current_session_id=current_session_id,
            prior_dataset_ids=[f"dataset-{session_id}" for session_id in session_ids[:-1]],
            selections=run["selections"],
            store=store,
            cancellation=None,
            promote_outputs=False,
        )

    def _run_selections(
        self,
        *,
        run_id: str | None,
        project_id: str,
        project_name: str,
        current_session_id: str,
        prior_dataset_ids: list[str],
        selections: list[dict[str, Any]],
        store: DevelopmentStateStore,
        cancellation: HarnessCancellation | None,
        promote_outputs: bool,
    ) -> dict[str, Any]:
        try:
            from openevo.evolution.framework.resolution import resolve_evolution_method
        except (ImportError, ModuleNotFoundError) as exc:
            raise EvolutionRunError(
                "OpenEvo Python dependencies are unavailable; run this daemon with `uv run python`"
            ) from exc

        persisted: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for item in selections:
            if cancellation is not None:
                cancellation.raise_if_requested()
            target_id = item["target_id"]
            requested_method_id = item["method"]
            method_id = resolve_evolution_method(
                target_id=target_id,
                requested_method=requested_method_id,
                prior_dataset_artifact_ids=prior_dataset_ids,
            )
            job_suffix = current_session_id if run_id is None else run_id
            job_id = f"job-{target_id.replace('_', '-')}-{job_suffix}"
            previous = store.latest_artifact(project_id, target_id)
            attempt = store.start_evolution_job(
                job_id=job_id,
                session_id=current_session_id,
                run_id=run_id,
                target_id=target_id,
                method_id=method_id,
                requested_method_id=requested_method_id,
                resolver_input_artifact_ids=prior_dataset_ids,
                previous_artifact_id=None if previous is None else previous["artifact_id"],
                config=item["config"],
            )
            try:
                job = store.get_evolution_job(job_id)
                persisted.extend(self._execute_fixed_job(
                    job=job,
                    attempt=attempt,
                    request={
                        "project_id": project_id,
                        "project_name": project_name,
                    },
                    store=store,
                    cancellation=cancellation,
                    promote_outputs=promote_outputs,
                ))
            except HarnessRunCancelled:
                raise
            except Exception as exc:
                errors.append({
                    "target_id": target_id,
                    "method": requested_method_id,
                    "message": str(exc),
                })
        return {"artifacts": persisted, "errors": errors}

    def retry(
        self,
        *,
        job: dict[str, Any],
        attempt: dict[str, Any],
        store: DevelopmentStateStore,
    ) -> list[dict[str, Any]]:
        session = store.get_session(job["session_id"])
        project = store.project(session["project_id"])
        request = {
            "project_id": project["project_id"],
            "project_name": project["display_name"],
            "task_title": session["task_title"],
            "instruction": session["instruction"],
        }
        return self._execute_fixed_job(
            job=job,
            attempt=attempt,
            request=request,
            store=store,
            cancellation=None,
        )

    def _execute_fixed_job(
        self,
        *,
        job: dict[str, Any],
        attempt: dict[str, Any],
        request: dict[str, str],
        store: DevelopmentStateStore,
        cancellation: HarnessCancellation | None,
        promote_outputs: bool | None = None,
    ) -> list[dict[str, Any]]:
        from openevo.evolution.framework.execution import (
            InputBindingSource,
            resolve_method_inputs,
        )
        from openevo.evolution.methods import METHOD_REGISTRY
        from openevo.evolution.models import WorkerClaimInputArtifact, WorkerClaimedJob

        job_id = job["job_id"]
        attempt_id = attempt["attempt_id"]
        attempt_ordinal = attempt["ordinal"]
        target_id = job["target_id"]
        method_id = job["method_id"]
        if promote_outputs is None:
            promote_outputs = job.get("run_id") is None
        stage = "input_resolution"
        try:
            if cancellation is not None:
                cancellation.raise_if_requested()
            target, method, handler = self._descriptor(target_id, method_id)
            method_config = self._method_config(method, job["config"])
            current_record = store.dataset_artifact(f"dataset-{job['session_id']}")
            current_dataset = WorkerClaimInputArtifact(
                artifact_id=current_record["artifact_id"],
                type="dataset",
                uri=current_record["uri"],
                name=current_record["name"],
            )
            prior_datasets = []
            for artifact_id in job["resolver_input_artifact_ids"]:
                record = store.dataset_artifact(artifact_id)
                prior_datasets.append(WorkerClaimInputArtifact(
                    artifact_id=record["artifact_id"],
                    type="dataset",
                    uri=record["uri"],
                    name=record["name"],
                ))
            previous = (
                None
                if job["previous_artifact_id"] is None
                else store.artifact(job["previous_artifact_id"])
            )
            previous_input = None
            if previous is not None:
                attempt_input_root = self._artifact_root / "attempt-inputs" / attempt_id
                attempt_input_root.mkdir(mode=0o700, parents=True, exist_ok=False)
                previous_uri = self._materialize_previous(
                    previous,
                    attempt_input_root / f"previous-{target_id}",
                )
                previous_input = WorkerClaimInputArtifact(
                    artifact_id=previous["artifact_id"],
                    type=target.artifact_type,
                    uri=previous_uri,
                    name=f"previous evolved {target.display_name}",
                )
            ordered_datasets = [*prior_datasets, current_dataset]
            candidates: dict[str, list[Any]] = {}
            for binding in method.input_bindings:
                if binding.source is InputBindingSource.CURRENT_DATASET:
                    candidates[binding.binding_id] = [current_dataset]
                elif binding.source is InputBindingSource.HISTORY_DATASETS:
                    candidates[binding.binding_id] = prior_datasets
                elif (
                    binding.source is InputBindingSource.EXPLICIT_INPUTS
                    and binding.artifact_type == "dataset"
                ):
                    candidates[binding.binding_id] = ordered_datasets
                elif binding.source is InputBindingSource.CURRENT_TARGET_ARTIFACTS:
                    candidates[binding.binding_id] = [] if previous_input is None else [previous_input]
                else:
                    candidates[binding.binding_id] = []
            resolved = resolve_method_inputs(method.input_bindings, candidates)
            store.update_evolution_attempt(
                attempt_id,
                stage="method_execution",
                message=f"Running {method_id} with the original fixed inputs.",
            )
            stage = "method_execution"
            method_handle = METHOD_REGISTRY.get(method_id)
            if method_handle is None:
                raise EvolutionRunError(f"{method_id} has no installed legacy worker handle")
            artifacts = method_handle(
                WorkerClaimedJob(
                    job_id=job_id,
                    lease_id=f"lease-{attempt_id}",
                    job_type="development_catalog",
                    method=method_id,
                    target_id=target_id,
                    registry_snapshot_digest=self._registry.registry_digest,
                    method_identity_digest=self._registry.identity_digest_for("method", method_id),
                    input_artifacts=list(resolved.input_artifacts),
                    config={
                        **method_config,
                        "name": f"{request['project_name']} evolved {target.display_name}",
                    },
                ),
                artifact_root=self._artifact_root,
            )
            if cancellation is not None:
                cancellation.raise_if_requested()
            stage = "output_validation"
            store.update_evolution_attempt(
                attempt_id,
                stage=stage,
                message="Validating the declared Evolution outputs.",
            )
            output_records: list[tuple[Any, str, list[dict[str, str]], str]] = []
            for output_index, artifact in enumerate(artifacts):
                artifact_type = artifact.type.value
                if artifact_type not in method.output_artifact_types:
                    raise EvolutionRunError(
                        f"{method_id} returned undeclared artifact type {artifact_type}"
                    )
                renderer_kind = (
                    "structured_summary" if artifact_type == "report"
                    else handler.renderer_kind.value
                )
                documents = self._read_documents(artifact.uri, renderer_kind)
                output_suffix = "" if output_index == 0 else f"-{output_index + 1}"
                retry_suffix = "" if attempt_ordinal == 1 else f"-attempt-{attempt_ordinal}"
                identity_suffix = (
                    job["session_id"].removeprefix("dev-session-")
                    if job.get("run_id") is None
                    else job["run_id"].removeprefix("evolution-run-")
                )
                artifact_id = (
                    f"dev-{artifact_type.replace('_', '-')}-{identity_suffix}"
                    f"{retry_suffix}{output_suffix}"
                )
                output_records.append((artifact, artifact_type, documents, artifact_id))
            stage = "artifact_persistence"
            store.update_evolution_attempt(
                attempt_id,
                stage=stage,
                message="Persisting the validated Evolution artifacts.",
            )
            persisted: list[dict[str, Any]] = []
            artifact_ids: list[str] = []
            for artifact, artifact_type, documents, artifact_id in output_records:
                artifact_ids.append(artifact_id)
                persisted.append(store.record_evolution_artifact(
                    artifact_id=artifact_id,
                    project_id=request["project_id"],
                    session_id=job["session_id"],
                    run_id=job.get("run_id"),
                    target_id=target_id,
                    artifact_type=artifact_type,
                    method_id=method_id,
                    renderer_kind=(
                        "structured_summary" if artifact_type == "report"
                        else handler.renderer_kind.value
                    ),
                    documents=documents,
                    manifest=artifact.manifest,
                    previous_artifact_id=(
                        previous["artifact_id"]
                        if previous is not None and artifact_type == target.artifact_type
                        else None
                    ),
                    promoted=bool(artifact.promoted) and promote_outputs,
                ))
            store.finish_evolution_job(
                job_id,
                attempt_id=attempt_id,
                artifact_ids=artifact_ids,
            )
            return persisted
        except HarnessRunCancelled:
            store.finish_evolution_job(
                job_id,
                attempt_id=attempt_id,
                error="Session cancelled by user",
                error_stage=stage,
                error_code="cancelled",
            )
            raise
        except Exception as exc:
            store.finish_evolution_job(
                job_id,
                attempt_id=attempt_id,
                error=str(exc),
                error_stage=stage,
                error_code=f"{stage}_failed",
            )
            raise


# Kept as a source-compatible name for development tests and scripts written before document
# evolution was expanded beyond text memory.
TextMemoryEvolutionRunner = DocumentEvolutionRunner


class DevelopmentSessionCoordinator:
    """Own asynchronous development Session execution independently of HTTP request threads."""

    def __init__(
        self,
        *,
        runner: CodexRunner,
        store: DevelopmentStateStore,
        evolution_runner: DocumentEvolutionRunner | None,
    ) -> None:
        self._runner = runner
        self._store = store
        self._evolution_runner = evolution_runner
        self._turn_lock = threading.Lock()
        self._executions_lock = threading.Lock()
        self._executions: dict[str, HarnessCancellation] = {}

    def submit(self, request: dict[str, str]) -> str:
        if not self._turn_lock.acquire(blocking=False):
            raise StateConflictError("another development session is running")
        session_id = f"dev-session-{secrets.token_hex(8)}"
        try:
            self._store.start_session(session_id, request)
        except Exception:
            self._turn_lock.release()
            raise
        cancellation = HarnessCancellation()
        with self._executions_lock:
            self._executions[session_id] = cancellation
        thread = threading.Thread(
            target=self._execute,
            name=f"openevo-{session_id}",
            args=(session_id, request, cancellation),
            daemon=True,
        )
        thread.start()
        return session_id

    def cancel(self, session_id: str) -> dict[str, Any]:
        session = self._store.request_session_cancellation(session_id)
        with self._executions_lock:
            cancellation = self._executions.get(session_id)
        if cancellation is not None:
            cancellation.cancel()
        return session

    def retry_evolution(self, job_id: str, *, action_id: str | None = None) -> dict[str, Any]:
        if self._evolution_runner is None:
            raise StateConflictError("the Evolution runner is unavailable")
        if action_id is not None:
            existing = self._store.evolution_retry_for_action(job_id, action_id)
            if existing is not None:
                return existing
        if not self._turn_lock.acquire(blocking=False):
            if action_id is not None:
                existing = self._store.evolution_retry_for_action(job_id, action_id)
                if existing is not None:
                    return existing
            raise StateConflictError("another development session or Evolution retry is running")
        try:
            effective_action_id = action_id or f"legacy-retry-{secrets.token_hex(16)}"
            job, attempt, created = self._store.start_evolution_retry_v2(
                job_id,
                effective_action_id,
            )
            if not created:
                self._turn_lock.release()
                return job
            self._store.set_evolution_error(
                job["session_id"],
                target_id=job["target_id"],
                method=job["requested_method_id"],
                message=None,
            )
        except Exception:
            self._turn_lock.release()
            raise
        thread = threading.Thread(
            target=self._execute_evolution_retry,
            name=f"openevo-retry-{attempt['attempt_id']}",
            args=(job, attempt),
            daemon=True,
        )
        thread.start()
        return self._store.get_evolution_job(job_id)

    def submit_evolution(self, request: dict[str, Any]) -> dict[str, Any]:
        if self._evolution_runner is None:
            raise StateConflictError("the Evolution runner is unavailable")
        if not self._turn_lock.acquire(blocking=False):
            existing = self._store.evolution_run_for_action(request)
            if existing is not None:
                return existing
            raise StateConflictError("another development Session or Evolution Run is active")
        run_id = f"evolution-run-{secrets.token_hex(8)}"
        try:
            run = self._store.start_evolution_run(run_id, request)
        except Exception:
            self._turn_lock.release()
            raise
        if run["run_id"] != run_id:
            self._turn_lock.release()
            return run
        thread = threading.Thread(
            target=self._execute_evolution_run,
            name=f"openevo-{run_id}",
            args=(run,),
            daemon=True,
        )
        thread.start()
        return run

    def apply_evolution(self, run_id: str) -> dict[str, Any]:
        if not self._turn_lock.acquire(blocking=False):
            raise StateConflictError("another development Session or Evolution Run is active")
        try:
            return self._store.apply_evolution_run(run_id)
        finally:
            self._turn_lock.release()

    def upload_workspace_file_v2(
        self,
        project_id: str,
        relative_path: object,
        payload: bytes,
        *,
        overwrite: bool,
    ) -> DevelopmentWorkspaceMutationV2:
        if not self._turn_lock.acquire(blocking=False):
            raise StateConflictError(
                "workspace uploads are unavailable while a Session or Evolution retry is running"
            )
        try:
            entry = self._store.upload_workspace_file(
                project_id, relative_path, payload, overwrite=overwrite
            )
            return self._store.workspace_mutation_v2(project_id, entry["path"])
        finally:
            self._turn_lock.release()

    def delete_workspace_file(self, project_id: str, relative_path: object) -> str:
        if not self._turn_lock.acquire(blocking=False):
            raise StateConflictError(
                "workspace deletes are unavailable while a Session or Evolution retry is running"
            )
        try:
            return self._store.delete_workspace_file(project_id, relative_path)
        finally:
            self._turn_lock.release()

    def delete_workspace_file_v2(
        self,
        project_id: str,
        relative_path: object,
    ) -> DevelopmentWorkspaceDeleteV2:
        if not self._turn_lock.acquire(blocking=False):
            raise StateConflictError(
                "workspace deletes are unavailable while a Session or Evolution retry is running"
            )
        try:
            deleted_path = self._store.delete_workspace_file(project_id, relative_path)
            return self._store.workspace_delete_v2(project_id, deleted_path)
        finally:
            self._turn_lock.release()

    def upload_workspace_file(
        self,
        project_id: str,
        relative_path: object,
        payload: bytes,
        *,
        overwrite: bool,
    ) -> dict[str, Any]:
        if not self._turn_lock.acquire(blocking=False):
            raise StateConflictError(
                "workspace uploads are unavailable while a Session or Evolution retry is running"
            )
        try:
            return self._store.upload_workspace_file(
                project_id,
                relative_path,
                payload,
                overwrite=overwrite,
            )
        finally:
            self._turn_lock.release()

    def _execute_evolution_retry(
        self,
        job: dict[str, Any],
        attempt: dict[str, Any],
    ) -> None:
        try:
            artifacts = self._evolution_runner.retry(
                job=job,
                attempt=attempt,
                store=self._store,
            )
            if job.get("run_id") is not None:
                self._store.reconcile_evolution_run(job["run_id"])
            self._store.set_evolution_error(
                job["session_id"],
                target_id=job["target_id"],
                method=job["requested_method_id"],
                message=None,
            )
            self._store.append_session_log(
                job["session_id"],
                f"Evolution retry attempt {attempt['ordinal']} completed and published "
                f"{len(artifacts)} artifact(s).",
            )
        except Exception as exc:
            self._store.set_evolution_error(
                job["session_id"],
                target_id=job["target_id"],
                method=job["requested_method_id"],
                message=str(exc),
            )
            self._store.append_session_log(
                job["session_id"],
                f"Evolution retry attempt {attempt['ordinal']} failed: {exc}",
            )
            if job.get("run_id") is not None:
                self._store.reconcile_evolution_run(job["run_id"])
        finally:
            self._turn_lock.release()

    def _execute_evolution_run(self, run: dict[str, Any]) -> None:
        try:
            result = self._evolution_runner.evolve_run(run=run, store=self._store)
            errors = result["errors"]
            self._store.finish_evolution_run(
                run["run_id"],
                artifact_ids=[artifact["artifact_id"] for artifact in result["artifacts"]],
                error=(
                    None
                    if not errors
                    else "; ".join(
                        f"{error['target_id']}: {error['message']}" for error in errors
                    )
                ),
            )
        except Exception as exc:
            try:
                self._store.finish_evolution_run(
                    run["run_id"],
                    artifact_ids=[],
                    error=str(exc),
                )
            except Exception:
                pass
        finally:
            self._turn_lock.release()

    def _execute(
        self,
        session_id: str,
        request: dict[str, str],
        cancellation: HarnessCancellation,
    ) -> None:
        workspace_before: dict[str, Any] = {
            "project_id": request["project_id"],
            "entries": [],
            "truncated": False,
        }
        try:
            workspace_path = self._store.workspace_path(request["project_id"])
            workspace_before = self._store.workspace_snapshot(request["project_id"])
            execution_request = {
                **request,
                "workspace_path": workspace_path,
                "workspace_snapshot": workspace_before,
                "evolved_contexts": self._store.latest_context_artifacts(
                    request["project_id"]
                ),
            }
            result = self._runner.run(
                execution_request,
                cancellation=cancellation,
                log=lambda message: self._store.append_session_log(session_id, message),
            )
            cancellation.raise_if_requested()
            mutations = result.pop(
                "file_mutations",
                {"file_writes": [], "delete_paths": []},
            )
            self._store.apply_workspace_mutations(request["project_id"], mutations)
            workspace_after = self._store.workspace_snapshot(request["project_id"])
            result = {
                **result,
                "session_id": session_id,
                "workspace_changes": ProjectWorkspaceStore.changes(
                    workspace_before, workspace_after
                ),
                "workspace": workspace_after,
            }
            cancellation.raise_if_requested()
            self._store.complete_session(session_id, result)
            if self._evolution_runner is not None:
                try:
                    self._evolution_runner.capture_session_dataset(
                        session_id=session_id,
                        request=request,
                        result=result,
                        store=self._store,
                    )
                    self._store.append_session_log(
                        session_id,
                        "Session transcript sealed as reusable Evolution evidence.",
                    )
                except Exception as exc:
                    self._store.append_session_log(
                        session_id,
                        f"Session completed, but Evolution evidence sealing failed: {exc}",
                    )
        except HarnessRunCancelled:
            workspace_after = self._store.workspace_snapshot(request["project_id"])
            self._store.cancel_session(
                session_id,
                ProjectWorkspaceStore.changes(workspace_before, workspace_after),
            )
        except (AgentRunError, HarnessRunError) as exc:
            workspace_after = self._store.workspace_snapshot(request["project_id"])
            self._store.fail_session(
                session_id,
                str(exc),
                ProjectWorkspaceStore.changes(workspace_before, workspace_after),
            )
        except Exception as exc:
            try:
                workspace_after = self._store.workspace_snapshot(request["project_id"])
                self._store.fail_session(
                    session_id,
                    f"unexpected development session failure: {exc}",
                    ProjectWorkspaceStore.changes(workspace_before, workspace_after),
                )
            except Exception:
                pass
        finally:
            with self._executions_lock:
                self._executions.pop(session_id, None)
            self._turn_lock.release()


class DevelopmentAgentServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        token: str,
        runner: CodexRunner,
        store: DevelopmentStateStore,
        evolution_runner: DocumentEvolutionRunner | None = None,
    ) -> None:
        super().__init__(address, DevelopmentAgentHandler)
        self.token = token
        self.runner = runner
        self.store = store
        self.evolution_runner = evolution_runner
        self.sessions = DevelopmentSessionCoordinator(
            runner=runner,
            store=store,
            evolution_runner=evolution_runner,
        )


class DevelopmentAgentHandler(BaseHTTPRequestHandler):
    server: DevelopmentAgentServer
    server_version = "OpenEvoDevelopmentAgent/1"

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        parsed_path = urlsplit(self.path)
        if parsed_path.path == DAEMON_V2_TASKS_PATH:
            try:
                parameters = parse_qs(
                    parsed_path.query, keep_blank_values=True, strict_parsing=True
                )
                if set(parameters) - {"project_id", "after", "limit"} or any(
                    len(values) != 1 for values in parameters.values()
                ):
                    raise RequestError("task query contains unsupported parameters")
                project_id = parameters.get("project_id", [None])[0]
                if project_id is not None and not ID_PATTERN.fullmatch(project_id):
                    raise RequestError("project_id is invalid")
                after_task_id = parameters.get("after", [None])[0]
                if after_task_id is not None and not ID_PATTERN.fullmatch(after_task_id):
                    raise RequestError("task cursor is invalid")
                limit_raw = parameters.get(
                    "limit", [str(MAX_DAEMON_V2_TASK_PAGE)]
                )[0]
                if not limit_raw.isascii() or not limit_raw.isdigit():
                    raise RequestError("task limit must be an integer")
                limit = int(limit_raw)
                if not 1 <= limit <= MAX_DAEMON_V2_TASK_PAGE:
                    raise RequestError("task limit is outside the supported bound")
                page = self.server.store.task_observations_v2(
                    project_id=project_id,
                    after_task_id=after_task_id,
                    limit=limit,
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            else:
                self._json(HTTPStatus.OK, page.model_dump(mode="json"))
            return
        task_logs_match = DAEMON_V2_TASK_LOGS_PATH_PATTERN.fullmatch(parsed_path.path)
        if task_logs_match:
            task_id = task_logs_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(task_id):
                    raise RequestError("task_id is invalid")
                parameters = parse_qs(
                    parsed_path.query, keep_blank_values=True, strict_parsing=True
                )
                if set(parameters) - {"after", "limit"} or any(
                    len(values) != 1 for values in parameters.values()
                ):
                    raise RequestError("log query contains unsupported parameters")
                after_raw = parameters.get("after", ["0"])[0]
                limit_raw = parameters.get("limit", [str(MAX_DAEMON_V2_LOG_PAGE)])[0]
                if not after_raw.isascii() or not after_raw.isdigit():
                    raise RequestError("log cursor must be a non-negative integer")
                if not limit_raw.isascii() or not limit_raw.isdigit():
                    raise RequestError("log limit must be an integer")
                after_sequence = int(after_raw)
                limit = int(limit_raw)
                if not 1 <= limit <= MAX_DAEMON_V2_LOG_PAGE:
                    raise RequestError("log limit is outside the supported bound")
                page = self.server.store.task_logs_v2(
                    task_id, after_sequence=after_sequence, limit=limit
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "task not found")
            else:
                self._json(HTTPStatus.OK, page.model_dump(mode="json"))
            return
        task_timeline_match = DAEMON_V2_TASK_TIMELINE_PATH_PATTERN.fullmatch(
            parsed_path.path
        )
        if task_timeline_match:
            task_id = task_timeline_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(task_id):
                    raise RequestError("task_id is invalid")
                parameters = parse_qs(
                    parsed_path.query, keep_blank_values=True, strict_parsing=True
                )
                if set(parameters) - {"after", "limit"} or any(
                    len(values) != 1 for values in parameters.values()
                ):
                    raise RequestError("timeline query contains unsupported parameters")
                after_raw = parameters.get("after", ["0"])[0]
                limit_raw = parameters.get(
                    "limit", [str(MAX_DAEMON_V2_LOG_PAGE)]
                )[0]
                if not after_raw.isascii() or not after_raw.isdigit():
                    raise RequestError("timeline cursor must be a non-negative integer")
                if not limit_raw.isascii() or not limit_raw.isdigit():
                    raise RequestError("timeline limit must be an integer")
                after_sequence = int(after_raw)
                limit = int(limit_raw)
                if not 1 <= limit <= MAX_DAEMON_V2_LOG_PAGE:
                    raise RequestError("timeline limit is outside the supported bound")
                page = self.server.store.task_timeline_v2(
                    task_id, after_sequence=after_sequence, limit=limit
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "task not found")
            else:
                self._json(HTTPStatus.OK, page.model_dump(mode="json"))
            return
        task_artifacts_match = DAEMON_V2_TASK_ARTIFACTS_PATH_PATTERN.fullmatch(
            parsed_path.path
        )
        if task_artifacts_match:
            task_id = task_artifacts_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(task_id):
                    raise RequestError("task_id is invalid")
                _, _, after_artifact_id, limit = self._artifact_page_query(
                    parsed_path.query,
                    allow_filters=False,
                    maximum_limit=MAX_DAEMON_V2_ARTIFACT_PAGE,
                )
                page = self.server.store.artifact_page_v2(
                    project_id=None,
                    task_id=task_id,
                    after_artifact_id=after_artifact_id,
                    limit=limit,
                    development_detail=False,
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "task not found")
            else:
                self._json(HTTPStatus.OK, page.model_dump(mode="json"))
            return
        artifact_content_match = DAEMON_V2_ARTIFACT_CONTENT_PATH_PATTERN.fullmatch(
            parsed_path.path
        )
        if artifact_content_match:
            artifact_id = artifact_content_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(artifact_id):
                    raise RequestError("artifact_id is invalid")
                artifact = self.server.store.artifact_content_observation_v2(artifact_id)
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "artifact not found")
            else:
                self._json(HTTPStatus.OK, artifact.model_dump(mode="json"))
            return
        artifact_match = DAEMON_V2_ARTIFACT_PATH_PATTERN.fullmatch(parsed_path.path)
        if artifact_match:
            artifact_id = artifact_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(artifact_id):
                    raise RequestError("artifact_id is invalid")
                artifact = self.server.store.artifact_observation_v2(artifact_id)
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "artifact not found")
            else:
                self._json(HTTPStatus.OK, artifact.model_dump(mode="json"))
            return
        if parsed_path.path == DAEMON_V2_DEVELOPMENT_ARTIFACTS_PATH:
            try:
                project_id, task_id, after_artifact_id, limit = self._artifact_page_query(
                    parsed_path.query,
                    allow_filters=True,
                    maximum_limit=MAX_DAEMON_V2_DEVELOPMENT_ARTIFACT_PAGE,
                )
                if project_id is None:
                    raise RequestError("development artifact query requires project_id")
                page = self.server.store.artifact_page_v2(
                    project_id=project_id,
                    task_id=task_id,
                    after_artifact_id=after_artifact_id,
                    limit=limit,
                    development_detail=True,
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "project or task not found")
            else:
                self._json(HTTPStatus.OK, page.model_dump(mode="json"))
            return
        development_artifact_match = (
            DAEMON_V2_DEVELOPMENT_ARTIFACT_PATH_PATTERN.fullmatch(parsed_path.path)
        )
        if development_artifact_match:
            artifact_id = development_artifact_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(artifact_id):
                    raise RequestError("artifact_id is invalid")
                artifact = self.server.store.development_artifact_v2(artifact_id)
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "artifact not found")
            else:
                self._json(HTTPStatus.OK, artifact.model_dump(mode="json"))
            return
        if parsed_path.path == DAEMON_V2_DEVELOPMENT_EVOLUTION_RUNS_PATH:
            try:
                parameters = parse_qs(
                    parsed_path.query, keep_blank_values=True, strict_parsing=True
                )
                if set(parameters) - {"project_id", "after", "limit"} or any(
                    len(values) != 1 for values in parameters.values()
                ):
                    raise RequestError(
                        "Evolution Run query contains unsupported parameters"
                    )
                project_id = parameters.get("project_id", [None])[0]
                if project_id is None or not ID_PATTERN.fullmatch(project_id):
                    raise RequestError("project_id is invalid")
                after_run_id = parameters.get("after", [None])[0]
                if after_run_id is not None and not ID_PATTERN.fullmatch(after_run_id):
                    raise RequestError("Evolution Run cursor is invalid")
                limit_raw = parameters.get(
                    "limit", [str(MAX_DAEMON_V2_EVOLUTION_RUN_PAGE)]
                )[0]
                if not limit_raw.isascii() or not limit_raw.isdigit():
                    raise RequestError("Evolution Run limit must be an integer")
                limit = int(limit_raw)
                if not 1 <= limit <= MAX_DAEMON_V2_EVOLUTION_RUN_PAGE:
                    raise RequestError(
                        "Evolution Run limit is outside the supported bound"
                    )
                page = self.server.store.evolution_run_page_v2(
                    project_id=project_id,
                    after_run_id=after_run_id,
                    limit=limit,
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "project not found")
            else:
                self._json(HTTPStatus.OK, page.model_dump(mode="json"))
            return
        if parsed_path.path == DAEMON_V2_DEVELOPMENT_EVOLUTION_JOBS_PATH:
            try:
                parameters = parse_qs(
                    parsed_path.query, keep_blank_values=True, strict_parsing=True
                )
                if set(parameters) - {"project_id", "after", "limit"} or any(
                    len(values) != 1 for values in parameters.values()
                ):
                    raise RequestError(
                        "Evolution Job query contains unsupported parameters"
                    )
                project_id = parameters.get("project_id", [None])[0]
                if project_id is None or not ID_PATTERN.fullmatch(project_id):
                    raise RequestError("project_id is invalid")
                after_job_id = parameters.get("after", [None])[0]
                if after_job_id is not None and not ID_PATTERN.fullmatch(after_job_id):
                    raise RequestError("Evolution Job cursor is invalid")
                limit_raw = parameters.get(
                    "limit", [str(MAX_DAEMON_V2_EVOLUTION_JOB_PAGE)]
                )[0]
                if not limit_raw.isascii() or not limit_raw.isdigit():
                    raise RequestError("Evolution Job limit must be an integer")
                limit = int(limit_raw)
                if not 1 <= limit <= MAX_DAEMON_V2_EVOLUTION_JOB_PAGE:
                    raise RequestError(
                        "Evolution Job limit is outside the supported bound"
                    )
                page = self.server.store.evolution_job_page_v2(
                    project_id=project_id,
                    after_job_id=after_job_id,
                    limit=limit,
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "project not found")
            else:
                self._json(HTTPStatus.OK, page.model_dump(mode="json"))
            return
        evolution_job_match = (
            DAEMON_V2_DEVELOPMENT_EVOLUTION_JOB_PATH_PATTERN.fullmatch(
                parsed_path.path
            )
        )
        if evolution_job_match:
            job_id = evolution_job_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(job_id):
                    raise RequestError("job_id is invalid")
                job = self.server.store.evolution_job_v2(job_id)
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(
                    HTTPStatus.NOT_FOUND, "not_found", "Evolution Job not found"
                )
            else:
                self._json(HTTPStatus.OK, job.model_dump(mode="json"))
            return
        evolution_run_match = (
            DAEMON_V2_DEVELOPMENT_EVOLUTION_RUN_PATH_PATTERN.fullmatch(parsed_path.path)
        )
        if evolution_run_match:
            run_id = evolution_run_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(run_id):
                    raise RequestError("run_id is invalid")
                run = self.server.store.evolution_run_v2(run_id)
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(
                    HTTPStatus.NOT_FOUND, "not_found", "Evolution Run not found"
                )
            else:
                self._json(HTTPStatus.OK, run.model_dump(mode="json"))
            return
        task_match = DAEMON_V2_TASK_PATH_PATTERN.fullmatch(parsed_path.path)
        if task_match:
            task_id = task_match.group(1)
            if not ID_PATTERN.fullmatch(task_id):
                self._json_error_v2(
                    HTTPStatus.BAD_REQUEST, "invalid_request", "task_id is invalid"
                )
                return
            try:
                task = self.server.store.task_observation_v2(task_id)
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "task not found")
            else:
                self._json(HTTPStatus.OK, task.model_dump(mode="json"))
            return
        workspace_page_match = DAEMON_V2_WORKSPACE_PATH_PATTERN.fullmatch(
            parsed_path.path
        )
        if workspace_page_match:
            project_id = workspace_page_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(project_id):
                    raise RequestError("project_id is invalid")
                parameters = parse_qs(
                    parsed_path.query, keep_blank_values=True, strict_parsing=True
                )
                if set(parameters) - {"after", "limit", "manifest_sha256"} or any(
                    len(values) != 1 for values in parameters.values()
                ):
                    raise RequestError("workspace query contains unsupported parameters")
                after_path = parameters.get("after", [None])[0]
                if after_path == "":
                    raise RequestError("workspace cursor cannot be empty")
                manifest_sha256 = parameters.get("manifest_sha256", [None])[0]
                if manifest_sha256 is not None and not re.fullmatch(
                    r"[0-9a-f]{64}", manifest_sha256
                ):
                    raise RequestError("workspace manifest digest is invalid")
                limit_raw = parameters.get(
                    "limit", [str(MAX_DAEMON_V2_WORKSPACE_PAGE)]
                )[0]
                if not limit_raw.isascii() or not limit_raw.isdigit():
                    raise RequestError("workspace limit must be an integer")
                limit = int(limit_raw)
                if not 1 <= limit <= MAX_DAEMON_V2_WORKSPACE_PAGE:
                    raise RequestError("workspace limit is outside the supported bound")
                page = self.server.store.workspace_page_v2(
                    project_id,
                    after_path=after_path,
                    expected_manifest_sha256=manifest_sha256,
                    limit=limit,
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "project not found")
            except StateConflictError as exc:
                self._json_error_v2(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.OK, page.model_dump(mode="json"))
            return
        workspace_file_v2_match = DAEMON_V2_WORKSPACE_FILES_PATH_PATTERN.fullmatch(
            parsed_path.path
        )
        if workspace_file_v2_match:
            project_id = workspace_file_v2_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(project_id):
                    raise RequestError("project_id is invalid")
                relative_path, _ = self._workspace_query(
                    parsed_path.query, allow_overwrite=False
                )
                payload, media_type, file_name = self.server.store.download_workspace_file(
                    project_id, relative_path
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(
                    HTTPStatus.NOT_FOUND, "not_found", "workspace file not found"
                )
            else:
                self._binary(
                    payload,
                    media_type,
                    file_name,
                    content_sha256=hashlib.sha256(payload).hexdigest(),
                )
            return
        workspace_match = WORKSPACE_FILES_PATH_PATTERN.fullmatch(parsed_path.path)
        if workspace_match:
            project_id = workspace_match.group(1)
            if not ID_PATTERN.fullmatch(project_id):
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", "project_id is invalid")
                return
            try:
                relative_path, _ = self._workspace_query(parsed_path.query, allow_overwrite=False)
                payload, media_type, file_name = self.server.store.download_workspace_file(
                    project_id,
                    relative_path,
                )
            except RequestError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "workspace file not found")
            else:
                self._binary(payload, media_type, file_name)
            return
        if self.path == "/openevo-dev-agent/health":
            self._json(HTTPStatus.OK, {"schema_version": "1", "status": "ready"})
            return
        if self.path == "/openevo-dev-agent/v1/state":
            self._json(HTTPStatus.OK, self.server.store.snapshot())
            return
        if parsed_path.path == DEVELOPMENT_EVENTS_PATH:
            try:
                parameters = parse_qs(
                    parsed_path.query,
                    keep_blank_values=True,
                    strict_parsing=True,
                )
                if set(parameters) - {"after", "limit", "wait_ms"}:
                    raise RequestError("event query contains unknown parameters")
                if any(len(values) != 1 for values in parameters.values()):
                    raise RequestError("event query parameters must be singular")
                after_raw = parameters.get("after", [None])[0]
                if after_raw is None:
                    after_sequence = None
                elif not after_raw.isascii() or not after_raw.isdigit():
                    raise RequestError("event cursor must be a non-negative integer")
                else:
                    after_sequence = int(after_raw)
                limit_raw = parameters.get("limit", [str(MAX_DEVELOPMENT_EVENT_PAGE)])[0]
                wait_raw = parameters.get("wait_ms", ["0"])[0]
                if not limit_raw.isascii() or not limit_raw.isdigit():
                    raise RequestError("event limit must be an integer")
                if not wait_raw.isascii() or not wait_raw.isdigit():
                    raise RequestError("event wait_ms must be an integer")
                limit = int(limit_raw)
                wait_ms = int(wait_raw)
                if not 1 <= limit <= MAX_DEVELOPMENT_EVENT_PAGE:
                    raise RequestError("event limit is outside the supported bound")
                if not 0 <= wait_ms <= int(MAX_DEVELOPMENT_EVENT_WAIT_SECONDS * 1000):
                    raise RequestError("event wait_ms is outside the supported bound")
                result = self.server.store.read_events(
                    after_sequence=after_sequence,
                    limit=limit,
                    wait_seconds=wait_ms / 1000,
                )
            except (RequestError, ValueError) as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except EventCursorExpiredError as exc:
                self._json_error(HTTPStatus.GONE, "event_cursor_expired", str(exc))
            else:
                self._json(HTTPStatus.OK, result)
            return
        if self.path == "/openevo-dev-agent/v1/capabilities":
            if self.server.evolution_runner is None:
                self._json_error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "capabilities_unavailable",
                    "development evolution runner is unavailable",
                )
            else:
                self._json(HTTPStatus.OK, self.server.evolution_runner.capabilities())
            return
        if self.path == "/openevo-dev-agent/v1/runtime-capabilities":
            self._json(HTTPStatus.OK, self.server.runner.runtime_capabilities())
            return
        session_match = SESSION_PATH_PATTERN.fullmatch(self.path)
        if session_match:
            session_id = session_match.group(1)
            if not ID_PATTERN.fullmatch(session_id):
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", "session_id is invalid")
                return
            try:
                session = self.server.store.get_session(session_id)
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "session not found")
            else:
                self._json(HTTPStatus.OK, {"schema_version": "1", "session": session})
            return
        self._json_error(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found")

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        if self.path == DAEMON_V2_DEVELOPMENT_EVOLUTION_RUNS_PATH:
            try:
                request = DevelopmentEvolutionRunCreateV2.model_validate(
                    self._read_json()
                )
                run = self.server.sessions.submit_evolution({
                    "action_id": request.action_id,
                    "project_id": request.project_id,
                    "session_ids": request.source_task_ids,
                    "selections": [
                        {
                            "target_id": selection.target_id,
                            "method": selection.method,
                            "config": selection.config,
                        }
                        for selection in request.selections
                    ],
                })
                response = self.server.store.evolution_run_v2(run["run_id"])
            except ValidationError as exc:
                self._json_error_v2(
                    HTTPStatus.BAD_REQUEST, "invalid_request", str(exc)
                )
            except RequestError as exc:
                self._json_error_v2(
                    HTTPStatus.BAD_REQUEST, "invalid_request", str(exc)
                )
            except KeyError:
                self._json_error_v2(
                    HTTPStatus.NOT_FOUND, "not_found", "project not found"
                )
            except StateConflictError as exc:
                self._json_error_v2(
                    HTTPStatus.CONFLICT, "state_conflict", str(exc)
                )
            else:
                self._json(HTTPStatus.ACCEPTED, response.model_dump(mode="json"))
            return
        retry_job_v2_match = (
            DAEMON_V2_DEVELOPMENT_EVOLUTION_JOB_RETRY_PATH_PATTERN.fullmatch(
                self.path
            )
        )
        if retry_job_v2_match:
            job_id = retry_job_v2_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(job_id):
                    raise RequestError("job_id is invalid")
                request = DevelopmentEvolutionJobRetryV2.model_validate(
                    self._read_json()
                )
                self.server.sessions.retry_evolution(
                    job_id,
                    action_id=request.action_id,
                )
                response = self.server.store.evolution_job_v2(job_id)
            except ValidationError as exc:
                self._json_error_v2(
                    HTTPStatus.BAD_REQUEST, "invalid_request", str(exc)
                )
            except RequestError as exc:
                self._json_error_v2(
                    HTTPStatus.BAD_REQUEST, "invalid_request", str(exc)
                )
            except KeyError:
                self._json_error_v2(
                    HTTPStatus.NOT_FOUND, "not_found", "Evolution Job not found"
                )
            except StateConflictError as exc:
                self._json_error_v2(
                    HTTPStatus.CONFLICT, "state_conflict", str(exc)
                )
            else:
                self._json(HTTPStatus.ACCEPTED, response.model_dump(mode="json"))
            return
        apply_v2_match = (
            DAEMON_V2_DEVELOPMENT_EVOLUTION_RUN_APPLY_PATH_PATTERN.fullmatch(
                self.path
            )
        )
        if apply_v2_match:
            run_id = apply_v2_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(run_id):
                    raise RequestError("run_id is invalid")
                DevelopmentEvolutionRunApplyV2.model_validate(self._read_json())
                self.server.sessions.apply_evolution(run_id)
                response = self.server.store.evolution_run_v2(run_id)
            except ValidationError as exc:
                self._json_error_v2(
                    HTTPStatus.BAD_REQUEST, "invalid_request", str(exc)
                )
            except RequestError as exc:
                self._json_error_v2(
                    HTTPStatus.BAD_REQUEST, "invalid_request", str(exc)
                )
            except KeyError:
                self._json_error_v2(
                    HTTPStatus.NOT_FOUND, "not_found", "Evolution Run not found"
                )
            except StateConflictError as exc:
                self._json_error_v2(
                    HTTPStatus.CONFLICT, "state_conflict", str(exc)
                )
            else:
                self._json(HTTPStatus.OK, response.model_dump(mode="json"))
            return
        if self.path == "/openevo-dev-agent/v1/projects":
            try:
                project = self.server.store.create_project(
                    validate_project_request(self._read_json())
                )
            except RequestError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except StateConflictError as exc:
                self._json_error(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.CREATED, {"schema_version": "1", **project})
            return
        activate_match = ACTIVATE_PATH_PATTERN.fullmatch(self.path)
        if activate_match:
            project_id = activate_match.group(1)
            if not ID_PATTERN.fullmatch(project_id):
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", "project_id is invalid")
                return
            try:
                payload = self._read_json()
                if payload != {"schema_version": "1"}:
                    raise RequestError("activation request must contain only schema_version '1'")
                self.server.store.activate_project(project_id)
            except RequestError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "project not found")
            else:
                self._json(HTTPStatus.OK, {"schema_version": "1", "project_id": project_id})
            return
        retry_match = EVOLUTION_JOB_RETRY_PATH_PATTERN.fullmatch(self.path)
        if retry_match:
            job_id = retry_match.group(1)
            if not ID_PATTERN.fullmatch(job_id):
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", "job_id is invalid")
                return
            try:
                payload = self._read_json()
                if payload != {"schema_version": "1"}:
                    raise RequestError("retry request must contain only schema_version '1'")
                job = self.server.sessions.retry_evolution(job_id)
            except RequestError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "Evolution Job not found")
            except StateConflictError as exc:
                self._json_error(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.ACCEPTED, {"schema_version": "1", "job": job})
            return
        if self.path == "/openevo-dev-agent/v1/evolution-runs":
            try:
                run = self.server.sessions.submit_evolution(
                    validate_evolution_run_request(self._read_json())
                )
            except RequestError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "Project not found")
            except StateConflictError as exc:
                self._json_error(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.ACCEPTED, {"schema_version": "1", "run": run})
            return
        apply_match = EVOLUTION_RUN_APPLY_PATH_PATTERN.fullmatch(self.path)
        if apply_match:
            run_id = apply_match.group(1)
            if not ID_PATTERN.fullmatch(run_id):
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", "run_id is invalid")
                return
            try:
                payload = self._read_json()
                if payload != {"schema_version": "1"}:
                    raise RequestError("apply request must contain only schema_version '1'")
                run = self.server.sessions.apply_evolution(run_id)
            except RequestError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "Evolution Run not found")
            except StateConflictError as exc:
                self._json_error(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.OK, {"schema_version": "1", "run": run})
            return
        cancel_match = SESSION_CANCEL_PATH_PATTERN.fullmatch(self.path)
        if cancel_match:
            session_id = cancel_match.group(1)
            if not ID_PATTERN.fullmatch(session_id):
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", "session_id is invalid")
                return
            try:
                payload = self._read_json()
                if payload != {"schema_version": "1"}:
                    raise RequestError("cancellation request must contain only schema_version '1'")
                session = self.server.sessions.cancel(session_id)
            except RequestError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "session not found")
            except StateConflictError as exc:
                self._json_error(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.ACCEPTED, {"schema_version": "1", "session": session})
            return
        if self.path != "/openevo-dev-agent/v1/sessions":
            self._json_error(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found")
            return
        self._run_session()

    def do_PUT(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        parsed_path = urlsplit(self.path)
        workspace_v2_match = DAEMON_V2_WORKSPACE_FILES_PATH_PATTERN.fullmatch(
            parsed_path.path
        )
        if workspace_v2_match:
            project_id = workspace_v2_match.group(1)
            try:
                if not ID_PATTERN.fullmatch(project_id):
                    raise RequestError("project_id is invalid")
                relative_path, overwrite = self._workspace_query(
                    parsed_path.query, allow_overwrite=True
                )
                expected_digest = self.headers.get("X-OpenEvo-Content-SHA256", "")
                if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
                    raise RequestError("X-OpenEvo-Content-SHA256 is required")
                payload = self._read_bytes(MAX_WORKSPACE_UPLOAD_FILE_BYTES)
                if not hmac.compare_digest(
                    hashlib.sha256(payload).hexdigest(), expected_digest
                ):
                    raise RequestError("uploaded workspace file digest does not match")
                mutation = self.server.sessions.upload_workspace_file_v2(
                    project_id, relative_path, payload, overwrite=overwrite
                )
            except RequestError as exc:
                self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "project not found")
            except StateConflictError as exc:
                self._json_error_v2(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(HTTPStatus.CREATED, mutation.model_dump(mode="json"))
            return
        workspace_match = WORKSPACE_FILES_PATH_PATTERN.fullmatch(parsed_path.path)
        if workspace_match:
            project_id = workspace_match.group(1)
            if not ID_PATTERN.fullmatch(project_id):
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", "project_id is invalid")
                return
            try:
                relative_path, overwrite = self._workspace_query(
                    parsed_path.query,
                    allow_overwrite=True,
                )
                entry = self.server.sessions.upload_workspace_file(
                    project_id,
                    relative_path,
                    self._read_bytes(MAX_WORKSPACE_UPLOAD_FILE_BYTES),
                    overwrite=overwrite,
                )
            except RequestError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND, "not_found", "project not found")
            except StateConflictError as exc:
                self._json_error(HTTPStatus.CONFLICT, "state_conflict", str(exc))
            else:
                self._json(
                    HTTPStatus.CREATED,
                    {"schema_version": "1", "project_id": project_id, "entry": entry},
                )
            return
        project_match = PROJECT_PATH_PATTERN.fullmatch(parsed_path.path)
        if not project_match:
            self._json_error(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found")
            return
        project_id = project_match.group(1)
        if not ID_PATTERN.fullmatch(project_id):
            self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", "project_id is invalid")
            return
        try:
            project = self.server.store.update_project(
                project_id,
                validate_project_request(self._read_json(), updating=True),
            )
        except RequestError as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
        except KeyError:
            self._json_error(HTTPStatus.NOT_FOUND, "not_found", "project not found")
        else:
            self._json(HTTPStatus.OK, {"schema_version": "1", **project})

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        parsed_path = urlsplit(self.path)
        workspace_match = DAEMON_V2_WORKSPACE_FILES_PATH_PATTERN.fullmatch(
            parsed_path.path
        )
        if not workspace_match:
            self._json_error_v2(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found")
            return
        project_id = workspace_match.group(1)
        try:
            if not ID_PATTERN.fullmatch(project_id):
                raise RequestError("project_id is invalid")
            relative_path, _ = self._workspace_query(
                parsed_path.query, allow_overwrite=False
            )
            result = self.server.sessions.delete_workspace_file_v2(
                project_id, relative_path
            )
        except RequestError as exc:
            self._json_error_v2(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
        except KeyError:
            self._json_error_v2(
                HTTPStatus.NOT_FOUND, "not_found", "workspace file not found"
            )
        except StateConflictError as exc:
            self._json_error_v2(HTTPStatus.CONFLICT, "state_conflict", str(exc))
        else:
            self._json(HTTPStatus.OK, result.model_dump(mode="json"))

    def _run_session(self) -> None:
        try:
            request = validate_request(self._read_json())
        except RequestError as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            return
        try:
            session_id = self.server.sessions.submit(request)
        except KeyError:
            self._json_error(HTTPStatus.NOT_FOUND, "not_found", "project not found")
        except StateConflictError as exc:
            code = "agent_busy" if "another development session" in str(exc) else "state_conflict"
            self._json_error(HTTPStatus.CONFLICT, code, str(exc))
        else:
            self._json(
                HTTPStatus.ACCEPTED,
                {
                    "schema_version": "1",
                    "session_id": session_id,
                    "state": "running",
                    "status_url": f"/openevo-dev-agent/v1/sessions/{session_id}",
                },
            )

    def _read_json(self) -> object:
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise RequestError("Content-Length is invalid") from exc
        if content_length <= 0:
            raise RequestError("request body is empty")
        if content_length > MAX_REQUEST_BYTES:
            raise RequestError("request body is too large")
        try:
            return json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError(f"request body is not valid UTF-8 JSON: {exc}") from exc

    def _read_bytes(self, maximum: int) -> bytes:
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise RequestError("Content-Length is invalid") from exc
        if content_length < 0:
            raise RequestError("Content-Length is invalid")
        if content_length > maximum:
            raise RequestError(
                f"request body exceeds the {maximum // (1024 * 1024)} MiB limit"
            )
        payload = self.rfile.read(content_length)
        if len(payload) != content_length:
            raise RequestError("request body ended before Content-Length bytes were received")
        return payload

    @staticmethod
    def _workspace_query(query: str, *, allow_overwrite: bool) -> tuple[str, bool]:
        try:
            parameters = parse_qs(query, keep_blank_values=True, strict_parsing=True)
        except ValueError as exc:
            raise RequestError("workspace file query is invalid") from exc
        allowed = {"path", "overwrite"} if allow_overwrite else {"path"}
        if set(parameters) - allowed:
            raise RequestError("workspace file query contains unknown fields")
        paths = parameters.get("path", [])
        if len(paths) != 1 or not paths[0]:
            raise RequestError("workspace file query requires one path")
        overwrite_values = parameters.get("overwrite", [])
        if not allow_overwrite:
            return paths[0], False
        if len(overwrite_values) > 1 or (
            overwrite_values and overwrite_values[0] not in {"true", "false"}
        ):
            raise RequestError("overwrite must be true or false")
        return paths[0], overwrite_values == ["true"]

    @staticmethod
    def _artifact_page_query(
        query: str,
        *,
        allow_filters: bool,
        maximum_limit: int,
    ) -> tuple[str | None, str | None, str | None, int]:
        try:
            parameters = parse_qs(query, keep_blank_values=True, strict_parsing=True)
        except ValueError as exc:
            raise RequestError("artifact query is invalid") from exc
        allowed = {"after", "limit"}
        if allow_filters:
            allowed.update({"project_id", "task_id"})
        if set(parameters) - allowed:
            raise RequestError("artifact query contains unsupported parameters")
        if any(len(values) != 1 for values in parameters.values()):
            raise RequestError("artifact query parameters must be singular")

        project_id = parameters.get("project_id", [None])[0]
        task_id = parameters.get("task_id", [None])[0]
        after_artifact_id = parameters.get("after", [None])[0]
        for field_name, value in (
            ("project_id", project_id),
            ("task_id", task_id),
            ("artifact cursor", after_artifact_id),
        ):
            if value is not None and not ID_PATTERN.fullmatch(value):
                raise RequestError(f"{field_name} is invalid")

        limit_raw = parameters.get("limit", [str(maximum_limit)])[0]
        if not limit_raw.isascii() or not limit_raw.isdigit():
            raise RequestError("artifact limit must be an integer")
        limit = int(limit_raw)
        if not 1 <= limit <= maximum_limit:
            raise RequestError("artifact limit is outside the supported bound")
        return project_id, task_id, after_artifact_id, limit

    def _authorized(self) -> bool:
        expected = f"Bearer {self.server.token}"
        actual = self.headers.get("Authorization", "")
        if not hmac.compare_digest(actual, expected):
            self._json_error(HTTPStatus.UNAUTHORIZED, "unauthorized", "valid bearer token required")
            return False
        return True

    def _json_error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._json(status, {"schema_version": "1", "error": {"code": code, "message": message}})

    def _json_error_v2(self, status: HTTPStatus, code: str, message: str) -> None:
        self._json(
            status,
            {
                "schema_version": "2",
                "error": {"code": code, "message": message, "retryable": False},
            },
        )

    def _json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _binary(
        self,
        payload: bytes,
        media_type: str,
        file_name: str,
        *,
        content_sha256: str | None = None,
    ) -> None:
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header(
            "Content-Disposition",
            f"attachment; filename*=UTF-8''{quote(file_name, safe='')}",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if content_sha256 is not None:
            self.send_header("X-OpenEvo-Content-SHA256", content_sha256)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the development-only OpenEvo Codex bridge")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--codex-binary", default="codex")
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path.home() / ".openevo" / "dev-agent" / "state.sqlite3",
        help="SQLite database used for development Project and Session history",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    if not 10 <= args.timeout_seconds <= 1_800:
        raise SystemExit("--timeout-seconds must be between 10 and 1800")
    token = os.environ.get("OPENEVO_DEV_AGENT_TOKEN", "").strip()
    if len(token) < 32:
        raise SystemExit("OPENEVO_DEV_AGENT_TOKEN must contain at least 32 characters")
    codex_binary = shutil.which(args.codex_binary)
    if not codex_binary:
        raise SystemExit(f"Codex executable was not found: {args.codex_binary}")
    model = os.environ.get("OPENEVO_DEV_CODEX_MODEL", "").strip() or None
    runner = CodexRunner(codex_binary, args.timeout_seconds, model)
    runner.check_ready()
    state_path = args.state_path.expanduser().resolve()
    store = DevelopmentStateStore(state_path)
    evolution_model = (
        os.environ.get("OPENEVO_DEV_EVOLUTION_MODEL", "").strip()
        or model
        or "gpt-5.5"
    )
    evolution_runner = DocumentEvolutionRunner(
        state_root=state_path.parent,
        codex_binary=codex_binary,
        model=evolution_model,
        timeout_seconds=args.timeout_seconds,
    )
    try:
        evolution_runner.check_ready()
    except EvolutionRunError as exc:
        raise SystemExit(str(exc)) from exc
    evidence_failures = evolution_runner.seal_completed_session_datasets(store)
    for failure in evidence_failures:
        print(f"Could not seal legacy Evolution evidence: {failure}", flush=True)
    server = DevelopmentAgentServer(
        ("127.0.0.1", args.port),
        token,
        runner,
        store,
        evolution_runner,
    )
    print(f"Development agent daemon listening on 127.0.0.1:{args.port}", flush=True)
    print(f"Development state database: {state_path}", flush=True)
    print(
        "Evolution methods are discovered from the Core development catalog "
        f"and executed with model {evolution_model}.",
        flush=True,
    )
    print("It is loopback-only; connect through an SSH local-forward tunnel.", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("Stopping development agent daemon.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
