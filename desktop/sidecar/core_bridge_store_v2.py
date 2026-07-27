"""Private durable authority store for the Desktop/Core v2 bridge."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
from typing import cast

from pydantic import ValidationError
from pydantic import TypeAdapter

from desktop.sidecar.core_bridge_v2 import (
    CoreBridgeMutationStateV2,
    CoreBridgeMutationV2,
    CoreProjectMappingV2,
    core_bridge_mutation_document_v2,
    core_project_mapping_document_v2,
    core_project_mapping_sha256_v2,
)
from openevo.backend.contracts.v2 import models as core_v2


_OPAQUE_ID = TypeAdapter(core_v2.OpaqueId)


STORE_NAMESPACE = "openevo.desktop.core_bridge.v2"
SCHEMA_VERSION = 1
DATABASE_FILENAME = "core-bridge-v2.sqlite3"
JOURNAL_FILENAME = f"{DATABASE_FILENAME}-journal"
WAL_FILENAME = f"{DATABASE_FILENAME}-wal"
SHM_FILENAME = f"{DATABASE_FILENAME}-shm"
OWNER_LOCK_FILENAME = "core-bridge-v2.lock"

MAX_DATABASE_BYTES = 128 * 1024 * 1024
MAX_JOURNAL_BYTES = 256 * 1024 * 1024
MAX_MAPPING_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_MUTATION_DOCUMENT_BYTES = 128 * 1024
MAX_RECOVERY_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_MAPPINGS = 100
DEFAULT_MAX_MAPPING_HISTORY = 10_000
DEFAULT_MAX_MUTATIONS = 10_000


class CoreBridgeStoreV2Error(RuntimeError):
    """Base class for closed v2 bridge persistence failures."""


class CoreBridgeStoreStateV2Error(CoreBridgeStoreV2Error):
    """The private state root or database identity is unsafe."""


class CoreBridgeStoreSchemaV2Error(CoreBridgeStoreV2Error):
    """The exact SQLite schema is absent, unsupported, or drifted."""


class CoreBridgeStoreDataV2Error(CoreBridgeStoreV2Error):
    """Persisted authority does not satisfy the closed v2 graph."""


class CoreBridgeStoreConflictV2(CoreBridgeStoreV2Error):
    """A compare-and-set, owner, or replay identity conflicts."""


class CoreBridgeStoreCapacityV2Error(CoreBridgeStoreV2Error):
    """A process or recovery capacity is exhausted."""


_SCHEMA_STATEMENTS = (
    f"""
    CREATE TABLE schema_metadata (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        namespace TEXT NOT NULL CHECK (namespace = '{STORE_NAMESPACE}'),
        schema_version INTEGER NOT NULL CHECK (schema_version = {SCHEMA_VERSION}),
        schema_sha256 TEXT NOT NULL CHECK (length(schema_sha256) = 64)
    ) STRICT
    """,
    f"""
    CREATE TABLE mappings (
        desktop_project_id TEXT PRIMARY KEY
            CHECK (length(CAST(desktop_project_id AS BLOB)) BETWEEN 1 AND 128),
        profile_id TEXT NOT NULL
            CHECK (length(CAST(profile_id AS BLOB)) BETWEEN 1 AND 128),
        core_project_id TEXT NOT NULL UNIQUE
            CHECK (length(CAST(core_project_id AS BLOB)) BETWEEN 1 AND 128),
        mapping_generation INTEGER NOT NULL
            CHECK (mapping_generation BETWEEN 1 AND 9007199254740991),
        document_sha256 TEXT NOT NULL CHECK (length(document_sha256) = 64),
        document_json BLOB NOT NULL
            CHECK (length(document_json) BETWEEN 2 AND {MAX_MAPPING_DOCUMENT_BYTES})
    ) STRICT
    """,
    f"""
    CREATE TABLE mapping_history (
        desktop_project_id TEXT NOT NULL
            CHECK (length(CAST(desktop_project_id AS BLOB)) BETWEEN 1 AND 128),
        mapping_generation INTEGER NOT NULL
            CHECK (mapping_generation BETWEEN 1 AND 9007199254740991),
        predecessor_mapping_sha256 TEXT,
        document_sha256 TEXT NOT NULL CHECK (length(document_sha256) = 64),
        document_json BLOB NOT NULL
            CHECK (length(document_json) BETWEEN 2 AND {MAX_MAPPING_DOCUMENT_BYTES}),
        PRIMARY KEY (desktop_project_id, mapping_generation),
        UNIQUE (document_sha256),
        CHECK (
            (mapping_generation = 1 AND predecessor_mapping_sha256 IS NULL) OR
            (mapping_generation > 1 AND length(predecessor_mapping_sha256) = 64)
        )
    ) STRICT
    """,
    f"""
    CREATE TABLE mutation_replays (
        desktop_project_id TEXT NOT NULL
            CHECK (length(CAST(desktop_project_id AS BLOB)) BETWEEN 1 AND 128),
        operation TEXT NOT NULL
            CHECK (length(CAST(operation AS BLOB)) BETWEEN 1 AND 128),
        idempotency_key TEXT NOT NULL
            CHECK (length(CAST(idempotency_key AS BLOB)) BETWEEN 16 AND 256),
        state TEXT NOT NULL CHECK (state IN ('prepared', 'unknown', 'applied')),
        request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
        document_sha256 TEXT NOT NULL CHECK (length(document_sha256) = 64),
        document_json BLOB NOT NULL
            CHECK (length(document_json) BETWEEN 2 AND {MAX_MUTATION_DOCUMENT_BYTES}),
        PRIMARY KEY (desktop_project_id, operation, idempotency_key)
    ) STRICT
    """,
    "CREATE INDEX mapping_history_project_idx ON mapping_history(desktop_project_id, mapping_generation)",
    "CREATE INDEX mutation_replays_project_idx ON mutation_replays(desktop_project_id, operation)",
)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _schema_rows(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
    )


def _expected_schema() -> tuple[tuple[tuple[object, ...], ...], str]:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in _SCHEMA_STATEMENTS:
            connection.execute(statement)
        rows = _schema_rows(connection)
    finally:
        connection.close()
    return rows, hashlib.sha256(_canonical_json_bytes(rows)).hexdigest()


EXPECTED_SCHEMA_ROWS, EXPECTED_SCHEMA_SHA256 = _expected_schema()


class DesktopCoreBridgeStoreV2:
    """Exact CAS store for mappings, history, and mutation replay authority."""

    def __init__(
        self,
        state_root: Path,
        *,
        max_mappings: int = DEFAULT_MAX_MAPPINGS,
        max_mapping_history: int = DEFAULT_MAX_MAPPING_HISTORY,
        max_mutations: int = DEFAULT_MAX_MUTATIONS,
    ) -> None:
        if (
            not isinstance(state_root, Path)
            or type(max_mappings) is not int
            or not 1 <= max_mappings <= 10_000
            or type(max_mapping_history) is not int
            or not 1 <= max_mapping_history <= 100_000
            or type(max_mutations) is not int
            or not 1 <= max_mutations <= 100_000
        ):
            raise ValueError("Core bridge store configuration is invalid")
        self._require_secure_platform()
        self._state_root = state_root.absolute()
        self._max_mappings = max_mappings
        self._max_mapping_history = max_mapping_history
        self._max_mutations = max_mutations
        self._lock = threading.RLock()
        self._closed = False
        self._root_fd = -1
        self._owner_lock_fd = -1
        self._connection: sqlite3.Connection | None = None
        try:
            self._create_or_validate_root(self._state_root)
            self._root_fd = os.open(
                self._state_root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            root_stat = os.fstat(self._root_fd)
            self._root_identity = (root_stat.st_dev, root_stat.st_ino)
            self._ensure_private_file(OWNER_LOCK_FILENAME)
            self._ensure_private_file(DATABASE_FILENAME)
            self._acquire_owner_lock()
            database = self._verify_private_file(DATABASE_FILENAME)
            self._database_identity = (database.st_dev, database.st_ino)
            self._connection = self._open_database()
            self._migrate()
            self._recover_and_validate()
        except BaseException:
            self._close_resources()
            raise

    @property
    def state_root(self) -> Path:
        return self._state_root

    @property
    def database_path(self) -> Path:
        return self._state_root / DATABASE_FILENAME

    @property
    def schema_fingerprint(self) -> str:
        return EXPECTED_SCHEMA_SHA256

    def __enter__(self) -> DesktopCoreBridgeStoreV2:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close(suppress_errors=True)
        except BaseException:
            pass

    def close(self, *, suppress_errors: bool = False) -> None:
        try:
            self._close_resources()
        except BaseException:
            if not suppress_errors:
                raise

    def load_mapping(self, desktop_project_id: str) -> CoreProjectMappingV2 | None:
        _validate_id(desktop_project_id)
        with self._transaction(write=False) as connection:
            row = connection.execute(
                """
                SELECT desktop_project_id, profile_id, core_project_id,
                       mapping_generation, document_sha256,
                       length(CAST(document_json AS BLOB)) AS document_size
                FROM mappings WHERE desktop_project_id = ?
                """,
                (desktop_project_id,),
            ).fetchone()
            if row is None:
                return None
            return self._mapping_from_sized_row(connection, "mappings", row)

    def load_mapping_by_core_project_id(
        self,
        core_project_id: str,
    ) -> CoreProjectMappingV2 | None:
        """Resolve a renderer-visible Core project to its private Desktop identity."""

        _validate_id(core_project_id)
        with self._transaction(write=False) as connection:
            row = connection.execute(
                """
                SELECT desktop_project_id, profile_id, core_project_id,
                       mapping_generation, document_sha256,
                       length(CAST(document_json AS BLOB)) AS document_size
                FROM mappings WHERE core_project_id = ?
                """,
                (core_project_id,),
            ).fetchone()
            if row is None:
                return None
            return self._mapping_from_sized_row(connection, "mappings", row)

    def load_mapping_history(self, desktop_project_id: str) -> tuple[CoreProjectMappingV2, ...]:
        _validate_id(desktop_project_id)
        with self._transaction(write=False) as connection:
            rows = connection.execute(
                """
                SELECT desktop_project_id, mapping_generation,
                       predecessor_mapping_sha256, document_sha256,
                       length(CAST(document_json AS BLOB)) AS document_size
                FROM mapping_history
                WHERE desktop_project_id = ?
                ORDER BY mapping_generation
                """,
                (desktop_project_id,),
            ).fetchall()
            if len(rows) > self._max_mapping_history:
                raise CoreBridgeStoreCapacityV2Error("Core mapping history exceeds its row bound")
            return tuple(
                self._mapping_from_sized_row(connection, "mapping_history", row) for row in rows
            )

    def commit_mapping(
        self,
        mapping: CoreProjectMappingV2,
        *,
        expected_previous: CoreProjectMappingV2 | None,
    ) -> None:
        if type(mapping) is not CoreProjectMappingV2 or (
            expected_previous is not None and type(expected_previous) is not CoreProjectMappingV2
        ):
            raise TypeError("Core mapping CAS requires exact v2 mapping models")
        document = _mapping_bytes(mapping)
        digest = hashlib.sha256(document).hexdigest()
        with self._transaction(write=True) as connection:
            current = self._load_mapping_conn(connection, mapping.desktop_project_id)
            if current == mapping:
                return
            if current != expected_previous:
                raise CoreBridgeStoreConflictV2(
                    "current Core mapping differs from the CAS predecessor"
                )
            _validate_mapping_transition(expected_previous, mapping)
            if current is None:
                count = cast(
                    int, connection.execute("SELECT count(*) FROM mappings").fetchone()[0]
                )
                if count >= self._max_mappings:
                    raise CoreBridgeStoreCapacityV2Error("Core mapping capacity is full")
            history_count = cast(
                int,
                connection.execute("SELECT count(*) FROM mapping_history").fetchone()[0],
            )
            if history_count >= self._max_mapping_history:
                raise CoreBridgeStoreCapacityV2Error("Core mapping history capacity is full")
            connection.execute(
                """
                INSERT INTO mapping_history(
                    desktop_project_id, mapping_generation,
                    predecessor_mapping_sha256, document_sha256, document_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    mapping.desktop_project_id,
                    mapping.mapping_generation,
                    mapping.predecessor_mapping_sha256,
                    digest,
                    document,
                ),
            )
            connection.execute(
                """
                INSERT INTO mappings(
                    desktop_project_id, profile_id, core_project_id,
                    mapping_generation, document_sha256, document_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(desktop_project_id) DO UPDATE SET
                    profile_id = excluded.profile_id,
                    core_project_id = excluded.core_project_id,
                    mapping_generation = excluded.mapping_generation,
                    document_sha256 = excluded.document_sha256,
                    document_json = excluded.document_json
                """,
                (
                    mapping.desktop_project_id,
                    mapping.profile_id,
                    mapping.core_project_id,
                    mapping.mapping_generation,
                    digest,
                    document,
                ),
            )
            readback = self._load_mapping_conn(connection, mapping.desktop_project_id)
            if readback != mapping:
                raise CoreBridgeStoreDataV2Error("Core mapping readback differs before commit")

    def load_mutation(
        self,
        desktop_project_id: str,
        operation: str,
        idempotency_key: str,
    ) -> CoreBridgeMutationV2 | None:
        _validate_id(desktop_project_id)
        _validate_id(operation)
        _validate_key(idempotency_key)
        with self._transaction(write=False) as connection:
            row = self._mutation_row(
                connection,
                desktop_project_id,
                operation,
                idempotency_key,
            )
            return None if row is None else self._mutation_from_sized_row(connection, row)

    def release_evidence_summary(
        self,
        *,
        core_project_id: str,
        action_id: str,
    ) -> dict[str, int]:
        """Return bounded counts only after exact release-create authority is proven."""

        _validate_id(core_project_id)
        _validate_key(action_id)
        with self._transaction(write=False) as connection:
            mapping_row = connection.execute(
                """
                SELECT desktop_project_id, profile_id, core_project_id,
                       mapping_generation, document_sha256,
                       length(CAST(document_json AS BLOB)) AS document_size
                FROM mappings WHERE core_project_id = ?
                """,
                (core_project_id,),
            ).fetchone()
            if mapping_row is None:
                raise CoreBridgeStoreDataV2Error(
                    "release evidence Core project mapping is absent"
                )
            mapping = self._mapping_from_sized_row(
                connection,
                "mappings",
                mapping_row,
            )
            mutation_row = self._mutation_row(
                connection,
                mapping.desktop_project_id,
                "create_project_v2",
                action_id,
            )
            if mutation_row is None:
                raise CoreBridgeStoreDataV2Error(
                    "release evidence project-create mutation is absent"
                )
            mutation = self._mutation_from_sized_row(connection, mutation_row)
            mapping_count = cast(
                int,
                connection.execute("SELECT count(*) FROM mappings").fetchone()[0],
            )
            applied_count = cast(
                int,
                connection.execute(
                    """
                    SELECT count(*) FROM mutation_replays
                    WHERE operation = 'create_project_v2' AND state = 'applied'
                    """
                ).fetchone()[0],
            )
            if (
                mapping_count != 1
                or applied_count != 1
                or mutation.state is not CoreBridgeMutationStateV2.APPLIED
                or mutation.response_resource_id != core_project_id
            ):
                raise CoreBridgeStoreDataV2Error(
                    "release evidence does not identify one applied project create"
                )
            return {
                "project_mapping_count": mapping_count,
                "applied_create_project_mutation_count": applied_count,
            }

    def reserve_mutation(self, mutation: CoreBridgeMutationV2) -> CoreBridgeMutationV2:
        if (
            type(mutation) is not CoreBridgeMutationV2
            or mutation.state is not CoreBridgeMutationStateV2.PREPARED
        ):
            raise TypeError("mutation reservation requires an exact prepared v2 model")
        document = _mutation_bytes(mutation)
        digest = hashlib.sha256(document).hexdigest()
        with self._transaction(write=True) as connection:
            row = self._mutation_row(
                connection,
                mutation.desktop_project_id,
                mutation.operation,
                mutation.idempotency_key,
            )
            if row is not None:
                current = self._mutation_from_sized_row(connection, row)
                if (
                    current.profile_id != mutation.profile_id
                    or current.profile_connection_generation
                    != mutation.profile_connection_generation
                    or current.resource_scope != mutation.resource_scope
                    or not hmac.compare_digest(current.request_sha256, mutation.request_sha256)
                ):
                    raise CoreBridgeStoreConflictV2(
                        "Core mutation idempotency identity was reused"
                    )
                return current
            count = cast(
                int,
                connection.execute("SELECT count(*) FROM mutation_replays").fetchone()[0],
            )
            if count >= self._max_mutations:
                raise CoreBridgeStoreCapacityV2Error("Core mutation replay capacity is full")
            connection.execute(
                """
                INSERT INTO mutation_replays(
                    desktop_project_id, operation, idempotency_key, state,
                    request_sha256, document_sha256, document_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mutation.desktop_project_id,
                    mutation.operation,
                    mutation.idempotency_key,
                    mutation.state.value,
                    mutation.request_sha256,
                    digest,
                    document,
                ),
            )
        return mutation

    def mark_mutation_unknown(self, mutation: CoreBridgeMutationV2) -> CoreBridgeMutationV2:
        if type(mutation) is not CoreBridgeMutationV2 or mutation.state not in {
            CoreBridgeMutationStateV2.PREPARED,
            CoreBridgeMutationStateV2.UNKNOWN,
        }:
            raise TypeError("unknown mutation transition requires prepared authority")
        target = replace(mutation, state=CoreBridgeMutationStateV2.UNKNOWN)
        return self._transition_mutation(mutation, target)

    def mark_mutation_applied(
        self,
        mutation: CoreBridgeMutationV2,
        *,
        response_sha256: str,
        response_resource_id: str,
    ) -> CoreBridgeMutationV2:
        if type(mutation) is not CoreBridgeMutationV2 or mutation.state not in {
            CoreBridgeMutationStateV2.PREPARED,
            CoreBridgeMutationStateV2.UNKNOWN,
            CoreBridgeMutationStateV2.APPLIED,
        }:
            raise TypeError("applied mutation transition requires replay authority")
        target = replace(
            mutation,
            state=CoreBridgeMutationStateV2.APPLIED,
            response_sha256=response_sha256,
            response_resource_id=response_resource_id,
        )
        return self._transition_mutation(mutation, target)

    def _transition_mutation(
        self,
        expected: CoreBridgeMutationV2,
        target: CoreBridgeMutationV2,
    ) -> CoreBridgeMutationV2:
        document = _mutation_bytes(target)
        digest = hashlib.sha256(document).hexdigest()
        with self._transaction(write=True) as connection:
            row = self._mutation_row(
                connection,
                expected.desktop_project_id,
                expected.operation,
                expected.idempotency_key,
            )
            if row is None:
                raise CoreBridgeStoreConflictV2("Core mutation replay row is absent")
            current = self._mutation_from_sized_row(connection, row)
            if current == target:
                return target
            if current != expected:
                raise CoreBridgeStoreConflictV2("Core mutation replay state changed during CAS")
            if (
                expected.state is CoreBridgeMutationStateV2.APPLIED
                or target.state is CoreBridgeMutationStateV2.PREPARED
                or (
                    expected.state is CoreBridgeMutationStateV2.UNKNOWN
                    and target.state is not CoreBridgeMutationStateV2.APPLIED
                )
            ):
                raise CoreBridgeStoreConflictV2("Core mutation replay transition is not monotonic")
            connection.execute(
                """
                UPDATE mutation_replays
                SET state = ?, document_sha256 = ?, document_json = ?
                WHERE desktop_project_id = ? AND operation = ? AND idempotency_key = ?
                """,
                (
                    target.state.value,
                    digest,
                    document,
                    target.desktop_project_id,
                    target.operation,
                    target.idempotency_key,
                ),
            )
        return target

    @staticmethod
    def _require_secure_platform() -> None:
        if (
            not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY")
            or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
        ):
            raise CoreBridgeStoreStateV2Error(
                "platform lacks descriptor-relative no-follow v2 storage"
            )

    @staticmethod
    def _create_or_validate_root(root: Path) -> None:
        try:
            metadata = os.lstat(root)
        except FileNotFoundError:
            root.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.mkdir(root, 0o700)
            except FileExistsError:
                metadata = os.lstat(root)
            else:
                os.chmod(root, 0o700, follow_symlinks=False)
                metadata = os.lstat(root)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise CoreBridgeStoreStateV2Error("Core bridge v2 root must be a real directory")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise CoreBridgeStoreStateV2Error("Core bridge v2 root has the wrong owner")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise CoreBridgeStoreStateV2Error("Core bridge v2 root mode must be 0700")

    def _verify_root(self) -> None:
        if self._closed:
            raise CoreBridgeStoreStateV2Error("Core bridge v2 store is closed")
        try:
            path_stat = os.lstat(self._state_root)
            fd_stat = os.fstat(self._root_fd)
        except OSError as exc:
            raise CoreBridgeStoreStateV2Error("Core bridge v2 root is unavailable") from exc
        if (
            not stat.S_ISDIR(path_stat.st_mode)
            or stat.S_ISLNK(path_stat.st_mode)
            or (path_stat.st_dev, path_stat.st_ino) != self._root_identity
            or (fd_stat.st_dev, fd_stat.st_ino) != self._root_identity
            or stat.S_IMODE(path_stat.st_mode) != 0o700
            or (hasattr(os, "getuid") and path_stat.st_uid != os.getuid())
        ):
            raise CoreBridgeStoreStateV2Error("Core bridge v2 root identity changed")

    def _ensure_private_file(self, name: str) -> None:
        self._verify_root()
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=self._root_fd)
        except FileExistsError:
            self._verify_private_file(name)
            return
        except OSError as exc:
            raise CoreBridgeStoreStateV2Error(
                f"could not create private Core bridge file {name}"
            ) from exc
        try:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(self._root_fd)

    @staticmethod
    def _validate_private_file(name: str, metadata: os.stat_result) -> os.stat_result:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise CoreBridgeStoreStateV2Error(f"private Core bridge file {name} is unsafe")
        return metadata

    def _verify_private_file(self, name: str) -> os.stat_result:
        self._verify_root()
        try:
            metadata = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        except OSError as exc:
            raise CoreBridgeStoreStateV2Error(
                f"private Core bridge file {name} is unavailable"
            ) from exc
        return self._validate_private_file(name, metadata)

    def _optional_private_file(self, name: str) -> os.stat_result | None:
        self._verify_root()
        try:
            metadata = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CoreBridgeStoreStateV2Error(
                f"Core bridge SQLite side file {name} is unavailable"
            ) from exc
        return self._validate_private_file(name, metadata)

    def _acquire_owner_lock(self) -> None:
        expected = self._verify_private_file(OWNER_LOCK_FILENAME)
        try:
            descriptor = os.open(
                OWNER_LOCK_FILENAME,
                os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=self._root_fd,
            )
        except OSError as exc:
            raise CoreBridgeStoreStateV2Error(
                "Core bridge owner lock could not be opened"
            ) from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise CoreBridgeStoreStateV2Error("Core bridge v2 root is already owned") from exc
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise CoreBridgeStoreStateV2Error("Core bridge owner lock identity changed")
        self._owner_lock_fd = descriptor
        self._owner_lock_identity = (actual.st_dev, actual.st_ino)

    def _verify_storage_files(self) -> None:
        self._verify_root()
        owner_path = self._verify_private_file(OWNER_LOCK_FILENAME)
        try:
            owner_fd = os.fstat(self._owner_lock_fd)
        except OSError as exc:
            raise CoreBridgeStoreStateV2Error(
                "Core bridge owner lock descriptor is unavailable"
            ) from exc
        owner_identity = getattr(self, "_owner_lock_identity", None)
        if (
            owner_identity is None
            or (owner_path.st_dev, owner_path.st_ino) != owner_identity
            or (owner_fd.st_dev, owner_fd.st_ino) != owner_identity
        ):
            raise CoreBridgeStoreStateV2Error("Core bridge owner lock identity changed")
        database = self._verify_private_file(DATABASE_FILENAME)
        if (
            hasattr(self, "_database_identity")
            and (database.st_dev, database.st_ino) != self._database_identity
        ):
            raise CoreBridgeStoreStateV2Error("Core bridge database pathname identity changed")
        if database.st_size > MAX_DATABASE_BYTES:
            raise CoreBridgeStoreStateV2Error("Core bridge database exceeds its byte budget")
        journal = self._optional_private_file(JOURNAL_FILENAME)
        if journal is not None and journal.st_size > MAX_JOURNAL_BYTES:
            raise CoreBridgeStoreStateV2Error("Core bridge journal exceeds its byte budget")
        for name in (WAL_FILENAME, SHM_FILENAME):
            if self._optional_private_file(name) is not None:
                raise CoreBridgeStoreStateV2Error(
                    f"Core bridge SQLite side file {name} is forbidden"
                )

    def _open_database(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute("PRAGMA database_list").fetchall()
            if len(rows) != 1:
                raise CoreBridgeStoreStateV2Error("SQLite opened an unexpected database set")
            self._verify_sqlite_identity(rows[0][2])
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
            if mode != "delete":
                raise CoreBridgeStoreStateV2Error("SQLite rollback journal mode is unavailable")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.execute("PRAGMA trusted_schema = OFF")
            journal_limit = cast(
                int,
                connection.execute(f"PRAGMA journal_size_limit = {MAX_JOURNAL_BYTES}").fetchone()[
                    0
                ],
            )
            if journal_limit != MAX_JOURNAL_BYTES:
                raise CoreBridgeStoreStateV2Error("SQLite journal limit could not be enforced")
            page_size = cast(int, connection.execute("PRAGMA page_size").fetchone()[0])
            max_pages = MAX_DATABASE_BYTES // page_size
            configured = connection.execute(f"PRAGMA max_page_count = {max_pages}").fetchone()[0]
            if configured != max_pages:
                raise CoreBridgeStoreStateV2Error("SQLite page limit could not be enforced")
        except BaseException:
            connection.close()
            raise
        return connection

    def _verify_sqlite_identity(self, opened_path: object) -> None:
        if type(opened_path) is not str or not os.path.isabs(opened_path):
            raise CoreBridgeStoreStateV2Error("SQLite returned an invalid Core bridge path")
        try:
            opened = os.stat(opened_path, follow_symlinks=False)
        except OSError as exc:
            raise CoreBridgeStoreStateV2Error(
                "SQLite Core bridge identity is unavailable"
            ) from exc
        managed = self._verify_private_file(DATABASE_FILENAME)
        if (opened.st_dev, opened.st_ino) != self._database_identity or (
            managed.st_dev,
            managed.st_ino,
        ) != self._database_identity:
            raise CoreBridgeStoreStateV2Error("SQLite opened an unexpected Core bridge inode")

    def _migrate(self) -> None:
        connection = self._require_connection()
        try:
            connection.execute("BEGIN EXCLUSIVE")
            version = cast(int, connection.execute("PRAGMA user_version").fetchone()[0])
            if version == 0:
                if _schema_rows(connection):
                    raise CoreBridgeStoreSchemaV2Error(
                        "unversioned Core bridge database is not empty"
                    )
                for statement in _SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_metadata VALUES (1, ?, ?, ?)",
                    (STORE_NAMESPACE, SCHEMA_VERSION, EXPECTED_SCHEMA_SHA256),
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise CoreBridgeStoreSchemaV2Error(
                    f"unsupported Core bridge schema version {version}"
                )
            self._validate_schema(connection)
            self._verify_storage_files()
            connection.commit()
        except CoreBridgeStoreV2Error:
            connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            raise CoreBridgeStoreSchemaV2Error("Core bridge schema migration failed") from exc
        except BaseException:
            connection.rollback()
            raise
        os.fsync(self._root_fd)

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        rows = _schema_rows(connection)
        digest = hashlib.sha256(_canonical_json_bytes(rows)).hexdigest()
        if rows != EXPECTED_SCHEMA_ROWS or digest != EXPECTED_SCHEMA_SHA256:
            raise CoreBridgeStoreSchemaV2Error("Core bridge schema fingerprint changed")
        metadata = connection.execute(
            "SELECT namespace, schema_version, schema_sha256 FROM schema_metadata"
        ).fetchall()
        if len(metadata) != 1 or tuple(metadata[0]) != (
            STORE_NAMESPACE,
            SCHEMA_VERSION,
            EXPECTED_SCHEMA_SHA256,
        ):
            raise CoreBridgeStoreSchemaV2Error("Core bridge schema metadata is invalid")

    def _recover_and_validate(self) -> None:
        with self._transaction(write=False) as connection:
            integrity = connection.execute("PRAGMA integrity_check(1)").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise CoreBridgeStoreDataV2Error("Core bridge database integrity check failed")
            counts = {
                "mappings": cast(
                    int, connection.execute("SELECT count(*) FROM mappings").fetchone()[0]
                ),
                "mapping_history": cast(
                    int,
                    connection.execute("SELECT count(*) FROM mapping_history").fetchone()[0],
                ),
                "mutation_replays": cast(
                    int,
                    connection.execute("SELECT count(*) FROM mutation_replays").fetchone()[0],
                ),
            }
            if (
                counts["mappings"] > self._max_mappings
                or counts["mapping_history"] > self._max_mapping_history
                or counts["mutation_replays"] > self._max_mutations
            ):
                raise CoreBridgeStoreCapacityV2Error(
                    "persisted Core bridge rows exceed configured capacity"
                )
            aggregate = cast(
                int,
                connection.execute(
                    """
                    SELECT
                        coalesce((SELECT sum(length(CAST(document_json AS BLOB)))
                                  FROM mappings), 0) +
                        coalesce((SELECT sum(length(CAST(document_json AS BLOB)))
                                  FROM mapping_history), 0) +
                        coalesce((SELECT sum(length(CAST(document_json AS BLOB)))
                                  FROM mutation_replays), 0)
                    """
                ).fetchone()[0],
            )
            if aggregate > MAX_RECOVERY_BYTES:
                raise CoreBridgeStoreDataV2Error(
                    "Core bridge recovery bytes exceed the aggregate budget"
                )
            mapping_rows = connection.execute(
                """
                SELECT desktop_project_id, profile_id, core_project_id,
                       mapping_generation, document_sha256,
                       length(CAST(document_json AS BLOB)) AS document_size
                FROM mappings ORDER BY desktop_project_id
                """
            ).fetchall()
            mappings = {
                row["desktop_project_id"]: self._mapping_from_sized_row(
                    connection, "mappings", row
                )
                for row in mapping_rows
            }
            history_rows = connection.execute(
                """
                SELECT desktop_project_id, mapping_generation,
                       predecessor_mapping_sha256, document_sha256,
                       length(CAST(document_json AS BLOB)) AS document_size
                FROM mapping_history
                ORDER BY desktop_project_id, mapping_generation
                """
            ).fetchall()
            history: dict[str, list[CoreProjectMappingV2]] = {}
            for row in history_rows:
                item = self._mapping_from_sized_row(connection, "mapping_history", row)
                history.setdefault(item.desktop_project_id, []).append(item)
            if set(history) != set(mappings):
                raise CoreBridgeStoreDataV2Error(
                    "Core mapping history owners differ from current mappings"
                )
            for project_id, current in mappings.items():
                rows = history[project_id]
                if not rows or rows[-1] != current:
                    raise CoreBridgeStoreDataV2Error(
                        "current Core mapping is not the latest history row"
                    )
                previous: CoreProjectMappingV2 | None = None
                for item in rows:
                    try:
                        _validate_mapping_transition(previous, item)
                    except CoreBridgeStoreConflictV2 as exc:
                        raise CoreBridgeStoreDataV2Error(
                            "Core mapping history is not a valid chain"
                        ) from exc
                    previous = item
            mutation_rows = connection.execute(
                """
                SELECT desktop_project_id, operation, idempotency_key, state,
                       request_sha256, document_sha256,
                       length(CAST(document_json AS BLOB)) AS document_size
                FROM mutation_replays
                ORDER BY desktop_project_id, operation, idempotency_key
                """
            ).fetchall()
            for row in mutation_rows:
                self._mutation_from_sized_row(connection, row)

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._verify_storage_files()
            connection = self._require_connection()
            try:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield connection
                connection.commit()
                self._verify_storage_files()
            except BaseException:
                connection.rollback()
                raise
        if write:
            os.fsync(self._root_fd)

    def _load_mapping_conn(
        self,
        connection: sqlite3.Connection,
        desktop_project_id: str,
    ) -> CoreProjectMappingV2 | None:
        row = connection.execute(
            """
            SELECT desktop_project_id, profile_id, core_project_id,
                   mapping_generation, document_sha256,
                   length(CAST(document_json AS BLOB)) AS document_size
            FROM mappings WHERE desktop_project_id = ?
            """,
            (desktop_project_id,),
        ).fetchone()
        return None if row is None else self._mapping_from_sized_row(connection, "mappings", row)

    def _mapping_from_sized_row(
        self,
        connection: sqlite3.Connection,
        table: str,
        row: sqlite3.Row,
    ) -> CoreProjectMappingV2:
        if table not in {"mappings", "mapping_history"}:
            raise AssertionError("unexpected Core mapping table")
        size = row["document_size"]
        if type(size) is not int or not 2 <= size <= MAX_MAPPING_DOCUMENT_BYTES:
            raise CoreBridgeStoreDataV2Error("stored Core mapping document exceeds its byte bound")
        if table == "mappings":
            selected = connection.execute(
                """
                SELECT CASE WHEN length(CAST(document_json AS BLOB)) = ?
                            THEN document_json ELSE NULL END AS document_json
                FROM mappings WHERE desktop_project_id = ?
                """,
                (size, row["desktop_project_id"]),
            ).fetchone()
        else:
            selected = connection.execute(
                """
                SELECT CASE WHEN length(CAST(document_json AS BLOB)) = ?
                            THEN document_json ELSE NULL END AS document_json
                FROM mapping_history
                WHERE desktop_project_id = ? AND mapping_generation = ?
                """,
                (size, row["desktop_project_id"], row["mapping_generation"]),
            ).fetchone()
        if selected is None or selected["document_json"] is None:
            raise CoreBridgeStoreDataV2Error("stored Core mapping length changed during read")
        raw = bytes(selected["document_json"])
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != row["document_sha256"]:
            raise CoreBridgeStoreDataV2Error("stored Core mapping digest does not match its bytes")
        mapping = _mapping_from_bytes(raw)
        if (
            mapping.desktop_project_id != row["desktop_project_id"]
            or mapping.mapping_generation != row["mapping_generation"]
            or ("profile_id" in row.keys() and mapping.profile_id != row["profile_id"])
            or (
                "core_project_id" in row.keys()
                and mapping.core_project_id != row["core_project_id"]
            )
            or (
                "predecessor_mapping_sha256" in row.keys()
                and mapping.predecessor_mapping_sha256 != row["predecessor_mapping_sha256"]
            )
        ):
            raise CoreBridgeStoreDataV2Error(
                "stored Core mapping indexed fields differ from its document"
            )
        return mapping

    @staticmethod
    def _mutation_row(
        connection: sqlite3.Connection,
        desktop_project_id: str,
        operation: str,
        idempotency_key: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT desktop_project_id, operation, idempotency_key, state,
                   request_sha256, document_sha256,
                   length(CAST(document_json AS BLOB)) AS document_size
            FROM mutation_replays
            WHERE desktop_project_id = ? AND operation = ? AND idempotency_key = ?
            """,
            (desktop_project_id, operation, idempotency_key),
        ).fetchone()

    def _mutation_from_sized_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> CoreBridgeMutationV2:
        size = row["document_size"]
        if type(size) is not int or not 2 <= size <= MAX_MUTATION_DOCUMENT_BYTES:
            raise CoreBridgeStoreDataV2Error(
                "stored Core mutation document exceeds its byte bound"
            )
        selected = connection.execute(
            """
            SELECT CASE WHEN length(CAST(document_json AS BLOB)) = ?
                        THEN document_json ELSE NULL END AS document_json
            FROM mutation_replays
            WHERE desktop_project_id = ? AND operation = ? AND idempotency_key = ?
            """,
            (
                size,
                row["desktop_project_id"],
                row["operation"],
                row["idempotency_key"],
            ),
        ).fetchone()
        if selected is None or selected["document_json"] is None:
            raise CoreBridgeStoreDataV2Error("stored Core mutation length changed during read")
        raw = bytes(selected["document_json"])
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != row["document_sha256"]:
            raise CoreBridgeStoreDataV2Error(
                "stored Core mutation digest does not match its bytes"
            )
        mutation = _mutation_from_bytes(raw)
        if (
            mutation.desktop_project_id != row["desktop_project_id"]
            or mutation.operation != row["operation"]
            or mutation.idempotency_key != row["idempotency_key"]
            or mutation.state.value != row["state"]
            or mutation.request_sha256 != row["request_sha256"]
        ):
            raise CoreBridgeStoreDataV2Error(
                "stored Core mutation indexed fields differ from its document"
            )
        return mutation

    def _require_connection(self) -> sqlite3.Connection:
        if self._closed or self._connection is None:
            raise CoreBridgeStoreStateV2Error("Core bridge v2 store is closed")
        return self._connection

    def _close_resources(self) -> None:
        with getattr(self, "_lock", threading.RLock()):
            if getattr(self, "_closed", True):
                return
            self._closed = True
            connection = getattr(self, "_connection", None)
            self._connection = None
            if connection is not None:
                connection.close()
            owner = getattr(self, "_owner_lock_fd", -1)
            self._owner_lock_fd = -1
            if owner >= 0:
                try:
                    fcntl.flock(owner, fcntl.LOCK_UN)
                finally:
                    os.close(owner)
            root = getattr(self, "_root_fd", -1)
            self._root_fd = -1
            if root >= 0:
                os.close(root)


