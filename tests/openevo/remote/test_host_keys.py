from __future__ import annotations

import base64
import gc
import hashlib
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import openevo.deployment.host_keys as host_keys_module
from openevo.deployment.host_keys import (
    HostKeyStoreError,
    HostKeyStoreErrorCode,
    ProviderKnownHostStore,
    TrustedKnownHostsBinding,
)
from openevo.deployment.profile import RemoteProfileConfig


def _profile(**overrides: object) -> RemoteProfileConfig:
    payload: dict[str, object] = {
        "version": 1,
        "id": "lab-gpu",
        "host": "gpu.example.edu",
        "port": 2222,
        "user": "alice",
    }
    payload.update(overrides)
    return RemoteProfileConfig.model_validate(payload)


def _public_key(key_type: str, marker: bytes = b"key-material") -> str:
    if key_type == "ssh-ed25519":
        fields = (key_type.encode("ascii"), hashlib.sha256(marker).digest())
    elif key_type == "ecdsa-sha2-nistp256":
        point = b"\x04" + hashlib.sha512(marker).digest()
        fields = (key_type.encode("ascii"), b"nistp256", point)
    elif key_type == "ssh-rsa":
        modulus = b"\x00\x80" + hashlib.shake_256(marker).digest(255)
        fields = (key_type.encode("ascii"), b"\x01\x00\x01", modulus)
    else:
        fields = (key_type.encode("ascii"), marker)
    blob = b"".join(struct.pack(">I", len(field)) + field for field in fields)
    return f"{key_type} {base64.b64encode(blob).decode('ascii')}"


def _fingerprint(public_key: str) -> str:
    blob = base64.b64decode(public_key.split()[1], validate=True)
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return f"SHA256:{digest}"


def _host_field(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def _line(host: str, port: int, public_key: str) -> str:
    return f"{_host_field(host, port)} {public_key}"


class KeyscanRunner:
    def __init__(self, *outputs: str, return_code: int = 0) -> None:
        self.outputs = list(outputs)
        self.return_code = return_code
        self.calls: list[tuple[list[str], float]] = []

    def __call__(
        self, argv: list[str], timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, timeout_seconds))
        output = self.outputs[min(len(self.calls) - 1, len(self.outputs) - 1)]
        return subprocess.CompletedProcess(
            argv,
            self.return_code,
            stdout=output,
            stderr="probe detail that must not enter trust state",
        )


def _valid_output(profile: RemoteProfileConfig, *, marker: bytes = b"key-material") -> str:
    keys = (
        _public_key("ssh-ed25519", marker + b"-ed25519"),
        _public_key("ecdsa-sha2-nistp256", marker + b"-ecdsa"),
        _public_key("ssh-rsa", marker + b"-rsa"),
    )
    return "\n".join(_line(profile.host, profile.port, key) for key in keys) + "\n"


def _confirmed_binding(
    tmp_path: Path,
    profile: RemoteProfileConfig | None = None,
) -> TrustedKnownHostsBinding:
    active_profile = profile or _profile()
    runner = KeyscanRunner(_valid_output(active_profile))
    store = ProviderKnownHostStore(tmp_path / "known-hosts", runner=runner)
    pending = store.probe(active_profile)
    candidate = pending.candidates[0]
    return store.confirm(
        pending,
        profile=active_profile,
        algorithm=candidate.algorithm,
        fingerprint=candidate.fingerprint,
    )


@pytest.mark.parametrize(
    ("host", "port", "expected_tail"),
    [
        ("gpu.example.edu", 22, ["gpu.example.edu"]),
        ("192.0.2.8", 2222, ["-p", "2222", "192.0.2.8"]),
        ("2001:db8::8", 2200, ["-p", "2200", "2001:db8::8"]),
    ],
)
def test_probe_uses_closed_keyscan_algorithm_set_and_parses_host_forms(
    tmp_path: Path,
    host: str,
    port: int,
    expected_tail: list[str],
) -> None:
    profile = _profile(host=host, port=port)
    runner = KeyscanRunner(_valid_output(profile))
    store = ProviderKnownHostStore(tmp_path / "known-hosts", runner=runner)

    pending = store.probe(profile, timeout_seconds=4.5)

    assert runner.calls == [
        (
            [
                host_keys_module.SSH_KEYSCAN_EXECUTABLE,
                "-T",
                "5",
                "-t",
                "ed25519,ecdsa,rsa",
                *expected_tail,
            ],
            4.5,
        )
    ]
    assert [candidate.algorithm for candidate in pending.candidates] == [
        "ssh-ed25519",
        "ecdsa-sha2-nistp256",
        "rsa-sha2-512",
    ]
    for candidate in pending.candidates:
        assert candidate.fingerprint == _fingerprint(candidate.public_key)
    assert not (tmp_path / "known-hosts").exists()


@pytest.mark.parametrize(
    "malicious_line",
    [
        "@cert-authority [gpu.example.edu]:2222 ssh-ed25519 KEY",
        "|1|hashed|host ssh-ed25519 KEY",
        "[gpu.example.edu]:2222,other.example ssh-ed25519 KEY",
        "[gpu.example.edu]:2222 ssh-ed25519 KEY extra-key",
        "[other.example]:2222 ssh-ed25519 KEY",
        "[gpu.example.edu]:22 ssh-ed25519 KEY",
        "[gpu.example.edu]:2222 ssh-dss KEY",
        "[gpu.example.edu]:2222 ssh-ed25519 !!!",
        "[gpu.example.edu]:2222 ssh-ed25519 KEY\r",
    ],
)
def test_probe_rejects_malicious_or_noncanonical_keyscan_lines(
    tmp_path: Path,
    malicious_line: str,
) -> None:
    store = ProviderKnownHostStore(
        tmp_path / "known-hosts",
        runner=KeyscanRunner(malicious_line + "\n"),
    )

    with pytest.raises(ValueError, match="ssh-keyscan"):
        store.probe(_profile())


