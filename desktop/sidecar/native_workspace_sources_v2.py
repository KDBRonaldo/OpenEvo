"""Private restart authority for selected native workspace directories."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import errno
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading


_SCHEMA_VERSION = "2"
_AUTH_KEY_NAME = ".native-workspace-source-auth-v2"
_AUTH_KEY_BYTES = 32
_MAX_RECORDS = 64
_MAX_RECORD_BYTES = 16 * 1024
_IMPORT_ID_RE = re.compile(r"^workspace-import-[0-9a-f]{48}$")
_TEMP_PREFIX = ".native-workspace-source-tmp-"
_MAC_DOMAIN = b"openevo.desktop.native-workspace-source.v2\0"


class NativeWorkspaceSourceStoreV2Error(RuntimeError):
    """Private native source authority is missing, corrupt, or unsafe."""


@dataclass(frozen=True, slots=True)
class NativeWorkspaceSourceRecordV2:
    schema_version: str
    action_id: str
    import_id: str
    selected_path: str
    selected_device: int
    selected_inode: int
    project_id: str
    display_name: str
    journal_sha256: str

    def __post_init__(self) -> None:
        if (
            self.schema_version != _SCHEMA_VERSION
            or not _valid_text(self.action_id, minimum=16, maximum=256)
            or _IMPORT_ID_RE.fullmatch(self.import_id) is None
            or not _valid_absolute_path(self.selected_path)
            or type(self.selected_device) is not int
            or self.selected_device < 0
            or type(self.selected_inode) is not int
            or self.selected_inode <= 0
            or not _valid_text(self.project_id, minimum=1, maximum=256)
            or not _valid_text(self.display_name, minimum=1, maximum=128)
            or os.path.basename(os.path.normpath(self.selected_path)) != self.display_name
            or not _is_digest(self.journal_sha256)
        ):
            raise ValueError("native workspace source record is invalid")


class NativeWorkspaceSourceStoreV2:
    """Persist path-bearing source authority outside renderer/provider contracts."""

    def __init__(self, root: Path | str) -> None:
        path = Path(os.path.abspath(os.fspath(Path(root).expanduser())))
        if not path.is_absolute():
            raise NativeWorkspaceSourceStoreV2Error(
                "native workspace source root must be absolute"
            )
        try:
            path.mkdir(mode=0o700, parents=False, exist_ok=True)
            os.chmod(path, 0o700, follow_symlinks=False)
            root_fd = os.open(
                path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
        except OSError as exc:
            raise NativeWorkspaceSourceStoreV2Error(
                "native workspace source root is unavailable"
            ) from exc
        self._root = path
        self._root_fd = root_fd
        self._root_identity = self._identity(os.fstat(root_fd))
        self._lock = threading.RLock()
        self._closed = False
        try:
            self._verify_root()
            self._auth_key = self._load_or_create_auth_key()
            self._remove_safe_crash_temps()
            self.list_records()
        except BaseException:
            os.close(root_fd)
            self._closed = True
            raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._auth_key = b""
            os.close(self._root_fd)

    def __del__(self) -> None:
        if getattr(self, "_closed", True) is False:
            try:
                self.close()
            except OSError:
                pass

    def put(self, record: NativeWorkspaceSourceRecordV2) -> bool:
        if type(record) is not NativeWorkspaceSourceRecordV2:
            raise TypeError("native workspace source record has the wrong type")
        encoded = self._encode(record)
        name = self._record_name(record.import_id)
        with self._lock:
            self._verify_root()
            existing = self._read_optional(name)
            if existing is not None:
                if existing != record:
                    raise NativeWorkspaceSourceStoreV2Error(
                        "native workspace source intent changed"
                    )
                return False
            if len(self._record_names()) >= _MAX_RECORDS:
                raise NativeWorkspaceSourceStoreV2Error(
                    "native workspace source capacity is exhausted"
                )
            temporary = f"{_TEMP_PREFIX}{secrets.token_hex(16)}"
            fd = -1
            linked = False
            try:
                fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=self._root_fd,
                )
                self._write_all(fd, encoded)
                os.fsync(fd)
                self._verify_record_stat(os.fstat(fd), expected_size=len(encoded), links=1)
                os.link(
                    temporary,
                    name,
                    src_dir_fd=self._root_fd,
                    dst_dir_fd=self._root_fd,
                    follow_symlinks=False,
                )
                linked = True
                os.unlink(temporary, dir_fd=self._root_fd)
                self._verify_record_stat(
                    os.stat(name, dir_fd=self._root_fd, follow_symlinks=False),
                    expected_size=len(encoded),
                    links=1,
                )
                os.fsync(self._root_fd)
            except FileExistsError:
                current = self._read_optional(name)
                if current != record:
                    raise NativeWorkspaceSourceStoreV2Error(
                        "native workspace source publication conflicted"
                    ) from None
                return False
            except OSError as exc:
                raise NativeWorkspaceSourceStoreV2Error(
                    "native workspace source could not be published"
                ) from exc
            finally:
                if fd >= 0:
                    os.close(fd)
                if not linked:
                    try:
                        os.unlink(temporary, dir_fd=self._root_fd)
                    except OSError:
                        pass
            self._verify_root()
            return True

    def list_records(self) -> tuple[NativeWorkspaceSourceRecordV2, ...]:
        with self._lock:
            self._verify_root()
            names = self._record_names()
            if len(names) > _MAX_RECORDS:
                raise NativeWorkspaceSourceStoreV2Error(
                    "native workspace source capacity is exceeded"
                )
            records = tuple(self._read_required(name) for name in names)
            if len({record.import_id for record in records}) != len(records):
                raise NativeWorkspaceSourceStoreV2Error(
                    "native workspace source identity is duplicated"
                )
            return records

    def remove(self, record: NativeWorkspaceSourceRecordV2) -> None:
        if type(record) is not NativeWorkspaceSourceRecordV2:
            raise TypeError("native workspace source record has the wrong type")
        name = self._record_name(record.import_id)
        with self._lock:
            self._verify_root()
            current = self._read_optional(name)
            if current is None:
                return
            if current != record:
                raise NativeWorkspaceSourceStoreV2Error(
                    "native workspace source changed before removal"
                )
            before = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
            self._verify_record_stat(before, links=1)
            os.unlink(name, dir_fd=self._root_fd)
            os.fsync(self._root_fd)
            self._verify_root()

    def _record_names(self) -> tuple[str, ...]:
        try:
            names = tuple(sorted(os.listdir(self._root_fd)))
        except OSError as exc:
            raise NativeWorkspaceSourceStoreV2Error(
                "native workspace source inventory is unavailable"
            ) from exc
        allowed: list[str] = []
        for name in names:
            if name == _AUTH_KEY_NAME:
                continue
            if name.startswith(_TEMP_PREFIX):
                raise NativeWorkspaceSourceStoreV2Error(
                    "native workspace source has unreconciled temporary state"
                )
            if not name.endswith(".json") or _IMPORT_ID_RE.fullmatch(name[:-5]) is None:
                raise NativeWorkspaceSourceStoreV2Error(
                    "native workspace source inventory is invalid"
                )
            allowed.append(name)
        return tuple(allowed)

    def _read_optional(self, name: str) -> NativeWorkspaceSourceRecordV2 | None:
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=self._root_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise NativeWorkspaceSourceStoreV2Error(
                "native workspace source record is unavailable"
            ) from exc
        try:
            metadata = os.fstat(fd)
            self._verify_record_stat(metadata, links=1)
            if metadata.st_size <= 0 or metadata.st_size > _MAX_RECORD_BYTES:
                raise NativeWorkspaceSourceStoreV2Error(
                    "native workspace source record exceeds its bound"
                )
            payload = self._read_exact(fd, metadata.st_size)
            if self._record_identity(os.fstat(fd)) != self._record_identity(metadata):
                raise NativeWorkspaceSourceStoreV2Error(
                    "native workspace source record changed during read"
                )
        finally:
            os.close(fd)
        return self._decode(payload)

    def _read_required(self, name: str) -> NativeWorkspaceSourceRecordV2:
        record = self._read_optional(name)
        if record is None:
            raise NativeWorkspaceSourceStoreV2Error(
                "native workspace source inventory changed during read"
            )
        if name != self._record_name(record.import_id):
            raise NativeWorkspaceSourceStoreV2Error(
                "native workspace source filename differs from its authority"
            )
        return record

    def _encode(self, record: NativeWorkspaceSourceRecordV2) -> bytes:
        document = asdict(record)
        canonical = self._canonical(document)
        document["hmac_sha256"] = hmac.new(
            self._auth_key,
            _MAC_DOMAIN + canonical,
            hashlib.sha256,
        ).hexdigest()
        encoded = self._canonical(document) + b"\n"
        if len(encoded) > _MAX_RECORD_BYTES:
            raise NativeWorkspaceSourceStoreV2Error(
                "native workspace source record exceeds its bound"
            )
        return encoded

    def _decode(self, payload: bytes) -> NativeWorkspaceSourceRecordV2:
        try:
            document = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NativeWorkspaceSourceStoreV2Error(
                "native workspace source record is invalid"
            ) from exc
        if not isinstance(document, dict) or set(document) != {
            "schema_version",
            "action_id",
            "import_id",
            "selected_path",
            "selected_device",
            "selected_inode",
            "project_id",
            "display_name",
            "journal_sha256",
            "hmac_sha256",
        }:
            raise NativeWorkspaceSourceStoreV2Error("native workspace source record is not closed")
        claimed = document.pop("hmac_sha256")
        if not _is_digest(claimed):
            raise NativeWorkspaceSourceStoreV2Error(
                "native workspace source authentication is invalid"
            )
        expected = hmac.new(
            self._auth_key,
            _MAC_DOMAIN + self._canonical(document),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(claimed, expected):
            raise NativeWorkspaceSourceStoreV2Error(
                "native workspace source authentication failed"
            )
        try:
            record = NativeWorkspaceSourceRecordV2(**document)
        except (TypeError, ValueError) as exc:
            raise NativeWorkspaceSourceStoreV2Error(
                "native workspace source record is invalid"
            ) from exc
        if payload != self._encode(record):
            raise NativeWorkspaceSourceStoreV2Error(
                "native workspace source record is not canonical"
            )
        return record

    def _load_or_create_auth_key(self) -> bytes:
        try:
            fd = os.open(
                _AUTH_KEY_NAME,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._root_fd,
            )
        except FileExistsError:
            try:
                fd = os.open(
                    _AUTH_KEY_NAME,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=self._root_fd,
                )
            except OSError as exc:
                raise NativeWorkspaceSourceStoreV2Error(
                    "native workspace source authentication is unavailable"
                ) from exc
            created = False
        except OSError as exc:
            raise NativeWorkspaceSourceStoreV2Error(
                "native workspace source authentication could not be created"
            ) from exc
        else:
            created = True
        try:
            if created:
                key = secrets.token_bytes(_AUTH_KEY_BYTES)
                self._write_all(fd, key)
                os.fsync(fd)
                os.fsync(self._root_fd)
            else:
                key = self._read_exact(fd, _AUTH_KEY_BYTES)
            self._verify_record_stat(
                os.fstat(fd),
                expected_size=_AUTH_KEY_BYTES,
                links=1,
            )
        finally:
            os.close(fd)
        return key

    def _remove_safe_crash_temps(self) -> None:
        for name in tuple(sorted(os.listdir(self._root_fd))):
            if not name.startswith(_TEMP_PREFIX):
                continue
            try:
                metadata = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
                self._verify_record_stat(metadata, links=1)
                os.unlink(name, dir_fd=self._root_fd)
            except OSError as exc:
                raise NativeWorkspaceSourceStoreV2Error(
                    "native workspace source temporary state is unsafe"
                ) from exc
        os.fsync(self._root_fd)

    def _verify_root(self) -> None:
        if self._closed:
            raise NativeWorkspaceSourceStoreV2Error("native workspace source store is closed")
        try:
            path_stat = os.stat(self._root, follow_symlinks=False)
            fd_stat = os.fstat(self._root_fd)
        except OSError as exc:
            raise NativeWorkspaceSourceStoreV2Error(
                "native workspace source root is unavailable"
            ) from exc
        if (
            self._identity(path_stat) != self._root_identity
            or self._identity(fd_stat) != self._root_identity
            or not stat.S_ISDIR(fd_stat.st_mode)
            or fd_stat.st_uid != os.geteuid()
            or stat.S_IMODE(fd_stat.st_mode) != 0o700
        ):
            raise NativeWorkspaceSourceStoreV2Error("native workspace source root changed")

    @staticmethod
    def _verify_record_stat(
        metadata: os.stat_result,
        *,
        expected_size: int | None = None,
        links: int,
    ) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != links
            or (expected_size is not None and metadata.st_size != expected_size)
        ):
            raise NativeWorkspaceSourceStoreV2Error(
                "native workspace source file metadata is unsafe"
            )

    @staticmethod
    def _identity(metadata: os.stat_result) -> tuple[int, int]:
        return metadata.st_dev, metadata.st_ino

    @staticmethod
    def _record_identity(
        metadata: os.stat_result,
    ) -> tuple[int, int, int, int, int, int, int, int, int]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_nlink,
            metadata.st_uid,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            metadata.st_blocks,
        )

    @staticmethod
    def _canonical(document: object) -> bytes:
        return json.dumps(
            document,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @staticmethod
    def _record_name(import_id: str) -> str:
        if _IMPORT_ID_RE.fullmatch(import_id) is None:
            raise ValueError("native workspace import identity is invalid")
        return f"{import_id}.json"

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError(errno.EIO, "short native workspace source write")
            offset += written

    @staticmethod
    def _read_exact(fd: int, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                raise NativeWorkspaceSourceStoreV2Error(
                    "native workspace source record ended early"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise NativeWorkspaceSourceStoreV2Error(
                "native workspace source record exceeds its declared size"
            )
        return b"".join(chunks)


def _valid_text(value: object, *, minimum: int, maximum: int) -> bool:
    return (
        type(value) is str
        and minimum <= len(value.encode("utf-8")) <= maximum
        and value == value.strip()
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    )


def _valid_absolute_path(value: object) -> bool:
    return (
        type(value) is str
        and 1 <= len(value.encode("utf-8")) <= 4096
        and value.startswith("/")
        and os.path.abspath(value) == value
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    )


def _is_digest(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "NativeWorkspaceSourceRecordV2",
    "NativeWorkspaceSourceStoreV2",
    "NativeWorkspaceSourceStoreV2Error",
]
