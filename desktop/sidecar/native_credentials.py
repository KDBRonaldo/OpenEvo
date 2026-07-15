from __future__ import annotations

from dataclasses import dataclass
import re
from threading import Lock
from typing import Literal

from desktop.sidecar.contracts.v1.models import CredentialSlotStatusV1


NativeAuthenticationKind = Literal[
    "ssh_agent",
    "native_private_key",
    "native_password",
]
NativeCredentialSlotKind = Literal[
    "ssh_password",
    "ssh_private_key",
    "ssh_private_key_passphrase",
]

_PROFILE_ID_RE = re.compile(r"^[^\x00-\x20\x7f]{1,256}$")
_MAX_PASSWORD_BYTES = 16 * 1024
_MAX_PRIVATE_KEY_BYTES = 1024 * 1024
_MAX_PASSPHRASE_BYTES = 16 * 1024


class NativeCredentialError(RuntimeError):
    """A private native credential boundary failure."""


class NativeCredentialCapacityError(NativeCredentialError):
    """The bounded in-memory vault cannot accept another credential."""


class NativeCredentialUnavailableError(NativeCredentialError):
    """The requested profile has no complete native credential."""


@dataclass(slots=True)
class NativeSshCredentialMaterial:
    authentication_kind: NativeAuthenticationKind
    password: bytearray | None = None
    private_key: bytearray | None = None
    passphrase: bytearray | None = None

    def clear(self) -> None:
        for value in (self.password, self.private_key, self.passphrase):
            _zeroize(value)


@dataclass(slots=True)
class _CredentialEntry:
    authentication_kind: NativeAuthenticationKind
    password: bytearray | None = None
    private_key: bytearray | None = None
    passphrase: bytearray | None = None

    @property
    def byte_size(self) -> int:
        return sum(
            len(value)
            for value in (self.password, self.private_key, self.passphrase)
            if value is not None
        )

    def clear(self) -> None:
        for value in (self.password, self.private_key, self.passphrase):
            _zeroize(value)


