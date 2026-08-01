"""Private authorization broker for the native system-OpenSSH askpass helper.

The broker never receives the prompt text or the user's response.  It accepts
only a fixed-size description of one prompt and consumes one HMAC-derived
capability after independently proving that the helper belongs to the current
OpenEvo-owned SSH process tree.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import ctypes
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import sys
import threading
import time
from typing import Literal, Protocol


_MAX_SOCKET_PATH_BYTES = 103
_MAX_REQUEST_BYTES = 512
_MAX_ANCESTORS = 32
_MAX_PROMPT_BYTES = 2_048
_MAX_SAFE_GENERATION = (1 << 53) - 1
_CAPABILITY_RE = re.compile(r"^[0-9a-f]{64}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SYSTEM_SSH_PATH = "/usr/bin/ssh"
_PROMPT_KINDS = frozenset({"password", "passphrase", "host_confirmation"})
_AUTHORIZATION_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "event",
        "capability",
        "connection_generation",
        "helper_pid",
        "ssh_parent_pid",
        "owner_pid",
        "prompt_kind",
        "prompt_sha256",
        "prompt_bytes",
    }
)
_COMPLETION_REQUEST_KEYS = _AUTHORIZATION_REQUEST_KEYS | {"outcome"}
_COMPLETION_OUTCOMES = frozenset({"accepted", "rejected", "cancelled"})
_CAPABILITY_DOMAIN = b"openevo-system-ssh-askpass-capability-v1\0"
_BROKER_BIND_WAIT_SECONDS = 2.0
_BROKER_ACCEPT_INTERVAL_SECONDS = 0.1
_BROKER_CLIENT_TIMEOUT_SECONDS = 2.0
_DARWIN_LOCAL_PEERPID = getattr(socket, "LOCAL_PEERPID", 0x002)
_DARWIN_LOCAL_PEERTOKEN = getattr(socket, "LOCAL_PEERTOKEN", 0x006)
_DARWIN_AUDIT_TOKEN = struct.Struct("=8I")


class AskpassBrokerError(RuntimeError):
    """A fixed, secret-free askpass broker authority failure."""


@dataclass(frozen=True, slots=True)
class UnixPeerAuthority:
    process_id: int
    user_id: int

    def __post_init__(self) -> None:
        if self.process_id <= 1 or self.user_id < 0:
            raise ValueError("invalid Unix peer authority")


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    process_id: int
    parent_process_id: int
    process_group_id: int
    session_id: int
    user_id: int
    birth_identity: str
    executable_path: str

    def __post_init__(self) -> None:
        if (
            type(self.process_id) is not int
            or self.process_id <= 1
            or type(self.parent_process_id) is not int
            or self.parent_process_id < 0
            or type(self.process_group_id) is not int
            or self.process_group_id <= 1
            or type(self.session_id) is not int
            # POSIX session ID 1 is valid for descendants of launchd/init and
            # is observed on GitHub-hosted macOS runners. Authority remains
            # bound by exact SID equality plus PID, PGID, UID, birth identity,
            # executable path, and parent-chain verification.
            or self.session_id < 1
            or type(self.user_id) is not int
            or self.user_id < 0
            or type(self.birth_identity) is not str
            or not self.birth_identity
            or len(self.birth_identity.encode("utf-8")) > 256
            or type(self.executable_path) is not str
            or not Path(self.executable_path).is_absolute()
            or len(os.fsencode(self.executable_path)) > 4_096
        ):
            raise ValueError("invalid process identity")


class ProcessInspector(Protocol):
    def inspect(self, process_id: int) -> ProcessIdentity | None: ...


@dataclass(frozen=True, slots=True)
class AskpassCapability:
    value: str = field(repr=False)
    connection_generation: int

    def __post_init__(self) -> None:
        if _CAPABILITY_RE.fullmatch(self.value) is None:
            raise ValueError("invalid askpass capability")
        _require_generation(self.connection_generation)


@dataclass(slots=True)
class _CapabilityRecord:
    generation: int
    nonce: bytearray = field(repr=False)
    capability_digest: bytearray = field(repr=False)
    owner: ProcessIdentity | None = None
    consumed: bool = False
    cancelled: bool = False
    prompt_binding: tuple[int | str, ...] | None = None
    helper_identity: ProcessIdentity | None = None
    completion_outcome: str | None = None


@dataclass(frozen=True, slots=True)
class AskpassPromptObservation:
    connection_generation: int
    kind: Literal["password", "passphrase", "host_confirmation"]
    state: Literal["pending", "completed", "rejected", "cancelled"]


@dataclass(frozen=True, slots=True)
class _SocketIdentity:
    device: int
    inode: int
    mode: int
    owner: int
    links: int


class AskpassAuthorizationBroker:
    """Authorize exactly one bounded prompt for one connection generation."""

    def __init__(
        self,
        socket_path: Path | str,
        *,
        helper_path: Path | str,
        inspector: ProcessInspector | None = None,
        hmac_key: bytes | None = None,
        peer_authority: Callable[[socket.socket], UnixPeerAuthority] | None = None,
        observation_callback: Callable[[AskpassPromptObservation], None] | None = None,
    ) -> None:
        self._socket_path = _validate_socket_path(socket_path)
        self._helper_path = _validate_helper_path(helper_path)
        if hmac_key is None:
            hmac_key = secrets.token_bytes(32)
        if type(hmac_key) is not bytes or len(hmac_key) != 32:
            raise ValueError("askpass broker HMAC key must contain exactly 32 bytes")
        self._hmac_key = bytearray(hmac_key)
        self._inspector = inspector or SystemProcessInspector()
        self._peer_authority = peer_authority or _unix_peer_authority
        if observation_callback is not None and not callable(observation_callback):
            raise TypeError("askpass observation callback must be callable")
        self._observation_callback = observation_callback
        self._guard = threading.Lock()
        self._bound = threading.Condition(self._guard)
        self._record: _CapabilityRecord | None = None
        self._listener: socket.socket | None = None
        self._socket_identity: _SocketIdentity | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._closing = False
        self._authorized_prompt_count = 0

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def authorized_prompt_count(self) -> int:
        with self._guard:
            return self._authorized_prompt_count

    @property
    def cancelled(self) -> bool:
        with self._guard:
            return self._record is not None and self._record.cancelled

    @property
    def prompt_observation(self) -> AskpassPromptObservation | None:
        with self._guard:
            record = self._record
            if record is None or record.prompt_binding is None:
                return None
            kind = record.prompt_binding[4]
            assert isinstance(kind, str) and kind in _PROMPT_KINDS
            if record.cancelled or record.completion_outcome == "cancelled":
                state = "cancelled"
            elif record.completion_outcome == "accepted":
                state = "completed"
            elif record.completion_outcome == "rejected":
                state = "rejected"
            else:
                state = "pending"
            return AskpassPromptObservation(
                connection_generation=record.generation,
                kind=kind,  # type: ignore[arg-type]
                state=state,  # type: ignore[arg-type]
            )

    def start(self) -> None:
        with self._guard:
            if self._closed or self._closing:
                raise AskpassBrokerError("askpass broker is closed")
            if self._listener is not None:
                raise AskpassBrokerError("askpass broker is already started")
        _require_private_parent(self._socket_path.parent)
        try:
            os.lstat(self._socket_path)
        except FileNotFoundError:
            pass
        else:
            raise AskpassBrokerError("askpass broker socket already exists")

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self._socket_path))
            os.chmod(self._socket_path, 0o600, follow_symlinks=False)
            metadata = os.lstat(self._socket_path)
            _require_private_socket(metadata)
            identity = _socket_identity(metadata)
            listener.listen(4)
            listener.settimeout(_BROKER_ACCEPT_INTERVAL_SECONDS)
            with self._guard:
                if self._closed or self._closing or self._listener is not None:
                    raise AskpassBrokerError("askpass broker start was superseded")
                self._listener = listener
                self._socket_identity = identity
                thread = threading.Thread(
                    target=self._serve,
                    name="openevo-ssh-askpass-broker",
                    daemon=True,
                )
                self._thread = thread
                thread.start()
        except BaseException:
            listener.close()
            try:
                metadata = os.lstat(self._socket_path)
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == os.geteuid():
                    try:
                        self._socket_path.unlink()
                    except OSError:
                        pass
            raise

    def issue_capability(self, *, connection_generation: int) -> AskpassCapability:
        _require_generation(connection_generation)
        with self._guard:
            if self._closed or self._closing or self._listener is None:
                raise AskpassBrokerError("askpass broker is unavailable")
            if self._record is not None:
                raise AskpassBrokerError("askpass prompt capacity is exhausted")
            nonce = bytearray(secrets.token_bytes(32))
            capability = self._derive_capability(connection_generation, nonce)
            self._record = _CapabilityRecord(
                generation=connection_generation,
                nonce=nonce,
                capability_digest=bytearray(hashlib.sha256(capability.encode("ascii")).digest()),
            )
            return AskpassCapability(
                value=capability,
                connection_generation=connection_generation,
            )

    def bind_owner(
        self,
        capability: AskpassCapability,
        owner: ProcessIdentity,
    ) -> None:
        if not isinstance(capability, AskpassCapability) or not isinstance(owner, ProcessIdentity):
            raise TypeError("invalid askpass owner binding")
        if owner.executable_path != _SYSTEM_SSH_PATH or owner.user_id != os.geteuid():
            raise AskpassBrokerError("askpass owner authority is invalid")
        with self._bound:
            record = self._record
            if (
                self._closed
                or self._closing
                or record is None
                or record.cancelled
                or record.consumed
                or record.owner is not None
                or record.generation != capability.connection_generation
                or not hmac.compare_digest(
                    self._derive_capability(record.generation, record.nonce),
                    capability.value,
                )
            ):
                raise AskpassBrokerError("askpass owner binding is unavailable")
            observed = self._inspector.inspect(owner.process_id)
            if observed != owner:
                raise AskpassBrokerError("askpass owner process identity changed")
            record.owner = owner
            self._bound.notify_all()

    def cancel_generation(self, connection_generation: int) -> None:
        _require_generation(connection_generation)
        with self._bound:
            record = self._record
            if record is None or record.generation != connection_generation:
                return
            record.cancelled = True
            _zero(record.nonce)
            _zero(record.capability_digest)
            self._bound.notify_all()

    def authorize_payload(
        self,
        payload: Mapping[str, object],
        *,
        peer: UnixPeerAuthority,
    ) -> bool:
        request = _validate_authorization_request(payload)
        if request is None or not isinstance(peer, UnixPeerAuthority):
            return False
        observation: AskpassPromptObservation | None = None
        with self._bound:
            record = self._record
            if record is not None and record.owner is None and not record.cancelled:
                deadline = time.monotonic() + _BROKER_BIND_WAIT_SECONDS
                while (
                    record.owner is None
                    and not record.cancelled
                    and not self._closed
                    and not self._closing
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._bound.wait(remaining)
            if (
                self._closed
                or self._closing
                or record is None
                or record.owner is None
                or record.cancelled
                or record.consumed
                or request["connection_generation"] != record.generation
                or request["owner_pid"] != record.owner.process_id
                or not hmac.compare_digest(
                    self._derive_capability(record.generation, record.nonce),
                    request["capability"],
                )
            ):
                return False
            helper_identity = self._authorize_process_chain(request, peer, record.owner)
            if helper_identity is None:
                return False
            # Consume only after every independent authority check succeeds.
            record.consumed = True
            record.prompt_binding = _prompt_binding(request)
            record.helper_identity = helper_identity
            _zero(record.nonce)
            self._authorized_prompt_count += 1
            observation = AskpassPromptObservation(
                connection_generation=record.generation,
                kind=request["prompt_kind"],  # type: ignore[arg-type]
                state="pending",
            )
        self._notify_observation(observation)
        return True

    def complete_payload(
        self,
        payload: Mapping[str, object],
        *,
        peer: UnixPeerAuthority,
    ) -> bool:
        request = _validate_completion_request(payload)
        if request is None or not isinstance(peer, UnixPeerAuthority):
            return False
        observation: AskpassPromptObservation | None = None
        with self._bound:
            record = self._record
            if (
                self._closed
                or self._closing
                or record is None
                or record.owner is None
                or record.cancelled
                or not record.consumed
                or record.prompt_binding is None
                or record.helper_identity is None
                or record.completion_outcome is not None
                or request["connection_generation"] != record.generation
                or request["owner_pid"] != record.owner.process_id
                or _prompt_binding(request) != record.prompt_binding
                or not hmac.compare_digest(
                    hashlib.sha256(request["capability"].encode("ascii")).digest(),
                    record.capability_digest,
                )
            ):
                return False
            helper_identity = self._authorize_process_chain(request, peer, record.owner)
            if helper_identity != record.helper_identity:
                return False
            record.completion_outcome = request["outcome"]
            _zero(record.capability_digest)
            self._bound.notify_all()
            state = {
                "accepted": "completed",
                "rejected": "rejected",
                "cancelled": "cancelled",
            }[request["outcome"]]
            observation = AskpassPromptObservation(
                connection_generation=record.generation,
                kind=request["prompt_kind"],  # type: ignore[arg-type]
                state=state,  # type: ignore[arg-type]
            )
        self._notify_observation(observation)
        return True

    def _notify_observation(self, observation: AskpassPromptObservation) -> None:
        callback = self._observation_callback
        if callback is None:
            return
        try:
            callback(observation)
        except Exception:
            # Renderer projection is best-effort and must never alter the native
            # OpenSSH authentication decision.
            pass

    def verify_socket_binding(self) -> None:
        with self._guard:
            if self._closed or self._closing:
                raise AskpassBrokerError("askpass broker socket is unavailable")
            expected = self._socket_identity
        if expected is None:
            raise AskpassBrokerError("askpass broker socket is unavailable")
        try:
            metadata = os.lstat(self._socket_path)
        except OSError as exc:
            raise AskpassBrokerError("askpass broker socket identity changed") from exc
        _require_private_socket(metadata)
        if _socket_identity(metadata) != expected:
            raise AskpassBrokerError("askpass broker socket identity changed")

    def close(self) -> None:
        with self._bound:
            if self._closed:
                return
            self._closing = True
            self._stop.set()
            record = self._record
            if record is not None:
                record.cancelled = True
                _zero(record.nonce)
                _zero(record.capability_digest)
            listener, self._listener = self._listener, None
            thread = self._thread
            self._bound.notify_all()
        if listener is not None:
            listener.close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)
            if thread.is_alive():
                raise AskpassBrokerError("askpass broker worker did not stop")
        cleanup_failure: AskpassBrokerError | None = None
        expected = self._socket_identity
        if expected is not None:
            try:
                metadata = os.lstat(self._socket_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                cleanup_failure = AskpassBrokerError("askpass broker socket identity changed")
                cleanup_failure.__cause__ = exc
            else:
                if _socket_identity(metadata) != expected:
                    cleanup_failure = AskpassBrokerError("askpass broker socket identity changed")
                else:
                    try:
                        self._socket_path.unlink()
                    except OSError as exc:
                        cleanup_failure = AskpassBrokerError(
                            "askpass broker socket cleanup failed"
                        )
                        cleanup_failure.__cause__ = exc
        _zero(self._hmac_key)
        if cleanup_failure is not None:
            raise cleanup_failure
        with self._bound:
            self._closed = True
            self._closing = False

    def _derive_capability(self, generation: int, nonce: bytearray) -> str:
        message = _CAPABILITY_DOMAIN + str(generation).encode("ascii") + b"\0" + bytes(nonce)
        return hmac.new(bytes(self._hmac_key), message, hashlib.sha256).hexdigest()

    def _authorize_process_chain(
        self,
        request: dict[str, int | str],
        peer: UnixPeerAuthority,
        owner: ProcessIdentity,
    ) -> ProcessIdentity | None:
        helper_pid = request["helper_pid"]
        ssh_parent_pid = request["ssh_parent_pid"]
        if (
            type(helper_pid) is not int
            or type(ssh_parent_pid) is not int
            or peer.process_id != helper_pid
            or peer.user_id != owner.user_id
        ):
            return None
        current_owner = self._inspector.inspect(owner.process_id)
        helper = self._inspector.inspect(helper_pid)
        ssh_parent = self._inspector.inspect(ssh_parent_pid)
        if (
            current_owner != owner
            or helper is None
            or helper.parent_process_id != ssh_parent_pid
            or helper.executable_path != self._helper_path
            or ssh_parent is None
            or ssh_parent.executable_path != _SYSTEM_SSH_PATH
        ):
            return None
        current = ssh_parent
        observed: set[int] = set()
        for _ in range(_MAX_ANCESTORS):
            if (
                current.process_id in observed
                or current.user_id != owner.user_id
                or current.process_group_id != owner.process_group_id
                or current.session_id != owner.session_id
            ):
                return None
            observed.add(current.process_id)
            if current.process_id == owner.process_id:
                return helper if current == owner else None
            current = self._inspector.inspect(current.parent_process_id)
            if current is None:
                return None
        return None

    def _serve(self) -> None:
        while not self._stop.is_set():
            with self._guard:
                listener = self._listener
            if listener is None:
                return
            try:
                client, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with client:
                client.settimeout(_BROKER_CLIENT_TIMEOUT_SECONDS)
                authorized = False
                try:
                    peer = self._peer_authority(client)
                    payload = _read_request(client)
                    if payload is not None:
                        event = payload.get("event")
                        if event == "authorize":
                            authorized = self.authorize_payload(payload, peer=peer)
                        elif event == "complete":
                            authorized = self.complete_payload(payload, peer=peer)
                except (OSError, ValueError, AskpassBrokerError):
                    authorized = False
                response = (
                    b'{"authorized":true,"schema_version":1}\n'
                    if authorized
                    else b'{"authorized":false,"schema_version":1}\n'
                )
                try:
                    client.sendall(response)
                except OSError:
                    pass

    def __repr__(self) -> str:
        return "AskpassAuthorizationBroker(<redacted>)"


class SystemProcessInspector:
    """Read bounded process identity from the supported local kernel API."""

    def inspect(self, process_id: int) -> ProcessIdentity | None:
        if type(process_id) is not int or process_id <= 1:
            return None
        try:
            if sys.platform == "darwin":
                return _inspect_darwin_process(process_id)
            if sys.platform.startswith("linux"):
                return _inspect_linux_process(process_id)
        except (OSError, ValueError, IndexError, UnicodeDecodeError):
            return None
        return None


def _read_request(client: socket.socket) -> dict[str, object] | None:
    payload = bytearray()
    while len(payload) < _MAX_REQUEST_BYTES:
        chunk = client.recv(min(128, _MAX_REQUEST_BYTES - len(payload)))
        if not chunk:
            return None
        newline = chunk.find(b"\n")
        if newline >= 0:
            payload.extend(chunk[:newline])
            if newline != len(chunk) - 1:
                return None
            break
        payload.extend(chunk)
    else:
        return None
    if not payload:
        return None
    try:
        decoded = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return decoded if type(decoded) is dict else None


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate askpass broker request key")
        value[key] = item
    return value


def _validate_authorization_request(
    payload: Mapping[str, object],
) -> dict[str, int | str] | None:
    if (
        type(payload) is not dict
        or set(payload) != _AUTHORIZATION_REQUEST_KEYS
        or payload.get("event") != "authorize"
    ):
        return None
    return _validate_prompt_request(payload, event="authorize")


def _validate_completion_request(
    payload: Mapping[str, object],
) -> dict[str, int | str] | None:
    if (
        type(payload) is not dict
        or set(payload) != _COMPLETION_REQUEST_KEYS
        or payload.get("event") != "complete"
        or type(payload.get("outcome")) is not str
        or payload["outcome"] not in _COMPLETION_OUTCOMES
    ):
        return None
    validated = _validate_prompt_request(payload, event="complete")
    if validated is None:
        return None
    validated["outcome"] = payload["outcome"]
    return validated


def _validate_prompt_request(
    payload: Mapping[str, object],
    *,
    event: str,
) -> dict[str, int | str] | None:
    generation = payload["connection_generation"]
    helper_pid = payload["helper_pid"]
    ssh_parent_pid = payload["ssh_parent_pid"]
    owner_pid = payload["owner_pid"]
    prompt_bytes = payload["prompt_bytes"]
    if (
        payload["schema_version"] != 1
        or payload["event"] != event
        or type(generation) is not int
        or not 1 <= generation <= _MAX_SAFE_GENERATION
        or type(helper_pid) is not int
        or not 1 < helper_pid <= (1 << 31) - 1
        or type(ssh_parent_pid) is not int
        or not 1 < ssh_parent_pid <= (1 << 31) - 1
        or type(owner_pid) is not int
        or not 1 < owner_pid <= (1 << 31) - 1
        or type(prompt_bytes) is not int
        or not 1 <= prompt_bytes <= _MAX_PROMPT_BYTES
        or type(payload["capability"]) is not str
        or _CAPABILITY_RE.fullmatch(payload["capability"]) is None
        or type(payload["prompt_kind"]) is not str
        or payload["prompt_kind"] not in _PROMPT_KINDS
        or type(payload["prompt_sha256"]) is not str
        or _DIGEST_RE.fullmatch(payload["prompt_sha256"]) is None
    ):
        return None
    return {
        "schema_version": 1,
        "event": event,
        "capability": payload["capability"],
        "connection_generation": generation,
        "helper_pid": helper_pid,
        "ssh_parent_pid": ssh_parent_pid,
        "owner_pid": owner_pid,
        "prompt_kind": payload["prompt_kind"],
        "prompt_sha256": payload["prompt_sha256"],
        "prompt_bytes": prompt_bytes,
    }


def _prompt_binding(request: Mapping[str, int | str]) -> tuple[int | str, ...]:
    return (
        request["connection_generation"],
        request["helper_pid"],
        request["ssh_parent_pid"],
        request["owner_pid"],
        request["prompt_kind"],
        request["prompt_sha256"],
        request["prompt_bytes"],
    )


def _validate_socket_path(value: Path | str) -> Path:
    path = Path(value)
    encoded = os.fsencode(path)
    if (
        not path.is_absolute()
        or not path.name
        or len(encoded) > _MAX_SOCKET_PATH_BYTES
        or b"\0" in encoded
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise ValueError("askpass broker socket path is invalid")
    return path


def _validate_helper_path(value: Path | str) -> str:
    path = Path(value)
    encoded = os.fsencode(path)
    if (
        not path.is_absolute()
        or not path.name
        or len(encoded) > 4_096
        or b"\0" in encoded
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise ValueError("askpass helper path is invalid")
    return str(path)


def _require_private_parent(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise AskpassBrokerError("askpass broker runtime is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise AskpassBrokerError("askpass broker runtime is not private")


def _require_private_socket(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISSOCK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
    ):
        raise AskpassBrokerError("askpass broker socket identity is invalid")


def _socket_identity(metadata: os.stat_result) -> _SocketIdentity:
    return _SocketIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        owner=metadata.st_uid,
        links=metadata.st_nlink,
    )


def _require_generation(value: int) -> None:
    if type(value) is not int or not 1 <= value <= _MAX_SAFE_GENERATION:
        raise ValueError("askpass connection generation is invalid")


def _zero(value: bytearray) -> None:
    value[:] = b"\0" * len(value)


def _unix_peer_authority(stream: socket.socket) -> UnixPeerAuthority:
    if sys.platform.startswith("linux"):
        payload = stream.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        process_id, user_id, _group_id = struct.unpack("3i", payload)
        return UnixPeerAuthority(process_id=process_id, user_id=user_id)
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        getpeereid = libc.getpeereid
        getpeereid.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        getpeereid.restype = ctypes.c_int
        user_id = ctypes.c_uint32()
        group_id = ctypes.c_uint32()
        ctypes.set_errno(0)
        if getpeereid(stream.fileno(), ctypes.byref(user_id), ctypes.byref(group_id)) != 0:
            raise OSError(ctypes.get_errno(), "askpass peer credentials unavailable")
        payload = stream.getsockopt(0, _DARWIN_LOCAL_PEERPID, struct.calcsize("i"))
        (process_id,) = struct.unpack("=i", payload)
        token = stream.getsockopt(0, _DARWIN_LOCAL_PEERTOKEN, _DARWIN_AUDIT_TOKEN.size)
        fields = _DARWIN_AUDIT_TOKEN.unpack(token)
        if fields[1] != user_id.value or fields[2] != group_id.value or fields[5] != process_id:
            raise AskpassBrokerError("askpass peer audit authority changed")
        return UnixPeerAuthority(process_id=process_id, user_id=user_id.value)
    raise AskpassBrokerError("askpass broker peer authority is unsupported")


def _inspect_linux_process(process_id: int) -> ProcessIdentity:
    root = Path("/proc") / str(process_id)
    raw = (root / "stat").read_text(encoding="ascii")
    suffix = raw.rsplit(")", 1)[1].split()
    parent_id = int(suffix[1])
    process_group_id = int(suffix[2])
    session_id = int(suffix[3])
    start_ticks = int(suffix[19])
    executable_path = os.readlink(root / "exe")
    user_id = root.stat().st_uid
    return ProcessIdentity(
        process_id=process_id,
        parent_process_id=parent_id,
        process_group_id=process_group_id,
        session_id=session_id,
        user_id=user_id,
        birth_identity=f"linux:{start_ticks}",
        executable_path=executable_path,
    )


class _DarwinProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _inspect_darwin_process(process_id: int) -> ProcessIdentity:
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    info = _DarwinProcBsdInfo()
    result = library.proc_pidinfo(
        process_id,
        3,  # PROC_PIDTBSDINFO
        0,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if result != ctypes.sizeof(info) or info.pbi_pid != process_id:
        raise OSError(ctypes.get_errno(), "process identity unavailable")
    buffer = ctypes.create_string_buffer(4_096)
    length = library.proc_pidpath(process_id, buffer, len(buffer))
    if length <= 0:
        raise OSError(ctypes.get_errno(), "process path unavailable")
    executable_path = os.fsdecode(buffer.value)
    session_id = os.getsid(process_id)
    return ProcessIdentity(
        process_id=process_id,
        parent_process_id=info.pbi_ppid,
        process_group_id=info.pbi_pgid,
        session_id=session_id,
        user_id=info.pbi_uid,
        birth_identity=f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}",
        executable_path=executable_path,
    )


__all__ = (
    "AskpassAuthorizationBroker",
    "AskpassBrokerError",
    "AskpassCapability",
    "AskpassPromptObservation",
    "ProcessIdentity",
    "ProcessInspector",
    "SystemProcessInspector",
    "UnixPeerAuthority",
)
