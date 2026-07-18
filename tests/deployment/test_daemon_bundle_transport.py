from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import threading

import pytest
from pydantic import SecretStr

from openevo.deployment import managed_runtime_assets
from openevo.deployment.core_control import RemoteCoreControlAttachment
from openevo.deployment.daemon_bundle_transport import (
    DOCKER_USER_CONTAINER_V1,
    DaemonBundleTransportContractError,
    OpenedDaemonBundle,
    StagedDaemonBundle,
    _STAGE_SCRIPT,
    build_daemon_bundle_ensure_command,
    parse_daemon_bundle_identity,
    parse_staged_daemon_bundle,
)
from openevo.deployment.host_keys import ProviderKnownHostStore, TrustedKnownHostsBinding
from openevo.deployment.profile import RemoteProfileConfig
from openevo.deployment.ssh import (
    SshRemoteExecutorTransport,
    SshTransportError,
    SshTransportErrorCode,
)
from tests.managed_runtime_testkit import write_test_managed_runtime_archive


_ROOT_ADMISSION = """relative_root=${root#/home/}
remote_user=${relative_root%%/*}
[ -n "$remote_user" ]
[ "$root" = "/home/$remote_user/.openevo/daemon-bundles" ] || exit 64
"""


def _canonical(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n"


def _profile() -> RemoteProfileConfig:
    return RemoteProfileConfig.model_validate(
        {
            "version": 1,
            "id": "clean-host",
            "host": "clean.example.test",
            "port": 22,
            "user": "alice",
        }
    )


def _trusted_binding(
    tmp_path: Path,
    profile: RemoteProfileConfig,
) -> TrustedKnownHostsBinding:
    key_type = "ssh-ed25519"
    encoded_type = key_type.encode("ascii")
    key = hashlib.sha256(b"daemon-bundle-transport-test").digest()
    blob = struct.pack(">I", len(encoded_type)) + encoded_type + struct.pack(">I", len(key)) + key
    public_key = f"{key_type} {base64.b64encode(blob).decode('ascii')}"

    def keyscan(
        argv: list[str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=f"{profile.host} {public_key}\n",
            stderr="",
        )

    store = ProviderKnownHostStore(tmp_path / "known-hosts", runner=keyscan)
    pending = store.probe(profile)
    candidate = pending.candidates[0]
    return store.confirm(
        pending,
        profile=profile,
        algorithm=candidate.algorithm,
        fingerprint=candidate.fingerprint,
    )


def _completion_stderr(remote_command: str, return_code: int = 0) -> str:
    matches = re.findall(
        r"__OPENEVO_(?:DAEMON_BUNDLE|MANAGED_RUNTIME|REMOTE)_COMPLETION_[0-9a-f]{32}__=",
        remote_command,
    )
    assert len(matches) == 1
    return f"\n{matches[0]}{return_code}\n"


def _stage_receipt(*, digest: str, size: int, reused: bool = False) -> str:
    return _canonical(
        {
            "executable_path": f"/home/alice/.openevo/daemon-bundles/bundle-{digest}",
            "host_profile": "docker_user_container_v1",
            "reused": reused,
            "schema_version": 1,
            "sha256": digest,
            "size": size,
        }
    )


def _identity(*, digest: str, size: int) -> str:
    return _canonical(
        {
            "bundle": {
                "format": "pyinstaller-onefile",
                "sha256": digest,
                "size": size,
            },
            "core": {
                "distribution": "openevo",
                "version": "1.2.3",
                "wheel_sha256": "1" * 64,
            },
            "dependencies": {"lock_sha256": "2" * 64},
            "framework": {
                "lock_sha256": "3" * 64,
                "registry_digest": "4" * 64,
            },
            "platform": {"architecture": "x86_64", "system": "linux"},
            "release": {
                "identity": "5" * 64,
                "source_commit": "6" * 40,
            },
            "schema_version": 1,
        }
    )


def _attachment() -> str:
    return _canonical(
        {
            "attached": False,
            "bearer_token": "a" * 64,
            "capture_mode": "transcript",
            "execution_mode": "subscription",
            "generation": "b" * 32,
            "host": "127.0.0.1",
            "port": 43123,
            "registry_digest": "4" * 64,
            "release_identity": "5" * 64,
            "schema_version": 1,
            "source_commit": "6" * 40,
            "status_proof": "7" * 64,
        }
    )


def _runtime_receipt(release: object, *, reused: bool) -> str:
    return _canonical(
        {
            "aliases": list(release.aliases),
            "archive_sha256": release.sha256,
            "archive_size": release.byte_size,
            "config_id": release.config_id,
            "oci_index_id": release.oci_index_id,
            "platform": release.platform,
            "reused": reused,
            "schema_version": 2,
            "status": "ready",
        }
    )


def _runtime_prepare(transfer_id: str) -> str:
    return _canonical(
        {
            "incoming_device": 1,
            "incoming_inode": 3,
            "incoming_root": (
                "/home/alice/.openevo/core/managed-runtime-staging/incoming-" + transfer_id
            ),
            "schema_version": 1,
            "service_root": "/home/alice/.openevo/core",
            "staging_device": 1,
            "staging_inode": 2,
            "transfer_id": transfer_id,
        }
    )


def _staged(*, digest: str, size: int) -> StagedDaemonBundle:
    return StagedDaemonBundle(
        host_profile="docker_user_container_v1",
        sha256=digest,
        size=size,
        reused=False,
        _service_root="/home/alice/.openevo/daemon-bundles",
        _executable_path=f"/home/alice/.openevo/daemon-bundles/bundle-{digest}",
    )


def _minimal_tool_path(tmp_path: Path) -> Path:
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in DOCKER_USER_CONTAINER_V1.required_commands:
        if name == "/bin/sh":
            continue
        executable = shutil.which(name)
        assert executable is not None
        (tools / name).symlink_to(executable)
    return tools


def _run_stage_script(
    *,
    root: Path,
    payload: bytes,
    digest: str,
    expected_size: int,
    tool_path: Path,
) -> subprocess.CompletedProcess[bytes]:
    script = _STAGE_SCRIPT.replace(_ROOT_ADMISSION, ":")
    assert script != _STAGE_SCRIPT
    return subprocess.run(
        [
            "/bin/sh",
            "-c",
            script,
            "openevo-daemon-stage-v1",
            str(root),
            digest,
            str(expected_size),
            "a" * 32,
            "docker_user_container_v1",
        ],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": str(tool_path)},
        check=False,
    )


def test_host_profile_declares_clean_host_tools_without_python_rsync_or_scp() -> None:
    assert DOCKER_USER_CONTAINER_V1.digest_command == "sha256sum"
    assert "python" not in DOCKER_USER_CONTAINER_V1.required_commands
    assert "rsync" not in DOCKER_USER_CONTAINER_V1.required_commands
    assert "scp" not in DOCKER_USER_CONTAINER_V1.required_commands
    assert 'cat > "$tmp"' in _STAGE_SCRIPT
    assert 'mkdir -- "$lock"' in _STAGE_SCRIPT
    assert 'ln -- "$tmp" "$target"' in _STAGE_SCRIPT
    assert "trap cleanup 0" in _STAGE_SCRIPT


def test_stage_script_works_with_only_declared_tools_and_reuses_exact_bundle(
    tmp_path: Path,
) -> None:
    payload = b"\x7fELF\0clean-host-bundle"
    digest = hashlib.sha256(payload).hexdigest()
    root = tmp_path / ".openevo" / "daemon-bundles"
    tools = _minimal_tool_path(tmp_path)

    first = _run_stage_script(
        root=root,
        payload=payload,
        digest=digest,
        expected_size=len(payload),
        tool_path=tools,
    )
    second = _run_stage_script(
        root=root,
        payload=payload,
        digest=digest,
        expected_size=len(payload),
        tool_path=tools,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout)["reused"] is False
    assert json.loads(second.stdout)["reused"] is True
    target = root / f"bundle-{digest}"
    assert target.read_bytes() == payload
    assert target.stat().st_mode & 0o777 == 0o700
    assert target.stat().st_nlink == 1


@pytest.mark.parametrize("failure", ["partial", "digest", "concurrent"])
def test_stage_script_failures_leave_no_partial_publication(
    tmp_path: Path,
    failure: str,
) -> None:
    payload = b"\x7fELF\0complete"
    digest = hashlib.sha256(payload).hexdigest()
    root = tmp_path / ".openevo" / "daemon-bundles"
    tools = _minimal_tool_path(tmp_path)
    if failure == "concurrent":
        root.mkdir(parents=True, mode=0o700)
        (root / ".bundle-stage.lock").mkdir(mode=0o700)
    sent = payload[:-1] if failure == "partial" else payload
    expected_digest = "f" * 64 if failure == "digest" else digest

    result = _run_stage_script(
        root=root,
        payload=sent,
        digest=expected_digest,
        expected_size=len(payload),
        tool_path=tools,
    )

    assert result.returncode != 0
    assert not (root / f"bundle-{expected_digest}").exists()
    assert not list(root.glob(".incoming-*"))
    if failure != "concurrent":
        assert not (root / ".bundle-stage.lock").exists()


def test_opened_bundle_rejects_symlink_and_detects_content_change(tmp_path: Path) -> None:
    payload = b"\x7fELF\0bundle"
    digest = hashlib.sha256(payload).hexdigest()
    bundle = tmp_path / "openevo-daemon"
    bundle.write_bytes(payload)
    link = tmp_path / "bundle-link"
    link.symlink_to(bundle)

    with pytest.raises(DaemonBundleTransportContractError):
        OpenedDaemonBundle.open(
            str(link),
            expected_sha256=digest,
            expected_size=len(payload),
        )

    with OpenedDaemonBundle.open(
        str(bundle),
        expected_sha256=digest,
        expected_size=len(payload),
    ) as snapshot:
        bundle.write_bytes(b"\x7fELF\0mutated")
        with pytest.raises(DaemonBundleTransportContractError):
            snapshot.verify_unchanged()


def test_closed_parsers_reject_noncanonical_and_extra_fields() -> None:
    digest = "a" * 64
    payload = _stage_receipt(digest=digest, size=12)
    assert parse_staged_daemon_bundle(payload).sha256 == digest
    with pytest.raises(DaemonBundleTransportContractError):
        parse_staged_daemon_bundle(payload.rstrip("\n"))

    identity = json.loads(_identity(digest=digest, size=12))
    identity["unexpected"] = True
    with pytest.raises(DaemonBundleTransportContractError):
        parse_daemon_bundle_identity(SecretStr(_canonical(identity)))


def test_ensure_command_uses_public_bundle_command() -> None:
    bundle = _staged(digest="a" * 64, size=12)

    command = build_daemon_bundle_ensure_command(
        bundle,
        port=0,
        deadline_seconds=45,
    )

    assert command.endswith("service ensure --port 0 --deadline-seconds 45.000000")
    assert "openevo.backend.service" not in command
    assert "attachment-name" not in command
    assert "/home/alice" not in repr(bundle)


def test_ssh_stage_streams_bundle_fd_without_binary_in_argv(tmp_path: Path) -> None:
    payload = b"\x7fELF\0binary-not-command-text"
    digest = hashlib.sha256(payload).hexdigest()
    bundle_path = tmp_path / "openevo-daemon"
    bundle_path.write_bytes(payload)
    calls: list[list[str]] = []

    def streaming_runner(
        argv: list[str],
        timeout_seconds: float,
        stdin_fd: int,
        cancel_event: threading.Event | None,
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds, cancel_event
        calls.append(argv)
        streamed = bytearray()
        while chunk := os.read(stdin_fd, 8):
            streamed.extend(chunk)
        assert bytes(streamed) == payload
        assert payload.decode("ascii", errors="ignore") not in "\0".join(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=_stage_receipt(digest=digest, size=len(payload)),
            stderr=_completion_stderr(argv[-1]),
        )

    profile = _profile()
    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=_trusted_binding(tmp_path, profile),
        streaming_runner=streaming_runner,
    )
    staged = transport.stage_daemon_bundle(
        bundle_path=str(bundle_path),
        bundle_sha256=digest,
        bundle_size=len(payload),
        timeout_seconds=10,
    )

    assert staged.sha256 == digest
    assert len(calls) == 1
    remote_command = calls[0][-1].lower()
    assert "python" not in remote_command
    assert "rsync" not in remote_command
    assert "scp" not in remote_command


def test_ssh_stage_revalidates_host_key_authority_before_streaming(tmp_path: Path) -> None:
    payload = b"\x7fELF\0bundle"
    digest = hashlib.sha256(payload).hexdigest()
    bundle_path = tmp_path / "openevo-daemon"
    bundle_path.write_bytes(payload)
    profile = _profile()
    binding = _trusted_binding(tmp_path, profile)
    called = False

    def streaming_runner(
        argv: list[str],
        timeout_seconds: float,
        stdin_fd: int,
        cancel_event: threading.Event | None,
    ) -> subprocess.CompletedProcess[str]:
        del argv, timeout_seconds, stdin_fd, cancel_event
        nonlocal called
        called = True
        raise AssertionError("streaming must not start after host-key drift")

    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=binding,
        streaming_runner=streaming_runner,
    )
    binding.known_hosts_file.unlink()

    with pytest.raises(SshTransportError) as raised:
        transport.stage_daemon_bundle(
            bundle_path=str(bundle_path),
            bundle_sha256=digest,
            bundle_size=len(payload),
            timeout_seconds=10,
        )

    assert raised.value.code is SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED
    assert called is False


@pytest.mark.parametrize(
    ("runner_failure", "expected_code"),
    [
        ("remote", SshTransportErrorCode.DAEMON_BUNDLE_FAILED),
        ("timeout", SshTransportErrorCode.TIMEOUT),
        ("cancelled", SshTransportErrorCode.CANCELLED),
        ("disconnect", SshTransportErrorCode.CONNECTION_FAILED),
    ],
)
def test_ssh_stage_failures_are_typed_and_fail_closed(
    tmp_path: Path,
    runner_failure: str,
    expected_code: SshTransportErrorCode,
) -> None:
    payload = b"\x7fELF\0bundle"
    digest = hashlib.sha256(payload).hexdigest()
    bundle_path = tmp_path / "openevo-daemon"
    bundle_path.write_bytes(payload)
    cancel_event = threading.Event()
    if runner_failure == "cancelled":
        cancel_event.set()

    def streaming_runner(
        argv: list[str],
        timeout_seconds: float,
        stdin_fd: int,
        event: threading.Event | None,
    ) -> subprocess.CompletedProcess[str]:
        del stdin_fd, event
        if runner_failure == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout_seconds)
        if runner_failure == "disconnect":
            return subprocess.CompletedProcess(argv, 255, stdout="", stderr="")
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr=_completion_stderr(argv[-1], 1),
        )

    profile = _profile()
    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=_trusted_binding(tmp_path, profile),
        streaming_runner=streaming_runner,
    )

    with pytest.raises(SshTransportError) as raised:
        transport.stage_daemon_bundle(
            bundle_path=str(bundle_path),
            bundle_sha256=digest,
            bundle_size=len(payload),
            timeout_seconds=10,
            cancel_event=cancel_event,
        )

    assert raised.value.code is expected_code


