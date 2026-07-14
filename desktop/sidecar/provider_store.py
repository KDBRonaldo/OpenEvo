from __future__ import annotations

import base64
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
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
CURSOR_KEY_FILENAME = "cursor-signing.key"
MAX_REQUEST_BYTES = 1_048_576
MAX_RESPONSE_BYTES = 2_097_152
MAX_CURSOR_BYTES = 2_048
MAX_RENDERED_CURSOR_BYTES = 256
MAX_IDEMPOTENCY_KEY_BYTES = 256
MAX_IDENTITY_BYTES = 512
DEFAULT_IDEMPOTENCY_RECORD_LIMIT = 10_000
DEFAULT_IDEMPOTENCY_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_CURSOR_TTL_SECONDS = 15 * 60

_RESOURCE_ID_BYTES = 32
_CURSOR_KEY_BYTES = 32
_ETAG_RE = re.compile(r'^"[0-9a-f]{64}"$')
_B64_RE = re.compile(r"^[A-Za-z0-9_-]+$")


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
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
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
        status_code INTEGER NOT NULL CHECK (status_code BETWEEN 100 AND 599),
        response_bytes BLOB NOT NULL,
        created_at_epoch INTEGER NOT NULL,
        expires_at_epoch INTEGER NOT NULL,
        PRIMARY KEY (principal, method, route, resource_scope, idempotency_key),
        CHECK (length(CAST(principal AS BLOB)) BETWEEN 1 AND 512),
        CHECK (length(CAST(method AS BLOB)) BETWEEN 1 AND 512),
        CHECK (length(CAST(route AS BLOB)) BETWEEN 1 AND 512),
        CHECK (length(CAST(resource_scope AS BLOB)) BETWEEN 1 AND 512),
        CHECK (length(CAST(idempotency_key AS BLOB)) BETWEEN 16 AND 256),
        CHECK (length(request_digest) = 64),
        CHECK (length(response_bytes) <= 2097152),
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
    "CREATE INDEX idempotency_expiry_idx ON idempotency_records(expires_at_epoch)",
)

_EXPECTED_TABLES = {
    "schema_migrations",
    "remote_profiles",
    "projects",
    "idempotency_records",
}


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
        raise ProviderDataCorruptionError(
            f"stored data violates {model_type.__name__}"
        ) from exc