def test_probe_rejects_duplicate_algorithm_and_failed_command(tmp_path: Path) -> None:
    profile = _profile()
    key = _public_key("ssh-ed25519")
    duplicate = f"{_line(profile.host, profile.port, key)}\n" * 2
    store = ProviderKnownHostStore(
        tmp_path / "known-hosts",
        runner=KeyscanRunner(duplicate),
    )

    with pytest.raises(ValueError, match="duplicate"):
        store.probe(profile)

    failed = ProviderKnownHostStore(
        tmp_path / "failed",
        runner=KeyscanRunner("", return_code=1),
    )
    with pytest.raises(RuntimeError, match="host-key probe failed"):
        failed.probe(profile)


def test_probe_rejects_key_blob_with_valid_type_but_invalid_algorithm_shape(
    tmp_path: Path,
) -> None:
    profile = _profile()
    key_type = b"ssh-ed25519"
    malformed_blob = struct.pack(">I", len(key_type)) + key_type + b"not-an-ssh-string"
    malformed_key = f"ssh-ed25519 {base64.b64encode(malformed_blob).decode('ascii')}"
    store = ProviderKnownHostStore(
        tmp_path / "known-hosts",
        runner=KeyscanRunner(_line(profile.host, profile.port, malformed_key) + "\n"),
    )

    with pytest.raises(ValueError, match="ssh-keyscan"):
        store.probe(profile)


def test_probe_rejects_rsa_host_key_smaller_than_2048_bits(tmp_path: Path) -> None:
    profile = _profile()
    key_type = b"ssh-rsa"
    modulus = b"\x00\x80" + hashlib.shake_256(b"weak-rsa").digest(127)
    fields = (key_type, b"\x01\x00\x01", modulus)
    blob = b"".join(struct.pack(">I", len(field)) + field for field in fields)
    public_key = f"ssh-rsa {base64.b64encode(blob).decode('ascii')}"
    store = ProviderKnownHostStore(
        tmp_path / "known-hosts",
        runner=KeyscanRunner(_line(profile.host, profile.port, public_key) + "\n"),
    )

    with pytest.raises(ValueError, match="2048"):
        store.probe(profile)


def test_probe_rejects_host_option_injection_before_spawning(tmp_path: Path) -> None:
    runner = KeyscanRunner("")
    store = ProviderKnownHostStore(tmp_path / "known-hosts", runner=runner)

    with pytest.raises(ValueError, match="host"):
        store.probe(_profile(host="-f/tmp/attacker-input"))

    assert runner.calls == []


@pytest.mark.parametrize("suffix", ["provider path", "provider%h", "provider\\path"])
def test_store_rejects_known_hosts_option_path_injection(
    tmp_path: Path,
    suffix: str,
) -> None:
    with pytest.raises(ValueError, match="path contains unsupported"):
        ProviderKnownHostStore(tmp_path / suffix, runner=KeyscanRunner(""))


