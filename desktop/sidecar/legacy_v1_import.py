"""Bounded read-only import of Desktop Local API v1 state.

This module never instantiates the v1 store, runs its migrations, or writes its
SQLite files.  It accepts only the exact final v1 schema, projects explicit
profiles into path-free migration records, and keeps draft documents process
local until a caller supplies a complete validated v2 configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from desktop.sidecar import provider_store as legacy_store
from desktop.sidecar.contracts.v1 import models as legacy_models
from desktop.sidecar.contracts.v2 import models as m
from desktop.sidecar.provider_store_v2 import (
    DesktopProviderStoreV2,
    LegacyDraftSourceV2,
    LegacyProfileImportV2,
    MigrationDiagnosticCodeV2,
    MigrationDiagnosticV2,
    ProviderConflictV2,
)


MAX_LEGACY_DATABASE_BYTES = 67_108_864
MAX_LEGACY_DOCUMENT_BYTES = 1_048_576
MAX_LEGACY_PROFILE_ROWS = m.MAX_PROFILE_COUNT
MAX_LEGACY_DRAFT_ROWS = 100
MAX_LEGACY_AGGREGATE_BYTES = 8_388_608
MAX_LEGACY_ID_BYTES = 256
MAX_LEGACY_NAME_BYTES = 512
MAX_LEGACY_DIAGNOSTICS = 64

_FALLBACK_DISPLAY_NAME = "Legacy profile requires review"
_FALLBACK_TIMESTAMP = "1970-01-01T00:00:00.000000Z"
_TIMESTAMP_ADAPTER = TypeAdapter(m.UtcTimestamp)


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class LegacyDraftCandidateV1(_StrictModel):
    """Process-local v1 draft input; no cached remote authority is included."""

    legacy_project_id: legacy_models.OpaqueId
    legacy_profile_ref_sha256: m.Digest
    source: LegacyDraftSourceV2
    request: legacy_models.ProjectCreateV1


@dataclass(frozen=True, slots=True)
class LegacyV1ImportReport:
    profiles: tuple[m.LegacyExplicitProfileV2, ...]
    drafts: tuple[LegacyDraftCandidateV1, ...]
    diagnostics: tuple[MigrationDiagnosticV2, ...]


@dataclass(frozen=True, slots=True)
class _DiagnosticSpec:
    code: MigrationDiagnosticCodeV2
    source_kind: Literal["store", "profile", "project"]
    source_ref_sha256: str | None


@dataclass(frozen=True, slots=True)
class _ScanResult:
    profiles: tuple[LegacyProfileImportV2, ...]
    drafts: tuple[LegacyDraftCandidateV1, ...]
    diagnostics: tuple[_DiagnosticSpec, ...]


class _LegacyUnavailable(RuntimeError):
    def __init__(self, code: MigrationDiagnosticCodeV2) -> None:
        super().__init__(code)
        self.code = code


def _legacy_scan_checkpoint(_stage: str) -> None:
    """Private replacement-race checkpoint used by migration tests."""


class LegacyV1Importer:
    """Read one exact v1 provider database without mutating it."""

    def __init__(self, legacy_root: Path | str) -> None:
        self._root = Path(os.path.abspath(os.fspath(Path(legacy_root).expanduser())))

    def import_into(self, store: DesktopProviderStoreV2) -> LegacyV1ImportReport:
        if type(store) is not DesktopProviderStoreV2:
            raise TypeError("legacy v1 import requires the exact v2 provider store")
        try:
            scanned = self._scan()
        except _LegacyUnavailable as exc:
            scanned = _ScanResult(
                profiles=(),
                drafts=(),
                diagnostics=(
                    _DiagnosticSpec(
                        code=exc.code,
                        source_kind="store",
                        source_ref_sha256=None,
                    ),
                ),
            )
        imported: list[m.LegacyExplicitProfileV2] = []
        diagnostics = list(scanned.diagnostics)
        for source in scanned.profiles:
            try:
                imported.append(store.import_legacy_profile(source))
            except ProviderConflictV2:
                diagnostics.append(
                    _DiagnosticSpec(
                        code="legacy_source_changed",
                        source_kind="profile",
                        source_ref_sha256=source.source_ref_sha256,
                    )
                )
        persisted_diagnostics: list[MigrationDiagnosticV2] = []
        seen: set[tuple[str, str, str | None]] = set()
        for diagnostic in diagnostics[:MAX_LEGACY_DIAGNOSTICS]:
            identity = (
                diagnostic.code,
                diagnostic.source_kind,
                diagnostic.source_ref_sha256,
            )
            if identity in seen:
                continue
            seen.add(identity)
            persisted_diagnostics.append(
                store.record_migration_diagnostic(
                    code=diagnostic.code,
                    source_kind=diagnostic.source_kind,
                    source_ref_sha256=diagnostic.source_ref_sha256,
                )
            )
        return LegacyV1ImportReport(
            profiles=tuple(sorted(imported, key=lambda item: item.profile_id)),
            drafts=scanned.drafts,
            diagnostics=tuple(persisted_diagnostics),
        )

    def _scan(self) -> _ScanResult:
        try:
            root_stat = os.lstat(self._root)
        except FileNotFoundError:
            return _ScanResult((), (), ())
        except OSError as exc:
            raise _LegacyUnavailable("legacy_store_unsafe") from exc
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or stat.S_ISLNK(root_stat.st_mode)
            or stat.S_IMODE(root_stat.st_mode) != 0o700
            or (hasattr(os, "getuid") and root_stat.st_uid != os.getuid())
        ):
            raise _LegacyUnavailable("legacy_store_unsafe")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            root_fd = os.open(self._root, flags)
        except OSError as exc:
            raise _LegacyUnavailable("legacy_store_unsafe") from exc
        root_identity = (root_stat.st_dev, root_stat.st_ino)
        lock_fd: int | None = None
        database_fd: int | None = None
        connection: sqlite3.Connection | None = None
        try:
            database = self._optional_private_file(root_fd, legacy_store.DATABASE_FILENAME)
            if database is None:
                return _ScanResult((), (), ())
            if database.st_size > MAX_LEGACY_DATABASE_BYTES:
                raise _LegacyUnavailable("legacy_store_oversized")
            database_identity = (database.st_dev, database.st_ino)
            cursor_key = self._require_private_file(root_fd, legacy_store.CURSOR_KEY_FILENAME)
            if cursor_key.st_size != 32:
                raise _LegacyUnavailable("legacy_store_unsafe")
            lock = self._require_private_file(root_fd, legacy_store.OWNER_LOCK_FILENAME)
            if lock.st_size != 0:
                raise _LegacyUnavailable("legacy_store_unsafe")
            try:
                lock_fd = os.open(
                    legacy_store.OWNER_LOCK_FILENAME,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
                fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise _LegacyUnavailable("legacy_store_busy") from exc
            except OSError as exc:
                raise _LegacyUnavailable("legacy_store_unsafe") from exc
            opened_lock = os.fstat(lock_fd)
            if (opened_lock.st_dev, opened_lock.st_ino) != (lock.st_dev, lock.st_ino):
                raise _LegacyUnavailable("legacy_store_replaced")
            try:
                database_fd = os.open(
                    legacy_store.DATABASE_FILENAME,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
            except OSError as exc:
                raise _LegacyUnavailable("legacy_store_unsafe") from exc
            opened_database = self._require_private_descriptor(
                database_fd,
                legacy_store.DATABASE_FILENAME,
            )
            if (opened_database.st_dev, opened_database.st_ino) != database_identity:
                raise _LegacyUnavailable("legacy_store_replaced")
            _legacy_scan_checkpoint("after_database_fd_open")
            journal = self._optional_private_file(root_fd, legacy_store.JOURNAL_FILENAME)
            if journal is not None and journal.st_size != 0:
                raise _LegacyUnavailable("legacy_store_busy")
            for name in (legacy_store.WAL_FILENAME, legacy_store.SHM_FILENAME):
                if self._optional_private_file(root_fd, name) is not None:
                    raise _LegacyUnavailable("legacy_store_unsafe")
            connection = sqlite3.connect(
                f"file:/dev/fd/{database_fd}?mode=ro",
                uri=True,
                timeout=1,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise _LegacyUnavailable("legacy_store_unsafe")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("BEGIN")
            rows = connection.execute("PRAGMA database_list").fetchall()
            if len(rows) != 1:
                raise _LegacyUnavailable("legacy_store_unsafe")
            self._verify_binding(
                root_fd,
                root_identity=root_identity,
                database_identity=database_identity,
                lock_identity=(lock.st_dev, lock.st_ino),
                database_descriptor=database_fd,
                sqlite_path=rows[0][2],
            )
            _legacy_scan_checkpoint("after_database_open")
            self._verify_binding(
                root_fd,
                root_identity=root_identity,
                database_identity=database_identity,
                lock_identity=(lock.st_dev, lock.st_ino),
                database_descriptor=database_fd,
                sqlite_path=rows[0][2],
            )
            self._validate_schema(connection)
            result = self._scan_rows(connection)
            self._verify_binding(
                root_fd,
                root_identity=root_identity,
                database_identity=database_identity,
                lock_identity=(lock.st_dev, lock.st_ino),
                database_descriptor=database_fd,
                sqlite_path=rows[0][2],
            )
            return result
        except _LegacyUnavailable:
            raise
        except (legacy_store.ProviderStoreError, sqlite3.DatabaseError, ValueError) as exc:
            raise _LegacyUnavailable("legacy_schema_unsupported") from exc
        except OSError as exc:
            raise _LegacyUnavailable("legacy_store_unsafe") from exc
        finally:
            if connection is not None:
                connection.close()
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
            if database_fd is not None:
                os.close(database_fd)
            os.close(root_fd)

    @staticmethod
    def _require_private_file(root_fd: int, name: str) -> os.stat_result:
        try:
            metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except OSError as exc:
            raise _LegacyUnavailable("legacy_store_unsafe") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise _LegacyUnavailable("legacy_store_unsafe")
        return metadata

    @staticmethod
    def _require_private_descriptor(descriptor: int, name: str) -> os.stat_result:
        try:
            metadata = os.fstat(descriptor)
        except OSError as exc:
            raise _LegacyUnavailable("legacy_store_unsafe") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise _LegacyUnavailable("legacy_store_unsafe")
        return metadata

    @classmethod
    def _optional_private_file(
        cls,
        root_fd: int,
        name: str,
    ) -> os.stat_result | None:
        try:
            os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _LegacyUnavailable("legacy_store_unsafe") from exc
        return cls._require_private_file(root_fd, name)

    def _verify_binding(
        self,
        root_fd: int,
        *,
        root_identity: tuple[int, int],
        database_identity: tuple[int, int],
        lock_identity: tuple[int, int],
        database_descriptor: int,
        sqlite_path: object,
    ) -> None:
        try:
            path_root = os.lstat(self._root)
            held_root = os.fstat(root_fd)
        except OSError as exc:
            raise _LegacyUnavailable("legacy_store_replaced") from exc
        if (path_root.st_dev, path_root.st_ino) != root_identity or (
            held_root.st_dev,
            held_root.st_ino,
        ) != root_identity:
            raise _LegacyUnavailable("legacy_store_replaced")
        current = self._require_private_file(root_fd, legacy_store.DATABASE_FILENAME)
        current_lock = self._require_private_file(root_fd, legacy_store.OWNER_LOCK_FILENAME)
        if (
            (current.st_dev, current.st_ino) != database_identity
            or current.st_size > MAX_LEGACY_DATABASE_BYTES
            or (current_lock.st_dev, current_lock.st_ino) != lock_identity
        ):
            raise _LegacyUnavailable("legacy_store_replaced")
        descriptor_path = f"/dev/fd/{database_descriptor}"
        managed_path = str(self._root / legacy_store.DATABASE_FILENAME)
        if sqlite_path == descriptor_path:
            follow_sqlite_path = True
            require_opened_device = False
        elif sqlite_path == managed_path:
            follow_sqlite_path = False
            require_opened_device = True
        else:
            raise _LegacyUnavailable("legacy_store_replaced")
        try:
            held_database = os.fstat(database_descriptor)
            opened = os.stat(sqlite_path, follow_symlinks=follow_sqlite_path)
        except OSError as exc:
            raise _LegacyUnavailable("legacy_store_replaced") from exc
        if (
            (held_database.st_dev, held_database.st_ino) != database_identity
            or opened.st_ino != held_database.st_ino
            or (require_opened_device and opened.st_dev != held_database.st_dev)
            or opened.st_size != held_database.st_size
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise _LegacyUnavailable("legacy_store_replaced")

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != legacy_store.SCHEMA_VERSION:
            raise _LegacyUnavailable("legacy_schema_unsupported")
        rows = legacy_store._schema_rows(connection)
        if rows != legacy_store._EXPECTED_SCHEMA_V7_ROWS:
            raise _LegacyUnavailable("legacy_schema_unsupported")
        migrations = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        if [row[0] for row in migrations] != list(range(1, legacy_store.SCHEMA_VERSION + 1)):
            raise _LegacyUnavailable("legacy_schema_unsupported")

    def _scan_rows(self, connection: sqlite3.Connection) -> _ScanResult:
        profiles, profile_diagnostics, profile_bytes = self._scan_profiles(
            connection,
            byte_budget=MAX_LEGACY_AGGREGATE_BYTES,
        )
        drafts, draft_diagnostics, _draft_bytes = self._scan_drafts(
            connection,
            byte_budget=MAX_LEGACY_AGGREGATE_BYTES - profile_bytes,
        )
        diagnostics = (*profile_diagnostics, *draft_diagnostics)
        return _ScanResult(
            profiles=profiles,
            drafts=drafts,
            diagnostics=tuple(diagnostics[:MAX_LEGACY_DIAGNOSTICS]),
        )

    def _scan_profiles(
        self,
        connection: sqlite3.Connection,
        *,
        byte_budget: int,
    ) -> tuple[
        tuple[LegacyProfileImportV2, ...],
        tuple[_DiagnosticSpec, ...],
        int,
    ]:
        summary = connection.execute(
            """
            SELECT count(*), coalesce(sum(
                length(CAST(profile_id AS BLOB)) + length(CAST(name AS BLOB)) +
                length(CAST(document_json AS BLOB)) +
                length(CAST(created_at AS BLOB)) + length(CAST(updated_at AS BLOB))
            ), 0)
            FROM remote_profiles
            """
        ).fetchone()
        count, _aggregate = cast(tuple[int, int], summary)
        diagnostics: list[_DiagnosticSpec] = []
        if count > MAX_LEGACY_PROFILE_ROWS:
            diagnostics.append(_DiagnosticSpec("legacy_row_budget_exhausted", "store", None))
        rowids = [
            row[0]
            for row in connection.execute(
                "SELECT rowid FROM remote_profiles ORDER BY rowid LIMIT ?",
                (MAX_LEGACY_PROFILE_ROWS,),
            )
        ]
        imported: list[LegacyProfileImportV2] = []
        consumed = 0
        for rowid in rowids:
            candidate, diagnostic, row_bytes = self._profile_candidate(
                connection,
                cast(int, rowid),
                byte_budget=byte_budget - consumed,
            )
            if candidate is None:
                if diagnostic is not None:
                    diagnostics.append(diagnostic)
                break
            imported.append(candidate)
            consumed += row_bytes
            if diagnostic is not None:
                diagnostics.append(diagnostic)
        return tuple(imported), tuple(diagnostics), consumed

    def _profile_candidate(
        self,
        connection: sqlite3.Connection,
        rowid: int,
        *,
        byte_budget: int,
    ) -> tuple[
        LegacyProfileImportV2 | None,
        _DiagnosticSpec | None,
        int,
    ]:
        lengths = connection.execute(
            """
            SELECT
                length(CAST(profile_id AS BLOB)), length(CAST(name AS BLOB)),
                length(CAST(document_json AS BLOB)),
                length(CAST(created_at AS BLOB)), length(CAST(updated_at AS BLOB))
            FROM remote_profiles WHERE rowid = ?
            """,
            (rowid,),
        ).fetchone()
        if lengths is None:
            raise _LegacyUnavailable("legacy_store_replaced")
        id_size, name_size, document_size, created_size, updated_size = lengths
        sizes = (id_size, name_size, document_size, created_size, updated_size)
        if any(type(value) is not int or value < 0 for value in sizes):
            return (
                None,
                _DiagnosticSpec("legacy_row_budget_exhausted", "store", None),
                0,
            )
        row_bytes = sum(cast(tuple[int, int, int, int, int], sizes))
        if row_bytes > byte_budget:
            return (
                None,
                _DiagnosticSpec("legacy_row_budget_exhausted", "store", None),
                0,
            )
        fields = connection.execute(
            """
            SELECT
                CASE WHEN length(CAST(profile_id AS BLOB)) <= ? THEN profile_id END,
                CASE WHEN length(CAST(name AS BLOB)) <= ? THEN name END,
                CASE WHEN length(CAST(document_json AS BLOB)) <= ? THEN document_json END,
                CASE WHEN length(CAST(created_at AS BLOB)) <= 64 THEN created_at END,
                CASE WHEN length(CAST(updated_at AS BLOB)) <= 64 THEN updated_at END
            FROM remote_profiles WHERE rowid = ?
            """,
            (
                MAX_LEGACY_ID_BYTES,
                MAX_LEGACY_NAME_BYTES,
                MAX_LEGACY_DOCUMENT_BYTES,
                rowid,
            ),
        ).fetchone()
        if fields is None:
            raise _LegacyUnavailable("legacy_store_replaced")
        profile_id, name, document, created_at, updated_at = fields
        source_ref = self._source_ref("profile", profile_id, rowid)
        oversized = (
            type(document_size) is not int
            or document_size < 2
            or document_size > MAX_LEGACY_DOCUMENT_BYTES
        )
        valid = not oversized
        raw = b"" if document is None else bytes(document)
        request: legacy_models.RemoteProfileCreateV1 | None = None
        if valid:
            try:
                request = legacy_models.RemoteProfileCreateV1.model_validate_json(raw)
            except ValidationError:
                valid = False
        display_name = self._safe_display_name(name)
        if request is None or request.name != name:
            valid = False
        if display_name is None:
            display_name = _FALLBACK_DISPLAY_NAME
            valid = False
        safe_created = self._safe_timestamp(created_at)
        safe_updated = self._safe_timestamp(updated_at)
        if safe_created is None or safe_updated is None:
            safe_created = _FALLBACK_TIMESTAMP
            safe_updated = _FALLBACK_TIMESTAMP
            valid = False
        if document is not None:
            document_sha256 = hashlib.sha256(raw).hexdigest()
        else:
            document_sha256 = hashlib.sha256(
                b"openevo-legacy-profile-unread-v2\0"
                + str(
                    (rowid, id_size, name_size, document_size, created_size, updated_size)
                ).encode("ascii")
            ).hexdigest()
        if not valid:
            display_name = _FALLBACK_DISPLAY_NAME
        candidate = LegacyProfileImportV2(
            source_ref_sha256=source_ref,
            source_document_sha256=document_sha256,
            display_name=display_name,
            migration_state="rebind_required" if valid else "quarantined",
            created_at=safe_created,
            updated_at=safe_updated,
        )
        diagnostic = None
        if not valid:
            diagnostic = _DiagnosticSpec(
                "legacy_profile_oversized" if oversized else "legacy_profile_corrupt",
                "profile",
                source_ref,
            )
        return candidate, diagnostic, row_bytes

    def _scan_drafts(
        self,
        connection: sqlite3.Connection,
        *,
        byte_budget: int,
    ) -> tuple[
        tuple[LegacyDraftCandidateV1, ...],
        tuple[_DiagnosticSpec, ...],
        int,
    ]:
        summary = connection.execute(
            """
            SELECT count(*), coalesce(sum(
                length(CAST(project_id AS BLOB)) + length(CAST(profile_id AS BLOB)) +
                length(CAST(name AS BLOB)) + length(CAST(document_json AS BLOB))
            ), 0)
            FROM projects WHERE state = 'draft'
            """
        ).fetchone()
        count, _aggregate = cast(tuple[int, int], summary)
        diagnostics: list[_DiagnosticSpec] = []
        if count > MAX_LEGACY_DRAFT_ROWS:
            diagnostics.append(_DiagnosticSpec("legacy_row_budget_exhausted", "store", None))
        rowids = [
            row[0]
            for row in connection.execute(
                "SELECT rowid FROM projects WHERE state = 'draft' ORDER BY rowid LIMIT ?",
                (MAX_LEGACY_DRAFT_ROWS,),
            )
        ]
        drafts: list[LegacyDraftCandidateV1] = []
        consumed = 0
        for rowid in rowids:
            candidate, diagnostic, row_bytes = self._draft_candidate(
                connection,
                cast(int, rowid),
                byte_budget=byte_budget - consumed,
            )
            if (
                candidate is None
                and diagnostic is not None
                and diagnostic.code == "legacy_row_budget_exhausted"
            ):
                diagnostics.append(diagnostic)
                break
            if candidate is not None:
                drafts.append(candidate)
            consumed += row_bytes
            if diagnostic is not None:
                diagnostics.append(diagnostic)
        return tuple(drafts), tuple(diagnostics), consumed

    def _draft_candidate(
        self,
        connection: sqlite3.Connection,
        rowid: int,
        *,
        byte_budget: int,
    ) -> tuple[LegacyDraftCandidateV1 | None, _DiagnosticSpec | None, int]:
        lengths = connection.execute(
            """
            SELECT
                length(CAST(project_id AS BLOB)), length(CAST(profile_id AS BLOB)),
                length(CAST(name AS BLOB)), length(CAST(document_json AS BLOB))
            FROM projects WHERE rowid = ? AND state = 'draft'
            """,
            (rowid,),
        ).fetchone()
        if lengths is None:
            raise _LegacyUnavailable("legacy_store_replaced")
        project_id_size, profile_id_size, name_size, document_size = lengths
        sizes = (project_id_size, profile_id_size, name_size, document_size)
        if any(type(value) is not int or value < 0 for value in sizes):
            return (
                None,
                _DiagnosticSpec("legacy_row_budget_exhausted", "store", None),
                0,
            )
        row_bytes = sum(cast(tuple[int, int, int, int], sizes))
        if row_bytes > byte_budget:
            return (
                None,
                _DiagnosticSpec("legacy_row_budget_exhausted", "store", None),
                0,
            )
        source_ref = self._source_ref("project", None, rowid)
        oversized = (
            type(document_size) is not int
            or document_size < 2
            or document_size > MAX_LEGACY_DOCUMENT_BYTES
        )
        if (
            type(project_id_size) is not int
            or project_id_size > MAX_LEGACY_ID_BYTES
            or type(profile_id_size) is not int
            or profile_id_size > MAX_LEGACY_ID_BYTES
            or type(name_size) is not int
            or name_size > MAX_LEGACY_NAME_BYTES
            or oversized
        ):
            return (
                None,
                _DiagnosticSpec("legacy_project_oversized", "project", source_ref),
                row_bytes,
            )
        row = connection.execute(
            """
            SELECT project_id, profile_id, name,
                   CASE WHEN length(CAST(document_json AS BLOB)) <= ?
                        THEN document_json END
            FROM projects WHERE rowid = ? AND state = 'draft'
            """,
            (MAX_LEGACY_DOCUMENT_BYTES, rowid),
        ).fetchone()
        if row is None:
            raise _LegacyUnavailable("legacy_store_replaced")
        project_id, profile_id, name, document = row
        source_ref = self._source_ref("project", project_id, rowid)
        raw = b"" if document is None else bytes(document)
        try:
            project_id = TypeAdapter(legacy_models.OpaqueId).validate_python(
                project_id, strict=True
            )
            profile_id = TypeAdapter(legacy_models.OpaqueId).validate_python(
                profile_id, strict=True
            )
            request = legacy_models.ProjectCreateV1.model_validate_json(raw)
            if (
                request.name != name
                or request.profile_id != profile_id
                or self._safe_display_name(name) is None
            ):
                raise ValueError("legacy draft row differs from its document")
            candidate = LegacyDraftCandidateV1(
                legacy_project_id=project_id,
                legacy_profile_ref_sha256=self._source_ref("profile", profile_id, rowid),
                source=LegacyDraftSourceV2(
                    source_ref_sha256=source_ref,
                    source_document_sha256=hashlib.sha256(raw).hexdigest(),
                    display_name=name,
                ),
                request=request,
            )
        except (ValidationError, ValueError, TypeError):
            return (
                None,
                _DiagnosticSpec("legacy_project_corrupt", "project", source_ref),
                row_bytes,
            )
        return candidate, None, row_bytes

    @staticmethod
    def _source_ref(kind: str, value: object, rowid: int) -> str:
        if type(value) is str and 1 <= len(value.encode("utf-8")) <= MAX_LEGACY_ID_BYTES:
            material = value.encode("utf-8")
        else:
            material = f"rowid:{rowid}".encode("ascii")
        return hashlib.sha256(
            b"openevo-desktop-legacy-source-v2\0" + kind.encode("ascii") + b"\0" + material
        ).hexdigest()

    @staticmethod
    def _safe_display_name(value: object) -> str | None:
        if (
            type(value) is not str
            or value != value.strip()
            or not value
            or len(value) > 128
            or any(ord(character) < 0x20 for character in value)
        ):
            return None
        try:
            TypeAdapter(m.DisplayName).validate_python(value, strict=True)
        except ValidationError:
            return None
        return value

    @staticmethod
    def _safe_timestamp(value: object) -> str | None:
        if type(value) is not str:
            return None
        try:
            return cast(str, _TIMESTAMP_ADAPTER.validate_python(value, strict=True))
        except ValidationError:
            return None


def import_legacy_v1_state(
    store: DesktopProviderStoreV2,
    legacy_root: Path | str,
) -> LegacyV1ImportReport:
    return LegacyV1Importer(legacy_root).import_into(store)


__all__ = [
    "LegacyDraftCandidateV1",
    "LegacyV1ImportReport",
    "LegacyV1Importer",
    "MAX_LEGACY_AGGREGATE_BYTES",
    "MAX_LEGACY_DATABASE_BYTES",
    "MAX_LEGACY_DIAGNOSTICS",
    "MAX_LEGACY_DOCUMENT_BYTES",
    "import_legacy_v1_state",
]