def test_ssh_ensure_returns_existing_remote_attachment_model(tmp_path: Path) -> None:
    digest = "a" * 64
    bundle = _staged(digest=digest, size=12)
    commands: list[str] = []

    def runner(
        argv: list[str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        command = argv[-1]
        commands.append(command)
        stdout = _identity(digest=digest, size=12) if " identity" in command else _attachment()
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=stdout,
            stderr=_completion_stderr(command),
        )

    profile = _profile()
    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=_trusted_binding(tmp_path, profile),
        runner=runner,
    )

    attachment = transport.ensure_daemon_bundle(
        bundle,
        port=0,
        timeout_seconds=45,
    )

    assert isinstance(attachment, RemoteCoreControlAttachment)
    assert attachment.remote_port == 43123
    assert attachment.bearer_token == "a" * 64
    assert len(commands) == 2
    assert " service ensure --port 0 " in commands[1]
    assert "openevo.backend.service" not in commands[1]


def test_ssh_inspect_and_stop_use_closed_bundle_responses(tmp_path: Path) -> None:
    bundle = _staged(digest="a" * 64, size=12)
    status = _canonical(
        {
            "attached": True,
            "generation": "b" * 32,
            "port": 43123,
            "registry_digest": "4" * 64,
            "release_identity": "5" * 64,
            "schema_version": 1,
            "source_commit": "6" * 40,
        }
    )
    stop = _canonical({"schema_version": 1, "stopped": True})

    def runner(
        argv: list[str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        command = argv[-1]
        stdout = status if " service inspect" in command else stop
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=stdout,
            stderr=_completion_stderr(command),
        )

    profile = _profile()
    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=_trusted_binding(tmp_path, profile),
        runner=runner,
    )

    observed = transport.inspect_daemon_bundle(bundle)
    stopped = transport.stop_daemon_bundle(bundle)

    assert observed.remote_port == 43123
    assert observed.generation == "b" * 32
    assert stopped.stopped is True