def test_store_allows_only_the_standard_macos_application_support_space(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "Research User"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    state_root = (
        home
        / "Library"
        / "Application Support"
        / "org.openevo.desktop"
        / "state-v2"
    )
    state_root.mkdir(parents=True, mode=0o700)
    ProviderKnownHostStore(
        state_root / "ssh-host-keys",
        secure_ancestor=state_root,
        runner=KeyscanRunner(""),
    )

    with pytest.raises(ValueError, match="path contains unsupported"):
        ProviderKnownHostStore(
            state_root / "unexpected space" / "ssh-host-keys",
            secure_ancestor=state_root / "unexpected space",
            runner=KeyscanRunner(""),
        )

    lookalike_root = (
        tmp_path
        / "Library"
        / "Application Support"
        / "org.openevo.desktop"
        / "state-v2"
    )
    with pytest.raises(ValueError, match="path contains unsupported"):
        ProviderKnownHostStore(
            lookalike_root / "ssh-host-keys",
            secure_ancestor=lookalike_root,
            runner=KeyscanRunner(""),
        )


def test_confirm_requires_exact_choice_and_unchanged_probe(tmp_path: Path) -> None:
    profile = _profile()
    first_output = _valid_output(profile, marker=b"first")
    runner = KeyscanRunner(first_output, _valid_output(profile, marker=b"changed"))
    store = ProviderKnownHostStore(tmp_path / "known-hosts", runner=runner)
    pending = store.probe(profile)
    candidate = pending.candidates[0]

    with pytest.raises(ValueError, match="changed before confirmation"):
        store.confirm(
            pending,
            profile=profile,
            algorithm=candidate.algorithm,
            fingerprint=candidate.fingerprint,
        )
    assert not (tmp_path / "known-hosts").exists()


def test_confirm_rejects_profile_host_port_or_fingerprint_change(tmp_path: Path) -> None:
    profile = _profile()
    store = ProviderKnownHostStore(
        tmp_path / "known-hosts",
        runner=KeyscanRunner(_valid_output(profile)),
    )
    pending = store.probe(profile)
    candidate = pending.candidates[0]

    for changed_profile in (
        _profile(id="other-profile"),
        _profile(host="new.example.edu"),
        _profile(port=2200),
    ):
        with pytest.raises(ValueError, match="does not match pending probe"):
            store.confirm(
                pending,
                profile=changed_profile,
                algorithm=candidate.algorithm,
                fingerprint=candidate.fingerprint,
            )

    with pytest.raises(ValueError, match="confirmation does not match"):
        store.confirm(
            pending,
            profile=profile,
            algorithm=candidate.algorithm,
            fingerprint="SHA256:not-the-candidate",
        )


def test_confirm_persists_full_key_and_metadata_in_owner_only_store(tmp_path: Path) -> None:
    profile = _profile(id="profile/with unsafe filename text")
    binding = _confirmed_binding(tmp_path, profile)

    root_stat = binding.known_hosts_file.parent.stat()
    file_stat = binding.known_hosts_file.stat()
    assert stat.S_IMODE(root_stat.st_mode) == 0o700
    assert stat.S_IMODE(file_stat.st_mode) == 0o600
    assert root_stat.st_uid == os.geteuid()
    assert file_stat.st_uid == os.geteuid()
    assert file_stat.st_nlink == 1
    assert binding.known_hosts_file.parent == tmp_path / "known-hosts"
    assert binding.known_hosts_file.name.endswith(".known_hosts")
    assert "/" not in binding.known_hosts_file.name

    metadata_line, known_host_line = binding.known_hosts_file.read_text(
        encoding="utf-8"
    ).splitlines()
    assert metadata_line.startswith("# openevo-host-key-v1 ")
    metadata = json.loads(metadata_line.removeprefix("# openevo-host-key-v1 "))
    assert metadata == {
        "algorithm": binding.algorithm,
        "fingerprint": binding.fingerprint,
        "host": profile.host,
        "port": profile.port,
        "profile_id": profile.id,
        "public_key": binding.public_key,
    }
    assert known_host_line == f"{_host_field(profile.host, profile.port)} {binding.public_key}"


def test_existing_changed_key_is_rejected_instead_of_replaced(tmp_path: Path) -> None:
    profile = _profile()
    first = _confirmed_binding(tmp_path, profile)
    original = first.known_hosts_file.read_bytes()
    changed_runner = KeyscanRunner(_valid_output(profile, marker=b"replacement"))
    changed_store = ProviderKnownHostStore(first.known_hosts_file.parent, runner=changed_runner)
    pending = changed_store.probe(profile)
    candidate = pending.candidates[0]

    with pytest.raises(ValueError, match="conflicts with existing trust"):
        changed_store.confirm(
            pending,
            profile=profile,
            algorithm=candidate.algorithm,
            fingerprint=candidate.fingerprint,
        )

    assert first.known_hosts_file.read_bytes() == original


def test_store_rejects_symlink_non_regular_and_insecure_existing_file(tmp_path: Path) -> None:
    profile = _profile()
    binding = _confirmed_binding(tmp_path, profile)
    path = binding.known_hosts_file
    original = path.read_bytes()

    path.unlink()
    path.symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symlink|regular"):
        ProviderKnownHostStore(path.parent, runner=KeyscanRunner("")).load(
            profile, expected_fingerprint=binding.fingerprint
        )

    path.unlink()
    path.mkdir()
    with pytest.raises(ValueError, match="regular"):
        ProviderKnownHostStore(path.parent, runner=KeyscanRunner("")).load(
            profile, expected_fingerprint=binding.fingerprint
        )

    path.rmdir()
    path.write_bytes(original)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        ProviderKnownHostStore(path.parent, runner=KeyscanRunner("")).load(
            profile, expected_fingerprint=binding.fingerprint
        )


def test_store_rejects_non_owner_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    binding = _confirmed_binding(tmp_path, profile)
    real_stat = os.stat

    def non_owner_stat(path: str | bytes | int, *args: object, **kwargs: object):
        result = real_stat(path, *args, **kwargs)
        if path == binding.known_hosts_file.name:
            values = list(result)
            values[4] = result.st_uid + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr("openevo.deployment.host_keys.os.stat", non_owner_stat)

    with pytest.raises(ValueError, match="owner-controlled"):
        ProviderKnownHostStore(binding.known_hosts_file.parent, runner=KeyscanRunner("")).load(
            profile, expected_fingerprint=binding.fingerprint
        )


def test_store_rejects_symlink_root_even_when_target_is_private(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    root = tmp_path / "known-hosts"
    root.symlink_to(target, target_is_directory=True)
    store = ProviderKnownHostStore(root, runner=KeyscanRunner(""))

    with pytest.raises(ValueError, match="root"):
        store.load(_profile(), expected_fingerprint="SHA256:unused")


def test_store_rejects_insecure_or_symlinked_secure_ancestor(tmp_path: Path) -> None:
    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o700)
    insecure.chmod(0o770)

    with pytest.raises(ValueError, match="secure ancestor.*writable"):
        ProviderKnownHostStore(insecure / "known-hosts", runner=KeyscanRunner(""))

    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(private, target_is_directory=True)
    with pytest.raises(ValueError, match="secure ancestor"):
        ProviderKnownHostStore(linked / "known-hosts", runner=KeyscanRunner(""))

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir(mode=0o700)
    nested_ancestor = real_parent / "ancestor"
    nested_ancestor.mkdir(mode=0o700)
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ValueError, match="secure ancestor"):
        ProviderKnownHostStore(
            parent_link / "ancestor" / "known-hosts",
            secure_ancestor=parent_link / "ancestor",
            runner=KeyscanRunner(""),
        )


