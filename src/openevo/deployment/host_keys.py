from __future__ import annotations

import base64
import binascii
import errno
import fcntl
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

from openevo.deployment.profile import RemoteProfileConfig, SystemOpenSshAliasProfile
from openevo.deployment.system_executables import (
    SSH_KEYSCAN_EXECUTABLE,
    VerifiedSystemExecutable,
)

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
_MAX_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_RETRY_SECONDS = 0.01
_LOCK_NAME = ".openevo-host-key.lock"

_TUNNEL_REGISTRY_GUARD = threading.Lock()
_TUNNEL_CLOSERS: dict[tuple[int, int, str, str], set[Callable[[], None]]] = {}
_MAX_KNOWN_HOST_SPAWN_LEASES = 64
_SPAWN_LEASE_REGISTRY_GUARD = threading.Lock()
_SPAWN_LEASES: dict[int, "_KnownHostsSpawnLease"] = {}
_MAX_STORE_LOCK_AUTHORITIES = 64
_STORE_LOCK_REGISTRY_GUARD = threading.Lock()
_STORE_LOCK_AUTHORITIES: dict[int, "_StoreLockAuthority"] = {}

_MAX_SYSTEM_SSH_DIAGNOSTIC_BYTES = 64 << 10
_MAX_SYSTEM_SSH_CONFIG_BYTES = 256 << 10
_MAX_SYSTEM_SSH_CONFIG_LINES = 4_096
_MAX_SYSTEM_SSH_CONFIG_LINE_BYTES = 4_096
_MAX_SYSTEM_KNOWN_HOSTS_BYTES = 8 << 20
_MAX_SYSTEM_HOST_KEY_REVIEWS = 64
_SYSTEM_FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_SYSTEM_CHANGED_KEY_MARKER = "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!"
_SYSTEM_STRICT_FIRST_USE_MARKER = "you have requested strict checking"
_SYSTEM_KEY_NAME_TO_ALGORITHM = {
    "ED25519": "ssh-ed25519",
    "RSA": "ssh-rsa",
}
_SYSTEM_CONFIG_FIELDS = frozenset(
    {
        "canonicalizehostname",
        "globalknownhostsfile",
        "hashknownhosts",
        "hostkeyalias",
        "hostname",
        "knownhostscommand",
        "port",
        "stricthostkeychecking",
        "userknownhostsfile",
    }
)
_SYSTEM_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._%-]{0,252}$")


class HostKeyStoreErrorCode(str, Enum):
    HOST_KEY_IN_USE = "host_key_in_use"
    ROTATION_INDETERMINATE = "host_key_rotation_indeterminate"


class HostKeyStoreError(RuntimeError):
    """A typed failure safe to surface without trust-store path details."""

    def __init__(
        self,
        code: HostKeyStoreErrorCode,
        *,
        authoritative_fingerprint: str | None = None,
    ) -> None:
        self.code = code
        self.authoritative_fingerprint = authoritative_fingerprint
        if code is HostKeyStoreErrorCode.ROTATION_INDETERMINATE:
            if authoritative_fingerprint is None:
                raise ValueError(
                    "indeterminate host-key rotation requires a candidate fingerprint"
                )
            message = (
                "SSH host-key rotation may have committed. Authoritatively reload trust "
                f"using the confirmed candidate fingerprint {authoritative_fingerprint} "
                "before retrying."
            )
        else:
            message = "SSH host-key trust is in use. Close the active tunnel and retry."
        super().__init__(message)


class SystemHostKeyFailureCode(str, Enum):
    """Closed outcomes derived from bounded system-OpenSSH diagnostics."""

    FIRST_USE_FORBIDDEN = "ssh_first_use_forbidden"
    CHANGED = "ssh_host_key_changed"
    VERIFICATION_FAILED = "ssh_host_key_verification_failed"


@dataclass(frozen=True, slots=True)
class SystemHostKeyFailureEvidence:
    """Internal evidence with path-bearing fields excluded from representations."""

    code: SystemHostKeyFailureCode
    presented_fingerprints: tuple[tuple[str, str], ...]
    offending_known_hosts_file: Path | None = field(repr=False, compare=False)
    offending_line: int | None = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SystemKnownHostsPolicy:
    """Fail-closed classification of the effective user trust configuration."""

    repair_support: Literal[
        "automatic_replacement_available",
        "administrator_required",
    ]
    reason: str
    known_hosts_file: Path | None = field(repr=False)
    lookup_token: str | None = field(repr=False)
    _file_identity: tuple[int, int, int, int, int, int, int, int] | None = field(
        repr=False,
        compare=False,
    )


@dataclass(frozen=True, slots=True)
class PendingSystemHostKeyReview:
    """One digest-bound changed-key decision safe to project into Local API v2."""

    review_id: str
    review_sha256: str
    profile_id: str
    connection_generation: int
    key_fingerprints: tuple[tuple[str, str], ...]
    repair_support: Literal[
        "automatic_replacement_available",
        "administrator_required",
    ]
    _policy: SystemKnownHostsPolicy = field(repr=False, compare=False)
    _authority_token: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SystemHostKeyReplacement:
    """Private exact input for one verified ``ssh-keygen -R`` invocation."""

    known_hosts_file: Path = field(repr=False)
    lookup_token: str = field(repr=False)
    _file_identity: tuple[int, int, int, int, int, int, int, int] = field(
        repr=False,
        compare=False,
    )

    def verify_current(self) -> None:
        observed = _system_known_hosts_file_identity(self.known_hosts_file)
        if observed != self._file_identity:
            raise ValueError("system known-hosts replacement is no longer current")

    def verify_replaced(self) -> None:
        observed = _system_known_hosts_file_identity(self.known_hosts_file)
        if observed == self._file_identity:
            raise ValueError("system known-hosts replacement did not change the trust store")


@dataclass(slots=True)
class _SystemHostKeyReviewRecord:
    review: PendingSystemHostKeyReview
    consumed: bool = False


