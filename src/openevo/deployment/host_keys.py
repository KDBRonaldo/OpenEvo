from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import stat
import struct
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openevo.deployment.profile import RemoteProfileConfig

HostKeyAlgorithm = Literal[
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "rsa-sha2-512",
]
KeyscanRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]

_ALGORITHM_ORDER: tuple[HostKeyAlgorithm, ...] = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "rsa-sha2-512",
)
_KEY_TYPE_TO_ALGORITHM: dict[str, HostKeyAlgorithm] = {
    "ssh-ed25519": "ssh-ed25519",
    "ecdsa-sha2-nistp256": "ecdsa-sha2-nistp256",
    # OpenSSH stores RSA public keys as ssh-rsa while rsa-sha2-512 selects
    # the signature algorithm used during key exchange.
    "ssh-rsa": "rsa-sha2-512",
}
_METADATA_PREFIX = "# openevo-host-key-v1 "
_METADATA_FIELDS = {
    "algorithm",
    "fingerprint",
    "host",
    "port",
    "profile_id",
    "public_key",
}
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9._%-]+$")
_MAX_KEYSCAN_OUTPUT_BYTES = 64 * 1024
_MAX_TRUST_FILE_BYTES = 32 * 1024


@dataclass(frozen=True)
class HostKeyCandidate:
    """A host key observed by a probe but not yet trusted."""

    algorithm: HostKeyAlgorithm
    public_key: str
    fingerprint: str


@dataclass(frozen=True)
class PendingHostKeyProbe:
    """An immutable host-key observation awaiting an exact user confirmation."""

    profile_id: str
    host: str
    port: int
    candidates: tuple[HostKeyCandidate, ...]


@dataclass(frozen=True)
class TrustedKnownHostsBinding:
    """A provider-owned known-host file bound to one remote profile identity."""

    profile_id: str
    host: str
    port: int
    algorithm: HostKeyAlgorithm
    public_key: str
    fingerprint: str
    known_hosts_file: Path

    def validate_for(self, profile: RemoteProfileConfig) -> None:
        """Revalidate the file and require an exact profile/host/port binding."""

        identity = (profile.id, profile.host, profile.port)
        if identity != (self.profile_id, self.host, self.port):
            raise ValueError("trusted host-key profile binding does not match remote profile")
        expected_name = _binding_filename(profile.id)
        path = self.known_hosts_file
        _validate_path_text(path)
        if not path.is_absolute() or path.name != expected_name:
            raise ValueError("trusted known-host file is not provider-owned")
        with _opened_store_root(path.parent, create=False) as root_fd:
            content = _read_secure_file(root_fd, path.name)
        if content is None:
            raise ValueError("trusted known-host file is missing")
        binding = _binding_from_content(path, content, profile)
        if binding != self:
            raise ValueError("trusted known-host file content does not match binding")