def test_macos_store_paths_normalize_only_fixed_system_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_keys_module.sys, "platform", "darwin")

    assert host_keys_module._canonical_darwin_system_alias(
        Path("/var/folders/user/state")
    ) == Path("/private/var/folders/user/state")
    assert host_keys_module._canonical_darwin_system_alias(
        Path("/tmp/openevo/state")
    ) == Path("/private/tmp/openevo/state")
    assert host_keys_module._canonical_darwin_system_alias(
        Path("/private/var/folders/user/state")
    ) == Path("/private/var/folders/user/state")
    assert host_keys_module._canonical_darwin_system_alias(
        Path("/Users/alice/state")
    ) == Path("/Users/alice/state")


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS system aliases")
def test_store_accepts_inode_bound_macos_var_alias() -> None:
    with tempfile.TemporaryDirectory(prefix="openevo-host-keys-", dir="/var/tmp") as value:
        requested_ancestor = Path(value)
        assert os.fspath(requested_ancestor).startswith("/var/tmp/")

        store = ProviderKnownHostStore(
            requested_ancestor / "known-hosts",
            secure_ancestor=requested_ancestor,
            runner=KeyscanRunner(""),
        )

        expected = (
            Path("/private")
            / requested_ancestor.relative_to("/")
            / "known-hosts"
        )
        assert store._root == expected
        requested = os.stat(requested_ancestor)
        opened = os.fstat(store._anchor._ancestor_fd)
        assert (requested.st_dev, requested.st_ino) == (opened.st_dev, opened.st_ino)


def test_store_alias_validation_failure_closes_ancestor_fd_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    real_close = os.close
    closed: list[int] = []

    monkeypatch.setattr(
        host_keys_module,
        "_open_secure_ancestor",
        lambda _path: descriptor,
    )

    def reject_alias(*_args: object) -> None:
        raise ValueError("injected alias mismatch")

    def record_close(value: int) -> None:
        if value == descriptor:
            closed.append(value)
        real_close(value)

    monkeypatch.setattr(
        host_keys_module,
        "_validate_darwin_system_alias_binding",
        reject_alias,
    )
    monkeypatch.setattr(host_keys_module.os, "close", record_close)

    with pytest.raises(ValueError, match="injected alias mismatch"):
        host_keys_module._StoreAnchor(
            tmp_path / "known-hosts",
            tmp_path,
            requested_secure_ancestor=tmp_path,
            lock_timeout_seconds=1.0,
        )
    gc.collect()

    assert closed == [descriptor]


def test_load_requires_explicit_matching_fingerprint(tmp_path: Path) -> None:
    profile = _profile()
    binding = _confirmed_binding(tmp_path, profile)
    store = ProviderKnownHostStore(binding.known_hosts_file.parent, runner=KeyscanRunner(""))

    with pytest.raises(TypeError):
        store.load(profile)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="fingerprint"):
        store.load(profile, expected_fingerprint="SHA256:not-the-stored-key")
    assert (
        store.load(profile, expected_fingerprint=binding.fingerprint).fingerprint
        == binding.fingerprint
    )


def test_revoke_and_rotate_are_fingerprint_compare_and_swap(tmp_path: Path) -> None:
    profile = _profile()
    old = _confirmed_binding(tmp_path, profile)
    replacement_output = _valid_output(profile, marker=b"replacement")
    store = ProviderKnownHostStore(
        old.known_hosts_file.parent,
        runner=KeyscanRunner(replacement_output),
    )
    pending = store.probe(profile)
    replacement = pending.candidates[0]
    with pytest.raises(ValueError, match="expected fingerprint"):
        store.rotate_from_pending(
            pending,
            profile=profile,
            algorithm=replacement.algorithm,
            fingerprint=replacement.fingerprint,
            expected_old_fingerprint="SHA256:not-current",
        )
    rotated = store.rotate_from_pending(
        pending,
        profile=profile,
        algorithm=replacement.algorithm,
        fingerprint=replacement.fingerprint,
        expected_old_fingerprint=old.fingerprint,
    )
    assert rotated.fingerprint == replacement.fingerprint

    with pytest.raises(ValueError, match="expected fingerprint"):
        store.revoke(profile, expected_fingerprint=old.fingerprint)
    store.revoke(profile, expected_fingerprint=rotated.fingerprint)
    assert store.load(profile, expected_fingerprint=rotated.fingerprint) is None


def test_rotate_rejects_cross_store_or_modified_pending_without_changing_trust(
    tmp_path: Path,
) -> None:
    profile = _profile()
    old = _confirmed_binding(tmp_path, profile)
    replacement_output = _valid_output(profile, marker=b"replacement")
    issuing_store = ProviderKnownHostStore(
        old.known_hosts_file.parent,
        runner=KeyscanRunner(replacement_output),
    )
    other_store = ProviderKnownHostStore(
        old.known_hosts_file.parent,
        runner=KeyscanRunner(replacement_output),
    )
    pending = issuing_store.probe(profile)
    replacement = pending.candidates[0]
    original = old.known_hosts_file.read_bytes()

    with pytest.raises(ValueError, match="not issued by this store"):
        other_store.rotate_from_pending(
            pending,
            profile=profile,
            algorithm=replacement.algorithm,
            fingerprint=replacement.fingerprint,
            expected_old_fingerprint=old.fingerprint,
        )
    modified = replace(pending, candidates=(pending.candidates[1],))
    with pytest.raises(ValueError, match="digest"):
        issuing_store.rotate_from_pending(
            modified,
            profile=profile,
            algorithm=pending.candidates[1].algorithm,
            fingerprint=pending.candidates[1].fingerprint,
            expected_old_fingerprint=old.fingerprint,
        )

    assert old.known_hosts_file.read_bytes() == original


def test_rotate_reprobe_failure_preserves_old_trust(tmp_path: Path) -> None:
    profile = _profile()
    old = _confirmed_binding(tmp_path, profile)
    original = old.known_hosts_file.read_bytes()
    store = ProviderKnownHostStore(
        old.known_hosts_file.parent,
        runner=KeyscanRunner(
            _valid_output(profile, marker=b"replacement"),
            _valid_output(profile, marker=b"changed-again"),
        ),
    )
    pending = store.probe(profile)
    candidate = pending.candidates[0]

    with pytest.raises(ValueError, match="changed before confirmation"):
        store.rotate_from_pending(
            pending,
            profile=profile,
            algorithm=candidate.algorithm,
            fingerprint=candidate.fingerprint,
            expected_old_fingerprint=old.fingerprint,
        )

    assert old.known_hosts_file.read_bytes() == original


