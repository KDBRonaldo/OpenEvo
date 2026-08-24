"""Bounded persistent project workspaces for the self-hosted daemon."""

from __future__ import annotations

import difflib
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree

from pypdf import PdfReader

from openevo.daemon.errors import AgentRunError, RequestError, StateConflictError


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
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
                modified_at = (
                    datetime.fromtimestamp(stat_result.st_mtime, timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
                if child.is_symlink():
                    entries.append(
                        {
                            "path": relative_text,
                            "kind": "symlink",
                            "byte_size": 0,
                            "content_sha256": None,
                            "media_type": None,
                            "content": None,
                            "modified_at": modified_at,
                        }
                    )
                    continue
                if child.is_dir(follow_symlinks=False):
                    entries.append(
                        {
                            "path": relative_text,
                            "kind": "directory",
                            "byte_size": 0,
                            "content_sha256": None,
                            "media_type": None,
                            "content": None,
                            "modified_at": modified_at,
                        }
                    )
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
                entries.append(
                    {
                        "path": relative_text,
                        "kind": "file",
                        "byte_size": size,
                        "content_sha256": digest,
                        "media_type": media_type,
                        "content": content,
                        "modified_at": modified_at,
                    }
                )

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
        modified_at = (
            datetime.fromtimestamp(after.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z")
        )
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
                    rendered_bytes[: byte_limit - len(encoded_marker)].decode(
                        "utf-8", errors="ignore"
                    )
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
        return encoded[: byte_limit - len(marker_bytes)].decode("utf-8", errors="ignore") + marker

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
                return cls._extract_spreadsheet_xml(path.name, archive, infos, byte_limit)
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
                    shared_strings.append(
                        "".join(
                            node.text or ""
                            for node in item.iter()
                            if node.tag.rsplit("}", 1)[-1] == "t"
                        )
                    )
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
                    node.text or "" for node in cell.iter() if node.tag.rsplit("}", 1)[-1] == "t"
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
                f"{info.filename}\t{info.file_size} bytes\n" for info in infos if not info.is_dir()
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
        if (
            self._workspace_size(project_root) - replaced_size + len(payload)
            > MAX_WORKSPACE_TOTAL_BYTES
        ):
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
            entry
            for entry in snapshot["entries"]
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
                name
                for name in directory_names
                if name not in {".git", ".openevo"} and not (Path(directory) / name).is_symlink()
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
            entry["path"]: entry for entry in before["entries"] if entry["kind"] == "file"
        }
        after_files = {
            entry["path"]: entry for entry in after["entries"] if entry["kind"] == "file"
        }
        changes: list[dict[str, Any]] = []
        for path in sorted(set(before_files) | set(after_files)):
            old = before_files.get(path)
            new = after_files.get(path)
            if (
                old is not None
                and new is not None
                and all(
                    old.get(field) == new.get(field)
                    for field in ("byte_size", "content_sha256", "modified_at")
                )
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
                    kind = (
                        "added"
                        if line.startswith("+")
                        else "removed"
                        if line.startswith("-")
                        else "context"
                    )
                    diff_lines.append(
                        {"kind": kind, "text": line[1:] if line[:1] in "+- " else line}
                    )
                    if len(diff_lines) >= 400:
                        break
            current = new or old
            changes.append(
                {
                    "path": path,
                    "change_type": change_type,
                    "byte_size": current["byte_size"],
                    "media_type": current.get("media_type"),
                    "content": new_content,
                    "previous_path": path if old is not None else None,
                    "diff_lines": diff_lines,
                }
            )
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