class SystemHostKeyReviewAuthority:
    """Issue and consume bounded changed-key review identities."""

    def __init__(self, *, hmac_key: bytes | None = None) -> None:
        if hmac_key is None:
            hmac_key = secrets.token_bytes(32)
        if type(hmac_key) is not bytes or len(hmac_key) != 32:
            raise ValueError("system host-key review key must contain exactly 32 bytes")
        self._hmac_key = bytearray(hmac_key)
        self._token = object()
        self._lock = threading.Lock()
        self._records: dict[str, _SystemHostKeyReviewRecord] = {}
        self._closed = False

    def issue(
        self,
        profile: SystemOpenSshAliasProfile,
        *,
        connection_generation: int,
        evidence: SystemHostKeyFailureEvidence,
        policy: SystemKnownHostsPolicy,
    ) -> PendingSystemHostKeyReview:
        if not isinstance(profile, SystemOpenSshAliasProfile):
            raise TypeError("system host-key review requires an alias profile")
        if (
            type(connection_generation) is not int
            or not 1 <= connection_generation <= (1 << 53) - 1
        ):
            raise ValueError("system host-key review generation is invalid")
        if (
            not isinstance(evidence, SystemHostKeyFailureEvidence)
            or evidence.code is not SystemHostKeyFailureCode.CHANGED
            or not evidence.presented_fingerprints
        ):
            raise ValueError("system host-key review requires changed-key evidence")
        if not isinstance(policy, SystemKnownHostsPolicy):
            raise TypeError("system host-key review policy is invalid")
        review_id = f"host-review-{secrets.token_hex(12)}"
        payload = _canonical_system_host_key_review(
            review_id=review_id,
            profile=profile,
            connection_generation=connection_generation,
            evidence=evidence,
            policy=policy,
        )
        review_sha256 = hashlib.sha256(payload).hexdigest()
        authority_token = hmac.new(bytes(self._hmac_key), payload, hashlib.sha256).digest()
        review = PendingSystemHostKeyReview(
            review_id=review_id,
            review_sha256=review_sha256,
            profile_id=profile.profile_id,
            connection_generation=connection_generation,
            key_fingerprints=evidence.presented_fingerprints,
            repair_support=policy.repair_support,
            _policy=policy,
            _authority_token=(self._token, authority_token),
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("system host-key review authority is closed")
            if len(self._records) >= _MAX_SYSTEM_HOST_KEY_REVIEWS:
                raise RuntimeError("system host-key review capacity is exhausted")
            self._records[review_id] = _SystemHostKeyReviewRecord(review=review)
        return review

    def claim_replacement(
        self,
        review: PendingSystemHostKeyReview,
        *,
        profile: SystemOpenSshAliasProfile,
        connection_generation: int,
        review_id: str,
        review_sha256: str,
    ) -> SystemHostKeyReplacement:
        if not isinstance(review, PendingSystemHostKeyReview) or not isinstance(
            profile, SystemOpenSshAliasProfile
        ):
            raise TypeError("system host-key replacement authority is invalid")
        if connection_generation != review.connection_generation:
            raise ValueError("system host-key review generation changed")
        if profile.profile_id != review.profile_id:
            raise ValueError("system host-key review profile changed")
        if review_id != review.review_id or not hmac.compare_digest(
            review_sha256,
            review.review_sha256,
        ):
            raise ValueError("system host-key review identity does not match")
        token = review._authority_token
        if (
            type(token) is not tuple
            or len(token) != 2
            or token[0] is not self._token
            or type(token[1]) is not bytes
        ):
            raise ValueError("system host-key review authority is invalid")
        policy = review._policy
        if (
            policy.repair_support != "automatic_replacement_available"
            or policy.known_hosts_file is None
            or policy.lookup_token is None
            or policy._file_identity is None
        ):
            raise ValueError("system host-key review requires administrator action")
        observed = _system_known_hosts_file_identity(policy.known_hosts_file)
        if observed != policy._file_identity:
            raise ValueError("system host-key review is no longer current")
        with self._lock:
            if self._closed:
                raise ValueError("system host-key review is no longer current")
            record = self._records.get(review.review_id)
            if record is None or record.review is not review or record.consumed:
                raise ValueError("system host-key review is no longer current")
            payload = _canonical_system_host_key_review(
                review_id=review.review_id,
                profile=profile,
                connection_generation=connection_generation,
                evidence=SystemHostKeyFailureEvidence(
                    code=SystemHostKeyFailureCode.CHANGED,
                    presented_fingerprints=review.key_fingerprints,
                    offending_known_hosts_file=policy.known_hosts_file,
                    offending_line=None,
                ),
                policy=policy,
            )
            # Offending line numbers are diagnostic-only and deliberately do not
            # participate in the review identity reconstructed here.
            expected_token = hmac.new(bytes(self._hmac_key), payload, hashlib.sha256).digest()
            if not hmac.compare_digest(token[1], expected_token):
                raise ValueError("system host-key review authority is invalid")
            record.consumed = True
        return SystemHostKeyReplacement(
            known_hosts_file=policy.known_hosts_file,
            lookup_token=policy.lookup_token,
            _file_identity=policy._file_identity,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._records.clear()
            _zero_bytearray(self._hmac_key)


def classify_system_openssh_host_key_failure(
    stderr: bytes,
) -> SystemHostKeyFailureEvidence:
    """Classify only bounded OpenSSH host-trust diagnostics.

    The returned representation never contains the host, trust-store path, raw
    stderr, or another free-form diagnostic.  Incomplete evidence collapses to
    one generic verification failure.
    """

    generic = SystemHostKeyFailureEvidence(
        code=SystemHostKeyFailureCode.VERIFICATION_FAILED,
        presented_fingerprints=(),
        offending_known_hosts_file=None,
        offending_line=None,
    )
    if type(stderr) is not bytes or not stderr or len(stderr) > _MAX_SYSTEM_SSH_DIAGNOSTIC_BYTES:
        return generic
    try:
        text = stderr.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return generic
    if "\x00" in text or "\r" in text.replace("\r\n", ""):
        return generic
    text = text.replace("\r\n", "\n")
    lines = text.splitlines()
    if not lines or len(lines) > 1_024 or any(len(line.encode("utf-8")) > 4_096 for line in lines):
        return generic
    if _SYSTEM_CHANGED_KEY_MARKER not in text:
        if (
            _SYSTEM_STRICT_FIRST_USE_MARKER in text.casefold()
            and "host key verification failed" in text.casefold()
        ):
            return SystemHostKeyFailureEvidence(
                code=SystemHostKeyFailureCode.FIRST_USE_FORBIDDEN,
                presented_fingerprints=(),
                offending_known_hosts_file=None,
                offending_line=None,
            )
        return generic

    algorithm: str | None = None
    fingerprint: str | None = None
    offending_path: Path | None = None
    offending_line: int | None = None
    for index, line in enumerate(lines):
        match = re.fullmatch(
            r"The fingerprint for the ([A-Z0-9-]{2,32}) key sent by the remote host is",
            line,
        )
        if match is not None and index + 1 < len(lines):
            candidate_algorithm = _SYSTEM_KEY_NAME_TO_ALGORITHM.get(match.group(1))
            candidate_fingerprint = lines[index + 1].removesuffix(".")
            if (
                candidate_algorithm is None
                or _SYSTEM_FINGERPRINT_RE.fullmatch(candidate_fingerprint) is None
                or algorithm is not None
            ):
                return generic
            algorithm = candidate_algorithm
            fingerprint = candidate_fingerprint
        match = re.fullmatch(
            r"Offending ([A-Z0-9-]{2,32}) key in (.{1,4096}):([1-9][0-9]{0,9})",
            line,
        )
        if match is not None:
            candidate_algorithm = _SYSTEM_KEY_NAME_TO_ALGORITHM.get(match.group(1))
            candidate_path = Path(match.group(2))
            if (
                candidate_algorithm is None
                or algorithm is not None
                and candidate_algorithm != algorithm
                or offending_path is not None
                or not _valid_system_local_path(candidate_path)
            ):
                return generic
            offending_path = candidate_path
            offending_line = int(match.group(3))
    if algorithm is None or fingerprint is None or offending_path is None:
        return generic
    return SystemHostKeyFailureEvidence(
        code=SystemHostKeyFailureCode.CHANGED,
        presented_fingerprints=((algorithm, fingerprint),),
        offending_known_hosts_file=offending_path,
        offending_line=offending_line,
    )


def inspect_system_known_hosts_policy(
    config_output: bytes,
    *,
    home: Path | str,
    offending_known_hosts_file: Path | None,
    conditional_config: bool = False,
) -> SystemKnownHostsPolicy:
    """Classify whether exact ``ssh-keygen -R`` repair is safe.

    ``config_output`` must be the bounded output of ``/usr/bin/ssh -G --
    <literal-alias>``.  Unknown OpenSSH fields are ignored, while duplicate or
    malformed authority fields fail closed.
    """

    if type(conditional_config) is not bool:
        raise TypeError("conditional OpenSSH config flag must be boolean")
    home_path = Path(home)
    if not _valid_system_local_path(home_path):
        raise ValueError("system OpenSSH home is invalid")
    if conditional_config:
        return _administrator_system_known_hosts_policy("conditional_config")
    values = _parse_system_openssh_config(config_output)
    if values is None:
        return _administrator_system_known_hosts_policy("unsupported_config_output")
    known_hosts_command = values.get("knownhostscommand")
    if known_hosts_command not in {None, "none"}:
        return _administrator_system_known_hosts_policy("known_hosts_command")
    host_key_alias = values.get("hostkeyalias")
    if host_key_alias not in {None, "none"}:
        return _administrator_system_known_hosts_policy("host_key_alias")
    if values.get("hashknownhosts") != "no":
        return _administrator_system_known_hosts_policy("unsupported_hash_policy")
    user_files_value = values.get("userknownhostsfile")
    if user_files_value is None:
        return _administrator_system_known_hosts_policy("no_user_known_hosts_file")
    user_files = user_files_value.split(" ")
    if len(user_files) != 1 or not user_files[0]:
        return _administrator_system_known_hosts_policy("multiple_user_known_hosts_files")
    path = _expand_system_known_hosts_path(user_files[0], home_path)
    if path is None:
        return _administrator_system_known_hosts_policy("unsafe_user_known_hosts_file")
    if offending_known_hosts_file is None or path != offending_known_hosts_file:
        return _administrator_system_known_hosts_policy("changed_key_source_mismatch")
    lookup_token = _system_known_hosts_lookup_token(
        values.get("hostname"),
        values.get("port"),
    )
    if lookup_token is None:
        return _administrator_system_known_hosts_policy("unsupported_lookup_token")
    try:
        _validate_system_known_hosts_parent_chain(path, home_path)
        identity = _system_known_hosts_file_identity(path)
    except (OSError, ValueError):
        return _administrator_system_known_hosts_policy("unsafe_user_known_hosts_file")
    return SystemKnownHostsPolicy(
        repair_support="automatic_replacement_available",
        reason="simple_user_known_hosts",
        known_hosts_file=path,
        lookup_token=lookup_token,
        _file_identity=identity,
    )


def _parse_system_openssh_config(output: bytes) -> dict[str, str] | None:
    if type(output) is not bytes or not output or len(output) > _MAX_SYSTEM_SSH_CONFIG_BYTES:
        return None
    try:
        text = output.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if "\r" in text or "\x00" in text:
        return None
    lines = text.splitlines()
    if not lines or len(lines) > _MAX_SYSTEM_SSH_CONFIG_LINES:
        return None
    values: dict[str, str] = {}
    for line in lines:
        if (
            not line
            or line != line.strip()
            or len(line.encode("utf-8")) > _MAX_SYSTEM_SSH_CONFIG_LINE_BYTES
            or " " not in line
        ):
            return None
        key, value = line.split(" ", 1)
        if not key or not value:
            return None
        key = key.casefold()
        if key not in _SYSTEM_CONFIG_FIELDS:
            continue
        if key in values:
            return None
        values[key] = value
    return values


def _expand_system_known_hosts_path(value: str, home: Path) -> Path | None:
    if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    if value.startswith("~/"):
        candidate = home / value[2:]
    else:
        candidate = Path(value)
    if not _valid_system_local_path(candidate):
        return None
    try:
        candidate.relative_to(home)
    except ValueError:
        return None
    return candidate


def _system_known_hosts_lookup_token(
    hostname: str | None,
    port_value: str | None,
) -> str | None:
    if (
        hostname is None
        or port_value is None
        or not port_value.isascii()
        or not port_value.isdecimal()
    ):
        return None
    port = int(port_value)
    if not 1 <= port <= 65_535 or hostname.startswith("-") or "," in hostname:
        return None
    try:
        parsed = ipaddress.ip_address(hostname)
    except ValueError:
        if _SYSTEM_HOSTNAME_RE.fullmatch(hostname) is None:
            return None
        normalized = hostname
    else:
        normalized = str(parsed)
    if port == 22:
        return normalized
    return f"[{normalized}]:{port}"


def _valid_system_local_path(path: Path) -> bool:
    value = os.fspath(path)
    encoded = os.fsencode(path)
    return (
        path.is_absolute()
        and bool(path.name)
        and len(encoded) <= 4_096
        and b"\x00" not in encoded
        and len(path.parts) <= 64
        and all(part not in {"", ".", ".."} for part in path.parts[1:])
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _validate_system_known_hosts_parent_chain(path: Path, home: Path) -> None:
    relative = path.relative_to(home)
    if len(relative.parts) < 2:
        raise ValueError("system known-hosts file has no private parent")
    current = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        home_metadata = os.fstat(current)
        if not stat.S_ISDIR(home_metadata.st_mode) or home_metadata.st_uid != os.geteuid():
            raise ValueError("system OpenSSH home is unsafe")
        for component in relative.parts[:-1]:
            following = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current,
            )
            try:
                metadata = os.fstat(following)
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                ):
                    raise ValueError("system known-hosts parent is unsafe")
            except BaseException:
                os.close(following)
                raise
            os.close(current)
            current = following
    finally:
        os.close(current)


def _system_known_hosts_file_identity(
    path: Path,
) -> tuple[int, int, int, int, int, int, int, int]:
    metadata = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or not metadata.st_mode & stat.S_IWUSR
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or metadata.st_size > _MAX_SYSTEM_KNOWN_HOSTS_BYTES
    ):
        raise ValueError("system known-hosts file is unsafe")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _administrator_system_known_hosts_policy(reason: str) -> SystemKnownHostsPolicy:
    return SystemKnownHostsPolicy(
        repair_support="administrator_required",
        reason=reason,
        known_hosts_file=None,
        lookup_token=None,
        _file_identity=None,
    )