class ProviderMutation:
    """Restricted state changes available inside an idempotent store transaction."""

    __slots__ = ("_connection", "_store")

    def __init__(self, store: DesktopProviderStore, connection: sqlite3.Connection) -> None:
        self._store = store
        self._connection = connection

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
        if state not in {"draft", "active", "archived", "blocked"}:
            raise ContractValidationError("project state is not a Desktop v1 state")
        if current_revision_id is not None:
            self._store._validate_resource_id(current_revision_id)
        row = self._store._require_project_row(self._connection, project_id)
        self._store._require_etag("project", project_id, row, if_match)
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
        if connection_state not in {
            "disconnected",
            "connecting",
            "host_key_required",
            "connected",
            "failed",
        }:
            raise ContractValidationError(
                "profile connection state is not a Desktop v1 state"
            )
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

        root = Path(os.path.abspath(os.fspath(Path(state_root).expanduser())))
        self._create_or_validate_root(root)
        self._state_root = root
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self._root_fd = os.open(root, flags)
        except OSError as exc:
            raise ProviderStateRootError("provider state root could not be securely opened") from exc
        root_stat = os.fstat(self._root_fd)
        self._root_identity = (root_stat.st_dev, root_stat.st_ino)

        try:
            self._ensure_empty_private_file(DATABASE_FILENAME)
            self._cursor_key = self._load_or_create_cursor_key()
            self._managed_identities = {
                name: self._file_identity(name)
                for name in (DATABASE_FILENAME, CURSOR_KEY_FILENAME)
            }
            self._migrate()
            self._verify_managed_files()
        except BaseException:
            os.close(self._root_fd)
            self._closed = True
            raise

    @property
    def database_path(self) -> Path:
        return self._state_root / DATABASE_FILENAME

    @property
    def state_root(self) -> Path:
        return self._state_root

    def close(self) -> None:
        if not self._closed and hasattr(self, "_root_fd"):
            os.close(self._root_fd)
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
        self._verify_private_file(DATABASE_FILENAME)
        self._verify_private_file(CURSOR_KEY_FILENAME)

    def _connect(self) -> sqlite3.Connection:
        self._verify_managed_files()
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
            check_same_thread=True,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.execute("PRAGMA trusted_schema = OFF")
        except BaseException:
            connection.close()
            raise
        return connection

    def _migrate(self) -> None:
        connection = self._connect()
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
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._verify_managed_files()
        os.fsync(self._root_fd)

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if tables != _EXPECTED_TABLES:
            raise ProviderSchemaError("provider database table set does not match schema v1")
        migrations = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        if [row[0] for row in migrations] != [SCHEMA_VERSION]:
            raise ProviderSchemaError("provider migration ledger does not match schema v1")

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
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
        principal: str,
        idempotency_key: str,
    ) -> RemoteProfileV1:
        validated = _validate_model(RemoteProfileCreateV1, request)

        def mutation(transaction: ProviderMutation) -> tuple[int, BaseModel]:
            return 201, transaction._create_profile(validated)

        result = self.execute_idempotent(
            principal=principal,
            method="POST",
            route="/desktop/v1/profiles",
            resource_scope="profiles",
            key=idempotency_key,
            request=validated,
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
            connection.execute(
                "DELETE FROM remote_profiles WHERE profile_id = ?", (profile_id,)
            )

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
        principal: str,
        idempotency_key: str,
    ) -> ProjectV1:
        validated = _validate_model(ProjectCreateV1, request)

        def mutation(transaction: ProviderMutation) -> tuple[int, BaseModel]:
            return 201, transaction._create_project(validated)

        result = self.execute_idempotent(
            principal=principal,
            method="POST",
            route="/desktop/v1/projects",
            resource_scope="projects",
            key=idempotency_key,
            request=validated,
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

    def execute_idempotent(
        self,
        *,
        principal: str,
        method: str,
        route: str,
        resource_scope: str,
        key: str,
        request: BaseModel | Mapping[str, object],
        mutation: Callable[[ProviderMutation], tuple[int, BaseModel]],
    ) -> IdempotencyResult:
        principal = self._bounded_identity("principal", principal, MAX_IDENTITY_BYTES)
        method = self._bounded_identity("method", method, MAX_IDENTITY_BYTES)
        route = self._bounded_identity("route", route, MAX_IDENTITY_BYTES)
        resource_scope = self._bounded_identity(
            "resource_scope", resource_scope, MAX_IDENTITY_BYTES
        )
        key = self._bounded_identity(
            "idempotency key", key, MAX_IDEMPOTENCY_KEY_BYTES, minimum=16
        )
        request_value = (
            request.model_dump(mode="json") if isinstance(request, BaseModel) else dict(request)
        )
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
                SELECT request_digest, status_code, response_bytes
                FROM idempotency_records
                WHERE principal = ? AND method = ? AND route = ?
                  AND resource_scope = ? AND idempotency_key = ?
                """,
                (principal, method, route, resource_scope, key),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise IdempotencyConflictError(
                        "idempotency key is already bound to a different request"
                    )
                return IdempotencyResult(
                    status_code=cast(int, existing["status_code"]),
                    response_bytes=bytes(existing["response_bytes"]),
                    replayed=True,
                )

            count = cast(
                int, connection.execute("SELECT count(*) FROM idempotency_records").fetchone()[0]
            )
            if count >= self._max_idempotency_records:
                raise IdempotencyCapacityError("live idempotency record capacity is exhausted")

            status_code, response = mutation(ProviderMutation(self, connection))
            if type(status_code) is not int or not 100 <= status_code <= 599:
                raise ProviderStoreError("idempotent mutation returned an invalid status code")
            if (
                not isinstance(response, BaseModel)
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
                principal=principal,
                method=method,
                route=route,
                resource_scope=resource_scope,
                key=key,
                request_digest=request_digest,
                status_code=status_code,
                response_bytes=response_bytes,
                now_epoch=now_epoch,
            )
            return IdempotencyResult(status_code, response_bytes, False)

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
        status_code: int,
        response_bytes: bytes,
        now_epoch: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO idempotency_records(
                principal, method, route, resource_scope, idempotency_key,
                request_digest, status_code, response_bytes,
                created_at_epoch, expires_at_epoch
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                principal,
                method,
                route,
                resource_scope,
                key,
                request_digest,
                status_code,
                response_bytes,
                now_epoch,
                now_epoch + self._idempotency_retention_seconds,
            ),
        )

    @staticmethod
    def _bounded_identity(label: str, value: str, maximum: int, *, minimum: int = 1) -> str:
        if type(value) is not str or value != value.strip() or any(ord(char) < 0x20 for char in value):
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
    def _require_profile_row(
        connection: sqlite3.Connection, profile_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM remote_profiles WHERE profile_id = ?", (profile_id,)
        ).fetchone()
        if row is None:
            raise ResourceNotFoundError("profile", profile_id)
        return cast(sqlite3.Row, row)

    @staticmethod
    def _require_project_row(
        connection: sqlite3.Connection, project_id: str
    ) -> sqlite3.Row:
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
        anchor_id = None
        if after is not None:
            anchor_id = self._decode_cursor(
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
            if anchor_id is not None:
                anchor_row = connection.execute(
                    f"SELECT {sort_column} FROM {table} WHERE {id_column} = ?",
                    (anchor_id,),
                ).fetchone()
                if anchor_row is None:
                    raise CursorInvalidError("provider cursor anchor no longer exists")
                anchor_value = anchor_row[sort_column]
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
        ).digest()
        payload = _canonical_json_bytes(
            {
                "a": anchor_id,
                "e": int(self._now().timestamp()) + self._cursor_ttl_seconds,
                "q": self._b64encode(query_digest),
                "v": 1,
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
    ) -> str:
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
        if type(payload) is not dict or set(payload) != {"a", "e", "q", "v"}:
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
            ).digest()
        )
        if (
            payload["v"] != 1
            or payload["q"] != query_digest
            or type(payload["e"]) is not int
            or type(payload["a"]) is not str
        ):
            raise CursorInvalidError("provider cursor is bound to another query")
        if payload["e"] <= int(self._now().timestamp()):
            raise CursorExpiredError("provider cursor has expired")
        return cast(str, payload["a"])

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