class NativeCredentialVault:
    """Bounded process-memory custody for native-delivered SSH credentials."""

    def __init__(
        self,
        *,
        max_profiles: int = 256,
        max_total_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if not 1 <= max_profiles <= 4096:
            raise ValueError("native credential profile capacity is invalid")
        if not 1 <= max_total_bytes <= 64 * 1024 * 1024:
            raise ValueError("native credential byte capacity is invalid")
        self._max_profiles = max_profiles
        self._max_total_bytes = max_total_bytes
        self._lock = Lock()
        self._entries: dict[str, _CredentialEntry] = {}
        self._closed = False

    def replace(
        self,
        profile_id: str,
        *,
        authentication_kind: NativeAuthenticationKind,
        password: bytearray | None = None,
        private_key: bytearray | None = None,
        passphrase: bytearray | None = None,
    ) -> tuple[CredentialSlotStatusV1, ...]:
        _validate_profile_id(profile_id)
        entry = _validated_entry(
            authentication_kind,
            password=password,
            private_key=private_key,
            passphrase=passphrase,
        )
        with self._lock:
            self._require_open()
            existing = self._entries.get(profile_id)
            proposed_count = len(self._entries) + (0 if existing is not None else 1)
            proposed_bytes = (
                sum(value.byte_size for value in self._entries.values())
                - (existing.byte_size if existing is not None else 0)
                + entry.byte_size
            )
            if proposed_count > self._max_profiles or proposed_bytes > self._max_total_bytes:
                entry.clear()
                raise NativeCredentialCapacityError("native credential capacity is exhausted")
            self._entries[profile_id] = entry
            if existing is not None:
                existing.clear()
            return _slot_statuses(entry)

    def delete_slot(
        self,
        profile_id: str,
        slot_kind: NativeCredentialSlotKind,
    ) -> tuple[CredentialSlotStatusV1, ...]:
        _validate_profile_id(profile_id)
        with self._lock:
            self._require_open()
            entry = self._entries.get(profile_id)
            if entry is None:
                return ()
            if slot_kind == "ssh_password":
                _zeroize(entry.password)
                entry.password = None
            elif slot_kind == "ssh_private_key":
                _zeroize(entry.private_key)
                _zeroize(entry.passphrase)
                entry.private_key = None
                entry.passphrase = None
            elif slot_kind == "ssh_private_key_passphrase":
                _zeroize(entry.passphrase)
                entry.passphrase = None
            else:
                raise ValueError("native credential slot is invalid")
            return _slot_statuses(entry)

    def remove_profile(self, profile_id: str) -> None:
        _validate_profile_id(profile_id)
        with self._lock:
            entry = self._entries.pop(profile_id, None)
            if entry is not None:
                entry.clear()

    def statuses_for(
        self,
        profile_id: str,
        authentication_kind: NativeAuthenticationKind,
    ) -> tuple[CredentialSlotStatusV1, ...]:
        _validate_profile_id(profile_id)
        with self._lock:
            self._require_open()
            entry = self._entries.get(profile_id)
            if entry is None or entry.authentication_kind != authentication_kind:
                return _empty_slot_statuses(authentication_kind)
            return _slot_statuses(entry)

    def material_for(self, profile_id: str) -> NativeSshCredentialMaterial:
        _validate_profile_id(profile_id)
        with self._lock:
            self._require_open()
            entry = self._entries.get(profile_id)
            if entry is None:
                raise NativeCredentialUnavailableError("native credential is unavailable")
            if entry.authentication_kind == "native_password" and entry.password is None:
                raise NativeCredentialUnavailableError("native password is unavailable")
            if entry.authentication_kind == "native_private_key" and entry.private_key is None:
                raise NativeCredentialUnavailableError("native private key is unavailable")
            return NativeSshCredentialMaterial(
                authentication_kind=entry.authentication_kind,
                password=_copy_secret(entry.password),
                private_key=_copy_secret(entry.private_key),
                passphrase=_copy_secret(entry.passphrase),
            )

    def clear(self) -> None:
        with self._lock:
            entries = tuple(self._entries.values())
            self._entries.clear()
        for entry in entries:
            entry.clear()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            entries = tuple(self._entries.values())
            self._entries.clear()
        for entry in entries:
            entry.clear()

    def _require_open(self) -> None:
        if self._closed:
            raise NativeCredentialUnavailableError("native credential vault is closed")

    def _test_secret_buffers(self, profile_id: str) -> tuple[bytearray, ...]:
        with self._lock:
            entry = self._entries[profile_id]
            return tuple(
                value
                for value in (entry.password, entry.private_key, entry.passphrase)
                if value is not None
            )


def _validated_entry(
    authentication_kind: NativeAuthenticationKind,
    *,
    password: bytearray | None,
    private_key: bytearray | None,
    passphrase: bytearray | None,
) -> _CredentialEntry:
    for value in (password, private_key, passphrase):
        if value is not None and type(value) is not bytearray:
            raise ValueError("native credential values must use mutable byte buffers")
    if authentication_kind == "ssh_agent":
        if any(value is not None for value in (password, private_key, passphrase)):
            raise ValueError("SSH agent authentication does not accept native secret bytes")
    elif authentication_kind == "native_password":
        if password is None or not password or private_key is not None or passphrase is not None:
            raise ValueError("native password authentication requires only a password")
        if len(password) > _MAX_PASSWORD_BYTES or b"\x00" in password:
            raise ValueError("native password exceeds its closed byte contract")
    elif authentication_kind == "native_private_key":
        if private_key is None or not private_key or password is not None:
            raise ValueError("native private-key authentication requires key bytes")
        if len(private_key) > _MAX_PRIVATE_KEY_BYTES or b"\x00" in private_key:
            raise ValueError("native private key exceeds its closed byte contract")
        if passphrase is not None and (
            not passphrase
            or len(passphrase) > _MAX_PASSPHRASE_BYTES
            or b"\x00" in passphrase
        ):
            raise ValueError("native passphrase exceeds its closed byte contract")
    else:
        raise ValueError("native authentication kind is invalid")
    return _CredentialEntry(
        authentication_kind=authentication_kind,
        password=password,
        private_key=private_key,
        passphrase=passphrase,
    )


def _slot_statuses(entry: _CredentialEntry) -> tuple[CredentialSlotStatusV1, ...]:
    if entry.authentication_kind == "ssh_agent":
        return ()
    if entry.authentication_kind == "native_password":
        return (
            CredentialSlotStatusV1(
                kind="ssh_password",
                status="stored" if entry.password is not None else "empty",
            ),
        )
    return (
        CredentialSlotStatusV1(
            kind="ssh_private_key",
            status="stored" if entry.private_key is not None else "empty",
        ),
        CredentialSlotStatusV1(
            kind="ssh_private_key_passphrase",
            status="stored" if entry.passphrase is not None else "empty",
        ),
    )


def _empty_slot_statuses(
    authentication_kind: NativeAuthenticationKind,
) -> tuple[CredentialSlotStatusV1, ...]:
    return _slot_statuses(_CredentialEntry(authentication_kind=authentication_kind))


def _copy_secret(value: bytearray | None) -> bytearray | None:
    return None if value is None else bytearray(value)


def _zeroize(value: bytearray | None) -> None:
    if value is not None:
        value[:] = b"\x00" * len(value)


def _validate_profile_id(profile_id: str) -> None:
    if type(profile_id) is not str or _PROFILE_ID_RE.fullmatch(profile_id) is None:
        raise ValueError("native credential profile id is invalid")


__all__ = (
    "NativeAuthenticationKind",
    "NativeCredentialCapacityError",
    "NativeCredentialError",
    "NativeCredentialSlotKind",
    "NativeCredentialUnavailableError",
    "NativeCredentialVault",
    "NativeSshCredentialMaterial",
)