def test_daemon_managed_runtime_streams_without_python_rsync_or_scp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "openevo-science-runtime-0.1.0-linux-amd64.tar.gz"
    release = write_test_managed_runtime_archive(archive)
    monkeypatch.setattr(managed_runtime_assets, "MANAGED_RUNTIME_ARCHIVE_RELEASE", release)
    payload = archive.read_bytes()
    transfer_id = "c" * 32
    commands: list[str] = []
    streamed_commands: list[str] = []

    def runner(
        argv: list[str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        command = argv[-1]
        commands.append(command)
        if " managed-runtime probe " in command:
            stdout = _canonical({"schema_version": 2, "status": "load_required"})
        elif " managed-runtime prepare " in command:
            stdout = _runtime_prepare(transfer_id)
        elif " managed-runtime finalize " in command:
            stdout = _runtime_receipt(release, reused=False)
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=stdout,
            stderr=_completion_stderr(command),
        )

    def streaming_runner(
        argv: list[str],
        timeout_seconds: float,
        stdin_fd: int,
        cancel_event: threading.Event | None,
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds, cancel_event
        command = argv[-1]
        streamed_commands.append(command)
        observed = bytearray()
        while chunk := os.read(stdin_fd, 64 * 1024):
            observed.extend(chunk)
        assert bytes(observed) == payload
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=_canonical({"schema_version": 1, "status": "received"}),
            stderr=_completion_stderr(command),
        )

    profile = _profile()
    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=_trusted_binding(tmp_path, profile),
        runner=runner,
        streaming_runner=streaming_runner,
    )

    receipt = transport.ensure_managed_runtime_from_daemon(
        _staged(digest="a" * 64, size=12),
        archive_path=str(archive),
        archive_sha256=release.sha256,
        archive_size=release.byte_size,
        platform=release.platform,
        config_id=release.config_id,
        oci_index_id=release.oci_index_id,
        aliases=release.aliases,
        timeout_seconds=30,
    )

    assert receipt.reused is False
    assert len(commands) == 3
    assert len(streamed_commands) == 1
    all_commands = " ".join(commands + streamed_commands).lower()
    assert "python" not in all_commands
    assert "rsync" not in all_commands
    assert "scp" not in all_commands
    assert str(archive) not in all_commands