def _mapping_bytes(mapping: CoreProjectMappingV2) -> bytes:
    raw = _canonical_json_bytes(core_project_mapping_document_v2(mapping))
    if len(raw) > MAX_MAPPING_DOCUMENT_BYTES:
        raise CoreBridgeStoreCapacityV2Error("Core mapping document exceeds its byte bound")
    return raw


def _mutation_bytes(mutation: CoreBridgeMutationV2) -> bytes:
    raw = _canonical_json_bytes(core_bridge_mutation_document_v2(mutation))
    if len(raw) > MAX_MUTATION_DOCUMENT_BYTES:
        raise CoreBridgeStoreCapacityV2Error("Core mutation document exceeds its byte bound")
    return raw


_MAPPING_KEYS = frozenset(
    {
        "schema_version",
        "record_type",
        "desktop_project_id",
        "profile_id",
        "profile_connection_generation",
        "core_project_id",
        "project_config_sha256",
        "project_etag",
        "project_admission_etag",
        "active_project_head",
        "project_head_successor_proof",
        "daemon_release_version",
        "daemon_build_id",
        "daemon_source_commit",
        "daemon_openapi_sha256",
        "daemon_event_schema_sha256",
        "daemon_registry_sha256",
        "daemon_runtime_contract_sha256",
        "core_project",
        "core_version",
        "mapping_generation",
        "predecessor_mapping_sha256",
        "last_core_event_id",
        "last_core_event_sequence",
        "last_core_event_payload_sha256",
    }
)