def test_rotate_validates_temporary_record_before_irreversible_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    old = _confirmed_binding(tmp_path, profile)
    original = old.known_hosts_file.read_bytes()
    replacement_output = _valid_output(profile, marker=b"replacement")
    store = ProviderKnownHostStore(
        old.known_hosts_file.parent,
        runner=KeyscanRunner(replacement_output),
    )
    pending = store.probe(profile)
    replacement = pending.candidates[0]
    real_binding_from_content = host_keys_module._binding_from_content

    def reject_temporary_record(path, content, active_profile, anchor):
        if path.name.endswith(".rotate"):
            raise ValueError("injected temporary validation failure")
        return real_binding_from_content(path, content, active_profile, anchor)

    monkeypatch.setattr(
        "openevo.deployment.host_keys._binding_from_content",
        reject_temporary_record,
    )

    with pytest.raises(ValueError, match="temporary validation failure"):
        store.rotate_from_pending(
            pending,
            profile=profile,
            algorithm=replacement.algorithm,
            fingerprint=replacement.fingerprint,
            expected_old_fingerprint=old.fingerprint,
        )

    assert old.known_hosts_file.read_bytes() == original
    assert store.load(profile, expected_fingerprint=old.fingerprint) is not None


def test_rotate_post_replace_failure_is_typed_indeterminate_and_reloadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    old = _confirmed_binding(tmp_path, profile)
    replacement_output = _valid_output(profile, marker=b"replacement")
    store = ProviderKnownHostStore(
        old.known_hosts_file.parent,
        runner=KeyscanRunner(replacement_output),
    )
    pending = store.probe(profile)
    replacement = pending.candidates[0]
    real_fsync = os.fsync
    directory_fsyncs = 0

    def fail_rotation_directory_fsync(fd: int) -> None:
        nonlocal directory_fsyncs
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_fsyncs += 1
            if directory_fsyncs == 1:
                raise OSError("SECRET_POST_REPLACE_PATH")
        real_fsync(fd)

    monkeypatch.setattr("openevo.deployment.host_keys.os.fsync", fail_rotation_directory_fsync)

    with pytest.raises(HostKeyStoreError) as exc_info:
        store.rotate_from_pending(
            pending,
            profile=profile,
            algorithm=replacement.algorithm,
            fingerprint=replacement.fingerprint,
            expected_old_fingerprint=old.fingerprint,
        )

    error = exc_info.value
    assert error.code is HostKeyStoreErrorCode.ROTATION_INDETERMINATE
    assert error.authoritative_fingerprint == replacement.fingerprint
    assert "reload" in str(error).lower()
    assert "candidate fingerprint" in str(error).lower()
    assert replacement.fingerprint in str(error)
    assert "SECRET_POST_REPLACE_PATH" not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    reloaded = store.load(profile, expected_fingerprint=replacement.fingerprint)
    assert reloaded is not None
    assert reloaded.fingerprint == replacement.fingerprint


@pytest.mark.parametrize("cleanup_failure", ["unlock", "close"])
def test_rotate_lock_cleanup_failure_after_replace_is_typed_and_closes_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: str,
) -> None:
    profile = _profile()
    old = _confirmed_binding(tmp_path, profile)
    replacement_output = _valid_output(profile, marker=b"replacement")
    store = ProviderKnownHostStore(
        old.known_hosts_file.parent,
        runner=KeyscanRunner(replacement_output),
    )
    pending = store.probe(profile)
    replacement = pending.candidates[0]
    real_replace = os.replace
    real_flock = host_keys_module.fcntl.flock
    real_close = os.close
    committed = False
    injected = False
    lock_fd: int | None = None
    close_attempted = False

    def record_replace(*args, **kwargs) -> None:
        nonlocal committed
        real_replace(*args, **kwargs)
        committed = True

    def fail_unlock(fd: int, operation: int) -> None:
        nonlocal injected, lock_fd
        if operation & host_keys_module.fcntl.LOCK_EX:
            lock_fd = fd
        if (
            cleanup_failure == "unlock"
            and committed
            and operation == host_keys_module.fcntl.LOCK_UN
            and not injected
        ):
            injected = True
            raise OSError("SECRET_ROTATION_UNLOCK_FAILURE")
        real_flock(fd, operation)

    def fail_close(fd: int) -> None:
        nonlocal close_attempted, injected
        if committed and fd == lock_fd:
            close_attempted = True
            if cleanup_failure == "close" and not injected:
                injected = True
                real_close(fd)
                raise OSError("SECRET_ROTATION_CLOSE_FAILURE")
        real_close(fd)

    monkeypatch.setattr("openevo.deployment.host_keys.os.replace", record_replace)
    monkeypatch.setattr("openevo.deployment.host_keys.fcntl.flock", fail_unlock)
    monkeypatch.setattr("openevo.deployment.host_keys.os.close", fail_close)

    with pytest.raises(HostKeyStoreError) as exc_info:
        store.rotate_from_pending(
            pending,
            profile=profile,
            algorithm=replacement.algorithm,
            fingerprint=replacement.fingerprint,
            expected_old_fingerprint=old.fingerprint,
        )

    error = exc_info.value
    rendered = "".join(traceback.format_exception(error))
    assert error.code is HostKeyStoreErrorCode.ROTATION_INDETERMINATE
    assert error.authoritative_fingerprint == replacement.fingerprint
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "SECRET_ROTATION_" not in str(error)
    assert "SECRET_ROTATION_" not in rendered
    assert injected is True
    assert close_attempted is True
    assert lock_fd is not None
    with pytest.raises(OSError):
        os.fstat(lock_fd)

    reloaded = store.load(profile, expected_fingerprint=replacement.fingerprint)
    assert reloaded is not None


