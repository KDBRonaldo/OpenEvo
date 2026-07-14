from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import struct
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from openevo.deployment.host_keys import (
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
                "ssh-keyscan",
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
    confirmed_pending = store.confirm_pending(
        pending,
        profile=profile,
        algorithm=replacement.algorithm,
        fingerprint=replacement.fingerprint,
    )

    with pytest.raises(ValueError, match="expected fingerprint"):
        store.rotate(
            profile,
            expected_old_fingerprint="SHA256:not-current",
            confirmed_pending=confirmed_pending,
        )
    rotated = store.rotate(
        profile,
        expected_old_fingerprint=old.fingerprint,
        confirmed_pending=confirmed_pending,
    )
    assert rotated.fingerprint == replacement.fingerprint

    with pytest.raises(ValueError, match="expected fingerprint"):
        store.revoke(profile, expected_fingerprint=old.fingerprint)
    store.revoke(profile, expected_fingerprint=rotated.fingerprint)
    assert store.load(profile, expected_fingerprint=rotated.fingerprint) is None


def test_concurrent_rotate_allows_only_one_first_writer(tmp_path: Path) -> None:
    profile = _profile()
    old = _confirmed_binding(tmp_path, profile)
    stores: list[ProviderKnownHostStore] = []
    confirmed = []
    for marker in (b"first-writer-a", b"first-writer-b"):
        store = ProviderKnownHostStore(
            old.known_hosts_file.parent,
            runner=KeyscanRunner(_valid_output(profile, marker=marker)),
        )
        pending = store.probe(profile)
        candidate = pending.candidates[0]
        stores.append(store)
        confirmed.append(
            store.confirm_pending(
                pending,
                profile=profile,
                algorithm=candidate.algorithm,
                fingerprint=candidate.fingerprint,
            )
        )

    def rotate(index: int) -> str:
        return stores[index].rotate(
            profile,
            expected_old_fingerprint=old.fingerprint,
            confirmed_pending=confirmed[index],
        ).fingerprint

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