def test_daemon_managed_runtime_reuses_ready_image_without_reading_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "openevo-science-runtime-0.1.0-linux-amd64.tar.gz"
    release = write_test_managed_runtime_archive(archive)
    monkeypatch.setattr(managed_runtime_assets, "MANAGED_RUNTIME_ARCHIVE_RELEASE", release)
    archive.unlink()

    def runner(
        argv: list[str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        command = argv[-1]
        assert " managed-runtime probe " in command
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=_runtime_receipt(release, reused=True),
            stderr=_completion_stderr(command),
        )

    def streaming_runner(*_args: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("ready runtime must not stream an archive")

    profile = _profile()
    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=_trusted_binding(tmp_path, profile),
        runner=runner,
        streaming_runner=streaming_runner,
    )

    receipt = transport.ensure_managed_runtime_from_daemon(
        _staged(digest="a" * 64, size=12),
        archive_path=str(archive),
        archive_sha256=release.sha256,
        archive_size=release.byte_size,
        platform=release.platform,
        config_id=release.config_id,
        oci_index_id=release.oci_index_id,
        aliases=release.aliases,
        timeout_seconds=30,
    )

    assert receipt.reused is True


def test_daemon_managed_runtime_stream_failure_discards_exact_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "openevo-science-runtime-0.1.0-linux-amd64.tar.gz"
    release = write_test_managed_runtime_archive(archive)
    monkeypatch.setattr(managed_runtime_assets, "MANAGED_RUNTIME_ARCHIVE_RELEASE", release)
    transfer_id = "d" * 32
    commands: list[str] = []

    def runner(
        argv: list[str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        command = argv[-1]
        commands.append(command)
        if " managed-runtime probe " in command:
            stdout = _canonical({"schema_version": 2, "status": "load_required"})
        elif " managed-runtime prepare " in command:
            stdout = _runtime_prepare(transfer_id)
        elif " managed-runtime discard " in command:
            stdout = _canonical({"schema_version": 1, "status": "discarded"})
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=stdout,
            stderr=_completion_stderr(command),
        )

    def streaming_runner(
        argv: list[str],
        timeout_seconds: float,
        stdin_fd: int,
        cancel_event: threading.Event | None,
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds, stdin_fd, cancel_event
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr=_completion_stderr(argv[-1], 1),
        )

    profile = _profile()
    transport = SshRemoteExecutorTransport(
        profile,
        trusted_host=_trusted_binding(tmp_path, profile),
        runner=runner,
        streaming_runner=streaming_runner,
    )

    with pytest.raises(SshTransportError) as raised:
        transport.ensure_managed_runtime_from_daemon(
            _staged(digest="a" * 64, size=12),
            archive_path=str(archive),
            archive_sha256=release.sha256,
            archive_size=release.byte_size,
            platform=release.platform,
            config_id=release.config_id,
            oci_index_id=release.oci_index_id,
            aliases=release.aliases,
            timeout_seconds=30,
        )

    assert raised.value.code is SshTransportErrorCode.MANAGED_RUNTIME_FAILED
    assert " managed-runtime discard " in commands[-1]
    assert transfer_id in commands[-1]