_MUTATION_KEYS = frozenset(
    {
        "schema_version",
        "record_type",
        "desktop_project_id",
        "profile_id",
        "profile_connection_generation",
        "operation",
        "resource_scope",
        "idempotency_key",
        "request_sha256",
        "state",
        "response_sha256",
        "response_resource_id",
    }
)


def _closed_document(raw: bytes, keys: frozenset[str], *, label: str) -> dict[str, object]:
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_no_duplicate_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (TypeError, UnicodeError, ValueError, RecursionError) as exc:
        raise CoreBridgeStoreDataV2Error(f"stored {label} JSON is invalid") from exc
    if type(value) is not dict or set(value) != keys:
        raise CoreBridgeStoreDataV2Error(f"stored {label} document is not closed")
    return cast(dict[str, object], value)


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object member")
        result[key] = value
    return result


def _mapping_from_bytes(raw: bytes) -> CoreProjectMappingV2:
    value = _closed_document(raw, _MAPPING_KEYS, label="Core mapping")
    if value["schema_version"] != "2" or value["record_type"] != "CoreProjectMappingV2":
        raise CoreBridgeStoreDataV2Error("stored Core mapping type is invalid")
    try:
        active_value = value["active_project_head"]
        mapping = CoreProjectMappingV2(
            desktop_project_id=_string(value["desktop_project_id"]),
            profile_id=_string(value["profile_id"]),
            profile_connection_generation=_integer(value["profile_connection_generation"]),
            core_project_id=_string(value["core_project_id"]),
            project_config_sha256=_string(value["project_config_sha256"]),
            project_etag=_string(value["project_etag"]),
            project_admission_etag=_optional_string(value["project_admission_etag"]),
            active_project_head=(
                None
                if active_value is None
                else core_v2.ProjectHeadRefV2.model_validate(active_value, strict=True)
            ),
            project_head_successor_proof=tuple(
                core_v2.ProjectHeadRefV2.model_validate(item, strict=True)
                for item in _list(value["project_head_successor_proof"])
            ),
            daemon_release_version=_string(value["daemon_release_version"]),
            daemon_build_id=_string(value["daemon_build_id"]),
            daemon_source_commit=_string(value["daemon_source_commit"]),
            daemon_openapi_sha256=_string(value["daemon_openapi_sha256"]),
            daemon_event_schema_sha256=_string(value["daemon_event_schema_sha256"]),
            daemon_registry_sha256=_string(value["daemon_registry_sha256"]),
            daemon_runtime_contract_sha256=_string(value["daemon_runtime_contract_sha256"]),
            core_project=core_v2.ProjectV2.model_validate(value["core_project"], strict=True),
            core_version=core_v2.VersionResponseV2.model_validate(
                value["core_version"], strict=True
            ),
            mapping_generation=_integer(value["mapping_generation"]),
            predecessor_mapping_sha256=_optional_string(value["predecessor_mapping_sha256"]),
            last_core_event_id=_optional_string(value["last_core_event_id"]),
            last_core_event_sequence=_optional_integer(value["last_core_event_sequence"]),
            last_core_event_payload_sha256=_optional_string(
                value["last_core_event_payload_sha256"]
            ),
        )
    except (TypeError, ValidationError, ValueError) as exc:
        raise CoreBridgeStoreDataV2Error("stored Core mapping is invalid") from exc
    if _mapping_bytes(mapping) != raw:
        raise CoreBridgeStoreDataV2Error("stored Core mapping is not canonical")
    return mapping


