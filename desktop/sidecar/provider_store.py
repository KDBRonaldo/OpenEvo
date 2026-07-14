from __future__ import annotations

import base64
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
from typing import Any, Literal, TypeVar, cast

from pydantic import BaseModel, ValidationError

from desktop.sidecar.contracts.v1.models import (
    CredentialSlotStatusV1,
    ProjectCreateV1,
    ProjectPageV1,
    ProjectPatchV1,
    ProjectV1,
    RemoteProfileCreateV1,
    RemoteProfilePageV1,
    RemoteProfilePatchV1,
    RemoteProfileV1,
)


SCHEMA_VERSION = 1
DATABASE_FILENAME = "provider.sqlite3"
JOURNAL_FILENAME = f"{DATABASE_FILENAME}-journal"
WAL_FILENAME = f"{DATABASE_FILENAME}-wal"
SHM_FILENAME = f"{DATABASE_FILENAME}-shm"
OWNER_LOCK_FILENAME = "provider.lock"
CURSOR_KEY_FILENAME = "cursor-signing.key"
LOCAL_PRINCIPAL = "desktop-local-v1"
MAX_DOCUMENT_BYTES = 136_314_880
MAX_REQUEST_BYTES = MAX_DOCUMENT_BYTES
MAX_RESPONSE_BYTES = MAX_DOCUMENT_BYTES
MAX_CURSOR_BYTES = 2_048
MAX_RENDERED_CURSOR_BYTES = 256
MAX_IDEMPOTENCY_KEY_BYTES = 256
MAX_IDENTITY_BYTES = 512
MAX_DATABASE_BYTES = 536_870_912
MAX_RECOVERY_ROWS = 100_000
MAX_RECOVERY_BYTES = 402_653_184
MAX_SCHEMA_OBJECTS = 32
MAX_SCHEMA_BYTES = 65_536
DEFAULT_IDEMPOTENCY_RECORD_LIMIT = 10_000
DEFAULT_IDEMPOTENCY_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_CURSOR_TTL_SECONDS = 15 * 60

_RESOURCE_ID_BYTES = 32
_CURSOR_KEY_BYTES = 32
_ETAG_RE = re.compile(r'^"[0-9a-f]{64}"$')
_B64_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d\.\d{6}Z$"
)


class ProviderStoreError(Exception):
    """Base class for local provider persistence failures."""


class ProviderStateRootError(ProviderStoreError):
    """The private provider state root or one of its files is unsafe."""


class ProviderSchemaError(ProviderStoreError):
    """The SQLite schema cannot be safely opened or migrated."""


class ProviderDataCorruptionError(ProviderStoreError):
    """Persisted provider data no longer satisfies its closed contract."""


class ContractValidationError(ProviderStoreError):
    """A caller supplied data that does not satisfy a Desktop v1 model."""


class ResourceNotFoundError(ProviderStoreError):
    def __init__(self, resource_type: str, resource_id: str) -> None:
        super().__init__(f"{resource_type} resource was not found")
        self.resource_type = resource_type
        self.resource_id = resource_id


class ResourceInUseError(ProviderStoreError):
    def __init__(self, resource_type: str, resource_id: str) -> None:
        super().__init__(f"{resource_type} resource is active or in use")
        self.resource_type = resource_type
        self.resource_id = resource_id


class ETagConflictError(ProviderStoreError):
    def __init__(self, resource_type: str, resource_id: str, current_etag: str) -> None:
        super().__init__(f"{resource_type} resource ETag does not match")
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.current_etag = current_etag


class IdempotencyConflictError(ProviderStoreError):
    """An idempotency key was reused with a different canonical request."""


class IdempotencyCapacityError(ProviderStoreError):
    """The bounded live idempotency record capacity is exhausted."""


class CursorInvalidError(ProviderStoreError):
    """A cursor is malformed, tampered with, or bound to another query."""


class CursorExpiredError(ProviderStoreError):
    """A valid provider cursor is outside its bounded replay window."""


@dataclass(frozen=True)
class IdempotencyResult:
    status_code: int
    response_bytes: bytes
    replayed: bool


_ModelT = TypeVar("_ModelT", bound=BaseModel)
_Direction = Literal["asc", "desc"]


_SCHEMA_V1 = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version = 1),
        applied_at TEXT NOT NULL CHECK (length(CAST(applied_at AS BLOB)) = 27)
    ) STRICT
    """,
    """
    CREATE TABLE remote_profiles (
        profile_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        document_json BLOB NOT NULL,
        connection_state TEXT NOT NULL,
        credential_slots_json BLOB NOT NULL,
        host_key_fingerprint TEXT,
        resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE projects (
        project_id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL,
        name TEXT NOT NULL,
        document_json BLOB NOT NULL,
        state TEXT NOT NULL,
        current_revision_id TEXT,
        resource_version INTEGER NOT NULL CHECK (resource_version >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (profile_id) REFERENCES remote_profiles(profile_id) ON DELETE RESTRICT
    ) STRICT
    """,
    """
    CREATE TABLE idempotency_records (
        principal TEXT NOT NULL,
        method TEXT NOT NULL,
        route TEXT NOT NULL,
        resource_scope TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        response_type TEXT NOT NULL,
        status_code INTEGER NOT NULL CHECK (status_code BETWEEN 100 AND 599),
        response_bytes BLOB NOT NULL,
        created_at_epoch INTEGER NOT NULL,
        expires_at_epoch INTEGER NOT NULL,
        PRIMARY KEY (principal, method, route, resource_scope, idempotency_key),
        CHECK (principal = 'desktop-local-v1'),
        CHECK (length(CAST(method AS BLOB)) BETWEEN 1 AND 512),
        CHECK (length(CAST(route AS BLOB)) BETWEEN 1 AND 512),
        CHECK (length(CAST(resource_scope AS BLOB)) BETWEEN 1 AND 512),
        CHECK (length(CAST(idempotency_key AS BLOB)) BETWEEN 16 AND 256),
        CHECK (length(request_digest) = 64),
        CHECK (response_type IN ('ProjectV1', 'RemoteProfileV1')),
        CHECK (length(response_bytes) <= 136314880),
        CHECK (expires_at_epoch > created_at_epoch)
    ) STRICT
    """,
    "CREATE INDEX remote_profiles_updated_idx ON remote_profiles(updated_at, profile_id)",
    "CREATE INDEX remote_profiles_created_idx ON remote_profiles(created_at, profile_id)",
    "CREATE INDEX remote_profiles_name_idx ON remote_profiles(name, profile_id)",
    "CREATE INDEX projects_updated_idx ON projects(updated_at, project_id)",
    "CREATE INDEX projects_created_idx ON projects(created_at, project_id)",
    "CREATE INDEX projects_name_idx ON projects(name, project_id)",
    "CREATE INDEX projects_profile_idx ON projects(profile_id, project_id)",
    "CREATE UNIQUE INDEX projects_single_active_idx ON projects((1)) WHERE state = 'active'",
    "CREATE INDEX idempotency_expiry_idx ON idempotency_records(expires_at_epoch)",
)


def _schema_rows(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    count, byte_count = connection.execute(
        """
        SELECT count(*), coalesce(sum(
            length(CAST(type AS BLOB)) + length(CAST(name AS BLOB)) +
            length(CAST(tbl_name AS BLOB)) + coalesce(length(CAST(sql AS BLOB)), 0)
        ), 0)
        FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%'
        """
    ).fetchone()
    if count > MAX_SCHEMA_OBJECTS or byte_count > MAX_SCHEMA_BYTES:
        raise ProviderSchemaError("provider schema exceeds its fingerprint bounds")
    return tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name, tbl_name
            """
        )
    )


