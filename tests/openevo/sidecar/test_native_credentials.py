from __future__ import annotations

import pytest

from desktop.sidecar.native_credentials import (
    NativeCredentialCapacityError,
    NativeCredentialUnavailableError,
    NativeCredentialVault,
)


def test_native_credential_vault_replaces_and_zeroizes_secret_buffers() -> None:
    vault = NativeCredentialVault(max_profiles=2, max_total_bytes=128)
    vault.replace(
        "profile-a",
        authentication_kind="native_password",
        password=bytearray(b"first-password"),
    )
    original = vault._test_secret_buffers("profile-a")[0]

    statuses = vault.replace(
        "profile-a",
        authentication_kind="native_password",
        password=bytearray(b"second-password"),
    )

    assert bytes(original) == b"\x00" * len(original)
    assert [(slot.kind, slot.status) for slot in statuses] == [
        ("ssh_password", "stored")
    ]


def test_native_credential_vault_private_key_passphrase_is_optional_and_clearable() -> None:
    vault = NativeCredentialVault(max_profiles=2, max_total_bytes=128)
    statuses = vault.replace(
        "profile-a",
        authentication_kind="native_private_key",
        private_key=bytearray(b"PRIVATE KEY"),
    )
    assert [(slot.kind, slot.status) for slot in statuses] == [
        ("ssh_private_key", "stored"),
        ("ssh_private_key_passphrase", "empty"),
    ]

    vault.replace(
        "profile-a",
        authentication_kind="native_private_key",
        private_key=bytearray(b"PRIVATE KEY"),
        passphrase=bytearray(b"passphrase"),
    )
    passphrase = vault._test_secret_buffers("profile-a")[1]
    statuses = vault.delete_slot("profile-a", "ssh_private_key_passphrase")

    assert bytes(passphrase) == b"\x00" * len(passphrase)
    assert [(slot.kind, slot.status) for slot in statuses] == [
        ("ssh_private_key", "stored"),
        ("ssh_private_key_passphrase", "empty"),
    ]


def test_native_credential_vault_capacity_failure_keeps_existing_entry() -> None:
    vault = NativeCredentialVault(max_profiles=1, max_total_bytes=16)
    vault.replace(
        "profile-a",
        authentication_kind="native_password",
        password=bytearray(b"password"),
    )

    with pytest.raises(NativeCredentialCapacityError):
        vault.replace(
            "profile-b",
            authentication_kind="native_password",
            password=bytearray(b"password"),
        )

    assert vault.material_for("profile-a").password == bytearray(b"password")


def test_native_credential_vault_close_zeroizes_every_profile() -> None:
    vault = NativeCredentialVault(max_profiles=2, max_total_bytes=128)
    vault.replace(
        "profile-a",
        authentication_kind="native_password",
        password=bytearray(b"password"),
    )
    vault.replace(
        "profile-b",
        authentication_kind="native_private_key",
        private_key=bytearray(b"PRIVATE KEY"),
        passphrase=bytearray(b"passphrase"),
    )
    buffers = [
        *vault._test_secret_buffers("profile-a"),
        *vault._test_secret_buffers("profile-b"),
    ]

    vault.close()

    assert all(bytes(value) == b"\x00" * len(value) for value in buffers)
    with pytest.raises(NativeCredentialUnavailableError):
        vault.material_for("profile-a")