def _mutation_from_bytes(raw: bytes) -> CoreBridgeMutationV2:
    value = _closed_document(raw, _MUTATION_KEYS, label="Core mutation")
    if value["schema_version"] != "2" or value["record_type"] != "CoreBridgeMutationV2":
        raise CoreBridgeStoreDataV2Error("stored Core mutation type is invalid")
    try:
        mutation = CoreBridgeMutationV2(
            desktop_project_id=_string(value["desktop_project_id"]),
            profile_id=_string(value["profile_id"]),
            profile_connection_generation=_integer(value["profile_connection_generation"]),
            operation=_string(value["operation"]),
            resource_scope=_string(value["resource_scope"]),
            idempotency_key=_string(value["idempotency_key"]),
            request_sha256=_string(value["request_sha256"]),
            state=CoreBridgeMutationStateV2(_string(value["state"])),
            response_sha256=_optional_string(value["response_sha256"]),
            response_resource_id=_optional_string(value["response_resource_id"]),
        )
    except (TypeError, ValueError) as exc:
        raise CoreBridgeStoreDataV2Error("stored Core mutation is invalid") from exc
    if _mutation_bytes(mutation) != raw:
        raise CoreBridgeStoreDataV2Error("stored Core mutation is not canonical")
    return mutation


def _validate_mapping_transition(
    previous: CoreProjectMappingV2 | None,
    current: CoreProjectMappingV2,
) -> None:
    expected_generation = 1 if previous is None else previous.mapping_generation + 1
    expected_predecessor = None if previous is None else core_project_mapping_sha256_v2(previous)
    if (
        current.mapping_generation != expected_generation
        or current.predecessor_mapping_sha256 != expected_predecessor
    ):
        raise CoreBridgeStoreConflictV2("Core mapping is not the exact next mapping generation")
    if previous is None:
        proof = current.project_head_successor_proof
        head = current.active_project_head
        if head is None:
            if proof:
                raise CoreBridgeStoreConflictV2("a headless initial mapping has a successor proof")
        elif proof != (head,) or head.generation != 0:
            raise CoreBridgeStoreConflictV2(
                "initial Core mapping does not prove its generation-zero head"
            )
        return
    stable_owner = (
        previous.desktop_project_id,
        previous.profile_id,
        previous.core_project_id,
        previous.daemon_release_version,
        previous.daemon_build_id,
        previous.daemon_source_commit,
        previous.daemon_openapi_sha256,
        previous.daemon_event_schema_sha256,
        previous.daemon_registry_sha256,
        previous.daemon_runtime_contract_sha256,
        previous.core_version,
        previous.core_project.created_at,
    )
    current_owner = (
        current.desktop_project_id,
        current.profile_id,
        current.core_project_id,
        current.daemon_release_version,
        current.daemon_build_id,
        current.daemon_source_commit,
        current.daemon_openapi_sha256,
        current.daemon_event_schema_sha256,
        current.daemon_registry_sha256,
        current.daemon_runtime_contract_sha256,
        current.core_version,
        current.core_project.created_at,
    )
    if stable_owner != current_owner or (
        current.profile_connection_generation < previous.profile_connection_generation
    ):
        raise CoreBridgeStoreConflictV2("Core mapping successor rewrites durable owner authority")
    old_head = previous.active_project_head
    new_head = current.active_project_head
    proof = current.project_head_successor_proof
    if old_head is None and new_head is not None:
        if (
            proof != (new_head,)
            or new_head.generation != 0
            or new_head.predecessor_project_head_id is not None
        ):
            raise CoreBridgeStoreConflictV2("first Core head is not a generation-zero authority")
    elif old_head is not None and new_head is None:
        raise CoreBridgeStoreConflictV2("Core mapping successor removes its active head")
    elif old_head is not None and new_head is not None:
        if new_head.generation == old_head.generation:
            if new_head != old_head or proof:
                raise CoreBridgeStoreConflictV2("same-generation Core head authority changed")
        else:
            predecessor = old_head
            for head in proof:
                if (
                    head.generation != predecessor.generation + 1
                    or head.predecessor_project_head_id != predecessor.project_head_id
                ):
                    raise CoreBridgeStoreConflictV2(
                        "Core project head mapping proof is not contiguous"
                    )
                predecessor = head
            if not proof or predecessor != new_head:
                raise CoreBridgeStoreConflictV2(
                    "Core project head mapping lacks an exact successor proof"
                )
    if previous.last_core_event_sequence is not None:
        if current.last_core_event_sequence is None:
            raise CoreBridgeStoreConflictV2("Core event cursor was removed")
        if current.last_core_event_sequence < previous.last_core_event_sequence:
            raise CoreBridgeStoreConflictV2("Core event cursor regressed")
        if current.last_core_event_sequence == previous.last_core_event_sequence and (
            current.last_core_event_id != previous.last_core_event_id
            or current.last_core_event_payload_sha256 != previous.last_core_event_payload_sha256
        ):
            raise CoreBridgeStoreConflictV2("same-sequence Core event cursor changed identity")