def test_rotate_lock_cleanup_failure_before_replace_stays_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    old = _confirmed_binding(tmp_path, profile)
    original = old.known_hosts_file.read_bytes()
    replacement_output = _valid_output(profile, marker=b"replacement")
    store = ProviderKnownHostStore(
        old.known_hosts_file.parent,
        runner=KeyscanRunner(replacement_output),
    )
    pending = store.probe(profile)
    replacement = pending.candidates[0]
    real_flock = host_keys_module.fcntl.flock
    injected = False

    def fail_unlock_once(fd: int, operation: int) -> None:
        nonlocal injected
        if operation == host_keys_module.fcntl.LOCK_UN and not injected:
            injected = True
            raise OSError("SECRET_PRECOMMIT_UNLOCK_FAILURE")
        real_flock(fd, operation)

    monkeypatch.setattr("openevo.deployment.host_keys.fcntl.flock", fail_unlock_once)

    with pytest.raises(ValueError, match="lock cleanup failed") as exc_info:
        store.rotate_from_pending(
            pending,
            profile=profile,
            algorithm=replacement.algorithm,
            fingerprint=replacement.fingerprint,
            expected_old_fingerprint=replacement.fingerprint,
        )

    assert not isinstance(exc_info.value, HostKeyStoreError)
    assert old.known_hosts_file.read_bytes() == original
    assert store.load(profile, expected_fingerprint=old.fingerprint) is not None


def test_concurrent_rotate_allows_only_one_first_writer(tmp_path: Path) -> None:
    profile = _profile()
    old = _confirmed_binding(tmp_path, profile)
    stores: list[ProviderKnownHostStore] = []
    pending_probes = []
    for marker in (b"first-writer-a", b"first-writer-b"):
        store = ProviderKnownHostStore(
            old.known_hosts_file.parent,
            runner=KeyscanRunner(_valid_output(profile, marker=marker)),
        )
        pending = store.probe(profile)
        candidate = pending.candidates[0]
        stores.append(store)
        pending_probes.append((pending, candidate))

    def rotate(index: int) -> str:
        pending, candidate = pending_probes[index]
        return (
            stores[index]
            .rotate_from_pending(
                pending,
                profile=profile,
                algorithm=candidate.algorithm,
                fingerprint=candidate.fingerprint,
                expected_old_fingerprint=old.fingerprint,
            )
            .fingerprint
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(rotate, index) for index in range(2)]
    successes = [future.result() for future in futures if future.exception() is None]
    failures = [future.exception() for future in futures if future.exception() is not None]

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert "expected fingerprint" in str(failures[0])


def test_spawn_lease_holds_shared_lock_until_process_lifecycle_ends(
    tmp_path: Path,
) -> None:
    profile = _profile()
    binding = _confirmed_binding(tmp_path, profile)
    revoker = ProviderKnownHostStore(
        binding.known_hosts_file.parent,
        runner=KeyscanRunner(""),
    )
    started = threading.Event()

    def revoke() -> None:
        started.set()
        revoker.revoke(profile, expected_fingerprint=binding.fingerprint)

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with binding.open_for_spawn(profile) as lease_path:
            assert lease_path.exists()
            future = executor.submit(revoke)
            assert started.wait(timeout=1.0)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.05)
            assert not future.done()

        future.result(timeout=1.0)
    finally:
        executor.shutdown(wait=True)
    assert not binding.known_hosts_file.exists()