def _expected_schema() -> tuple[tuple[tuple[object, ...], ...], str]:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in _SCHEMA_V1:
            connection.execute(statement)
        rows = _schema_rows(connection)
    finally:
        connection.close()
    digest = sha256(_canonical_json_bytes(rows)).hexdigest()
    return rows, digest


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractValidationError("value is not canonical JSON data") from exc


_EXPECTED_SCHEMA_ROWS, _EXPECTED_SCHEMA_DIGEST = _expected_schema()


def _decode_json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderDataCorruptionError(f"stored {label} is not valid JSON") from exc
    if type(value) is not dict:
        raise ProviderDataCorruptionError(f"stored {label} is not a JSON object")
    try:
        canonical = _canonical_json_bytes(value)
    except ContractValidationError as exc:
        raise ProviderDataCorruptionError(f"stored {label} is not canonical JSON") from exc
    if canonical != raw:
        raise ProviderDataCorruptionError(f"stored {label} is not canonical JSON")
    return cast(dict[str, Any], value)


def _validate_model(model_type: type[_ModelT], value: object) -> _ModelT:
    try:
        return model_type.model_validate(value)
    except ValidationError as exc:
        raise ContractValidationError(f"{model_type.__name__} validation failed") from exc


def _validate_json_model(model_type: type[_ModelT], value: object) -> _ModelT:
    try:
        encoded = _canonical_json_bytes(value)
        return model_type.model_validate_json(encoded)
    except (ContractValidationError, ValidationError) as exc:
        raise ProviderDataCorruptionError(f"stored data violates {model_type.__name__}") from exc


class ProviderMutation:
    """Restricted state changes available inside an idempotent store transaction."""

    __slots__ = ("_connection", "_if_match", "_store")

    def __init__(
        self,
        store: DesktopProviderStore,
        connection: sqlite3.Connection,
        *,
        if_match: str | None = None,
    ) -> None:
        self._store = store
        self._connection = connection
        self._if_match = if_match

    def _require_bound_if_match(self, if_match: str) -> None:
        if self._if_match is not None and not hmac.compare_digest(self._if_match, if_match):
            raise ContractValidationError(
                "action mutation If-Match differs from its idempotency envelope"
            )

    def set_project_state(
        self,
        project_id: str,
        *,
        if_match: str,
        state: Literal["draft", "active", "archived", "blocked"],
        current_revision_id: str | None = None,
    ) -> ProjectV1:
        self._store._validate_resource_id(project_id)
        self._store._validate_if_match(if_match)
        self._require_bound_if_match(if_match)
        if state not in {"draft", "active", "archived", "blocked"}:
            raise ContractValidationError("project state is not a Desktop v1 state")
        if current_revision_id is not None:
            self._store._validate_resource_id(current_revision_id)
        row = self._store._require_project_row(self._connection, project_id)
        self._store._require_etag("project", project_id, row, if_match)
        if state == "active":
            self._connection.execute(
                """
                UPDATE projects
                SET state = 'draft', current_revision_id = NULL,
                    resource_version = resource_version + 1, updated_at = ?
                WHERE state = 'active' AND project_id != ?
                """,
                (self._store._timestamp(), project_id),
            )
        self._connection.execute(
            """
            UPDATE projects
            SET state = ?, current_revision_id = ?,
                resource_version = resource_version + 1, updated_at = ?
            WHERE project_id = ?
            """,
            (state, current_revision_id, self._store._timestamp(), project_id),
        )
        return self._store._project_from_row(
            self._store._require_project_row(self._connection, project_id)
        )

    def set_profile_runtime_state(
        self,
        profile_id: str,
        *,
        if_match: str,
        connection_state: Literal[
            "disconnected",
            "connecting",
            "host_key_required",
            "connected",
            "failed",
        ],
        credential_slots: tuple[CredentialSlotStatusV1, ...],
        host_key_fingerprint: str | None,
    ) -> RemoteProfileV1:
        self._store._validate_resource_id(profile_id)
        self._store._validate_if_match(if_match)
        self._require_bound_if_match(if_match)
        if connection_state not in {
            "disconnected",
            "connecting",
            "host_key_required",
            "connected",
            "failed",
        }:
            raise ContractValidationError("profile connection state is not a Desktop v1 state")
        row = self._store._require_profile_row(self._connection, profile_id)
        self._store._require_etag("profile", profile_id, row, if_match)
        timestamp = self._store._timestamp()
        current = self._store._profile_from_row(row)
        proposed = _validate_model(
            RemoteProfileV1,
            {
                **current.model_dump(mode="python"),
                "connection_state": connection_state,
                "credential_slots": credential_slots,
                "host_key_fingerprint": host_key_fingerprint,
                "etag": self._store._etag(
                    "profile", profile_id, cast(int, row["resource_version"]) + 1
                ),
                "updated_at": timestamp,
            },
        )
        self._connection.execute(
            """
            UPDATE remote_profiles
            SET connection_state = ?, credential_slots_json = ?,
                host_key_fingerprint = ?, resource_version = resource_version + 1,
                updated_at = ?
            WHERE profile_id = ?
            """,
            (
                connection_state,
                _canonical_json_bytes(
                    [slot.model_dump(mode="json") for slot in proposed.credential_slots]
                ),
                proposed.host_key_fingerprint,
                timestamp,
                profile_id,
            ),
        )
        return self._store._profile_from_row(
            self._store._require_profile_row(self._connection, profile_id)
        )

    def _create_profile(self, request: RemoteProfileCreateV1) -> RemoteProfileV1:
        profile_id = self._store._new_id()
        timestamp = self._store._timestamp()
        document = _canonical_json_bytes(request.model_dump(mode="json"))
        self._connection.execute(
            """
            INSERT INTO remote_profiles(
                profile_id, name, document_json, connection_state,
                credential_slots_json, host_key_fingerprint, resource_version,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'disconnected', ?, NULL, 1, ?, ?)
            """,
            (profile_id, request.name, document, b"[]", timestamp, timestamp),
        )
        return self._store._profile_from_row(
            self._store._require_profile_row(self._connection, profile_id)
        )

    def _create_project(self, request: ProjectCreateV1) -> ProjectV1:
        self._store._require_profile_row(self._connection, request.profile_id)
        project_id = self._store._new_id()
        timestamp = self._store._timestamp()
        self._connection.execute(
            """
            INSERT INTO projects(
                project_id, profile_id, name, document_json, state,
                current_revision_id, resource_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'draft', NULL, 1, ?, ?)
            """,
            (
                project_id,
                request.profile_id,
                request.name,
                _canonical_json_bytes(request.model_dump(mode="json")),
                timestamp,
                timestamp,
            ),
        )
        return self._store._project_from_row(
            self._store._require_project_row(self._connection, project_id)
        )