def _validate_id(value: str) -> None:
    try:
        _OPAQUE_ID.validate_python(value, strict=True)
    except (TypeError, ValidationError, ValueError) as exc:
        raise ValueError("Core bridge resource identity is invalid") from exc


def _validate_key(value: str) -> None:
    if (
        type(value) is not str
        or not 16 <= len(value) <= 256
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
    ):
        raise ValueError("Core bridge idempotency key is invalid")


def _string(value: object) -> str:
    if type(value) is not str:
        raise TypeError("value is not an exact string")
    return value


def _optional_string(value: object) -> str | None:
    return None if value is None else _string(value)


def _integer(value: object) -> int:
    if type(value) is not int:
        raise TypeError("value is not an exact integer")
    return value


def _optional_integer(value: object) -> int | None:
    return None if value is None else _integer(value)


def _list(value: object) -> list[object]:
    if type(value) is not list:
        raise TypeError("value is not an exact list")
    return value


__all__ = (
    "DATABASE_FILENAME",
    "EXPECTED_SCHEMA_SHA256",
    "CoreBridgeStoreCapacityV2Error",
    "CoreBridgeStoreConflictV2",
    "CoreBridgeStoreDataV2Error",
    "CoreBridgeStoreSchemaV2Error",
    "CoreBridgeStoreStateV2Error",
    "CoreBridgeStoreV2Error",
    "DesktopCoreBridgeStoreV2",
)