def test_spawn_lease_enter_cancellation_closes_fd_and_removes_private_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cancelled(BaseException):
        pass

    profile = _profile()
    binding = _confirmed_binding(tmp_path, profile)
    original_write = host_keys_module._write_new_secure_file

    def write_then_cancel(*args: object, **kwargs: object) -> None:
        original_write(*args, **kwargs)
        raise Cancelled

    monkeypatch.setattr(host_keys_module, "_write_new_secure_file", write_then_cancel)

    with pytest.raises(Cancelled):
        binding.open_for_spawn(profile).__enter__()

    assert not list(tmp_path.rglob(".openevo-ssh-lease-*"))
    with binding._anchor.locked_root(create=False, exclusive=True):
        pass


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_spawn_lease_directory_publish_cancellation_is_recovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
) -> None:
    profile = _profile()
    binding = _confirmed_binding(tmp_path, profile)
    original_open = os.open
    injected = False

    def cancel_before_directory_pin(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal injected
        if (
            not injected
            and isinstance(path, str)
            and path.startswith(".openevo-ssh-lease-")
            and flags & os.O_DIRECTORY
        ):
            injected = True
            raise interruption()
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", cancel_before_directory_pin)

    with pytest.raises(interruption):
        binding.open_for_spawn(profile).__enter__()

    assert injected is True
    assert not list(tmp_path.rglob(".openevo-ssh-lease-*"))
    with binding._anchor.locked_root(create=False, exclusive=True):
        pass


@pytest.mark.parametrize("cleanup_failure", ["fstat", "unlink", "rmdir"])
def test_spawn_lease_cleanup_failure_retains_exact_identity_and_shared_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: str,
) -> None:
    profile = _profile()
    binding = _confirmed_binding(tmp_path, profile)
    lease = binding.open_for_spawn(profile)
    lease_path = lease.__enter__()
    directory_fd = lease._directory_fd
    directory_name = lease._directory_name
    assert directory_fd is not None
    assert directory_name is not None
    original_fstat = os.fstat
    original_unlink = os.unlink
    original_rmdir = os.rmdir
    remaining_failures = 2

    def flaky_fstat(fd: int) -> os.stat_result:
        nonlocal remaining_failures
        if cleanup_failure == "fstat" and fd == directory_fd and remaining_failures:
            remaining_failures -= 1
            raise OSError("injected lease fstat failure")
        return original_fstat(fd)

    def flaky_unlink(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal remaining_failures
        if (
            cleanup_failure == "unlink"
            and path == "known_hosts"
            and dir_fd == directory_fd
            and remaining_failures
        ):
            remaining_failures -= 1
            raise OSError("injected lease unlink failure")
        original_unlink(path, dir_fd=dir_fd)

    def flaky_rmdir(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal remaining_failures
        if cleanup_failure == "rmdir" and path == directory_name and remaining_failures:
            remaining_failures -= 1
            raise OSError("injected lease rmdir failure")
        original_rmdir(path, dir_fd=dir_fd)

    contender = ProviderKnownHostStore(
        binding.known_hosts_file.parent,
        runner=KeyscanRunner(""),
        lock_timeout_seconds=0.05,
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "fstat", flaky_fstat)
        scoped.setattr(os, "unlink", flaky_unlink)
        scoped.setattr(os, "rmdir", flaky_rmdir)
        with pytest.raises(OSError, match="injected lease"):
            lease.__exit__(None, None, None)
        host_keys_module._retry_retained_spawn_lease_cleanup()
        assert id(lease) in host_keys_module._SPAWN_LEASES

    assert remaining_failures == 0
    assert lease_path.parent.exists()
    with pytest.raises(HostKeyStoreError) as error:
        contender.revoke(profile, expected_fingerprint=binding.fingerprint)
    assert error.value.code is HostKeyStoreErrorCode.HOST_KEY_IN_USE

    host_keys_module._retry_retained_spawn_lease_cleanup()

    assert not lease_path.parent.exists()
    contender.revoke(profile, expected_fingerprint=binding.fingerprint)


def test_spawn_lease_retries_repeated_lock_cleanup_failures_before_registry_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    binding = _confirmed_binding(tmp_path, profile)
    lock_registry_ids = set(host_keys_module._STORE_LOCK_AUTHORITIES)
    lease = binding.open_for_spawn(profile)
    lease_path = lease.__enter__()
    lock_authority = lease._locked
    assert lock_authority is not None
    contender = ProviderKnownHostStore(
        binding.known_hosts_file.parent,
        runner=KeyscanRunner(""),
        lock_timeout_seconds=0.05,
    )
    real_flock = host_keys_module.fcntl.flock
    real_close = os.close
    real_fstat = os.fstat
    lock_fd: int | None = None
    unlock_failures = 8
    close_failures = 8
    fstat_failures = 8

    def fail_repeated_unlock(fd: int, operation: int) -> None:
        nonlocal lock_fd, unlock_failures
        if operation == host_keys_module.fcntl.LOCK_UN:
            lock_fd = fd
            if unlock_failures:
                unlock_failures -= 1
                raise OSError("injected repeated unlock failure")
        real_flock(fd, operation)

    def fail_repeated_close(fd: int) -> None:
        nonlocal close_failures
        if fd == lock_fd and close_failures:
            close_failures -= 1
            raise OSError("injected repeated close failure")
        real_close(fd)

    def fail_repeated_fstat(fd: int) -> os.stat_result:
        nonlocal fstat_failures
        if fd == lock_fd and fstat_failures:
            fstat_failures -= 1
            raise OSError("injected repeated fstat failure")
        return real_fstat(fd)

    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(host_keys_module.fcntl, "flock", fail_repeated_unlock)
            scoped.setattr(host_keys_module.os, "close", fail_repeated_close)
            scoped.setattr(host_keys_module.os, "fstat", fail_repeated_fstat)

            with pytest.raises(ValueError, match="lock cleanup failed"):
                lease.__exit__(None, None, None)
            assert lock_fd is not None
            for _ in range(2):
                host_keys_module._retry_retained_spawn_lease_cleanup()
                assert id(lease) in host_keys_module._SPAWN_LEASES
                assert id(lock_authority) in host_keys_module._STORE_LOCK_AUTHORITIES
                assert real_fstat(lock_fd).st_ino > 0
                with pytest.raises(HostKeyStoreError) as error:
                    contender.revoke(profile, expected_fingerprint=binding.fingerprint)
                assert error.value.code is HostKeyStoreErrorCode.HOST_KEY_IN_USE

        host_keys_module._retry_retained_spawn_lease_cleanup()

        assert id(lease) not in host_keys_module._SPAWN_LEASES
        assert set(host_keys_module._STORE_LOCK_AUTHORITIES) == lock_registry_ids
        assert not lease_path.parent.exists()
        assert lock_fd is not None
        with pytest.raises(OSError) as closed:
            real_fstat(lock_fd)
        assert closed.value.errno == 9
        contender.revoke(profile, expected_fingerprint=binding.fingerprint)
    finally:
        if id(lease) in host_keys_module._SPAWN_LEASES:
            host_keys_module._retry_retained_spawn_lease_cleanup()
        if lock_fd is not None:
            try:
                real_flock(lock_fd, host_keys_module.fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                real_close(lock_fd)
            except OSError:
                pass


def test_spawn_lease_retained_cleanup_capacity_fails_closed_before_new_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    binding = _confirmed_binding(tmp_path, profile)
    monkeypatch.setattr(
        host_keys_module,
        "_MAX_KNOWN_HOST_SPAWN_LEASES",
        1,
        raising=False,
    )
    original_unlink = os.unlink

    def fail_unlink(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> None:
        if path == "known_hosts":
            raise OSError("persistent lease unlink failure")
        original_unlink(path, dir_fd=dir_fd)

    first = binding.open_for_spawn(profile)
    first.__enter__()
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "unlink", fail_unlink)
        with pytest.raises(OSError, match="persistent lease unlink failure"):
            first.__exit__(None, None, None)
        with pytest.raises((HostKeyStoreError, ValueError)):
            binding.open_for_spawn(profile).__enter__()

    assert len(list(tmp_path.rglob(".openevo-ssh-lease-*"))) == 1
    host_keys_module._retry_retained_spawn_lease_cleanup()
    assert not list(tmp_path.rglob(".openevo-ssh-lease-*"))


def test_exclusive_lock_timeout_is_typed_and_shared_across_store_instances(
    tmp_path: Path,
) -> None:
    profile = _profile()
    binding = _confirmed_binding(tmp_path, profile)
    contender = ProviderKnownHostStore(
        binding.known_hosts_file.parent,
        runner=KeyscanRunner(""),
        lock_timeout_seconds=0.05,
    )

    with binding.open_for_spawn(profile):
        with pytest.raises(HostKeyStoreError) as exc_info:
            contender.revoke(profile, expected_fingerprint=binding.fingerprint)

    assert exc_info.value.code is HostKeyStoreErrorCode.HOST_KEY_IN_USE
    lock_path = binding.known_hosts_file.parent / ".openevo-host-key.lock"
    assert lock_path.is_file()
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_shared_lock_timeout_is_monotonic_bounded_and_typed(tmp_path: Path) -> None:
    profile = _profile()
    binding = _confirmed_binding(tmp_path, profile)
    holder = ProviderKnownHostStore(
        binding.known_hosts_file.parent,
        runner=KeyscanRunner(""),
        lock_timeout_seconds=0.05,
    )
    contender = ProviderKnownHostStore(
        binding.known_hosts_file.parent,
        runner=KeyscanRunner(""),
        lock_timeout_seconds=0.05,
    )

    started = time.monotonic()
    with holder._anchor.locked_root(create=False, exclusive=True):
        with pytest.raises(HostKeyStoreError) as exc_info:
            contender.load(profile, expected_fingerprint=binding.fingerprint)
    elapsed = time.monotonic() - started

    assert exc_info.value.code is HostKeyStoreErrorCode.HOST_KEY_IN_USE
    assert elapsed < 0.5


def test_existing_instances_reject_replaced_lock_namespace(tmp_path: Path) -> None:
    profile = _profile()
    binding = _confirmed_binding(tmp_path, profile)
    stores = [
        ProviderKnownHostStore(binding.known_hosts_file.parent, runner=KeyscanRunner(""))
        for _ in range(2)
    ]
    for store in stores:
        assert store.load(profile, expected_fingerprint=binding.fingerprint) is not None

    lock_path = binding.known_hosts_file.parent / ".openevo-host-key.lock"
    lock_path.unlink()
    lock_path.touch(mode=0o600)

    for store in stores:
        with pytest.raises(ValueError, match="lock"):
            store.load(profile, expected_fingerprint=binding.fingerprint)


def test_confirmation_fsyncs_file_and_store_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    store = ProviderKnownHostStore(
        tmp_path / "known-hosts",
        runner=KeyscanRunner(_valid_output(profile)),
    )
    pending = store.probe(profile)
    candidate = pending.candidates[0]
    real_fsync = os.fsync
    fsynced_modes: list[int] = []

    def recording_fsync(fd: int) -> None:
        fsynced_modes.append(os.fstat(fd).st_mode)
        real_fsync(fd)

    monkeypatch.setattr("openevo.deployment.host_keys.os.fsync", recording_fsync)

    store.confirm(
        pending,
        profile=profile,
        algorithm=candidate.algorithm,
        fingerprint=candidate.fingerprint,
    )

    assert any(stat.S_ISREG(mode) for mode in fsynced_modes)
    assert any(stat.S_ISDIR(mode) for mode in fsynced_modes)


def test_publish_race_with_symlink_fails_without_touching_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    runner = KeyscanRunner(_valid_output(profile))
    store = ProviderKnownHostStore(tmp_path / "known-hosts", runner=runner)
    pending = store.probe(profile)
    candidate = pending.candidates[0]
    outside = tmp_path / "outside"
    outside.write_text("unchanged", encoding="utf-8")
    real_link = os.link

    def racing_link(
        src: str,
        dst: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        os.symlink(outside, dst, dir_fd=dst_dir_fd)
        real_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr("openevo.deployment.host_keys.os.link", racing_link)

    with pytest.raises(ValueError, match="race|symlink"):
        store.confirm(
            pending,
            profile=profile,
            algorithm=candidate.algorithm,
            fingerprint=candidate.fingerprint,
        )

    assert outside.read_text(encoding="utf-8") == "unchanged"


def test_binding_rejects_tamper_and_wrong_profile(tmp_path: Path) -> None:
    profile = _profile()
    binding = _confirmed_binding(tmp_path, profile)

    with pytest.raises(ValueError, match="profile binding"):
        binding.validate_for(_profile(host="other.example.edu"))

    binding.known_hosts_file.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="content"):
        binding.validate_for(profile)


def test_transport_binding_revalidation_rejects_deleted_file(tmp_path: Path) -> None:
    profile = _profile()
    binding = _confirmed_binding(tmp_path, profile)
    binding.known_hosts_file.unlink()

    with pytest.raises(ValueError, match="missing"):
        binding.validate_for(profile)
