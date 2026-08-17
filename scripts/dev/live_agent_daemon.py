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
import sys
import tempfile
import threading
import zipfile
from urllib.parse import parse_qs, quote, urlsplit
from xml.etree import ElementTree
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator

from pypdf import PdfReader


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
WORKSPACE_FILES_PATH_PATTERN = re.compile(
    r"^/openevo-dev-agent/v1/projects/([^/]+)/workspace/files$"
)


class RequestError(ValueError):
    pass


class AgentRunError(RuntimeError):
    pass


class EvolutionRunError(RuntimeError):
    pass


class StateConflictError(RuntimeError):
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
        if path.is_symlink() or not path.is_file():
            raise KeyError(relative_path)
        size = path.stat().st_size
        if size > MAX_WORKSPACE_DOWNLOAD_FILE_BYTES:
            raise RequestError(
                f"workspace file exceeds the {MAX_WORKSPACE_DOWNLOAD_FILE_BYTES // (1024 * 1024)} MiB download limit"
            )
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise RequestError(f"could not read the workspace file: {exc}") from exc
        if len(payload) != size or path.is_symlink() or not path.is_file():
            raise RequestError("workspace file changed while it was being read")
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return payload, media_type, path.name

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
                CREATE TABLE IF NOT EXISTS development_evolution_jobs (
                    job_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES development_sessions(session_id),
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
                    UNIQUE(session_id, target_id)
                );
                CREATE INDEX IF NOT EXISTS development_evolution_jobs_session
                    ON development_evolution_jobs(session_id, created_at, job_id);
                CREATE TABLE IF NOT EXISTS development_evolution_job_attempts (
                    attempt_id TEXT PRIMARY KEY,
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
            connection.execute(
                """
                UPDATE development_sessions
                SET state = 'failed', terminal_kind = 'failed', error = ?, updated_at = ?
                WHERE state = 'running'
                """,
                ("Development daemon restarted before this session completed.", restarted_at),
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
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

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
            sessions = [self._session_record(row) for row in connection.execute(
                "SELECT * FROM development_sessions ORDER BY created_at, session_id"
            )]
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

    def apply_workspace_mutations(self, project_id: str, mutations: object) -> None:
        self.workspace_path(project_id)
        self.workspaces.apply_mutations(project_id, mutations)

    def upload_workspace_file(
        self,
        project_id: str,
        relative_path: object,
        payload: bytes,
        *,
        overwrite: bool,
    ) -> dict[str, Any]:
        self.workspace_path(project_id)
        return self.workspaces.upload_file(
            project_id,
            relative_path,
            payload,
            overwrite=overwrite,
        )

    def download_workspace_file(
        self,
        project_id: str,
        relative_path: object,
    ) -> tuple[bytes, str, str]:
        self.workspace_path(project_id)
        return self.workspaces.read_file(project_id, relative_path)

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
            connection.execute(
                """
                INSERT INTO development_sessions(
                    session_id, project_id, task_title, instruction, response, model,
                    state, duration_ms, logs_json, selected_evolution_json,
                    evolution_errors_json, workspace_changes_json, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, 'running', NULL, ?, ?, '[]', '[]', NULL, ?, ?)
                """,
                (
                    session_id,
                    request["project_id"],
                    request["task_title"],
                    request["instruction"],
                    canonical_json(["Remote development daemon admitted the session."]),
                    canonical_json(selected_document_evolution(json.loads(project["config_json"]))),
                    now,
                    now,
                ),
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
            for message in result["logs"]:
                if message not in merged_logs:
                    merged_logs.append(message)
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
        return logs

    def get_session(self, session_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM development_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return self._session_record(row)

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
                WHERE project_id = ? AND target_id = ? AND promoted = 1
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
    ) -> None:
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
                (artifact_id, project_id, session_id, uri, name, utc_now()),
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

    def start_evolution_job(
        self,
        *,
        job_id: str,
        session_id: str,
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
                    job_id, session_id, target_id, method_id, requested_method_id,
                    resolver_input_artifact_ids_json, previous_artifact_id, config_json, state,
                    artifact_ids_json, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', '[]', NULL, ?, ?)
                """,
                (
                    job_id,
                    session_id,
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

    def start_evolution_retry(self, job_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        now = utc_now()
        with self._lock, self._connection() as connection:
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
                    attempt_id, job_id, ordinal, state, stage, artifact_ids_json,
                    error_code, error_message, logs_json, created_at, started_at,
                    completed_at, updated_at
                ) VALUES (?, ?, ?, 'running', 'input_resolution', '[]', NULL, NULL,
                          ?, ?, ?, NULL, ?)
                """,
                (
                    attempt_id,
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
            job = connection.execute(
                "SELECT * FROM development_evolution_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            attempt = connection.execute(
                "SELECT * FROM development_evolution_job_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        return self._job_record(job, [self._attempt_record(attempt)]), self._attempt_record(attempt)

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

    def record_evolution_artifact(
        self,
        *,
        artifact_id: str,
        project_id: str,
        session_id: str,
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
                    artifact_id, project_id, session_id, target_id, artifact_type,
                    method_id, renderer_kind, documents_json, manifest_json,
                    content_sha256, byte_size, previous_artifact_id, promoted, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    project_id,
                    session_id,
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
            logs.append(f"Session failed: {error}")
            connection.execute(
                """
                UPDATE development_sessions
                SET state = 'failed', logs_json = ?, workspace_changes_json = ?,
                    terminal_kind = 'failed', error = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (canonical_json(logs), canonical_json(workspace_changes or []), error, now, session_id),
            )

    @staticmethod
    def _session_record(row: sqlite3.Row) -> dict[str, Any]:
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
            "runtime_activation": json.loads(row["runtime_activation_json"]),
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _artifact_record(row: sqlite3.Row) -> dict[str, Any]:
        documents = json.loads(row["documents_json"])
        primary = documents[0] if documents else None
        return {
            "artifact_id": row["artifact_id"],
            "project_id": row["project_id"],
            "session_id": row["session_id"],
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
            "created_at": row["created_at"],
        }

    @staticmethod
    def _job_record(
        row: sqlite3.Row,
        attempts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "job_id": row["job_id"],
            "session_id": row["session_id"],
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
    def _attempt_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
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

        try:
            from openevo.evolution.framework.resolution import resolve_evolution_method
        except (ImportError, ModuleNotFoundError) as exc:
            raise EvolutionRunError(
                "OpenEvo Python dependencies are unavailable; run this daemon with `uv run python`"
            ) from exc

        prior_dataset_records = store.dataset_artifacts(request["project_id"])
        dataset_dir = self._artifact_root / "datasets" / session_id
        dataset_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
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
        )
        if not selected:
            return {"artifacts": [], "errors": []}

        prior_dataset_ids = [dataset["artifact_id"] for dataset in prior_dataset_records]
        persisted: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for item in selected:
            if cancellation is not None:
                cancellation.raise_if_requested()
            target_id = item["target_id"]
            requested_method_id = item["method"]
            method_id = resolve_evolution_method(
                target_id=target_id,
                requested_method=requested_method_id,
                prior_dataset_artifact_ids=prior_dataset_ids,
            )
            job_id = f"job-{target_id.replace('_', '-')}-{session_id}"
            previous = store.latest_artifact(request["project_id"], target_id)
            attempt = store.start_evolution_job(
                job_id=job_id,
                session_id=session_id,
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
                    request=request,
                    store=store,
                    cancellation=cancellation,
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
                artifact_id = (
                    f"dev-{artifact_type.replace('_', '-')}-"
                    f"{job['session_id'].removeprefix('dev-session-')}"
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
                    promoted=artifact.promoted,
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

    def retry_evolution(self, job_id: str) -> dict[str, Any]:
        if self._evolution_runner is None:
            raise StateConflictError("the Evolution runner is unavailable")
        if not self._turn_lock.acquire(blocking=False):
            raise StateConflictError("another development session or Evolution retry is running")
        try:
            job, attempt = self._store.start_evolution_retry(job_id)
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
            if self._evolution_runner is not None:
                self._store.append_session_log(
                    session_id,
                    "Running the selected evolution methods in the background.",
                )
                evolved = self._evolution_runner.evolve(
                    session_id=session_id,
                    request=request,
                    result=result,
                    store=self._store,
                    cancellation=cancellation,
                )
                result["evolution_artifacts"] = evolved["artifacts"]
                result["evolution_errors"] = evolved["errors"]
                self._store.record_evolution_errors(session_id, evolved["errors"])
                for artifact in evolved["artifacts"]:
                    self._store.append_session_log(
                        session_id,
                        f"OpenEvo {artifact['method']} published {artifact['artifact_type']} "
                        "for the next session.",
                    )
                for error in evolved["errors"]:
                    self._store.append_session_log(
                        session_id,
                        f"{error['method']} failed: {error['message']}",
                    )
            cancellation.raise_if_requested()
            self._store.complete_session(session_id, result)
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

    def _authorized(self) -> bool:
        expected = f"Bearer {self.server.token}"
        actual = self.headers.get("Authorization", "")
        if not hmac.compare_digest(actual, expected):
            self._json_error(HTTPStatus.UNAUTHORIZED, "unauthorized", "valid bearer token required")
            return False
        return True

    def _json_error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._json(status, {"schema_version": "1", "error": {"code": code, "message": message}})

    def _json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _binary(self, payload: bytes, media_type: str, file_name: str) -> None:
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header(
            "Content-Disposition",
            f"attachment; filename*=UTF-8''{quote(file_name, safe='')}",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
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