class DesktopProviderStore:
    """Durable, local-only persistence for Desktop Local API v1 resources."""

    def __init__(
        self,
        state_root: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
        cursor_ttl_seconds: int = DEFAULT_CURSOR_TTL_SECONDS,
        idempotency_retention_seconds: int = DEFAULT_IDEMPOTENCY_RETENTION_SECONDS,
        max_idempotency_records: int = DEFAULT_IDEMPOTENCY_RECORD_LIMIT,
    ) -> None:
        self._require_secure_platform()
        if type(cursor_ttl_seconds) is not int or cursor_ttl_seconds < 1:
            raise ValueError("cursor_ttl_seconds must be a positive integer")
        if type(idempotency_retention_seconds) is not int or idempotency_retention_seconds < 1:
            raise ValueError("idempotency_retention_seconds must be a positive integer")
        if type(max_idempotency_records) is not int or max_idempotency_records < 1:
            raise ValueError("max_idempotency_records must be a positive integer")

        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cursor_ttl_seconds = cursor_ttl_seconds
        self._idempotency_retention_seconds = idempotency_retention_seconds
        self._max_idempotency_records = max_idempotency_records
        self._closed = False
        self._transaction_lock = threading.RLock()

        root = Path(os.path.abspath(os.fspath(Path(state_root).expanduser())))
        self._create_or_validate_root(root)
        self._state_root = root
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self._root_fd = os.open(root, flags)
        except OSError as exc:
            raise ProviderStateRootError(
                "provider state root could not be securely opened"
            ) from exc
        root_stat = os.fstat(self._root_fd)
        self._root_identity = (root_stat.st_dev, root_stat.st_ino)

        try:
            self._ensure_empty_private_file(OWNER_LOCK_FILENAME)
            self._acquire_owner_lock()
            self._ensure_empty_private_file(DATABASE_FILENAME)
            self._ensure_empty_private_file(JOURNAL_FILENAME)
            self._cursor_key = self._load_or_create_cursor_key()
            self._managed_identities = {
                name: self._file_identity(name)
                for name in (
                    DATABASE_FILENAME,
                    JOURNAL_FILENAME,
                    OWNER_LOCK_FILENAME,
                    CURSOR_KEY_FILENAME,
                )
            }
            self._verify_no_wal_side_files()
            self._open_database_fd()
            self._connection = self._open_bound_connection()
            self._migrate()
            self._recover_and_validate()
            self._verify_managed_files()
        except BaseException:
            self._close_resources()
            raise

    @property
    def database_path(self) -> Path:
        return self._state_root / DATABASE_FILENAME

    @property
    def state_root(self) -> Path:
        return self._state_root

    def close(self) -> None:
        if not self._closed:
            self._close_resources()

    def _close_resources(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            try:
                connection.close()
            finally:
                del self._connection
        database_fd = getattr(self, "_database_fd", None)
        if database_fd is not None:
            try:
                os.close(database_fd)
            finally:
                del self._database_fd
        owner_lock_fd = getattr(self, "_owner_lock_fd", None)
        if owner_lock_fd is not None:
            try:
                fcntl.flock(owner_lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(owner_lock_fd)
                del self._owner_lock_fd
        root_fd = getattr(self, "_root_fd", None)
        if root_fd is not None:
            os.close(root_fd)
            del self._root_fd
        self._closed = True

    def __enter__(self) -> DesktopProviderStore:
        self._verify_root()
        return self

    def __exit__(self, *args: object) -> None:
        del args
        self.close()

    def __del__(self) -> None:
        if getattr(self, "_closed", True) is False:
            try:
                self.close()
            except OSError:
                pass

    @staticmethod
    def _require_secure_platform() -> None:
        if (
            not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY")
            or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
            or os.stat not in os.supports_follow_symlinks
        ):
            raise ProviderStateRootError(
                "platform lacks no-follow descriptor-relative provider storage"
            )

    @staticmethod
    def _create_or_validate_root(root: Path) -> None:
        try:
            root_stat = os.lstat(root)
        except FileNotFoundError:
            root.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.mkdir(root, 0o700)
            except FileExistsError:
                root_stat = os.lstat(root)
            else:
                os.chmod(root, 0o700, follow_symlinks=False)
                root_stat = os.lstat(root)
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
            raise ProviderStateRootError("provider state root must be a real directory")
        if hasattr(os, "getuid") and root_stat.st_uid != os.getuid():
            raise ProviderStateRootError("provider state root must be owned by this user")
        if stat.S_IMODE(root_stat.st_mode) != 0o700:
            raise ProviderStateRootError("provider state root mode must be 0700")

    def _verify_root(self) -> None:
        if self._closed:
            raise ProviderStateRootError("provider store is closed")
        try:
            path_stat = os.lstat(self._state_root)
            fd_stat = os.fstat(self._root_fd)
        except OSError as exc:
            raise ProviderStateRootError("provider state root is unavailable") from exc
        if (
            not stat.S_ISDIR(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or (path_stat.st_dev, path_stat.st_ino) != self._root_identity
            or (fd_stat.st_dev, fd_stat.st_ino) != self._root_identity
        ):
            raise ProviderStateRootError("provider state root identity changed")
        if hasattr(os, "getuid") and path_stat.st_uid != os.getuid():
            raise ProviderStateRootError("provider state root ownership changed")
        if stat.S_IMODE(path_stat.st_mode) != 0o700:
            raise ProviderStateRootError("provider state root mode changed")

    def _ensure_empty_private_file(self, name: str) -> None:
        self._verify_root()
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, 0o600, dir_fd=self._root_fd)
        except FileExistsError:
            self._verify_private_file(name)
            return
        except OSError as exc:
            raise ProviderStateRootError(f"could not create private provider file {name}") from exc
        try:
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self._root_fd)

    def _verify_private_file(self, name: str) -> os.stat_result:
        self._verify_root()
        try:
            file_stat = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        except OSError as exc:
            raise ProviderStateRootError(f"private provider file {name} is unavailable") from exc
        if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
            raise ProviderStateRootError(f"private provider file {name} must be regular")
        if file_stat.st_nlink != 1:
            raise ProviderStateRootError(f"private provider file {name} must have one link")
        if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
            raise ProviderStateRootError(f"private provider file {name} has the wrong owner")
        if stat.S_IMODE(file_stat.st_mode) != 0o600:
            raise ProviderStateRootError(f"private provider file {name} mode must be 0600")
        expected = getattr(self, "_managed_identities", {}).get(name)
        if expected is not None and (file_stat.st_dev, file_stat.st_ino) != expected:
            raise ProviderStateRootError(f"private provider file {name} identity changed")
        return file_stat

    def _file_identity(self, name: str) -> tuple[int, int]:
        file_stat = self._verify_private_file(name)
        return file_stat.st_dev, file_stat.st_ino

    def _acquire_owner_lock(self) -> None:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        expected_stat = self._verify_private_file(OWNER_LOCK_FILENAME)
        try:
            descriptor = os.open(OWNER_LOCK_FILENAME, flags, dir_fd=self._root_fd)
        except OSError as exc:
            raise ProviderStateRootError("provider owner lock could not be opened") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise ProviderStateRootError(
                "provider state root is already owned by another process"
            ) from exc
        except OSError:
            os.close(descriptor)
            raise
        descriptor_stat = os.fstat(descriptor)
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
            expected_stat.st_dev,
            expected_stat.st_ino,
        ):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise ProviderStateRootError("provider owner lock identity changed")
        self._owner_lock_fd = descriptor

    def _open_database_fd(self) -> None:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(DATABASE_FILENAME, flags, dir_fd=self._root_fd)
        except OSError as exc:
            raise ProviderStateRootError("provider database could not be securely opened") from exc
        descriptor_stat = os.fstat(descriptor)
        expected = self._managed_identities[DATABASE_FILENAME]
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) != expected:
            os.close(descriptor)
            raise ProviderStateRootError("provider database descriptor identity changed")
        self._database_fd = descriptor

    def _open_bound_connection(self) -> sqlite3.Connection:
        candidates = (
            f"/dev/fd/{self._database_fd}",
            f"/proc/self/fd/{self._database_fd}",
        )
        open_path = next(
            (candidate for candidate in candidates if os.path.exists(candidate)), None
        )
        if open_path is None:
            raise ProviderStateRootError(
                "platform cannot bind SQLite to a securely opened database descriptor"
            )
        connection = sqlite3.connect(
            open_path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            database_rows = connection.execute("PRAGMA database_list").fetchall()
            if len(database_rows) != 1:
                raise ProviderStateRootError("SQLite opened an unexpected database set")
            opened_path = os.path.abspath(cast(str, database_rows[0][2]))
            if opened_path != os.path.abspath(self.database_path):
                raise ProviderStateRootError(
                    "SQLite database descriptor no longer names the managed database"
                )
            self._sqlite_open_path = open_path
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            journal_mode = connection.execute("PRAGMA journal_mode = PERSIST").fetchone()[0]
            if journal_mode != "persist":
                raise ProviderStateRootError("SQLite persistent rollback journal is unavailable")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.execute("PRAGMA trusted_schema = OFF")
        except BaseException:
            connection.close()
            raise
        return connection

    def _load_or_create_cursor_key(self) -> bytes:
        self._verify_root()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        key = secrets.token_bytes(_CURSOR_KEY_BYTES)
        try:
            fd = os.open(CURSOR_KEY_FILENAME, flags, 0o600, dir_fd=self._root_fd)
        except FileExistsError:
            return self._read_cursor_key()
        except OSError as exc:
            raise ProviderStateRootError("could not create cursor signing key") from exc
        try:
            os.fchmod(fd, 0o600)
            if os.write(fd, key) != len(key):
                raise ProviderStateRootError("cursor signing key write was incomplete")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self._root_fd)
        self._verify_private_file(CURSOR_KEY_FILENAME)
        return key

    def _read_cursor_key(self) -> bytes:
        self._verify_private_file(CURSOR_KEY_FILENAME)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(CURSOR_KEY_FILENAME, flags, dir_fd=self._root_fd)
        except OSError as exc:
            raise ProviderStateRootError("could not open cursor signing key") from exc
        try:
            key = os.read(fd, _CURSOR_KEY_BYTES + 1)
        finally:
            os.close(fd)
        if len(key) != _CURSOR_KEY_BYTES:
            raise ProviderStateRootError("cursor signing key has an invalid size")
        return key

    def _verify_managed_files(self) -> None:
        for name in (
            DATABASE_FILENAME,
            JOURNAL_FILENAME,
            OWNER_LOCK_FILENAME,
            CURSOR_KEY_FILENAME,
        ):
            self._verify_private_file(name)
        self._verify_no_wal_side_files()
        database_stat = os.fstat(self._database_fd)
        if (database_stat.st_dev, database_stat.st_ino) != self._managed_identities[
            DATABASE_FILENAME
        ]:
            raise ProviderStateRootError("provider database descriptor identity changed")
        if database_stat.st_size > MAX_DATABASE_BYTES:
            raise ProviderStateRootError("provider database exceeds its recovery byte bound")

    def _verify_no_wal_side_files(self) -> None:
        for name in (WAL_FILENAME, SHM_FILENAME):
            try:
                os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ProviderStateRootError(
                    f"unmanaged SQLite side file {name} could not be inspected"
                ) from exc
            raise ProviderStateRootError(f"unmanaged SQLite side file {name} is forbidden")

    def _migrate(self) -> None:
        connection = self._connection
        try:
            connection.execute("BEGIN EXCLUSIVE")
            version = cast(int, connection.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                existing = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if existing:
                    raise ProviderSchemaError("unversioned provider database is not empty")
                for statement in _SCHEMA_V1:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, self._timestamp()),
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise ProviderSchemaError(f"unsupported provider schema version {version}")
            self._validate_schema(connection)
            connection.commit()
        except ProviderStoreError:
            connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise ProviderSchemaError("provider schema migration validation failed") from exc
        except BaseException:
            connection.rollback()
            raise
        self._verify_managed_files()
        os.fsync(self._root_fd)

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        try:
            actual_rows = _schema_rows(connection)
        except sqlite3.DatabaseError as exc:
            raise ProviderSchemaError("provider schema could not be read") from exc
        actual_digest = sha256(_canonical_json_bytes(actual_rows)).hexdigest()
        if actual_digest != _EXPECTED_SCHEMA_DIGEST or actual_rows != _EXPECTED_SCHEMA_ROWS:
            raise ProviderSchemaError("provider schema fingerprint does not match canonical v1")

    def _recover_and_validate(self) -> None:
        connection = self._connection
        try:
            connection.execute("BEGIN EXCLUSIVE")
            self._validate_schema(connection)
            integrity = connection.execute("PRAGMA integrity_check(1)").fetchall()
            if [tuple(row) for row in integrity] != [("ok",)]:
                raise ProviderDataCorruptionError("provider SQLite integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ProviderDataCorruptionError("provider foreign key check failed")
            self._validate_recovery_budget(connection)
            self._validate_migration_rows(connection)
            for row in connection.execute("SELECT * FROM remote_profiles"):
                self._validate_profile_recovery_row(cast(sqlite3.Row, row))
            for row in connection.execute("SELECT * FROM projects"):
                self._validate_project_recovery_row(cast(sqlite3.Row, row))
            for row in connection.execute("SELECT * FROM idempotency_records"):
                self._validate_idempotency_recovery_row(cast(sqlite3.Row, row))
            timestamp = self._timestamp()
            connection.execute(
                """
                UPDATE remote_profiles
                SET connection_state = 'disconnected',
                    resource_version = resource_version + 1, updated_at = ?
                WHERE connection_state != 'disconnected'
                """,
                (timestamp,),
            )
            connection.execute(
                """
                UPDATE projects
                SET state = CASE WHEN state = 'active' THEN 'draft' ELSE state END,
                    current_revision_id = NULL,
                    resource_version = resource_version + 1, updated_at = ?
                WHERE state = 'active' OR current_revision_id IS NOT NULL
                """,
                (timestamp,),
            )
            connection.commit()
        except ProviderStoreError:
            connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise ProviderDataCorruptionError("provider SQLite recovery failed") from exc
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _validate_recovery_budget(connection: sqlite3.Connection) -> None:
        total_rows = 0
        total_bytes = 0
        specifications = (
            (
                "remote_profiles",
                "profile_id, name, document_json, connection_state, "
                "credential_slots_json, host_key_fingerprint, created_at, updated_at",
            ),
            (
                "projects",
                "project_id, profile_id, name, document_json, state, "
                "current_revision_id, created_at, updated_at",
            ),
            (
                "idempotency_records",
                "principal, method, route, resource_scope, idempotency_key, request_digest, "
                "response_type, response_bytes",
            ),
            ("schema_migrations", "applied_at"),
        )
        for table, columns in specifications:
            length_sum = " + ".join(
                f"coalesce(length(CAST({column.strip()} AS BLOB)), 0)"
                for column in columns.split(",")
            )
            row = connection.execute(
                f"SELECT count(*), coalesce(sum({length_sum}), 0) FROM {table}"
            ).fetchone()
            total_rows += cast(int, row[0])
            total_bytes += cast(int, row[1])
            if total_rows > MAX_RECOVERY_ROWS or total_bytes > MAX_RECOVERY_BYTES:
                raise ProviderDataCorruptionError("provider recovery budget exceeded")

    def _validate_migration_rows(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        if len(rows) != 1 or rows[0]["version"] != SCHEMA_VERSION:
            raise ProviderSchemaError("provider migration ledger does not match schema v1")
        self._validate_persisted_timestamp(cast(str, rows[0]["applied_at"]))

    @staticmethod
    def _validate_persisted_timestamp(value: str) -> datetime:
        if type(value) is not str or _TIMESTAMP_RE.fullmatch(value) is None:
            raise ProviderDataCorruptionError("provider timestamp is not canonical UTC")
        try:
            return datetime.fromisoformat(f"{value[:-1]}+00:00")
        except ValueError as exc:
            raise ProviderDataCorruptionError("provider timestamp is invalid") from exc

    def _validate_common_resource_row(self, row: sqlite3.Row, id_column: str) -> None:
        self._validate_resource_id(cast(str, row[id_column]))
        if type(row["resource_version"]) is not int or row["resource_version"] < 1:
            raise ProviderDataCorruptionError("provider resource version is invalid")
        created_at = self._validate_persisted_timestamp(cast(str, row["created_at"]))
        updated_at = self._validate_persisted_timestamp(cast(str, row["updated_at"]))
        if updated_at < created_at:
            raise ProviderDataCorruptionError("provider resource timestamps are reversed")

    def _validate_profile_recovery_row(self, row: sqlite3.Row) -> None:
        self._validate_common_resource_row(row, "profile_id")
        document = _decode_json_object(bytes(row["document_json"]), label="profile")
        if document.get("name") != row["name"]:
            raise ProviderDataCorruptionError("profile name scalar differs from its document")
        slots_raw = bytes(row["credential_slots_json"])
        try:
            slots = json.loads(slots_raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderDataCorruptionError("stored credential slots are invalid") from exc
        if _canonical_json_bytes(slots) != slots_raw:
            raise ProviderDataCorruptionError("stored credential slots are not canonical")
        self._profile_from_row(row)

    def _validate_project_recovery_row(self, row: sqlite3.Row) -> None:
        self._validate_common_resource_row(row, "project_id")
        document = _decode_json_object(bytes(row["document_json"]), label="project")
        if document.get("name") != row["name"] or document.get("profile_id") != row["profile_id"]:
            raise ProviderDataCorruptionError(
                "project indexed scalars differ from its canonical document"
            )
        self._project_from_row(row)

    def _validate_idempotency_recovery_row(self, row: sqlite3.Row) -> None:
        if row["principal"] != LOCAL_PRINCIPAL:
            raise ProviderDataCorruptionError("idempotency principal is not local")
        for label, column, maximum, minimum in (
            ("method", "method", MAX_IDENTITY_BYTES, 1),
            ("route", "route", MAX_IDENTITY_BYTES, 1),
            ("resource scope", "resource_scope", MAX_IDENTITY_BYTES, 1),
            (
                "idempotency key",
                "idempotency_key",
                MAX_IDEMPOTENCY_KEY_BYTES,
                16,
            ),
        ):
            try:
                self._bounded_identity(label, cast(str, row[column]), maximum, minimum=minimum)
            except ContractValidationError as exc:
                raise ProviderDataCorruptionError("idempotency identity is invalid") from exc
        if _DIGEST_RE.fullmatch(cast(str, row["request_digest"])) is None:
            raise ProviderDataCorruptionError("idempotency request digest is invalid")
        if (
            type(row["created_at_epoch"]) is not int
            or type(row["expires_at_epoch"]) is not int
            or row["expires_at_epoch"] <= row["created_at_epoch"]
        ):
            raise ProviderDataCorruptionError("idempotency retention timestamps are invalid")
        response_model = self._response_model_for_name(cast(str, row["response_type"]))
        response_bytes = bytes(row["response_bytes"])
        response = self._model_from_response(response_model, response_bytes)
        if _canonical_json_bytes(response.model_dump(mode="json")) != response_bytes:
            raise ProviderDataCorruptionError("idempotency response is not canonical")

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        with self._transaction_lock:
            self._verify_managed_files()
            connection = self._connection
            try:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield connection
                if write:
                    self._validate_recovery_budget(connection)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                self._verify_managed_files()

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ProviderStoreError("provider clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _timestamp(self) -> str:
        return self._now().isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _new_id() -> str:
        return secrets.token_urlsafe(_RESOURCE_ID_BYTES)

    @staticmethod
    def _etag(resource_type: str, resource_id: str, version: int) -> str:
        digest = sha256(
            _canonical_json_bytes(
                {"resource_id": resource_id, "resource_type": resource_type, "version": version}
            )
        ).hexdigest()
        return f'"{digest}"'

    @staticmethod
    def _validate_if_match(if_match: str) -> None:
        if type(if_match) is not str or _ETAG_RE.fullmatch(if_match) is None:
            raise ContractValidationError("If-Match is not a Desktop v1 ETag")

    def create_profile(
        self,
        request: RemoteProfileCreateV1 | Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> RemoteProfileV1:
        validated = _validate_model(RemoteProfileCreateV1, request)

        def mutation(transaction: ProviderMutation) -> tuple[int, BaseModel]:
            return 201, transaction._create_profile(validated)

        result = self._execute_idempotent(
            method="POST",
            route="/desktop/v1/profiles",
            resource_scope="profiles",
            key=idempotency_key,
            request_value=validated.model_dump(mode="json"),
            response_model=RemoteProfileV1,
            bound_if_match=None,
            mutation=mutation,
        )
        return self._model_from_response(RemoteProfileV1, result.response_bytes)

    def get_profile(self, profile_id: str) -> RemoteProfileV1:
        self._validate_resource_id(profile_id)
        with self._transaction(write=False) as connection:
            return self._profile_from_row(self._require_profile_row(connection, profile_id))

    def patch_profile(
        self,
        profile_id: str,
        patch: RemoteProfilePatchV1 | Mapping[str, object],
        *,
        if_match: str,
    ) -> RemoteProfileV1:
        self._validate_resource_id(profile_id)
        validated_patch = _validate_model(RemoteProfilePatchV1, patch)
        self._validate_if_match(if_match)
        with self._transaction(write=True) as connection:
            row = self._require_profile_row(connection, profile_id)
            self._require_etag("profile", profile_id, row, if_match)
            protected_connection_fields = {"host", "user", "authentication_kind"}
            if row["connection_state"] != "disconnected" and (
                validated_patch.model_fields_set & protected_connection_fields
            ):
                raise ResourceInUseError("profile", profile_id)
            current = _decode_json_object(bytes(row["document_json"]), label="profile")
            current.update(validated_patch.model_dump(mode="json", exclude_unset=True))
            validated = _validate_json_model(RemoteProfileCreateV1, current)
            version = cast(int, row["resource_version"]) + 1
            timestamp = self._timestamp()
            connection.execute(
                """
                UPDATE remote_profiles
                SET name = ?, document_json = ?, resource_version = ?, updated_at = ?
                WHERE profile_id = ?
                """,
                (
                    validated.name,
                    _canonical_json_bytes(validated.model_dump(mode="json")),
                    version,
                    timestamp,
                    profile_id,
                ),
            )
            return self._profile_from_row(self._require_profile_row(connection, profile_id))

    def delete_profile(self, profile_id: str, *, if_match: str) -> None:
        self._validate_resource_id(profile_id)
        self._validate_if_match(if_match)
        with self._transaction(write=True) as connection:
            row = self._require_profile_row(connection, profile_id)
            self._require_etag("profile", profile_id, row, if_match)
            referenced = connection.execute(
                "SELECT 1 FROM projects WHERE profile_id = ? LIMIT 1", (profile_id,)
            ).fetchone()
            if row["connection_state"] != "disconnected" or referenced is not None:
                raise ResourceInUseError("profile", profile_id)
            connection.execute("DELETE FROM remote_profiles WHERE profile_id = ?", (profile_id,))

    def list_profiles(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        sort: str = "updated_at",
        direction: _Direction = "desc",
        filters: Mapping[str, str] | None = None,
    ) -> RemoteProfilePageV1:
        rows, next_cursor = self._list_rows(
            resource="profiles",
            table="remote_profiles",
            id_column="profile_id",
            limit=limit,
            after=after,
            sort=sort,
            direction=direction,
            filters=filters,
            filter_columns={"connection_state": "connection_state"},
        )
        return RemoteProfilePageV1(
            items=tuple(self._profile_from_row(row) for row in rows),
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    def create_project(
        self,
        request: ProjectCreateV1 | Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> ProjectV1:
        validated = _validate_model(ProjectCreateV1, request)

        def mutation(transaction: ProviderMutation) -> tuple[int, BaseModel]:
            return 201, transaction._create_project(validated)

        result = self._execute_idempotent(
            method="POST",
            route="/desktop/v1/projects",
            resource_scope="projects",
            key=idempotency_key,
            request_value=validated.model_dump(mode="json"),
            response_model=ProjectV1,
            bound_if_match=None,
            mutation=mutation,
        )
        return self._model_from_response(ProjectV1, result.response_bytes)

    def get_project(self, project_id: str) -> ProjectV1:
        self._validate_resource_id(project_id)
        with self._transaction(write=False) as connection:
            return self._project_from_row(self._require_project_row(connection, project_id))

    def patch_project(
        self,
        project_id: str,
        patch: ProjectPatchV1 | Mapping[str, object],
        *,
        if_match: str,
    ) -> ProjectV1:
        self._validate_resource_id(project_id)
        validated_patch = _validate_model(ProjectPatchV1, patch)
        self._validate_if_match(if_match)
        with self._transaction(write=True) as connection:
            row = self._require_project_row(connection, project_id)
            self._require_etag("project", project_id, row, if_match)
            if row["state"] == "active" or row["current_revision_id"] is not None:
                raise ResourceInUseError("project", project_id)
            current = _decode_json_object(bytes(row["document_json"]), label="project")
            current.update(validated_patch.model_dump(mode="json", exclude_unset=True))
            validated = _validate_json_model(ProjectCreateV1, current)
            self._require_profile_row(connection, validated.profile_id)
            version = cast(int, row["resource_version"]) + 1
            timestamp = self._timestamp()
            connection.execute(
                """
                UPDATE projects
                SET profile_id = ?, name = ?, document_json = ?,
                    resource_version = ?, updated_at = ?
                WHERE project_id = ?
                """,
                (
                    validated.profile_id,
                    validated.name,
                    _canonical_json_bytes(validated.model_dump(mode="json")),
                    version,
                    timestamp,
                    project_id,
                ),
            )
            return self._project_from_row(self._require_project_row(connection, project_id))

    def delete_project(self, project_id: str, *, if_match: str) -> None:
        self._validate_resource_id(project_id)
        self._validate_if_match(if_match)
        with self._transaction(write=True) as connection:
            row = self._require_project_row(connection, project_id)
            self._require_etag("project", project_id, row, if_match)
            if row["state"] == "active" or row["current_revision_id"] is not None:
                raise ResourceInUseError("project", project_id)
            connection.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))

    def list_projects(
        self,
        *,
        limit: int = 50,
        after: str | None = None,
        sort: str = "updated_at",
        direction: _Direction = "desc",
        filters: Mapping[str, str] | None = None,
    ) -> ProjectPageV1:
        rows, next_cursor = self._list_rows(
            resource="projects",
            table="projects",
            id_column="project_id",
            limit=limit,
            after=after,
            sort=sort,
            direction=direction,
            filters=filters,
            filter_columns={"state": "state", "profile_id": "profile_id"},
        )
        return ProjectPageV1(
            items=tuple(self._project_from_row(row) for row in rows),
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    def execute_idempotent_action(
        self,
        *,
        route: str,
        resource_scope: str,
        key: str,
        body: BaseModel | Mapping[str, object],
        if_match: str,
        semantic_headers: Mapping[str, str],
        response_model: type[_ModelT],
        mutation: Callable[[ProviderMutation], tuple[int, BaseModel]],
    ) -> IdempotencyResult:
        self._validate_if_match(if_match)
        headers = self._normalize_semantic_headers(semantic_headers)
        headers["if-match"] = if_match
        body_value = body.model_dump(mode="json") if isinstance(body, BaseModel) else dict(body)
        return self._execute_idempotent(
            method="POST",
            route=route,
            resource_scope=resource_scope,
            key=key,
            request_value={"body": body_value, "headers": headers},
            response_model=response_model,
            bound_if_match=if_match,
            mutation=mutation,
        )

    def _execute_idempotent(
        self,
        *,
        method: str,
        route: str,
        resource_scope: str,
        key: str,
        request_value: object,
        response_model: type[_ModelT],
        bound_if_match: str | None,
        mutation: Callable[[ProviderMutation], tuple[int, BaseModel]],
    ) -> IdempotencyResult:
        response_type = self._response_type_name(response_model)
        method = self._bounded_identity("method", method, MAX_IDENTITY_BYTES)
        route = self._bounded_identity("route", route, MAX_IDENTITY_BYTES)
        resource_scope = self._bounded_identity(
            "resource_scope", resource_scope, MAX_IDENTITY_BYTES
        )
        key = self._bounded_identity("idempotency key", key, MAX_IDEMPOTENCY_KEY_BYTES, minimum=16)
        request_bytes = _canonical_json_bytes(request_value)
        if len(request_bytes) > MAX_REQUEST_BYTES:
            raise ContractValidationError("canonical idempotency request exceeds the byte limit")
        request_digest = sha256(request_bytes).hexdigest()
        now_epoch = int(self._now().timestamp())

        with self._transaction(write=True) as connection:
            connection.execute(
                "DELETE FROM idempotency_records WHERE expires_at_epoch <= ?", (now_epoch,)
            )
            existing = connection.execute(
                """
                SELECT request_digest, response_type, status_code, response_bytes
                FROM idempotency_records
                WHERE principal = ? AND method = ? AND route = ?
                  AND resource_scope = ? AND idempotency_key = ?
                """,
                (LOCAL_PRINCIPAL, method, route, resource_scope, key),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise IdempotencyConflictError(
                        "idempotency key is already bound to a different request"
                    )
                if existing["response_type"] != response_type:
                    raise ProviderDataCorruptionError(
                        "idempotency replay response type differs from the typed route"
                    )
                response_bytes = bytes(existing["response_bytes"])
                replay_response = self._model_from_response(response_model, response_bytes)
                if (
                    _canonical_json_bytes(replay_response.model_dump(mode="json"))
                    != response_bytes
                ):
                    raise ProviderDataCorruptionError(
                        "stored idempotency replay response is not canonical"
                    )
                return IdempotencyResult(
                    status_code=cast(int, existing["status_code"]),
                    response_bytes=response_bytes,
                    replayed=True,
                )

            count = cast(
                int, connection.execute("SELECT count(*) FROM idempotency_records").fetchone()[0]
            )
            if count >= self._max_idempotency_records:
                raise IdempotencyCapacityError("live idempotency record capacity is exhausted")

            status_code, response = mutation(
                ProviderMutation(self, connection, if_match=bound_if_match)
            )
            if type(status_code) is not int or not 100 <= status_code <= 599:
                raise ProviderStoreError("idempotent mutation returned an invalid status code")
            if (
                type(response) is not response_model
                or type(response).__module__ != "desktop.sidecar.contracts.v1.models"
                or response.model_config.get("extra") != "forbid"
            ):
                raise ProviderStoreError(
                    "idempotent mutation must return a closed Desktop v1 response model"
                )
            response_bytes = _canonical_json_bytes(response.model_dump(mode="json"))
            if len(response_bytes) > MAX_RESPONSE_BYTES:
                raise ProviderStoreError("idempotent response exceeds the byte limit")
            self._insert_idempotency_record(
                connection,
                principal=LOCAL_PRINCIPAL,
                method=method,
                route=route,
                resource_scope=resource_scope,
                key=key,
                request_digest=request_digest,
                response_type=response_type,
                status_code=status_code,
                response_bytes=response_bytes,
                now_epoch=now_epoch,
            )
            return IdempotencyResult(status_code, response_bytes, False)

    @staticmethod
    def _normalize_semantic_headers(headers: Mapping[str, str]) -> dict[str, str]:
        if not isinstance(headers, Mapping):
            raise ContractValidationError("semantic headers must be a mapping")
        normalized: dict[str, str] = {}
        forbidden = {"authorization", "cookie", "idempotency-key", "if-match"}
        for key, value in headers.items():
            normalized_key = key.lower() if type(key) is str else ""
            if (
                type(key) is not str
                or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", normalized_key) is None
                or normalized_key in forbidden
                or "session-token" in normalized_key
                or normalized_key in normalized
            ):
                raise ContractValidationError(
                    "semantic header name is invalid or credential-bearing"
                )
            normalized[normalized_key] = DesktopProviderStore._bounded_identity(
                "semantic header value", value, MAX_IDENTITY_BYTES
            )
        return dict(sorted(normalized.items()))

    @staticmethod
    def _response_type_name(model_type: type[BaseModel]) -> str:
        allowed = {RemoteProfileV1: "RemoteProfileV1", ProjectV1: "ProjectV1"}
        try:
            return allowed[model_type]
        except KeyError as exc:
            raise ContractValidationError(
                "idempotent response model is not a persisted Desktop resource type"
            ) from exc

    @staticmethod
    def _response_model_for_name(name: str) -> type[BaseModel]:
        allowed: dict[str, type[BaseModel]] = {
            "RemoteProfileV1": RemoteProfileV1,
            "ProjectV1": ProjectV1,
        }
        try:
            return allowed[name]
        except KeyError as exc:
            raise ProviderDataCorruptionError("idempotency response type is invalid") from exc

    def _insert_idempotency_record(
        self,
        connection: sqlite3.Connection,
        *,
        principal: str,
        method: str,
        route: str,
        resource_scope: str,
        key: str,
        request_digest: str,
        response_type: str,
        status_code: int,
        response_bytes: bytes,
        now_epoch: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO idempotency_records(
                principal, method, route, resource_scope, idempotency_key,
                request_digest, response_type, status_code, response_bytes,
                created_at_epoch, expires_at_epoch
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                principal,
                method,
                route,
                resource_scope,
                key,
                request_digest,
                response_type,
                status_code,
                response_bytes,
                now_epoch,
                now_epoch + self._idempotency_retention_seconds,
            ),
        )

    @staticmethod
    def _bounded_identity(label: str, value: str, maximum: int, *, minimum: int = 1) -> str:
        if (
            type(value) is not str
            or value != value.strip()
            or any(ord(char) < 0x20 for char in value)
        ):
            raise ContractValidationError(f"{label} must be trimmed text without controls")
        size = len(value.encode("utf-8"))
        if not minimum <= size <= maximum:
            raise ContractValidationError(f"{label} exceeds its byte bounds")
        return value

    @staticmethod
    def _validate_resource_id(resource_id: str) -> None:
        DesktopProviderStore._bounded_identity("resource id", resource_id, 256)

    @staticmethod
    def _model_from_response(model_type: type[_ModelT], response_bytes: bytes) -> _ModelT:
        try:
            return model_type.model_validate_json(response_bytes)
        except ValidationError as exc:
            raise ProviderDataCorruptionError(
                f"stored idempotent {model_type.__name__} response is invalid"
            ) from exc

    @staticmethod
    def _require_profile_row(connection: sqlite3.Connection, profile_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM remote_profiles WHERE profile_id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("profile", profile_id)
        return cast(sqlite3.Row, row)

    @staticmethod
    def _require_project_row(connection: sqlite3.Connection, project_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("project", project_id)
        return cast(sqlite3.Row, row)

    def _profile_from_row(self, row: sqlite3.Row) -> RemoteProfileV1:
        document = _decode_json_object(bytes(row["document_json"]), label="profile")
        try:
            slots = json.loads(bytes(row["credential_slots_json"]))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderDataCorruptionError("stored credential slot status is invalid") from exc
        payload = {
            **document,
            "profile_id": row["profile_id"],
            "credential_slots": slots,
            "connection_state": row["connection_state"],
            "host_key_fingerprint": row["host_key_fingerprint"],
            "etag": self._etag("profile", row["profile_id"], row["resource_version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        return _validate_json_model(RemoteProfileV1, payload)

    def _project_from_row(self, row: sqlite3.Row) -> ProjectV1:
        document = _decode_json_object(bytes(row["document_json"]), label="project")
        payload = {
            **document,
            "project_id": row["project_id"],
            "state": row["state"],
            "current_revision_id": row["current_revision_id"],
            "etag": self._etag("project", row["project_id"], row["resource_version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        return _validate_json_model(ProjectV1, payload)

    def _require_etag(
        self,
        resource_type: str,
        resource_id: str,
        row: sqlite3.Row,
        if_match: str,
    ) -> None:
        current = self._etag(resource_type, resource_id, row["resource_version"])
        if not hmac.compare_digest(current, if_match):
            raise ETagConflictError(resource_type, resource_id, current)

    def _list_rows(
        self,
        *,
        resource: Literal["profiles", "projects"],
        table: Literal["remote_profiles", "projects"],
        id_column: Literal["profile_id", "project_id"],
        limit: int,
        after: str | None,
        sort: str,
        direction: _Direction,
        filters: Mapping[str, str] | None,
        filter_columns: Mapping[str, str],
    ) -> tuple[list[sqlite3.Row], str | None]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ContractValidationError("pagination limit must be between 1 and 100")
        sort_columns = {"created_at": "created_at", "updated_at": "updated_at", "name": "name"}
        if sort not in sort_columns:
            raise ContractValidationError("unsupported provider sort key")
        if direction not in {"asc", "desc"}:
            raise ContractValidationError("pagination direction must be asc or desc")
        normalized_filters = self._normalize_filters(filters, filter_columns)
        anchor: tuple[str, str] | None = None
        if after is not None:
            anchor = self._decode_cursor(
                after,
                resource=resource,
                filters=normalized_filters,
                sort=sort,
                direction=direction,
            )

        clauses: list[str] = []
        parameters: list[object] = []
        for key, value in normalized_filters.items():
            clauses.append(f"{filter_columns[key]} = ?")
            parameters.append(value)
        sort_column = sort_columns[sort]
        sql_direction = "ASC" if direction == "asc" else "DESC"
        with self._transaction(write=False) as connection:
            if anchor is not None:
                anchor_value, anchor_id = anchor
                operator = ">" if direction == "asc" else "<"
                clauses.append(
                    f"({sort_column} {operator} ? OR "
                    f"({sort_column} = ? AND {id_column} {operator} ?))"
                )
                parameters.extend((anchor_value, anchor_value, anchor_id))
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            parameters.append(limit + 1)
            fetched = connection.execute(
                f"SELECT * FROM {table}{where} "
                f"ORDER BY {sort_column} {sql_direction}, {id_column} {sql_direction} LIMIT ?",
                tuple(parameters),
            ).fetchall()
        has_more = len(fetched) > limit
        rows = list(fetched[:limit])
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = self._encode_cursor(
                resource=resource,
                filters=normalized_filters,
                sort=sort,
                direction=direction,
                anchor_id=last[id_column],
                sort_value=last[sort_column],
            )
        return rows, next_cursor

    @staticmethod
    def _normalize_filters(
        filters: Mapping[str, str] | None, supported: Mapping[str, str]
    ) -> dict[str, str]:
        if filters is None:
            return {}
        normalized: dict[str, str] = {}
        for key, value in filters.items():
            if key not in supported or type(value) is not str:
                raise ContractValidationError("unsupported provider pagination filter")
            if len(value.encode("utf-8")) > 256 or "\x00" in value:
                raise ContractValidationError("provider pagination filter exceeds its bounds")
            normalized[key] = value
        return dict(sorted(normalized.items()))

    def _encode_cursor(
        self,
        *,
        resource: str,
        filters: Mapping[str, str],
        sort: str,
        direction: _Direction,
        anchor_id: str,
        sort_value: str,
    ) -> str:
        query_digest = sha256(
            _canonical_json_bytes(
                {
                    "direction": direction,
                    "filters": dict(filters),
                    "resource": resource,
                    "sort": sort,
                }
            )
        ).digest()[:16]
        payload = _canonical_json_bytes(
            {
                "e": int(self._now().timestamp()) + self._cursor_ttl_seconds,
                "i": anchor_id,
                "q": self._b64encode(query_digest),
                "s": {"t": "s", "v": sort_value},
                "v": 2,
            }
        )
        encoded = self._b64encode(payload)
        signature = self._b64encode(hmac.digest(self._cursor_key, payload, "sha256"))
        cursor = f"{encoded}.{signature}"
        if len(cursor.encode("ascii")) > MAX_RENDERED_CURSOR_BYTES:
            raise ProviderStoreError("provider cursor exceeds its byte limit")
        return cursor

    def _decode_cursor(
        self,
        cursor: str,
        *,
        resource: str,
        filters: Mapping[str, str],
        sort: str,
        direction: _Direction,
    ) -> tuple[str, str]:
        if type(cursor) is not str or not 1 <= len(cursor.encode("utf-8")) <= MAX_CURSOR_BYTES:
            raise CursorInvalidError("provider cursor is outside its byte bounds")
        parts = cursor.split(".")
        if len(parts) != 2:
            raise CursorInvalidError("provider cursor has an invalid envelope")
        try:
            payload_bytes = self._b64decode(parts[0])
            signature = self._b64decode(parts[1])
        except ValueError as exc:
            raise CursorInvalidError("provider cursor encoding is invalid") from exc
        expected_signature = hmac.digest(self._cursor_key, payload_bytes, "sha256")
        if not hmac.compare_digest(expected_signature, signature):
            raise CursorInvalidError("provider cursor signature is invalid")
        try:
            payload = json.loads(payload_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CursorInvalidError("provider cursor payload is invalid") from exc
        if type(payload) is not dict or set(payload) != {"e", "i", "q", "s", "v"}:
            raise CursorInvalidError("provider cursor payload is not closed")
        if _canonical_json_bytes(payload) != payload_bytes:
            raise CursorInvalidError("provider cursor payload is not canonical")
        query_digest = self._b64encode(
            sha256(
                _canonical_json_bytes(
                    {
                        "direction": direction,
                        "filters": dict(filters),
                        "resource": resource,
                        "sort": sort,
                    }
                )
            ).digest()[:16]
        )
        if (
            payload["v"] != 2
            or payload["q"] != query_digest
            or type(payload["e"]) is not int
            or type(payload["i"]) is not str
            or type(payload["s"]) is not dict
            or set(payload["s"]) != {"t", "v"}
            or payload["s"]["t"] != "s"
            or type(payload["s"]["v"]) is not str
        ):
            raise CursorInvalidError("provider cursor is bound to another query")
        try:
            self._validate_resource_id(cast(str, payload["i"]))
        except ContractValidationError as exc:
            raise CursorInvalidError("provider cursor resource boundary is invalid") from exc
        if len(payload["s"]["v"].encode("utf-8")) > 4096:
            raise CursorInvalidError("provider cursor sort boundary exceeds its byte bound")
        if payload["e"] <= int(self._now().timestamp()):
            raise CursorExpiredError("provider cursor has expired")
        return cast(str, payload["s"]["v"]), cast(str, payload["i"])

    @staticmethod
    def _b64encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _b64decode(value: str) -> bytes:
        if not value or _B64_RE.fullmatch(value) is None:
            raise ValueError("invalid base64url")
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        if DesktopProviderStore._b64encode(decoded) != value:
            raise ValueError("non-canonical base64url")
        return decoded


__all__ = (
    "ContractValidationError",
    "CursorExpiredError",
    "CursorInvalidError",
    "DesktopProviderStore",
    "ETagConflictError",
    "IdempotencyCapacityError",
    "IdempotencyConflictError",
    "IdempotencyResult",
    "ProviderDataCorruptionError",
    "ProviderMutation",
    "ProviderSchemaError",
    "ProviderStateRootError",
    "ProviderStoreError",
    "ResourceInUseError",
    "ResourceNotFoundError",
)