class ProviderKnownHostStore:
    """Probe, confirm, and persist Desktop-owned SSH host-key trust.

    Probes never mutate trust state. Confirmation repeats the probe, requires the
    complete observation to remain unchanged, and publishes an immutable 0600
    known-host file inside a provider-owned 0700 directory.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        runner: KeyscanRunner | None = None,
    ) -> None:
        self._root = Path(root).expanduser()
        if not self._root.is_absolute():
            raise ValueError("provider known-host store root must be absolute")
        _validate_path_text(self._root)
        self._runner = runner or _run_keyscan

    def probe(
        self,
        profile: RemoteProfileConfig,
        *,
        timeout_seconds: float = 10.0,
    ) -> PendingHostKeyProbe:
        """Return pending keys from a strict, non-mutating ssh-keyscan probe."""

        _validate_profile_identity(profile)
        keyscan_timeout = _keyscan_timeout(timeout_seconds)
        argv = [
            "ssh-keyscan",
            "-T",
            str(keyscan_timeout),
            "-t",
            "ed25519,ecdsa,rsa",
        ]
        if profile.port != 22:
            argv.extend(["-p", str(profile.port)])
        argv.append(profile.host)
        try:
            completed = self._runner(argv, timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("SSH host-key probe timed out") from exc
        except OSError as exc:
            raise RuntimeError("SSH host-key probe could not be started") from exc
        if completed.returncode != 0:
            raise RuntimeError("SSH host-key probe failed")
        output = completed.stdout or ""
        if len(output.encode("utf-8")) > _MAX_KEYSCAN_OUTPUT_BYTES:
            raise ValueError("ssh-keyscan output exceeds the allowed size")
        candidates = _parse_keyscan_output(output, profile.host, profile.port)
        return PendingHostKeyProbe(
            profile_id=profile.id,
            host=profile.host,
            port=profile.port,
            candidates=candidates,
        )

    def confirm(
        self,
        pending: PendingHostKeyProbe,
        *,
        profile: RemoteProfileConfig,
        algorithm: HostKeyAlgorithm,
        fingerprint: str,
        timeout_seconds: float = 10.0,
    ) -> TrustedKnownHostsBinding:
        """Persist one exact pending key after an unchanged second probe."""

        _validate_profile_identity(profile)
        if (profile.id, profile.host, profile.port) != (
            pending.profile_id,
            pending.host,
            pending.port,
        ):
            raise ValueError("remote profile does not match pending probe")
        selected = next(
            (
                candidate
                for candidate in pending.candidates
                if candidate.algorithm == algorithm and candidate.fingerprint == fingerprint
            ),
            None,
        )
        if selected is None:
            raise ValueError("host-key confirmation does not match a pending candidate")
        current = self.probe(profile, timeout_seconds=timeout_seconds)
        if current != pending:
            raise ValueError("SSH host keys changed before confirmation")
        return self._persist(profile, selected)

    def load(self, profile: RemoteProfileConfig) -> TrustedKnownHostsBinding | None:
        """Load and revalidate an existing binding for a profile, if present."""

        _validate_profile_identity(profile)
        try:
            self._root.lstat()
        except FileNotFoundError:
            return None
        path = self._root / _binding_filename(profile.id)
        with _opened_store_root(self._root, create=False) as root_fd:
            content = _read_secure_file(root_fd, path.name)
        if content is None:
            return None
        return _binding_from_content(path, content, profile)

    def _persist(
        self,
        profile: RemoteProfileConfig,
        selected: HostKeyCandidate,
    ) -> TrustedKnownHostsBinding:
        path = self._root / _binding_filename(profile.id)
        expected = _render_binding_content(profile, selected)
        with _opened_store_root(self._root, create=True) as root_fd:
            existing = _read_secure_file(root_fd, path.name)
            if existing is not None:
                binding = _binding_from_content(path, existing, profile)
                if existing != expected:
                    raise ValueError("confirmed host key conflicts with existing trust")
                return binding

            temp_name = f".{path.stem}.{secrets.token_hex(12)}.tmp"
            temp_created = False
            try:
                temp_fd = os.open(
                    temp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=root_fd,
                )
                temp_created = True
                try:
                    os.fchmod(temp_fd, 0o600)
                    _write_all(temp_fd, expected)
                    os.fsync(temp_fd)
                    _validate_file_stat(os.fstat(temp_fd), require_single_link=True)
                finally:
                    os.close(temp_fd)

                try:
                    os.link(
                        temp_name,
                        path.name,
                        src_dir_fd=root_fd,
                        dst_dir_fd=root_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raced = _read_secure_file(root_fd, path.name)
                    if raced != expected:
                        raise ValueError("known-host publish race or symlink detected") from exc
                finally:
                    if temp_created:
                        os.unlink(temp_name, dir_fd=root_fd)
                        temp_created = False
                os.fsync(root_fd)
                published = _read_secure_file(root_fd, path.name)
                if published != expected:
                    raise ValueError("known-host publication content changed")
                return _binding_from_content(path, published, profile)
            finally:
                if temp_created:
                    try:
                        os.unlink(temp_name, dir_fd=root_fd)
                    except FileNotFoundError:
                        pass


class _OpenedStoreRoot:
    def __init__(self, path: Path, *, create: bool) -> None:
        self._path = path
        self._create = create
        self._fd: int | None = None

    def __enter__(self) -> int:
        if self._create:
            try:
                os.mkdir(self._path, 0o700)
            except FileExistsError:
                pass
        before = _validate_root_path(self._path)
        try:
            root_fd = os.open(
                self._path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
        except OSError as exc:
            raise ValueError("provider known-host store root could not be opened safely") from exc
        try:
            after = os.fstat(root_fd)
            _validate_root_stat(after)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError("provider known-host store root changed during open")
        except Exception:
            os.close(root_fd)
            raise
        self._fd = root_fd
        return self._fd

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def _opened_store_root(path: Path, *, create: bool) -> _OpenedStoreRoot:
    return _OpenedStoreRoot(path, create=create)


def _run_keyscan(argv: list[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def _parse_keyscan_output(
    output: str,
    host: str,
    port: int,
) -> tuple[HostKeyCandidate, ...]:
    if "\r" in output:
        raise ValueError("ssh-keyscan returned noncanonical line endings")
    expected_host = _known_hosts_host(host, port)
    observed: dict[HostKeyAlgorithm, HostKeyCandidate] = {}
    lines = output.splitlines()
    if not lines:
        raise ValueError("ssh-keyscan returned no host keys")
    for line in lines:
        if not line or line != line.strip():
            raise ValueError("ssh-keyscan returned a noncanonical line")
        fields = line.split(" ")
        if len(fields) != 3 or any(not field for field in fields):
            raise ValueError("ssh-keyscan returned a malformed key line")
        host_field, key_type, encoded_key = fields
        if host_field != expected_host:
            raise ValueError("ssh-keyscan returned a key for a different host or port")
        algorithm = _KEY_TYPE_TO_ALGORITHM.get(key_type)
        if algorithm is None:
            raise ValueError("ssh-keyscan returned an unsupported host-key algorithm")
        public_key, fingerprint = _validate_public_key(key_type, encoded_key)
        if algorithm in observed:
            raise ValueError("ssh-keyscan returned a duplicate host-key algorithm")
        observed[algorithm] = HostKeyCandidate(
            algorithm=algorithm,
            public_key=public_key,
            fingerprint=fingerprint,
        )
    if not observed:
        raise ValueError("ssh-keyscan returned no supported host keys")
    return tuple(observed[algorithm] for algorithm in _ALGORITHM_ORDER if algorithm in observed)


def _validate_public_key(key_type: str, encoded_key: str) -> tuple[str, str]:
    try:
        blob = base64.b64decode(encoded_key, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("ssh-keyscan returned invalid public-key encoding") from exc
    if base64.b64encode(blob).decode("ascii") != encoded_key:
        raise ValueError("ssh-keyscan returned noncanonical public-key encoding")
    embedded_type_bytes, offset = _read_ssh_string(blob, 0)
    try:
        embedded_type = embedded_type_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("ssh-keyscan returned a malformed public-key type") from exc
    if embedded_type != key_type:
        raise ValueError("ssh-keyscan public-key type does not match its key blob")
    if key_type == "ssh-ed25519":
        key, offset = _read_ssh_string(blob, offset)
        if len(key) != 32:
            raise ValueError("ssh-keyscan returned a malformed Ed25519 public key")
    elif key_type == "ecdsa-sha2-nistp256":
        curve, offset = _read_ssh_string(blob, offset)
        point, offset = _read_ssh_string(blob, offset)
        if curve != b"nistp256" or len(point) != 65 or point[0] != 4:
            raise ValueError("ssh-keyscan returned a malformed NIST P-256 public key")
    elif key_type == "ssh-rsa":
        exponent, offset = _read_ssh_string(blob, offset)
        modulus, offset = _read_ssh_string(blob, offset)
        _validate_positive_mpint(exponent, "RSA exponent")
        _validate_positive_mpint(modulus, "RSA modulus")
        exponent_value = int.from_bytes(exponent, "big", signed=False)
        modulus_value = int.from_bytes(modulus, "big", signed=False)
        if exponent_value < 3 or exponent_value % 2 == 0 or modulus_value.bit_length() < 1024:
            raise ValueError("ssh-keyscan returned malformed RSA public-key parameters")
    else:  # pragma: no cover - guarded by the closed key-type map
        raise ValueError("ssh-keyscan returned an unsupported public-key type")
    if offset != len(blob):
        raise ValueError("ssh-keyscan returned trailing public-key data")
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return f"{key_type} {encoded_key}", f"SHA256:{digest}"


def _read_ssh_string(blob: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 4 > len(blob):
        raise ValueError("ssh-keyscan returned a truncated public key")
    length = struct.unpack(">I", blob[offset : offset + 4])[0]
    start = offset + 4
    end = start + length
    if length == 0 or end > len(blob):
        raise ValueError("ssh-keyscan returned a malformed public key")
    return blob[start:end], end


def _validate_positive_mpint(value: bytes, field_name: str) -> None:
    if value[0] & 0x80:
        raise ValueError(f"ssh-keyscan returned a negative {field_name}")
    if len(value) > 1 and value[0] == 0 and value[1] & 0x80 == 0:
        raise ValueError(f"ssh-keyscan returned a noncanonical {field_name}")
    if int.from_bytes(value, "big", signed=False) == 0:
        raise ValueError(f"ssh-keyscan returned a zero {field_name}")


def _render_binding_content(
    profile: RemoteProfileConfig,
    selected: HostKeyCandidate,
) -> bytes:
    metadata = {
        "algorithm": selected.algorithm,
        "fingerprint": selected.fingerprint,
        "host": profile.host,
        "port": profile.port,
        "profile_id": profile.id,
        "public_key": selected.public_key,
    }
    metadata_json = json.dumps(
        metadata,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    content = (
        f"{_METADATA_PREFIX}{metadata_json}\n"
        f"{_known_hosts_host(profile.host, profile.port)} {selected.public_key}\n"
    ).encode("utf-8")
    if len(content) > _MAX_TRUST_FILE_BYTES:
        raise ValueError("trusted host-key record exceeds the allowed size")
    return content


def _binding_from_content(
    path: Path,
    content: bytes,
    profile: RemoteProfileConfig,
) -> TrustedKnownHostsBinding:
    if len(content) > _MAX_TRUST_FILE_BYTES:
        raise ValueError("trusted known-host file exceeds the allowed size")
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("trusted known-host file content is not UTF-8") from exc
    lines = text.splitlines(keepends=True)
    if len(lines) != 2 or not all(line.endswith("\n") for line in lines):
        raise ValueError("trusted known-host file content is malformed")
    metadata_line = lines[0][:-1]
    if not metadata_line.startswith(_METADATA_PREFIX):
        raise ValueError("trusted known-host file content is malformed")
    try:
        metadata = json.loads(
            metadata_line.removeprefix(_METADATA_PREFIX),
            object_pairs_hook=_strict_json_object,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("trusted known-host metadata is invalid") from exc
    if type(metadata) is not dict or set(metadata) != _METADATA_FIELDS:
        raise ValueError("trusted known-host metadata has unexpected fields")
    if (
        type(metadata["profile_id"]) is not str
        or type(metadata["host"]) is not str
        or type(metadata["port"]) is not int
        or type(metadata["algorithm"]) is not str
        or type(metadata["public_key"]) is not str
        or type(metadata["fingerprint"]) is not str
    ):
        raise ValueError("trusted known-host metadata has invalid field types")
    if (metadata["profile_id"], metadata["host"], metadata["port"]) != (
        profile.id,
        profile.host,
        profile.port,
    ):
        raise ValueError("trusted known-host metadata does not match remote profile")
    public_key_fields = metadata["public_key"].split(" ")
    if len(public_key_fields) != 2:
        raise ValueError("trusted known-host metadata has an invalid public key")
    public_key, fingerprint = _validate_public_key(*public_key_fields)
    algorithm = _KEY_TYPE_TO_ALGORITHM.get(public_key_fields[0])
    if (
        algorithm is None
        or metadata["algorithm"] != algorithm
        or metadata["public_key"] != public_key
        or metadata["fingerprint"] != fingerprint
    ):
        raise ValueError("trusted known-host fingerprint or algorithm is invalid")
    candidate = HostKeyCandidate(
        algorithm=algorithm,
        public_key=public_key,
        fingerprint=fingerprint,
    )
    canonical = _render_binding_content(profile, candidate)
    if content != canonical:
        raise ValueError("trusted known-host file content is not canonical")
    return TrustedKnownHostsBinding(
        profile_id=profile.id,
        host=profile.host,
        port=profile.port,
        algorithm=algorithm,
        public_key=public_key,
        fingerprint=fingerprint,
        known_hosts_file=path,
    )


def _read_secure_file(root_fd: int, name: str) -> bytes | None:
    try:
        before = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    _validate_file_stat(before, require_single_link=True)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
    except OSError as exc:
        raise ValueError("trusted known-host file could not be opened safely") from exc
    try:
        opened = os.fstat(fd)
        _validate_file_stat(opened, require_single_link=True)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("trusted known-host file changed during open")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(8192, _MAX_TRUST_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_TRUST_FILE_BYTES:
                raise ValueError("trusted known-host file exceeds the allowed size")
        after = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("trusted known-host file changed while reading")
    finally:
        os.close(fd)
    try:
        final = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except OSError as exc:
        raise ValueError("trusted known-host file path changed while reading") from exc
    _validate_file_stat(final, require_single_link=True)
    if (after.st_dev, after.st_ino) != (final.st_dev, final.st_ino):
        raise ValueError("trusted known-host file path changed while reading")
    return b"".join(chunks)


def _validate_root_path(path: Path) -> os.stat_result:
    try:
        result = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("provider known-host store root does not exist") from exc
    _validate_root_stat(result)
    return result


def _validate_root_stat(result: os.stat_result) -> None:
    if not stat.S_ISDIR(result.st_mode):
        raise ValueError("provider known-host store root must be a regular directory")
    if result.st_uid != os.geteuid():
        raise ValueError("provider known-host store root must be owner-controlled")
    if stat.S_IMODE(result.st_mode) != 0o700:
        raise ValueError("provider known-host store root must have mode 0700")


def _validate_file_stat(result: os.stat_result, *, require_single_link: bool) -> None:
    if not stat.S_ISREG(result.st_mode):
        raise ValueError("trusted known-host file must be a regular file, not a symlink")
    if result.st_uid != os.geteuid():
        raise ValueError("trusted known-host file must be owner-controlled")
    if stat.S_IMODE(result.st_mode) != 0o600:
        raise ValueError("trusted known-host file must have mode 0600")
    if require_single_link and result.st_nlink != 1:
        raise ValueError("trusted known-host file must have exactly one link")


def _validate_profile_identity(profile: RemoteProfileConfig) -> None:
    _validate_host(profile.host)
    if isinstance(profile.port, bool) or not 1 <= int(profile.port) <= 65535:
        raise ValueError("remote profile port must be between 1 and 65535")
    if (
        not profile.id
        or len(profile.id.encode("utf-8")) > 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in profile.id)
    ):
        raise ValueError("remote profile id is invalid for host-key trust")


def _validate_host(host: str) -> None:
    if (
        not host
        or host.startswith("-")
        or "@" in host
        or any(ord(character) < 32 or ord(character) == 127 for character in host)
    ):
        raise ValueError("remote profile host contains unsupported characters")
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    if len(host) > 253 or _HOSTNAME_RE.fullmatch(host) is None:
        raise ValueError("remote profile host contains unsupported characters")


def _known_hosts_host(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def _binding_filename(profile_id: str) -> str:
    digest = hashlib.sha256(profile_id.encode("utf-8")).hexdigest()
    return f"{digest}.known_hosts"


def _validate_path_text(path: Path) -> None:
    text = str(path)
    if (
        "%" in text
        or "\\" in text
        or '"' in text
        or "'" in text
        or any(character.isspace() or ord(character) == 127 for character in text)
    ):
        raise ValueError("provider known-host store path contains unsupported characters")


def _keyscan_timeout(timeout_seconds: float) -> int:
    if (
        isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > 60
    ):
        raise ValueError("host-key probe timeout must be between 0 and 60 seconds")
    return max(1, math.ceil(timeout_seconds))


def _write_all(fd: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(fd, content[offset:])
        if written <= 0:
            raise OSError("failed to write trusted known-host file")
        offset += written


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


__all__ = [
    "HostKeyAlgorithm",
    "HostKeyCandidate",
    "PendingHostKeyProbe",
    "ProviderKnownHostStore",
    "TrustedKnownHostsBinding",
]