def _canonical_system_host_key_review(
    *,
    review_id: str,
    profile: SystemOpenSshAliasProfile,
    connection_generation: int,
    evidence: SystemHostKeyFailureEvidence,
    policy: SystemKnownHostsPolicy,
) -> bytes:
    hidden_policy_identity = None
    if (
        policy.known_hosts_file is not None
        and policy.lookup_token is not None
        and policy._file_identity is not None
    ):
        hidden_policy_identity = hashlib.sha256(
            json.dumps(
                {
                    "file": os.fspath(policy.known_hosts_file),
                    "identity": policy._file_identity,
                    "lookup_token": policy.lookup_token,
                },
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
    value = {
        "connection_generation": connection_generation,
        "failure_code": evidence.code.value,
        "hidden_policy_identity": hidden_policy_identity,
        "key_fingerprints": evidence.presented_fingerprints,
        "profile_id": profile.profile_id,
        "repair_support": policy.repair_support,
        "review_id": review_id,
        "schema_version": 1,
        "ssh_host_alias": profile.ssh_host_alias,
    }
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


@dataclass(frozen=True)
class HostKeyCandidate:
    """A host key observed by a probe but not yet trusted."""

    algorithm: HostKeyAlgorithm
    public_key: str
    fingerprint: str


@dataclass(frozen=True)
class PendingHostKeyProbe:
    """A store-issued host-key observation awaiting exact user confirmation."""

    profile_id: str
    host: str
    port: int
    candidates: tuple[HostKeyCandidate, ...]
    _store_token: object = field(repr=False, compare=False)
    _digest: str = field(repr=False, compare=False)


@dataclass
class _RotationCommitState:
    committed: bool = False


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
    _anchor: _StoreAnchor = field(repr=False, compare=False)

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
        with self._anchor.locked_root(create=False, exclusive=False) as root_fd:
            if root_fd is None:
                raise ValueError("trusted known-host file is missing")
            content = _read_secure_file(root_fd, path.name)
        if content is None:
            raise ValueError("trusted known-host file is missing")
        binding = _binding_from_content(path, content, profile, self._anchor)
        if binding != self:
            raise ValueError("trusted known-host file content does not match binding")

    def open_for_spawn(
        self,
        profile: RemoteProfileConfig,
    ) -> AbstractContextManager[Path]:
        """Publish a stable private copy for one SSH subprocess lifecycle."""

        return _KnownHostsSpawnLease(self, profile)

    def _register_tunnel(self, closer: Callable[[], None]) -> Callable[[], None]:
        return _register_tunnel_closer(
            self._anchor.registry_key,
            self.profile_id,
            closer,
        )


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
        secure_ancestor: Path | str | None = None,
        runner: KeyscanRunner | None = None,
        lock_timeout_seconds: float = 1.0,
    ) -> None:
        requested_root = Path(root).expanduser()
        if not requested_root.is_absolute():
            raise ValueError("provider known-host store root must be absolute")
        _validate_path_text(requested_root)
        requested_ancestor = (
            Path(secure_ancestor).expanduser()
            if secure_ancestor is not None
            else requested_root.parent
        )
        if not requested_ancestor.is_absolute():
            raise ValueError("provider known-host secure ancestor must be absolute")
        _validate_path_text(requested_ancestor)
        if requested_root.parent != requested_ancestor:
            raise ValueError(
                "provider known-host store root must be a direct child of its secure ancestor"
            )
        self._root = _canonical_darwin_system_alias(requested_root)
        ancestor = _canonical_darwin_system_alias(requested_ancestor)
        if self._root.parent != ancestor:
            raise ValueError(
                "provider known-host store root must remain a direct child after canonicalization"
            )
        self._anchor = _StoreAnchor(
            self._root,
            ancestor,
            requested_secure_ancestor=requested_ancestor,
            lock_timeout_seconds=_validate_lock_timeout(lock_timeout_seconds),
        )
        self._runner = runner or _run_keyscan
        self._pending_token = object()
        self._pending_digest_key = secrets.token_bytes(32)

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
            SSH_KEYSCAN_EXECUTABLE,
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
        return self._issue_pending(profile, candidates)

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

        selected = self._confirm_pending(
            pending,
            profile=profile,
            algorithm=algorithm,
            fingerprint=fingerprint,
            timeout_seconds=timeout_seconds,
        )
        return self._persist(profile, selected)

    def _confirm_pending(
        self,
        pending: PendingHostKeyProbe,
        *,
        profile: RemoteProfileConfig,
        algorithm: HostKeyAlgorithm,
        fingerprint: str,
        timeout_seconds: float = 10.0,
    ) -> HostKeyCandidate:
        """Validate a store-issued observation and repeat it before mutation."""

        self._validate_pending(pending, profile)
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
        if (
            current.profile_id,
            current.host,
            current.port,
            current.candidates,
        ) != (
            pending.profile_id,
            pending.host,
            pending.port,
            pending.candidates,
        ):
            raise ValueError("SSH host keys changed before confirmation")
        self._validate_pending(current, profile)
        return selected

    def _issue_pending(
        self,
        profile: RemoteProfileConfig,
        candidates: tuple[HostKeyCandidate, ...],
    ) -> PendingHostKeyProbe:
        payload = _canonical_pending_payload(profile.id, profile.host, profile.port, candidates)
        return PendingHostKeyProbe(
            profile_id=profile.id,
            host=profile.host,
            port=profile.port,
            candidates=candidates,
            _store_token=self._pending_token,
            _digest=hmac.new(self._pending_digest_key, payload, hashlib.sha256).hexdigest(),
        )

    def _validate_pending(
        self,
        pending: PendingHostKeyProbe,
        profile: RemoteProfileConfig,
    ) -> None:
        _validate_profile_identity(profile)
        if pending._store_token is not self._pending_token:
            raise ValueError("pending host-key probe was not issued by this store")
        if (profile.id, profile.host, profile.port) != (
            pending.profile_id,
            pending.host,
            pending.port,
        ):
            raise ValueError("remote profile does not match pending probe")
        payload = _canonical_pending_payload(
            pending.profile_id,
            pending.host,
            pending.port,
            pending.candidates,
        )
        expected = hmac.new(self._pending_digest_key, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(pending._digest, expected):
            raise ValueError("pending host-key probe digest does not match its observation")

    def load(
        self,
        profile: RemoteProfileConfig,
        *,
        expected_fingerprint: str,
    ) -> TrustedKnownHostsBinding | None:
        """Compare-and-load a binding already attached to the caller's profile."""

        _validate_profile_identity(profile)
        path = self._root / _binding_filename(profile.id)
        with self._anchor.locked_root(create=False, exclusive=False) as root_fd:
            if root_fd is None:
                return None
            content = _read_secure_file(root_fd, path.name)
        if content is None:
            return None
        binding = _binding_from_content(path, content, profile, self._anchor)
        if binding.fingerprint != expected_fingerprint:
            raise ValueError("trusted host-key fingerprint does not match expected fingerprint")
        return binding

    def revoke(
        self,
        profile: RemoteProfileConfig,
        *,
        expected_fingerprint: str,
    ) -> None:
        """Atomically remove exact trust after callers close tunnels/transports."""

        _validate_profile_identity(profile)
        _request_tunnel_closure(self._anchor.registry_key, profile.id)
        path = self._root / _binding_filename(profile.id)
        with self._anchor.locked_root(create=False, exclusive=True) as root_fd:
            if root_fd is None:
                raise ValueError("trusted host key is missing")
            content = _read_secure_file(root_fd, path.name)
            if content is None:
                raise ValueError("trusted host key is missing")
            current = _binding_from_content(path, content, profile, self._anchor)
            if current.fingerprint != expected_fingerprint:
                raise ValueError("trusted host key does not match expected fingerprint")
            os.unlink(path.name, dir_fd=root_fd)
            os.fsync(root_fd)

    def rotate_from_pending(
        self,
        pending: PendingHostKeyProbe,
        *,
        profile: RemoteProfileConfig,
        algorithm: HostKeyAlgorithm,
        fingerprint: str,
        expected_old_fingerprint: str,
        timeout_seconds: float = 10.0,
    ) -> TrustedKnownHostsBinding:
        """Re-probe and atomically CAS exact old trust to one pending candidate."""

        selected = self._confirm_pending(
            pending,
            profile=profile,
            algorithm=algorithm,
            fingerprint=fingerprint,
            timeout_seconds=timeout_seconds,
        )
        _request_tunnel_closure(self._anchor.registry_key, profile.id)
        path = self._root / _binding_filename(profile.id)
        expected = _render_binding_content(profile, selected)
        commit_state = _RotationCommitState()
        published_binding: TrustedKnownHostsBinding | None = None
        post_commit_failed = False
        try:
            with self._anchor.locked_root(create=False, exclusive=True) as root_fd:
                if root_fd is None:
                    raise ValueError("trusted host key is missing")
                content = _read_secure_file(root_fd, path.name)
                if content is None:
                    raise ValueError("trusted host key is missing")
                current = _binding_from_content(path, content, profile, self._anchor)
                if current.fingerprint != expected_old_fingerprint:
                    raise ValueError("trusted host key does not match expected fingerprint")
                published_binding = _replace_secure_file(
                    root_fd,
                    path,
                    expected,
                    profile=profile,
                    anchor=self._anchor,
                    commit_state=commit_state,
                )
        except BaseException:
            if not commit_state.committed:
                raise
            post_commit_failed = True
        if post_commit_failed:
            raise HostKeyStoreError(
                HostKeyStoreErrorCode.ROTATION_INDETERMINATE,
                authoritative_fingerprint=selected.fingerprint,
            )
        assert published_binding is not None
        return published_binding

    def _persist(
        self,
        profile: RemoteProfileConfig,
        selected: HostKeyCandidate,
    ) -> TrustedKnownHostsBinding:
        path = self._root / _binding_filename(profile.id)
        expected = _render_binding_content(profile, selected)
        with self._anchor.locked_root(create=True, exclusive=True) as root_fd:
            assert root_fd is not None
            existing = _read_secure_file(root_fd, path.name)
            if existing is not None:
                binding = _binding_from_content(path, existing, profile, self._anchor)
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
                return _binding_from_content(path, published, profile, self._anchor)
            finally:
                if temp_created:
                    try:
                        os.unlink(temp_name, dir_fd=root_fd)
                    except FileNotFoundError:
                        pass


class _StoreLockAuthority(AbstractContextManager[int | None]):
    """Own one lock descriptor until unlock and close are both proven."""

    def __init__(self, anchor: _StoreAnchor, *, create: bool, exclusive: bool) -> None:
        self._anchor = anchor
        self._create = create
        self._exclusive = exclusive
        self._root_fd: int | None = None
        self._root_identity: tuple[int, int] | None = None
        self._lock_fd: int | None = None
        self._lock_identity: tuple[int, int] | None = None
        self._locked = False
        self._slot_held = False
        self._cleanup_requested = False
        self._entered = False
        self._released = False
        self._cleanup_guard = threading.Lock()

    def __enter__(self) -> int | None:
        if self._entered or self._released:
            raise ValueError("provider known-host store lock authority cannot be reused")
        self._entered = True
        self._reserve_slot()
        try:
            root_fd = self._anchor._get_root_fd(create=self._create)
            if root_fd is None:
                self._release_slot()
                self._released = True
                return None
            root_stat = os.fstat(root_fd)
            lock_fd = self._anchor._open_lock(root_fd)
            self._lock_fd = lock_fd
            lock_stat = os.fstat(lock_fd)
            self._lock_identity = (lock_stat.st_dev, lock_stat.st_ino)
            self._anchor._validate_open_lock(lock_fd)
            self._root_fd = root_fd
            self._root_identity = (root_stat.st_dev, root_stat.st_ino)
            _acquire_store_lock(
                lock_fd,
                exclusive=self._exclusive,
                timeout_seconds=self._anchor._lock_timeout_seconds,
            )
            self._locked = True
            self._anchor._validate_ancestor_binding()
            self._anchor._validate_root_binding()
            self._anchor._validate_lock_binding(root_fd, lock_fd)
            return root_fd
        except BaseException:
            self._cleanup_requested = True
            try:
                self._cleanup()
            except BaseException:
                pass
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self._cleanup_requested = True
        self._cleanup()

    def _reserve_slot(self) -> None:
        _retry_retained_store_lock_cleanup()
        with _STORE_LOCK_REGISTRY_GUARD:
            if len(_STORE_LOCK_AUTHORITIES) >= _MAX_STORE_LOCK_AUTHORITIES:
                raise ValueError("provider known-host store lock cleanup capacity is exhausted")
            _STORE_LOCK_AUTHORITIES[id(self)] = self
            self._slot_held = True

    def _cleanup(self) -> None:
        with self._cleanup_guard:
            if self._released:
                return
            failure = self._validate_owned_descriptor()
            lock_fd = self._lock_fd
            if lock_fd is not None and self._locked:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except BaseException as exc:
                    if failure is None:
                        failure = exc
                else:
                    self._locked = False
            if lock_fd is not None:
                try:
                    os.close(lock_fd)
                except BaseException as exc:
                    if failure is None:
                        failure = exc
                    if self._descriptor_is_closed(lock_fd):
                        self._lock_fd = None
                        self._locked = False
                else:
                    self._lock_fd = None
                    self._locked = False
            if self._lock_fd is None:
                self._released = True
                self._release_slot()
            if failure is not None:
                raise ValueError("provider known-host store lock cleanup failed") from None
            if not self._released:
                raise ValueError("provider known-host store lock cleanup failed")

    def _validate_owned_descriptor(self) -> BaseException | None:
        lock_fd = self._lock_fd
        if lock_fd is None:
            return None
        try:
            lock_stat = os.fstat(lock_fd)
            root_fd = self._root_fd
            if root_fd is None:
                raise ValueError("provider known-host store lock root authority is incomplete")
            root_stat = os.fstat(root_fd)
            if self._lock_identity != (lock_stat.st_dev, lock_stat.st_ino):
                raise ValueError("provider known-host store lock FD identity changed")
            if self._root_identity != (root_stat.st_dev, root_stat.st_ino):
                raise ValueError("provider known-host store root FD identity changed")
        except BaseException as exc:
            return exc
        return None

    def _descriptor_is_closed(self, lock_fd: int) -> bool:
        try:
            opened = os.fstat(lock_fd)
        except OSError as exc:
            return exc.errno == errno.EBADF
        except BaseException:
            return False
        return self._lock_identity is not None and self._lock_identity != (
            opened.st_dev,
            opened.st_ino,
        )

    def _release_slot(self) -> None:
        with _STORE_LOCK_REGISTRY_GUARD:
            if not self._slot_held:
                return
            _STORE_LOCK_AUTHORITIES.pop(id(self), None)
            self._slot_held = False


def _retry_retained_store_lock_cleanup() -> None:
    with _STORE_LOCK_REGISTRY_GUARD:
        retained = tuple(
            authority
            for authority in _STORE_LOCK_AUTHORITIES.values()
            if authority._cleanup_requested
        )
    for authority in retained:
        try:
            authority._cleanup()
        except BaseException:
            continue


class _StoreAnchor:
    """Held secure ancestor/root descriptors plus the cross-process lock namespace."""

    def __init__(
        self,
        root: Path,
        secure_ancestor: Path,
        *,
        requested_secure_ancestor: Path,
        lock_timeout_seconds: float,
    ) -> None:
        self.root = root
        self.secure_ancestor = secure_ancestor
        ancestor_fd = _open_secure_ancestor(secure_ancestor)
        try:
            _validate_darwin_system_alias_binding(
                requested_secure_ancestor,
                secure_ancestor,
                os.fstat(ancestor_fd),
            )
        except BaseException:
            os.close(ancestor_fd)
            raise
        self._ancestor_fd = ancestor_fd
        self._root_fd: int | None = None
        self._lock_anchor_fd: int | None = None
        self._guard = threading.Lock()
        self._lock_timeout_seconds = lock_timeout_seconds
        ancestor_stat = os.fstat(self._ancestor_fd)
        self.registry_key = (ancestor_stat.st_dev, ancestor_stat.st_ino, root.name)

    def locked_root(
        self,
        *,
        create: bool,
        exclusive: bool,
    ) -> AbstractContextManager[int | None]:
        return _StoreLockAuthority(self, create=create, exclusive=exclusive)

    def _open_lock(self, root_fd: int) -> int:
        with self._guard:
            try:
                if self._lock_anchor_fd is None:
                    self._lock_anchor_fd = os.open(
                        _LOCK_NAME,
                        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=root_fd,
                    )
                anchored = os.fstat(self._lock_anchor_fd)
                _validate_file_stat(anchored, require_single_link=True)
            except Exception as exc:
                raise ValueError("provider known-host store lock binding changed") from exc
            try:
                return os.open(
                    _LOCK_NAME,
                    os.O_RDWR | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
            except OSError as exc:
                raise ValueError(
                    "provider known-host store lock could not be opened safely"
                ) from exc

    def _validate_open_lock(self, lock_fd: int) -> None:
        with self._guard:
            try:
                opened = os.fstat(lock_fd)
                _validate_file_stat(opened, require_single_link=True)
                if self._lock_anchor_fd is None:
                    raise ValueError("provider known-host store lock anchor is unavailable")
                anchored = os.fstat(self._lock_anchor_fd)
                _validate_file_stat(anchored, require_single_link=True)
                if (opened.st_dev, opened.st_ino) != (
                    anchored.st_dev,
                    anchored.st_ino,
                ):
                    raise ValueError("provider known-host store lock binding changed")
            except Exception as exc:
                raise ValueError("provider known-host store lock binding changed") from exc

    def _validate_lock_binding(self, root_fd: int, lock_fd: int) -> None:
        try:
            current = os.stat(_LOCK_NAME, dir_fd=root_fd, follow_symlinks=False)
        except OSError as exc:
            raise ValueError("provider known-host store lock binding changed") from exc
        opened = os.fstat(lock_fd)
        _validate_file_stat(current, require_single_link=True)
        _validate_file_stat(opened, require_single_link=True)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("provider known-host store lock binding changed")

    def _get_root_fd(self, *, create: bool) -> int | None:
        with self._guard:
            if self._root_fd is None:
                try:
                    before = os.stat(
                        self.root.name,
                        dir_fd=self._ancestor_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    if not create:
                        return None
                    try:
                        os.mkdir(self.root.name, 0o700, dir_fd=self._ancestor_fd)
                    except FileExistsError:
                        pass
                    before = os.stat(
                        self.root.name,
                        dir_fd=self._ancestor_fd,
                        follow_symlinks=False,
                    )
                _validate_root_stat(before)
                try:
                    fd = os.open(
                        self.root.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=self._ancestor_fd,
                    )
                except OSError as exc:
                    raise ValueError(
                        "provider known-host store root could not be opened safely"
                    ) from exc
                opened = os.fstat(fd)
                _validate_root_stat(opened)
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    os.close(fd)
                    raise ValueError("provider known-host store root changed during open")
                self._root_fd = fd
            self._validate_root_binding()
            return self._root_fd

    def _validate_ancestor_binding(self) -> None:
        reopened_fd = _open_secure_ancestor(self.secure_ancestor)
        try:
            reopened = os.fstat(reopened_fd)
            opened = os.fstat(self._ancestor_fd)
            _validate_secure_ancestor_stat(opened)
            if (reopened.st_dev, reopened.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("provider known-host secure ancestor binding changed")
        finally:
            os.close(reopened_fd)

    def _validate_root_binding(self) -> None:
        assert self._root_fd is not None
        try:
            current = os.stat(
                self.root.name,
                dir_fd=self._ancestor_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ValueError("provider known-host store root binding changed") from exc
        opened = os.fstat(self._root_fd)
        _validate_root_stat(current)
        _validate_root_stat(opened)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("provider known-host store root binding changed")

    def __del__(self) -> None:
        root_fd = getattr(self, "_root_fd", None)
        lock_anchor_fd = getattr(self, "_lock_anchor_fd", None)
        ancestor_fd = getattr(self, "_ancestor_fd", None)
        try:
            if root_fd is not None:
                os.close(root_fd)
            if lock_anchor_fd is not None:
                os.close(lock_anchor_fd)
            if ancestor_fd is not None:
                os.close(ancestor_fd)
        except OSError:
            pass


class _KnownHostsSpawnLease(AbstractContextManager[Path]):
    def __init__(
        self,
        binding: TrustedKnownHostsBinding,
        profile: RemoteProfileConfig,
    ) -> None:
        self._binding = binding
        self._profile = profile
        self._locked = None
        self._directory_fd: int | None = None
        self._directory_identity: tuple[int, int] | None = None
        self._directory_name: str | None = None
        self._slot_held = False
        self._cleanup_requested = False
        self._cleanup_guard = threading.Lock()
        self._released = False

    def __enter__(self) -> Path:
        try:
            self._reserve_cleanup_slot()
            self._binding.validate_for(self._profile)
            anchor = self._binding._anchor
            self._locked = anchor.locked_root(create=False, exclusive=False)
            root_fd = self._locked.__enter__()
            if root_fd is None:
                raise ValueError("trusted known-host file is missing")
            content = _read_secure_file(root_fd, self._binding.known_hosts_file.name)
            if content is None:
                raise ValueError("trusted known-host file is missing")
            current = _binding_from_content(
                self._binding.known_hosts_file,
                content,
                self._profile,
                anchor,
            )
            if current != self._binding:
                raise ValueError("trusted known-host file content does not match binding")
            self._directory_name = f".openevo-ssh-lease-{secrets.token_hex(16)}"
            os.mkdir(self._directory_name, 0o700, dir_fd=anchor._ancestor_fd)
            os.fsync(anchor._ancestor_fd)
            self._directory_fd = os.open(
                self._directory_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=anchor._ancestor_fd,
            )
            opened_directory = os.fstat(self._directory_fd)
            _validate_root_stat(opened_directory)
            self._directory_identity = (
                opened_directory.st_dev,
                opened_directory.st_ino,
            )
            current_directory = os.stat(
                self._directory_name,
                dir_fd=anchor._ancestor_fd,
                follow_symlinks=False,
            )
            _validate_root_stat(current_directory)
            if self._directory_identity != (
                current_directory.st_dev,
                current_directory.st_ino,
            ):
                raise ValueError("known-host spawn lease directory changed during pin")
            _write_new_secure_file(self._directory_fd, "known_hosts", content)
            os.fsync(self._directory_fd)
            current_directory = os.stat(
                self._directory_name,
                dir_fd=anchor._ancestor_fd,
                follow_symlinks=False,
            )
            _validate_root_stat(current_directory)
            if self._directory_identity != (
                current_directory.st_dev,
                current_directory.st_ino,
            ):
                raise ValueError("known-host spawn lease directory binding changed")
            return anchor.secure_ancestor / self._directory_name / "known_hosts"
        except BaseException:
            self._cleanup_requested = True
            try:
                self._cleanup()
            except BaseException:
                pass
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self._cleanup_requested = True
        self._cleanup()

    def _reserve_cleanup_slot(self) -> None:
        if self._slot_held:
            return
        if self._released or self._locked is not None or self._directory_name is not None:
            raise ValueError("known-host spawn lease cannot be reused")
        _retry_retained_spawn_lease_cleanup()
        with _SPAWN_LEASE_REGISTRY_GUARD:
            if len(_SPAWN_LEASES) >= _MAX_KNOWN_HOST_SPAWN_LEASES:
                raise ValueError("known-host spawn lease cleanup capacity is exhausted")
            _SPAWN_LEASES[id(self)] = self
            self._slot_held = True

    def _cleanup(self) -> None:
        with self._cleanup_guard:
            if self._released:
                return
            try:
                self._remove_private_directory()
                locked = self._locked
                if locked is not None:
                    locked.__exit__(None, None, None)
                    self._locked = None
            except BaseException:
                self._retain_for_retry()
                raise
            self._directory_name = None
            self._released = True
            with _SPAWN_LEASE_REGISTRY_GUARD:
                if self._slot_held:
                    _SPAWN_LEASES.pop(id(self), None)
                    self._slot_held = False

    def _remove_private_directory(self) -> None:
        anchor = self._binding._anchor
        directory_name = self._directory_name
        directory_fd = self._directory_fd
        if directory_name is None:
            if directory_fd is not None or self._directory_identity is not None:
                raise ValueError("known-host spawn lease directory identity is incomplete")
            return
        if directory_fd is None:
            try:
                before = os.stat(
                    directory_name,
                    dir_fd=anchor._ancestor_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                self._directory_name = None
                self._directory_identity = None
                return
            _validate_root_stat(before)
            directory_fd = os.open(
                directory_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=anchor._ancestor_fd,
            )
            self._directory_fd = directory_fd
            opened = os.fstat(directory_fd)
            _validate_root_stat(opened)
            self._directory_identity = (opened.st_dev, opened.st_ino)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("known-host spawn lease directory changed during pin")

        opened = os.fstat(directory_fd)
        try:
            current = os.stat(
                directory_name,
                dir_fd=anchor._ancestor_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            _validate_root_stat(opened)
            if self._directory_identity != (opened.st_dev, opened.st_ino):
                raise ValueError("known-host spawn lease directory FD identity changed")
            os.fsync(anchor._ancestor_fd)
            self._close_directory_fd()
            self._directory_name = None
            return
        _validate_root_stat(current)
        _validate_root_stat(opened)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("known-host spawn lease directory binding changed")
        try:
            os.unlink("known_hosts", dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.fsync(directory_fd)
        final = os.stat(
            directory_name,
            dir_fd=anchor._ancestor_fd,
            follow_symlinks=False,
        )
        _validate_root_stat(final)
        if (opened.st_dev, opened.st_ino) != (final.st_dev, final.st_ino):
            raise ValueError("known-host spawn lease directory binding changed")
        os.rmdir(directory_name, dir_fd=anchor._ancestor_fd)
        os.fsync(anchor._ancestor_fd)
        try:
            os.stat(
                directory_name,
                dir_fd=anchor._ancestor_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise ValueError("known-host spawn lease directory removal was not durable")
        self._close_directory_fd()
        self._directory_name = None

    def _close_directory_fd(self) -> None:
        directory_fd = self._directory_fd
        identity = self._directory_identity
        if directory_fd is None:
            self._directory_identity = None
            return
        try:
            os.close(directory_fd)
        except BaseException:
            try:
                opened = os.fstat(directory_fd)
            except OSError as probe_error:
                if probe_error.errno != errno.EBADF:
                    raise
            else:
                if identity == (opened.st_dev, opened.st_ino):
                    raise
        self._directory_fd = None
        self._directory_identity = None

    def _retain_for_retry(self) -> None:
        with _SPAWN_LEASE_REGISTRY_GUARD:
            if not self._slot_held:
                if len(_SPAWN_LEASES) >= _MAX_KNOWN_HOST_SPAWN_LEASES:
                    raise ValueError("known-host spawn lease cleanup capacity is exhausted")
                self._slot_held = True
            _SPAWN_LEASES[id(self)] = self


def _retry_retained_spawn_lease_cleanup() -> None:
    with _SPAWN_LEASE_REGISTRY_GUARD:
        retained = tuple(lease for lease in _SPAWN_LEASES.values() if lease._cleanup_requested)
    for lease in retained:
        try:
            lease._cleanup()
        except BaseException:
            continue


def _register_tunnel_closer(
    store_key: tuple[int, int, str],
    profile_id: str,
    closer: Callable[[], None],
) -> Callable[[], None]:
    key = (*store_key, profile_id)
    with _TUNNEL_REGISTRY_GUARD:
        _TUNNEL_CLOSERS.setdefault(key, set()).add(closer)

    def unregister() -> None:
        with _TUNNEL_REGISTRY_GUARD:
            closers = _TUNNEL_CLOSERS.get(key)
            if closers is None:
                return
            closers.discard(closer)
            if not closers:
                _TUNNEL_CLOSERS.pop(key, None)

    return unregister


def _request_tunnel_closure(store_key: tuple[int, int, str], profile_id: str) -> None:
    key = (*store_key, profile_id)
    with _TUNNEL_REGISTRY_GUARD:
        closers = tuple(_TUNNEL_CLOSERS.get(key, ()))
    for closer in closers:
        try:
            closer()
        except Exception:
            # The bounded exclusive lock remains the authoritative fail-closed gate.
            continue


def _run_keyscan(argv: list[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    if not argv or argv[0] != SSH_KEYSCAN_EXECUTABLE:
        raise ValueError("ssh-keyscan executable is not fixed")
    with VerifiedSystemExecutable.open(SSH_KEYSCAN_EXECUTABLE) as executable:
        completed = subprocess.run(
            argv,
            executable=executable.execution_path,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env={},
            pass_fds=(executable.descriptor,),
        )
        executable.verify_path_binding()
        return completed


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


def _canonical_pending_payload(
    profile_id: str,
    host: str,
    port: int,
    candidates: tuple[HostKeyCandidate, ...],
) -> bytes:
    if not candidates or len(candidates) > len(_ALGORITHM_ORDER):
        raise ValueError("pending host-key probe has an invalid candidate count")
    canonical_candidates: list[dict[str, str]] = []
    algorithms: list[HostKeyAlgorithm] = []
    for candidate in candidates:
        fields = candidate.public_key.split(" ")
        if len(fields) != 2 or any(not field for field in fields):
            raise ValueError("pending host-key candidate public key is malformed")
        public_key, fingerprint = _validate_public_key(*fields)
        algorithm = _KEY_TYPE_TO_ALGORITHM.get(fields[0])
        if (
            algorithm is None
            or candidate.algorithm != algorithm
            or candidate.public_key != public_key
            or candidate.fingerprint != fingerprint
            or algorithm in algorithms
        ):
            raise ValueError("pending host-key candidate is not canonical")
        algorithms.append(algorithm)
        canonical_candidates.append(
            {
                "algorithm": algorithm,
                "fingerprint": fingerprint,
                "public_key": public_key,
            }
        )
    expected_order = [algorithm for algorithm in _ALGORITHM_ORDER if algorithm in algorithms]
    if algorithms != expected_order:
        raise ValueError("pending host-key candidates are not canonically ordered")
    return json.dumps(
        {
            "candidates": canonical_candidates,
            "host": host,
            "port": port,
            "profile_id": profile_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


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
        if exponent_value < 3 or exponent_value % 2 == 0:
            raise ValueError("ssh-keyscan returned malformed RSA public-key parameters")
        if modulus_value.bit_length() < 2048:
            raise ValueError("ssh-keyscan returned an RSA host key smaller than 2048 bits")
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
    anchor: _StoreAnchor,
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
        _anchor=anchor,
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


def _open_secure_ancestor(path: Path) -> int:
    parts = path.parts
    if not parts or parts[0] != os.sep or any(part in {".", ".."} for part in parts[1:]):
        raise ValueError("provider known-host secure ancestor path is not canonical")
    try:
        current_fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("provider known-host secure ancestor could not be opened safely") from exc
    try:
        for index, part in enumerate(parts[1:], start=1):
            try:
                before = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current_fd,
                )
            except OSError as exc:
                raise ValueError(
                    "provider known-host secure ancestor could not be opened safely"
                ) from exc
            try:
                opened = os.fstat(next_fd)
                is_final = index == len(parts) - 1
                if is_final:
                    _validate_secure_ancestor_stat(before)
                    _validate_secure_ancestor_stat(opened)
                else:
                    _validate_secure_path_component_stat(before)
                    _validate_secure_path_component_stat(opened)
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ValueError("provider known-host secure ancestor changed during open")
            except Exception:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
    except Exception:
        os.close(current_fd)
        raise
    return current_fd


def _canonical_darwin_system_alias(path: Path) -> Path:
    """Map only Apple's fixed /tmp and /var aliases to no-follow paths."""

    if sys.platform != "darwin":
        return path
    parts = path.parts
    if len(parts) >= 2 and parts[0] == os.sep and parts[1] in {"tmp", "var"}:
        return Path("/private").joinpath(*parts[1:])
    return path


def _validate_darwin_system_alias_binding(
    requested: Path,
    canonical: Path,
    opened: os.stat_result,
) -> None:
    if requested == canonical:
        return
    try:
        observed = os.stat(requested)
    except OSError as exc:
        raise ValueError(
            "provider known-host secure ancestor system alias could not be verified"
        ) from exc
    if (observed.st_dev, observed.st_ino) != (opened.st_dev, opened.st_ino):
        raise ValueError(
            "provider known-host secure ancestor system alias changed during open"
        )


def _validate_secure_path_component_stat(result: os.stat_result) -> None:
    if not stat.S_ISDIR(result.st_mode):
        raise ValueError("provider known-host secure ancestor path contains a symlink")
    if result.st_uid not in {0, os.geteuid()}:
        raise ValueError("provider known-host secure ancestor path is not owner-controlled")
    mode = stat.S_IMODE(result.st_mode)
    if mode & 0o022 and not (result.st_uid == 0 and mode & stat.S_ISVTX):
        raise ValueError("provider known-host secure ancestor path contains a writable component")


def _validate_secure_ancestor_stat(result: os.stat_result) -> None:
    if not stat.S_ISDIR(result.st_mode):
        raise ValueError("provider known-host secure ancestor must be a directory")
    if result.st_uid != os.geteuid():
        raise ValueError("provider known-host secure ancestor must be owner-controlled")
    if stat.S_IMODE(result.st_mode) & 0o022:
        raise ValueError("provider known-host secure ancestor must not be group/world writable")


def _validate_root_stat(result: os.stat_result) -> None:
    if not stat.S_ISDIR(result.st_mode):
        raise ValueError("provider known-host store root must be a regular directory")
    if result.st_uid != os.geteuid():
        raise ValueError("provider known-host store root must be owner-controlled")
    if stat.S_IMODE(result.st_mode) != 0o700:
        raise ValueError("provider known-host store root must have mode 0700")


def _write_new_secure_file(root_fd: int, name: str, content: bytes) -> None:
    try:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_fd,
        )
    except OSError as exc:
        raise ValueError("trusted known-host file could not be created safely") from exc
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, content)
        os.fsync(fd)
        _validate_file_stat(os.fstat(fd), require_single_link=True)
    finally:
        os.close(fd)


def _replace_secure_file(
    root_fd: int,
    path: Path,
    content: bytes,
    *,
    profile: RemoteProfileConfig,
    anchor: _StoreAnchor,
    commit_state: _RotationCommitState,
) -> TrustedKnownHostsBinding:
    temp_name = f".{path.name}.{secrets.token_hex(12)}.rotate"
    created = False
    try:
        _write_new_secure_file(root_fd, temp_name, content)
        created = True
        prepared = _read_secure_file(root_fd, temp_name)
        if prepared != content:
            raise ValueError("known-host rotation temporary content changed")
        _binding_from_content(anchor.root / temp_name, prepared, profile, anchor)

        os.replace(temp_name, path.name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        commit_state.committed = True
        created = False
        os.fsync(root_fd)
        published = _read_secure_file(root_fd, path.name)
        if published != content:
            raise ValueError("known-host rotation content changed")
        return _binding_from_content(
            path,
            published,
            profile,
            anchor,
        )
    finally:
        if created:
            try:
                os.unlink(temp_name, dir_fd=root_fd)
            except FileNotFoundError:
                pass


def _validate_file_stat(result: os.stat_result, *, require_single_link: bool) -> None:
    if not stat.S_ISREG(result.st_mode):
        raise ValueError("trusted known-host file must be a regular file, not a symlink")
    if result.st_uid != os.geteuid():
        raise ValueError("trusted known-host file must be owner-controlled")
    if stat.S_IMODE(result.st_mode) != 0o600:
        raise ValueError("trusted known-host file must have mode 0600")
    if require_single_link and result.st_nlink != 1:
        raise ValueError("trusted known-host file must have exactly one link")


def _acquire_store_lock(
    lock_fd: int,
    *,
    exclusive: bool,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    while True:
        try:
            fcntl.flock(lock_fd, operation | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EINTR}:
                raise
        if time.monotonic() >= deadline:
            raise HostKeyStoreError(HostKeyStoreErrorCode.HOST_KEY_IN_USE)
        time.sleep(min(_LOCK_RETRY_SECONDS, max(0.0, deadline - time.monotonic())))


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
        or any(
            (character.isspace() and character != " ") or ord(character) == 127
            for character in text
        )
        or (" " in text and not _is_macos_desktop_config_path(path))
    ):
        raise ValueError("provider known-host store path contains unsupported characters")


def _is_macos_desktop_config_path(path: Path) -> bool:
    desktop_root = (
        Path.home() / "Library" / "Application Support" / "org.openevo.desktop"
    )
    try:
        relative = path.relative_to(desktop_root)
    except ValueError:
        return False
    return all(" " not in component for component in relative.parts)


def _keyscan_timeout(timeout_seconds: float) -> int:
    if (
        isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > 60
    ):
        raise ValueError("host-key probe timeout must be between 0 and 60 seconds")
    return max(1, math.ceil(timeout_seconds))


def _validate_lock_timeout(timeout_seconds: float) -> float:
    if (
        isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > _MAX_LOCK_TIMEOUT_SECONDS
    ):
        raise ValueError("host-key lock timeout must be between 0 and 5 seconds")
    return timeout_seconds


def _write_all(fd: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(fd, content[offset:])
        if written <= 0:
            raise OSError("failed to write trusted known-host file")
        offset += written


def _zero_bytearray(value: bytearray) -> None:
    value[:] = b"\0" * len(value)
    value.clear()


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
    "HostKeyStoreError",
    "HostKeyStoreErrorCode",
    "PendingHostKeyProbe",
    "PendingSystemHostKeyReview",
    "ProviderKnownHostStore",
    "SystemHostKeyFailureCode",
    "SystemHostKeyFailureEvidence",
    "SystemHostKeyReplacement",
    "SystemHostKeyReviewAuthority",
    "SystemKnownHostsPolicy",
    "TrustedKnownHostsBinding",
    "classify_system_openssh_host_key_failure",
    "inspect_system_known_hosts_policy",
]
