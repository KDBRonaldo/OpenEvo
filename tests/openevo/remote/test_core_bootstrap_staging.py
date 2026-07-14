from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import signal
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

from pydantic import SecretStr
import pytest

from openevo.deployment.host_keys import ProviderKnownHostStore
from openevo.deployment import core_assets, core_runtime
import openevo.deployment.ssh as ssh_module
from openevo.deployment.profile import RemoteProfileConfig
from openevo.deployment.ssh import (
    SshRemoteExecutorTransport,
    SshTransportError,
    SshTransportErrorCode,
    StagedCoreBootstrapAssets,
)


WHEEL_NAME = "openevo-0.1.0-py3-none-any.whl"
BUNDLE_ID = "a" * 64
TRANSFER_ID = "b" * 32
SECOND_TRANSFER_ID = "c" * 32


class RecordingRunner:
    def __init__(self, return_codes: list[int] | None = None) -> None:
        self.return_codes = list(return_codes or [])
        self.calls: list[tuple[list[str], float]] = []

    def __call__(
        self, argv: list[str], timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, timeout_seconds))
        code = self.return_codes.pop(0) if self.return_codes else 0
        return subprocess.CompletedProcess(argv, code, stdout="", stderr="private remote path")


class RemoteFailureRunner(RecordingRunner):
    def __call__(
        self, argv: list[str], timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, timeout_seconds))
        marked_command = argv[-1]
        marker = re.search(r"(__OPENEVO_REMOTE_COMPLETION_[0-9a-f]{32}__=)", marked_command)
        assert marker is not None
        return subprocess.CompletedProcess(
            argv,
            77,
            stdout="private remote output",
            stderr=f"private remote path\n{marker.group(1)}77\n",
        )


def _profile() -> RemoteProfileConfig:
    return RemoteProfileConfig.model_validate(
        {
            "version": 1,
            "id": "profile-a",
            "host": "gpu.example.edu",
            "port": 2222,
            "user": "alice",
        }
    )


def _transport(
    tmp_path: Path,
    runner: RecordingRunner,
) -> SshRemoteExecutorTransport:
    profile = _profile()
    key_type = "ssh-ed25519"
    encoded_type = key_type.encode("ascii")
    key = hashlib.sha256(b"staging-test-key").digest()
    blob = struct.pack(">I", len(encoded_type)) + encoded_type + struct.pack(">I", len(key)) + key
    public_key = f"{key_type} {base64.b64encode(blob).decode('ascii')}"
    host = f"[{profile.host}]:{profile.port}"

    def host_key_runner(
        argv: list[str], timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=f"{host} {public_key}\n",
            stderr="",
        )

    store = ProviderKnownHostStore(tmp_path / "known-hosts", runner=host_key_runner)
    pending = store.probe(profile)
    candidate = pending.candidates[0]
    binding = store.confirm(
        pending,
        profile=profile,
        algorithm=candidate.algorithm,
        fingerprint=candidate.fingerprint,
    )
    return SshRemoteExecutorTransport(profile, trusted_host=binding, runner=runner)


def _assets(tmp_path: Path) -> tuple[Path, str, Path, str]:
    wheel = tmp_path / WHEEL_NAME
    wheel.write_bytes(b"sealed wheel bytes")
    wheel_digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    lock = tmp_path / "framework-lock.json"
    lock.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "distribution": "openevo",
                "distribution_version": "0.1.0",
                "distribution_digest": wheel_digest,
                "wheel_filename": wheel.name,
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return wheel, wheel_digest, lock, hashlib.sha256(lock.read_bytes()).hexdigest()


def _stage_responses(
    wheel_digest: str,
    lock_digest: str,
    *,
    transfer_id: str = TRANSFER_ID,
) -> list[SecretStr]:
    root = "/home/alice/.openevo/core"
    incoming = f"{root}/asset-staging/incoming-{BUNDLE_ID}-{transfer_id}"
    final = f"{root}/assets/{BUNDLE_ID}"
    return [
        SecretStr(
            json.dumps(
                {
                    "schema_version": 1,
                    "service_root": root,
                    "incoming_root": incoming,
                    "transfer_id": transfer_id,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        ),
        SecretStr(
            json.dumps(
                {
                    "schema_version": 1,
                    "service_root": root,
                    "wheel_path": f"{final}/{WHEEL_NAME}",
                    "framework_lock_path": f"{final}/framework-lock.json",
                    "wheel_sha256": wheel_digest,
                    "framework_lock_sha256": lock_digest,
                    "bundle_device": 11,
                    "bundle_inode": 12,
                    "wheel_device": 13,
                    "wheel_inode": 14,
                    "framework_lock_device": 15,
                    "framework_lock_inode": 16,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        ),
    ]


def _install_secret_responses(
    monkeypatch: pytest.MonkeyPatch,
    transport: SshRemoteExecutorTransport,
    responses: list[SecretStr],
) -> list[tuple[str, float]]:
    calls: list[tuple[str, float]] = []

    def run_secret(
        command: str,
        *,
        timeout_seconds: float,
        remote_failure_code: SshTransportErrorCode,
    ) -> SecretStr:
        del remote_failure_code
        calls.append((command, timeout_seconds))
        return responses.pop(0)

    monkeypatch.setattr(transport, "_run_secret_with_remote_failure", run_secret)
    return calls


def _stage(
    transport: SshRemoteExecutorTransport,
    assets: tuple[Path, str, Path, str],
    *,
    timeout_seconds: float = 20.0,
) -> StagedCoreBootstrapAssets:
    wheel, wheel_digest, lock, lock_digest = assets
    return transport.stage_core_bootstrap_assets(
        wheel_path=str(wheel),
        wheel_sha256=wheel_digest,
        wheel_size=wheel.stat().st_size,
        framework_lock_path=str(lock),
        framework_lock_sha256=lock_digest,
        framework_lock_size=lock.stat().st_size,
        bundle_id=BUNDLE_ID,
        timeout_seconds=timeout_seconds,
    )


def _execute_remote_script(
    script: str,
    argv: list[str],
    *,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as scoped:
        scoped.setattr(
            pwd,
            "getpwuid",
            lambda _uid: SimpleNamespace(pw_dir=str(home)),
        )
        scoped.setattr(sys, "argv", ["core-assets-remote", *argv])
        exec(compile(script, "<core-assets-remote>", "exec"), {"__name__": "__main__"})


def test_remote_asset_scripts_prepare_an_empty_private_root_and_publish_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    assets = _assets(tmp_path)

    _execute_remote_script(
        core_assets._REMOTE_PREPARE_SCRIPT,
        [BUNDLE_ID],
        home=home,
        monkeypatch=monkeypatch,
    )
    prepared = json.loads(capsys.readouterr().out)
    service_root = home / ".openevo" / "core"
    transfer_id = prepared["transfer_id"]
    incoming = Path(prepared["incoming_root"])
    assert prepared == {
        "schema_version": 1,
        "service_root": str(service_root),
        "incoming_root": str(incoming),
        "transfer_id": transfer_id,
    }
    assert re.fullmatch(r"[0-9a-f]{32}", transfer_id)
    for source, target in (
        (assets[0], incoming / WHEEL_NAME),
        (assets[2], incoming / "framework-lock.json"),
    ):
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)

    _execute_remote_script(
        core_assets._REMOTE_FINALIZE_SCRIPT,
        [
            str(service_root),
            BUNDLE_ID,
            transfer_id,
            WHEEL_NAME,
            assets[1],
            str(assets[0].stat().st_size),
            assets[3],
            str(assets[2].stat().st_size),
        ],
        home=home,
        monkeypatch=monkeypatch,
    )
    finalized = json.loads(capsys.readouterr().out)
    final_root = service_root / "assets" / BUNDLE_ID
    assert finalized["wheel_path"] == str(final_root / WHEEL_NAME)
    assert finalized["framework_lock_path"] == str(final_root / "framework-lock.json")
    assert not incoming.exists()
    assert (final_root / WHEEL_NAME).read_bytes() == assets[0].read_bytes()
    assert stat_mode(service_root) == 0o700
    assert stat_mode(final_root) == 0o500
    assert stat_mode(final_root / WHEEL_NAME) == 0o400
    assert finalized["bundle_device"] == final_root.stat().st_dev
    assert finalized["bundle_inode"] == final_root.stat().st_ino
    assert finalized["wheel_device"] == (final_root / WHEEL_NAME).stat().st_dev
    assert finalized["wheel_inode"] == (final_root / WHEEL_NAME).stat().st_ino
    assert finalized["framework_lock_device"] == (final_root / "framework-lock.json").stat().st_dev
    assert finalized["framework_lock_inode"] == (final_root / "framework-lock.json").stat().st_ino

    _execute_remote_script(
        core_assets._REMOTE_PREPARE_SCRIPT,
        [BUNDLE_ID],
        home=home,
        monkeypatch=monkeypatch,
    )
    retried_prepare = json.loads(capsys.readouterr().out)
    retried_incoming = Path(retried_prepare["incoming_root"])
    assert retried_prepare["transfer_id"] != transfer_id
    assert retried_incoming != incoming
    for source, target in (
        (assets[0], retried_incoming / WHEEL_NAME),
        (assets[2], retried_incoming / "framework-lock.json"),
    ):
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
    _execute_remote_script(
        core_assets._REMOTE_FINALIZE_SCRIPT,
        [
            str(service_root),
            BUNDLE_ID,
            retried_prepare["transfer_id"],
            WHEEL_NAME,
            assets[1],
            str(assets[0].stat().st_size),
            assets[3],
            str(assets[2].stat().st_size),
        ],
        home=home,
        monkeypatch=monkeypatch,
    )
    assert json.loads(capsys.readouterr().out) == finalized
    assert not retried_incoming.exists()
    assert (final_root / WHEEL_NAME).read_bytes() == assets[0].read_bytes()


def test_remote_asset_publication_isolated_from_held_writer_and_incoming_dir_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    assets = _assets(tmp_path)
    _execute_remote_script(
        core_assets._REMOTE_PREPARE_SCRIPT,
        [BUNDLE_ID],
        home=home,
        monkeypatch=monkeypatch,
    )
    prepared = json.loads(capsys.readouterr().out)
    incoming = Path(prepared["incoming_root"])
    for source, target in (
        (assets[0], incoming / WHEEL_NAME),
        (assets[2], incoming / "framework-lock.json"),
    ):
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)

    writer_fd = os.open(incoming / WHEEL_NAME, os.O_WRONLY | os.O_CLOEXEC)
    incoming_fd = os.open(incoming, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    incoming_wheel_identity = os.fstat(writer_fd).st_dev, os.fstat(writer_fd).st_ino
    try:
        service_root = home / ".openevo" / "core"
        _execute_remote_script(
            core_assets._REMOTE_FINALIZE_SCRIPT,
            [
                str(service_root),
                BUNDLE_ID,
                prepared["transfer_id"],
                WHEEL_NAME,
                assets[1],
                str(assets[0].stat().st_size),
                assets[3],
                str(assets[2].stat().st_size),
            ],
            home=home,
            monkeypatch=monkeypatch,
        )
        capsys.readouterr()
        final_wheel = service_root / "assets" / BUNDLE_ID / WHEEL_NAME
        final_identity = final_wheel.stat().st_dev, final_wheel.stat().st_ino
        assert final_identity != incoming_wheel_identity

        os.lseek(writer_fd, 0, os.SEEK_SET)
        os.write(writer_fd, b"X" * assets[0].stat().st_size)
        os.fsync(writer_fd)
        try:
            late_fd = os.open(
                "late-write",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
                dir_fd=incoming_fd,
            )
        except FileNotFoundError:
            late_fd = -1
        if late_fd >= 0:
            try:
                os.write(late_fd, b"late")
                os.fsync(late_fd)
            finally:
                os.close(late_fd)
    finally:
        os.close(incoming_fd)
        os.close(writer_fd)

    final_root = home / ".openevo" / "core" / "assets" / BUNDLE_ID
    assert {path.name for path in final_root.iterdir()} == {
        WHEEL_NAME,
        "framework-lock.json",
    }
    assert (final_root / WHEEL_NAME).read_bytes() == assets[0].read_bytes()


def test_remote_asset_finalize_rejects_exact_replacement_immediately_after_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    assets = _assets(tmp_path)
    _execute_remote_script(
        core_assets._REMOTE_PREPARE_SCRIPT,
        [BUNDLE_ID],
        home=home,
        monkeypatch=monkeypatch,
    )
    prepared = json.loads(capsys.readouterr().out)
    service_root = home / ".openevo" / "core"
    incoming = Path(prepared["incoming_root"])
    for source, target in (
        (assets[0], incoming / WHEEL_NAME),
        (assets[2], incoming / "framework-lock.json"),
    ):
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)

    final_root = service_root / "assets" / BUNDLE_ID
    displaced = service_root / "assets" / f"{BUNDLE_ID}.displaced"
    replaced = False
    real_fsync = os.fsync

    def replace_after_rename(fd: int) -> None:
        nonlocal replaced
        real_fsync(fd)
        if replaced or not final_root.exists():
            return
        replaced = True
        final_root.rename(displaced)
        final_root.mkdir(mode=0o700)
        for source, target in (
            (assets[0], final_root / WHEEL_NAME),
            (assets[2], final_root / "framework-lock.json"),
        ):
            target.write_bytes(source.read_bytes())
            target.chmod(0o600)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "fsync", replace_after_rename)
        with pytest.raises(SystemExit):
            _execute_remote_script(
                core_assets._REMOTE_FINALIZE_SCRIPT,
                [
                    str(service_root),
                    BUNDLE_ID,
                    prepared["transfer_id"],
                    WHEEL_NAME,
                    assets[1],
                    str(assets[0].stat().st_size),
                    assets[3],
                    str(assets[2].stat().st_size),
                ],
                home=home,
                monkeypatch=scoped,
            )

    assert replaced is True
    assert (final_root / WHEEL_NAME).read_bytes() == assets[0].read_bytes()
    assert (displaced / WHEEL_NAME).read_bytes() == assets[0].read_bytes()


def test_remote_asset_publication_exposes_no_member_writer_during_copy_or_after_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    assets = _assets(tmp_path)
    _execute_remote_script(
        core_assets._REMOTE_PREPARE_SCRIPT,
        [BUNDLE_ID],
        home=home,
        monkeypatch=monkeypatch,
    )
    prepared = json.loads(capsys.readouterr().out)
    service_root = home / ".openevo" / "core"
    incoming = Path(prepared["incoming_root"])
    for source, target in (
        (assets[0], incoming / WHEEL_NAME),
        (assets[2], incoming / "framework-lock.json"),
    ):
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)

    staging = service_root / "asset-staging"
    writer_fd: int | None = None
    attempted_paths: list[Path] = []
    real_fsync = os.fsync

    def try_hold_candidate_writer(fd: int) -> None:
        nonlocal writer_fd
        real_fsync(fd)
        if writer_fd is not None:
            return
        candidates = list(staging.glob(f"publish-{BUNDLE_ID}-*/{WHEEL_NAME}"))
        candidates.extend((service_root / "assets").glob(f"{BUNDLE_ID}/{WHEEL_NAME}"))
        for candidate in candidates:
            if candidate in attempted_paths:
                continue
            attempted_paths.append(candidate)
            if stat_mode(candidate) & 0o222 == 0:
                continue
            try:
                writer_fd = os.open(candidate, os.O_WRONLY | os.O_CLOEXEC)
            except PermissionError:
                continue
            break

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "fsync", try_hold_candidate_writer)
        _execute_remote_script(
            core_assets._REMOTE_FINALIZE_SCRIPT,
            [
                str(service_root),
                BUNDLE_ID,
                prepared["transfer_id"],
                WHEEL_NAME,
                assets[1],
                str(assets[0].stat().st_size),
                assets[3],
                str(assets[2].stat().st_size),
            ],
            home=home,
            monkeypatch=scoped,
        )
    capsys.readouterr()

    try:
        assert attempted_paths
        assert writer_fd is None
        final_wheel = service_root / "assets" / BUNDLE_ID / WHEEL_NAME
        assert final_wheel.read_bytes() == assets[0].read_bytes()
    finally:
        if writer_fd is not None:
            os.lseek(writer_fd, 0, os.SEEK_SET)
            os.write(writer_fd, b"X" * assets[0].stat().st_size)
            os.close(writer_fd)


def test_remote_asset_consumer_rejects_same_name_replacement_after_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    assets = _assets(tmp_path)
    _execute_remote_script(
        core_assets._REMOTE_PREPARE_SCRIPT,
        [BUNDLE_ID],
        home=home,
        monkeypatch=monkeypatch,
    )
    prepared = json.loads(capsys.readouterr().out)
    service_root = home / ".openevo" / "core"
    incoming = Path(prepared["incoming_root"])
    for source, target in (
        (assets[0], incoming / WHEEL_NAME),
        (assets[2], incoming / "framework-lock.json"),
    ):
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
    _execute_remote_script(
        core_assets._REMOTE_FINALIZE_SCRIPT,
        [
            str(service_root),
            BUNDLE_ID,
            prepared["transfer_id"],
            WHEEL_NAME,
            assets[1],
            str(assets[0].stat().st_size),
            assets[3],
            str(assets[2].stat().st_size),
        ],
        home=home,
        monkeypatch=monkeypatch,
    )
    receipt = json.loads(capsys.readouterr().out)
    final_root = service_root / "assets" / BUNDLE_ID
    displaced = service_root / "assets" / f"{BUNDLE_ID}.displaced"
    final_root.rename(displaced)
    final_root.mkdir(mode=0o500)
    for source, target in (
        (assets[0], final_root / WHEEL_NAME),
        (assets[2], final_root / "framework-lock.json"),
    ):
        target.write_bytes(source.read_bytes())
        target.chmod(0o400)

    with pytest.raises(SystemExit):
        _execute_remote_script(
            core_assets._REMOTE_CONSUME_SCRIPT,
            [
                str(service_root),
                BUNDLE_ID,
                WHEEL_NAME,
                assets[1],
                str(assets[0].stat().st_size),
                assets[3],
                str(assets[2].stat().st_size),
                str(receipt["bundle_device"]),
                str(receipt["bundle_inode"]),
                str(receipt["wheel_device"]),
                str(receipt["wheel_inode"]),
                str(receipt["framework_lock_device"]),
                str(receipt["framework_lock_inode"]),
                "printf should-not-run",
            ],
            home=home,
            monkeypatch=monkeypatch,
        )
    assert "should-not-run" not in capsys.readouterr().out


def test_remote_asset_consumer_reads_verified_pinned_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    assets = _assets(tmp_path)
    _execute_remote_script(
        core_assets._REMOTE_PREPARE_SCRIPT,
        [BUNDLE_ID],
        home=home,
        monkeypatch=monkeypatch,
    )
    prepared = json.loads(capsys.readouterr().out)
    service_root = home / ".openevo" / "core"
    incoming = Path(prepared["incoming_root"])
    for source, target in (
        (assets[0], incoming / WHEEL_NAME),
        (assets[2], incoming / "framework-lock.json"),
    ):
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
    _execute_remote_script(
        core_assets._REMOTE_FINALIZE_SCRIPT,
        [
            str(service_root),
            BUNDLE_ID,
            prepared["transfer_id"],
            WHEEL_NAME,
            assets[1],
            str(assets[0].stat().st_size),
            assets[3],
            str(assets[2].stat().st_size),
        ],
        home=home,
        monkeypatch=monkeypatch,
    )
    receipt = json.loads(capsys.readouterr().out)
    final_root = service_root / "assets" / BUNDLE_ID
    consumed = tmp_path / "consumed"
    nested_reader = (
        "import hashlib,pathlib,sys;"
        "assert hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest()==sys.argv[3];"
        "assert hashlib.sha256(pathlib.Path(sys.argv[2]).read_bytes()).hexdigest()==sys.argv[4]"
    )
    nested_launcher = (
        "import subprocess,sys;"
        "completed=subprocess.run([sys.executable,'-I','-c',sys.argv[1],*sys.argv[2:]],"
        "close_fds=True);"
        "raise SystemExit(completed.returncode)"
    )
    command = (
        f"{shlex.quote(sys.executable)} -I -c {shlex.quote(nested_launcher)} "
        f"{shlex.quote(nested_reader)} "
        f"{final_root / WHEEL_NAME} {final_root / 'framework-lock.json'} "
        f"{assets[1]} {assets[3]} "
        f"&& printf consumed > {consumed}"
    )

    with pytest.raises(SystemExit) as exit_info:
        _execute_remote_script(
            core_assets._REMOTE_CONSUME_SCRIPT,
            [
                str(service_root),
                BUNDLE_ID,
                WHEEL_NAME,
                assets[1],
                str(assets[0].stat().st_size),
                assets[3],
                str(assets[2].stat().st_size),
                str(receipt["bundle_device"]),
                str(receipt["bundle_inode"]),
                str(receipt["wheel_device"]),
                str(receipt["wheel_inode"]),
                str(receipt["framework_lock_device"]),
                str(receipt["framework_lock_inode"]),
                command,
            ],
            home=home,
            monkeypatch=monkeypatch,
        )

    assert exit_info.value.code == 0
    assert consumed.read_text(encoding="ascii") == "consumed"


def test_remote_asset_attempts_are_unique_and_reconcile_retired_partial_uploads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _execute_remote_script(
        core_assets._REMOTE_PREPARE_SCRIPT,
        [BUNDLE_ID],
        home=home,
        monkeypatch=monkeypatch,
    )
    prepared = json.loads(capsys.readouterr().out)
    incoming = Path(prepared["incoming_root"])
    (incoming / "partial-wheel").write_bytes(b"x" * (5 * 1024 * 1024))
    (incoming / "partial-lock").write_bytes(b"partial")
    for path in incoming.iterdir():
        path.chmod(0o600)

    _execute_remote_script(
        core_assets._REMOTE_DISCARD_SCRIPT,
        [
            str(home / ".openevo" / "core"),
            BUNDLE_ID,
            prepared["transfer_id"],
        ],
        home=home,
        monkeypatch=monkeypatch,
    )
    assert not incoming.exists()

    staging = home / ".openevo" / "core" / "asset-staging"
    sealed_candidate = staging / f"publish-{BUNDLE_ID}-{SECOND_TRANSFER_ID}"
    sealed_candidate.mkdir(mode=0o700)
    for name in (WHEEL_NAME, "framework-lock.json"):
        path = sealed_candidate / name
        path.write_bytes(b"private crashed candidate")
        path.chmod(0o400)
    sealed_candidate.chmod(0o500)

    _execute_remote_script(
        core_assets._REMOTE_PREPARE_SCRIPT,
        [BUNDLE_ID],
        home=home,
        monkeypatch=monkeypatch,
    )
    retried = json.loads(capsys.readouterr().out)
    assert retried["transfer_id"] != prepared["transfer_id"]
    assert list(staging.iterdir()) == [Path(retried["incoming_root"])]


def test_remote_asset_prepare_bounds_concurrent_incoming_authorities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    transfers: set[str] = set()
    for _index in range(16):
        _execute_remote_script(
            core_assets._REMOTE_PREPARE_SCRIPT,
            [BUNDLE_ID],
            home=home,
            monkeypatch=monkeypatch,
        )
        transfers.add(json.loads(capsys.readouterr().out)["transfer_id"])

    assert len(transfers) == 16
    with pytest.raises(SystemExit):
        _execute_remote_script(
            core_assets._REMOTE_PREPARE_SCRIPT,
            [BUNDLE_ID],
            home=home,
            monkeypatch=monkeypatch,
        )
    staging = home / ".openevo" / "core" / "asset-staging"
    assert len(list(staging.iterdir())) == 16


def test_remote_asset_prepare_recovers_sixteen_proven_stale_incoming_authorities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    incoming: list[Path] = []
    for _index in range(16):
        _execute_remote_script(
            core_assets._REMOTE_PREPARE_SCRIPT,
            [BUNDLE_ID],
            home=home,
            monkeypatch=monkeypatch,
        )
        incoming.append(Path(json.loads(capsys.readouterr().out)["incoming_root"]))

    stale_time = time.time() - 601
    for path in incoming:
        os.utime(path, (stale_time, stale_time), follow_symlinks=False)

    _execute_remote_script(
        core_assets._REMOTE_PREPARE_SCRIPT,
        [BUNDLE_ID],
        home=home,
        monkeypatch=monkeypatch,
    )
    recovered = Path(json.loads(capsys.readouterr().out)["incoming_root"])
    staging = home / ".openevo" / "core" / "asset-staging"
    assert list(staging.iterdir()) == [recovered]
    assert recovered not in incoming


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_remote_asset_prepare_recovers_marker_publication_interruptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interruption: type[BaseException],
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    original_open = os.open
    injections = 0

    def interrupt_marker_publish(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal injections
        if (
            path == ".openevo-transfer.lock"
            and flags & os.O_CREAT
            and flags & os.O_EXCL
            and injections < 16
        ):
            injections += 1
            raise interruption()
        return original_open(path, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "open", interrupt_marker_publish)
        for _index in range(16):
            with pytest.raises(interruption):
                _execute_remote_script(
                    core_assets._REMOTE_PREPARE_SCRIPT,
                    [BUNDLE_ID],
                    home=home,
                    monkeypatch=monkeypatch,
                )
            staging = home / ".openevo" / "core" / "asset-staging"
            assert len(list(staging.iterdir())) <= 1

    _execute_remote_script(
        core_assets._REMOTE_PREPARE_SCRIPT,
        [BUNDLE_ID],
        home=home,
        monkeypatch=monkeypatch,
    )
    recovered = Path(json.loads(capsys.readouterr().out)["incoming_root"])
    staging = home / ".openevo" / "core" / "asset-staging"
    assert injections == 16
    assert list(staging.iterdir()) == [recovered]
    assert (recovered / ".openevo-transfer.lock").is_file()


def test_remote_asset_prepare_rejects_nonempty_markerless_incoming_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _execute_remote_script(
        core_assets._REMOTE_PREPARE_SCRIPT,
        [BUNDLE_ID],
        home=home,
        monkeypatch=monkeypatch,
    )
    prepared = json.loads(capsys.readouterr().out)
    _execute_remote_script(
        core_assets._REMOTE_DISCARD_SCRIPT,
        [prepared["service_root"], BUNDLE_ID, prepared["transfer_id"]],
        home=home,
        monkeypatch=monkeypatch,
    )
    capsys.readouterr()

    staging = home / ".openevo" / "core" / "asset-staging"
    malicious = staging / f"incoming-{BUNDLE_ID}-{'d' * 32}"
    malicious.mkdir(mode=0o700)
    payload = malicious / "unowned-payload"
    payload.write_bytes(b"must not be treated as an interrupted prepare")
    payload.chmod(0o600)

    with pytest.raises(SystemExit):
        _execute_remote_script(
            core_assets._REMOTE_PREPARE_SCRIPT,
            [BUNDLE_ID],
            home=home,
            monkeypatch=monkeypatch,
        )

    assert malicious.is_dir()
    assert payload.read_bytes() == b"must not be treated as an interrupted prepare"


def test_stale_prepare_and_discard_preserve_cross_process_active_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _execute_remote_script(
        core_assets._REMOTE_PREPARE_SCRIPT,
        [BUNDLE_ID],
        home=home,
        monkeypatch=monkeypatch,
    )
    prepared = json.loads(capsys.readouterr().out)
    incoming = Path(prepared["incoming_root"])
    lease_path = incoming / ".openevo-transfer.lock"
    ready_path = tmp_path / "lease-ready"
    active_write_path = incoming / "partial-wheel"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl,os,sys,time;"
                "fd=os.open(sys.argv[1],os.O_RDWR);"
                "fcntl.flock(fd,fcntl.LOCK_SH);"
                "data=os.open(sys.argv[3],os.O_WRONLY|os.O_CREAT,0o600);"
                "open(sys.argv[2],'w').close();"
                "[(os.lseek(data,0,0),os.write(data,b'active'),os.fsync(data),time.sleep(.01)) "
                "for _ in range(3000)]"
            ),
            str(lease_path),
            str(ready_path),
            str(active_write_path),
        ]
    )
    try:
        deadline = time.monotonic() + 3
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready_path.exists()
        stale_time = time.time() - 601
        os.utime(incoming, (stale_time, stale_time), follow_symlinks=False)

        _execute_remote_script(
            core_assets._REMOTE_PREPARE_SCRIPT,
            [BUNDLE_ID],
            home=home,
            monkeypatch=monkeypatch,
        )
        capsys.readouterr()
        assert incoming.is_dir()

        with pytest.raises(SystemExit):
            _execute_remote_script(
                core_assets._REMOTE_DISCARD_SCRIPT,
                [
                    str(home / ".openevo" / "core"),
                    BUNDLE_ID,
                    prepared["transfer_id"],
                ],
                home=home,
                monkeypatch=monkeypatch,
            )
        assert incoming.is_dir()
    finally:
        holder.terminate()
        holder.wait(timeout=3)

    stale_time = time.time() - 601
    os.utime(incoming, (stale_time, stale_time), follow_symlinks=False)
    _execute_remote_script(
        core_assets._REMOTE_PREPARE_SCRIPT,
        [BUNDLE_ID],
        home=home,
        monkeypatch=monkeypatch,
    )
    capsys.readouterr()
    assert not incoming.exists()


def test_remote_rsync_wrapper_holds_transfer_lease_through_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class ExecObserved(BaseException):
        pass

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _execute_remote_script(
        core_assets._REMOTE_PREPARE_SCRIPT,
        [BUNDLE_ID],
        home=home,
        monkeypatch=monkeypatch,
    )
    prepared = json.loads(capsys.readouterr().out)
    lease_path = Path(prepared["incoming_root"]) / ".openevo-transfer.lock"
    namespace: dict[str, object] = {"__name__": "__main__"}

    def observe_exec(executable: str, arguments: list[str]) -> None:
        assert executable == "rsync"
        assert arguments == ["rsync", "--server"]
        contender_fd = os.open(lease_path, os.O_RDWR | os.O_CLOEXEC)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(contender_fd)
        raise ExecObserved

    with monkeypatch.context() as scoped:
        scoped.setattr(pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir=str(home)))
        scoped.setattr(
            sys,
            "argv",
            [
                "remote-rsync-lease",
                prepared["service_root"],
                BUNDLE_ID,
                prepared["transfer_id"],
                "rsync",
                "--server",
            ],
        )
        scoped.setattr(os, "execvp", observe_exec)
        with pytest.raises(ExecObserved):
            exec(
                compile(
                    core_assets._REMOTE_RSYNC_LEASE_SCRIPT,
                    "<core-asset-rsync-lease>",
                    "exec",
                ),
                namespace,
            )

    lease_fd = namespace.get("lease_fd")
    assert isinstance(lease_fd, int)
    os.close(lease_fd)


def stat_mode(path: Path) -> int:
    return os.stat(path, follow_symlinks=False).st_mode & 0o777


def test_remote_asset_finalize_rejects_tampered_upload_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    assets = _assets(tmp_path)
    _execute_remote_script(
        core_assets._REMOTE_PREPARE_SCRIPT,
        [BUNDLE_ID],
        home=home,
        monkeypatch=monkeypatch,
    )
    prepared = json.loads(capsys.readouterr().out)
    service_root = home / ".openevo" / "core"
    incoming = Path(prepared["incoming_root"])
    (incoming / WHEEL_NAME).write_bytes(b"tampered wheel byt")
    (incoming / "framework-lock.json").write_bytes(assets[2].read_bytes())
    (incoming / WHEEL_NAME).chmod(0o600)
    (incoming / "framework-lock.json").chmod(0o600)

    with pytest.raises(SystemExit):
        _execute_remote_script(
            core_assets._REMOTE_FINALIZE_SCRIPT,
            [
                str(service_root),
                BUNDLE_ID,
                prepared["transfer_id"],
                WHEEL_NAME,
                assets[1],
                str(assets[0].stat().st_size),
                assets[3],
                str(assets[2].stat().st_size),
            ],
            home=home,
            monkeypatch=monkeypatch,
        )

    assert not (service_root / "assets" / BUNDLE_ID).exists()


def test_stage_core_assets_prepares_fresh_host_uploads_private_snapshot_and_finalizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = _assets(tmp_path)
    runner = RecordingRunner()
    transport = _transport(tmp_path, runner)
    secret_calls = _install_secret_responses(
        monkeypatch,
        transport,
        _stage_responses(assets[1], assets[3]),
    )

    staged = _stage(transport, assets)

    assert staged.service_root == "/home/alice/.openevo/core"
    assert staged.wheel_sha256 == assets[1]
    assert staged.framework_lock_sha256 == assets[3]
    assert staged.wheel_size == assets[0].stat().st_size
    assert staged.framework_lock_size == assets[2].stat().st_size
    assert staged.bundle_inode == 12
    assert staged.wheel_inode == 14
    assert staged.framework_lock_inode == 16
    consumer_command = f"consume {staged.wheel_path} {staged.framework_lock_path}"
    bound_command = transport._bind_core_asset_consumption(consumer_command)
    assert bound_command.startswith("python3 -I -c ")
    assert shlex.quote(consumer_command) in bound_command
    assert bound_command != consumer_command
    assert len(secret_calls) == 2
    assert all(0 < timeout <= 20 for _command, timeout in secret_calls)
    assert len(runner.calls) == 1
    argv, timeout = runner.calls[0]
    assert argv[0] == "rsync"
    assert "--chmod=F600,D700" in argv
    assert "--filter=protect /.openevo-transfer.lock" in argv
    rsync_path_index = argv.index("--rsync-path")
    assert "python3 -I -c" in argv[rsync_path_index + 1]
    assert "rsync" in argv[rsync_path_index + 1]
    assert "--no-perms" not in argv
    assert "--links" not in argv
    assert timeout <= 20
    assert str(tmp_path) not in repr(staged)


def test_core_supervisor_runtime_preflight_rejects_missing_pidfd_wrappers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = RecordingRunner()
    transport = _transport(tmp_path, runner)
    commands: list[tuple[str, float]] = []

    def unsupported(
        command: str,
        *,
        timeout_seconds: float,
        remote_failure_code: SshTransportErrorCode,
    ) -> SecretStr:
        assert remote_failure_code is SshTransportErrorCode.CORE_RUNTIME_PREFLIGHT_FAILED
        commands.append((command, timeout_seconds))
        return SecretStr(
            json.dumps(
                {
                    "schema_version": 1,
                    "supported": False,
                    "reason": "missing_python_pidfd_api",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )

    monkeypatch.setattr(transport, "_run_secret_with_remote_failure", unsupported)

    with pytest.raises(SshTransportError) as error:
        transport.require_core_supervisor_runtime(timeout_seconds=300)

    assert error.value.code is SshTransportErrorCode.CORE_RUNTIME_UNSUPPORTED
    assert len(commands) == 1
    assert commands[0][1] == 300
    assert "pidfd_open" in commands[0][0]
    assert "pidfd_send_signal" in commands[0][0]
    assert str(tmp_path) not in str(error.value)


def test_secret_runner_maps_authenticated_remote_failure_without_output_leak(
    tmp_path: Path,
) -> None:
    runner = RemoteFailureRunner()
    transport = _transport(tmp_path, runner)

    with pytest.raises(SshTransportError) as error:
        transport._run_secret_with_remote_failure(
            "python3 -I -c private-command",
            timeout_seconds=10,
            remote_failure_code=SshTransportErrorCode.CORE_ASSET_FAILED,
        )

    assert error.value.code is SshTransportErrorCode.CORE_ASSET_FAILED
    assert "private-command" not in str(error.value)
    assert "private remote path" not in str(error.value)
    assert "private remote output" not in str(error.value)


def test_core_supervisor_runtime_script_reports_missing_python_wrappers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with monkeypatch.context() as scoped:
        scoped.setattr(sys, "platform", "linux")
        scoped.setattr(sys, "version_info", (3, 12, 0))
        scoped.delattr(os, "pidfd_open", raising=False)
        scoped.delattr(signal, "pidfd_send_signal", raising=False)
        exec(
            compile(
                core_runtime._REMOTE_PREFLIGHT_SCRIPT,
                "<core-runtime-preflight>",
                "exec",
            ),
            {"__name__": "__main__"},
        )

    reason = core_runtime.parse_core_supervisor_runtime_preflight(
        SecretStr(capsys.readouterr().out)
    )
    assert reason == "missing_python_pidfd_api"


def test_core_supervisor_runtime_script_rejects_non_linux_remote_host(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with monkeypatch.context() as scoped:
        scoped.setattr(sys, "platform", "darwin")
        scoped.setattr(sys, "version_info", (3, 11, 0))
        exec(
            compile(
                core_runtime._REMOTE_PREFLIGHT_SCRIPT,
                "<core-runtime-preflight>",
                "exec",
            ),
            {"__name__": "__main__"},
        )

    reason = core_runtime.parse_core_supervisor_runtime_preflight(
        SecretStr(capsys.readouterr().out)
    )
    assert reason == "unsupported_platform"


def test_core_staging_closed_payloads_reject_boolean_numeric_aliases() -> None:
    root = "/home/alice/.openevo/core"
    with pytest.raises(ValueError):
        core_assets.parse_core_asset_prepare(
            SecretStr(
                json.dumps(
                    {
                        "schema_version": True,
                        "service_root": root,
                        "incoming_root": (
                            f"{root}/asset-staging/incoming-{BUNDLE_ID}-{TRANSFER_ID}"
                        ),
                        "transfer_id": TRANSFER_ID,
                    }
                )
            ),
            bundle_id=BUNDLE_ID,
        )
    with pytest.raises(ValueError):
        core_runtime.parse_core_supervisor_runtime_preflight(
            SecretStr(
                json.dumps(
                    {
                        "schema_version": True,
                        "supported": False,
                        "reason": "missing_python_pidfd_api",
                    }
                )
            )
        )
    with pytest.raises(ValueError):
        core_assets.build_core_asset_finalize_command(
            service_root=root,
            bundle_id=BUNDLE_ID,
            transfer_id=TRANSFER_ID,
            wheel_filename=WHEEL_NAME,
            wheel_sha256="a" * 64,
            wheel_size=True,
            framework_lock_sha256="b" * 64,
            framework_lock_size=1,
        )


def test_stage_core_assets_rejects_symlink_and_digest_tamper_before_remote_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = _assets(tmp_path)
    runner = RecordingRunner()
    transport = _transport(tmp_path, runner)
    calls = _install_secret_responses(monkeypatch, transport, [])
    symlink = tmp_path / "wheel-link.whl"
    symlink.symlink_to(assets[0])

    with pytest.raises(SshTransportError) as symlink_error:
        transport.stage_core_bootstrap_assets(
            wheel_path=str(symlink),
            wheel_sha256=assets[1],
            wheel_size=assets[0].stat().st_size,
            framework_lock_path=str(assets[2]),
            framework_lock_sha256=assets[3],
            framework_lock_size=assets[2].stat().st_size,
            bundle_id=BUNDLE_ID,
            timeout_seconds=20,
        )
    assert symlink_error.value.code is SshTransportErrorCode.INVALID_REQUEST

    assets[0].write_bytes(b"tampered")
    with pytest.raises(SshTransportError) as digest_error:
        _stage(transport, assets)
    assert digest_error.value.code is SshTransportErrorCode.INVALID_REQUEST
    assert calls == []
    assert runner.calls == []


def test_stage_core_assets_rejects_symlinked_parent_before_remote_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    assets = _assets(real_root)
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)
    runner = RecordingRunner()
    transport = _transport(tmp_path, runner)
    calls = _install_secret_responses(monkeypatch, transport, [])

    with pytest.raises(SshTransportError) as error:
        transport.stage_core_bootstrap_assets(
            wheel_path=str(linked_root / WHEEL_NAME),
            wheel_sha256=assets[1],
            wheel_size=assets[0].stat().st_size,
            framework_lock_path=str(linked_root / "framework-lock.json"),
            framework_lock_sha256=assets[3],
            framework_lock_size=assets[2].stat().st_size,
            bundle_id=BUNDLE_ID,
            timeout_seconds=20,
        )

    assert error.value.code is SshTransportErrorCode.INVALID_REQUEST
    assert calls == []
    assert runner.calls == []


def test_stage_core_assets_applies_one_deadline_across_prepare_and_upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = _assets(tmp_path)
    runner = RecordingRunner()
    transport = _transport(tmp_path, runner)
    prepare = _stage_responses(assets[1], assets[3])[0]

    def delayed_prepare(
        _command: str,
        *,
        timeout_seconds: float,
        remote_failure_code: SshTransportErrorCode,
    ) -> SecretStr:
        assert remote_failure_code is SshTransportErrorCode.CORE_ASSET_FAILED
        assert timeout_seconds <= 0.05
        time.sleep(0.06)
        return prepare

    monkeypatch.setattr(transport, "_run_secret_with_remote_failure", delayed_prepare)

    with pytest.raises(SshTransportError) as error:
        _stage(transport, assets, timeout_seconds=0.05)

    assert error.value.code is SshTransportErrorCode.TIMEOUT
    assert runner.calls == []


def test_stage_core_assets_partial_upload_retry_converges_to_same_remote_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = _assets(tmp_path)
    runner = RecordingRunner([23, 0])
    transport = _transport(tmp_path, runner)
    first = _stage_responses(assets[1], assets[3], transfer_id=TRANSFER_ID)
    second = _stage_responses(
        assets[1],
        assets[3],
        transfer_id=SECOND_TRANSFER_ID,
    )
    responses = [first[0], SecretStr(""), second[0], second[1]]
    _install_secret_responses(monkeypatch, transport, responses)

    with pytest.raises(SshTransportError) as partial:
        _stage(transport, assets)
    assert partial.value.code is SshTransportErrorCode.RSYNC_FAILED

    staged = _stage(transport, assets)
    assert staged.wheel_path.endswith(f"/{BUNDLE_ID}/{WHEEL_NAME}")
    assert len(runner.calls) == 2


def test_stage_core_assets_retries_failed_discard_with_independent_cleanup_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = _assets(tmp_path)
    runner = RecordingRunner([23, 0])
    transport = _transport(tmp_path, runner)
    first = _stage_responses(assets[1], assets[3], transfer_id=TRANSFER_ID)
    second = _stage_responses(assets[1], assets[3], transfer_id=SECOND_TRANSFER_ID)
    calls: list[tuple[str, float]] = []

    def run_secret(
        command: str,
        *,
        timeout_seconds: float,
        remote_failure_code: SshTransportErrorCode,
    ) -> SecretStr:
        del remote_failure_code
        calls.append((command, timeout_seconds))
        if len(calls) == 1:
            return first[0]
        if len(calls) == 2:
            raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
        if len(calls) == 3:
            return SecretStr("")
        if len(calls) == 4:
            return second[0]
        return second[1]

    monkeypatch.setattr(transport, "_run_secret_with_remote_failure", run_secret)

    with pytest.raises(SshTransportError) as first_error:
        _stage(transport, assets)
    assert first_error.value.code is SshTransportErrorCode.RSYNC_FAILED
    assert len(transport._core_asset_cleanup_authorities) == 1
    assert 0 < calls[1][1] <= 10

    staged = _stage(transport, assets)
    assert staged.wheel_path.endswith(f"/{BUNDLE_ID}/{WHEEL_NAME}")
    assert transport._core_asset_cleanup_authorities == {}
    assert 0 < calls[2][1] <= 10


@pytest.mark.parametrize("initial_failure", ["timeout", "parse", "remote"])
def test_stage_core_assets_reconciles_finalize_before_discard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initial_failure: str,
) -> None:
    assets = _assets(tmp_path)
    transport = _transport(tmp_path, RecordingRunner())
    prepared, receipt = _stage_responses(assets[1], assets[3])
    calls: list[tuple[str, float]] = []

    def run_secret(
        command: str,
        *,
        timeout_seconds: float,
        remote_failure_code: SshTransportErrorCode,
    ) -> SecretStr:
        del remote_failure_code
        calls.append((command, timeout_seconds))
        if len(calls) == 1:
            return prepared
        if len(calls) == 2 and initial_failure == "timeout":
            raise SshTransportError(SshTransportErrorCode.TIMEOUT)
        if len(calls) == 2 and initial_failure == "parse":
            return SecretStr('{"schema_version":1}')
        if len(calls) in {2, 3} and initial_failure == "remote":
            raise SshTransportError(SshTransportErrorCode.CORE_ASSET_FAILED)
        if len(calls) == 4 and initial_failure == "remote":
            return SecretStr("")
        return receipt

    monkeypatch.setattr(transport, "_run_secret_with_remote_failure", run_secret)

    if initial_failure == "remote":
        with pytest.raises(SshTransportError) as error:
            _stage(transport, assets)
        assert error.value.code is SshTransportErrorCode.CORE_ASSET_FAILED
        assert len(calls) == 4
    else:
        staged = _stage(transport, assets)
        assert staged.bundle_inode == 12
        assert len(calls) == 3
    assert transport._core_asset_cleanup_authorities == {}
    assert 0 < calls[2][1] <= 10


def test_stage_core_assets_retains_unknown_finalize_and_preserves_exact_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = _assets(tmp_path)
    transport = _transport(tmp_path, RecordingRunner())
    first = _stage_responses(assets[1], assets[3], transfer_id=TRANSFER_ID)
    second = _stage_responses(assets[1], assets[3], transfer_id=SECOND_TRANSFER_ID)
    calls: list[str] = []

    def run_secret(
        command: str,
        *,
        timeout_seconds: float,
        remote_failure_code: SshTransportErrorCode,
    ) -> SecretStr:
        del timeout_seconds, remote_failure_code
        calls.append(command)
        if len(calls) == 1:
            return first[0]
        if len(calls) in {2, 3}:
            raise SshTransportError(SshTransportErrorCode.TIMEOUT)
        if len(calls) == 4:
            return first[1]
        if len(calls) == 5:
            return second[0]
        return second[1]

    monkeypatch.setattr(transport, "_run_secret_with_remote_failure", run_secret)

    with pytest.raises(SshTransportError) as unknown:
        _stage(transport, assets)
    assert unknown.value.code is SshTransportErrorCode.TIMEOUT
    assert len(transport._core_asset_cleanup_authorities) == 1

    staged = _stage(transport, assets)
    assert staged.bundle_inode == 12
    assert transport._core_asset_cleanup_authorities == {}
    assert len(calls) == 6
    assert all(shlex.quote(core_assets._REMOTE_DISCARD_SCRIPT) not in command for command in calls)


def test_malformed_finalize_receipts_hit_cleanup_authority_backpressure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = _assets(tmp_path)
    transport = _transport(tmp_path, RecordingRunner())
    prepare_command = core_assets.build_core_asset_prepare_command(BUNDLE_ID)
    prepare_calls = 0

    def malformed_receipts(
        command: str,
        *,
        timeout_seconds: float,
        remote_failure_code: SshTransportErrorCode,
    ) -> SecretStr:
        nonlocal prepare_calls
        del timeout_seconds, remote_failure_code
        if command == prepare_command:
            transfer_id = f"{prepare_calls + 1:032x}"
            prepare_calls += 1
            return _stage_responses(
                assets[1],
                assets[3],
                transfer_id=transfer_id,
            )[0]
        return SecretStr("{}")

    monkeypatch.setattr(ssh_module, "_MAX_CORE_ASSET_CLEANUP_AUTHORITIES", 2)
    monkeypatch.setattr(
        transport,
        "_run_secret_with_remote_failure",
        malformed_receipts,
    )

    for _index in range(2):
        with pytest.raises(SshTransportError) as error:
            _stage(transport, assets)
        assert error.value.code is SshTransportErrorCode.CORE_ASSET_FAILED

    with pytest.raises(SshTransportError) as backpressure:
        _stage(transport, assets)

    assert backpressure.value.code is SshTransportErrorCode.CORE_ASSET_FAILED
    assert prepare_calls == 2
    assert len(transport._core_asset_cleanup_authorities) == 2
    assert transport._core_asset_pending_admissions == 0


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("handoff_timing", ["before_update", "after_update"])
def test_prepare_authority_handoff_interruptions_remain_bounded_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
    handoff_timing: str,
) -> None:
    assets = _assets(tmp_path)
    runner = RecordingRunner()
    transport = _transport(tmp_path, runner)
    prepare_command = core_assets.build_core_asset_prepare_command(BUNDLE_ID)
    original_remember = transport._remember_core_asset_transfer
    prepare_calls = 0
    interrupted_handoffs = 0

    def remote_response(
        command: str,
        *,
        timeout_seconds: float,
        remote_failure_code: SshTransportErrorCode,
    ) -> SecretStr:
        nonlocal prepare_calls
        del timeout_seconds, remote_failure_code
        if command == prepare_command:
            prepare_calls += 1
            return _stage_responses(
                assets[1],
                assets[3],
                transfer_id=f"{prepare_calls:032x}",
            )[0]
        if shlex.quote(core_assets._REMOTE_DISCARD_SCRIPT) in command:
            return SecretStr("")
        if shlex.quote(core_assets._REMOTE_FINALIZE_SCRIPT) in command:
            return _stage_responses(assets[1], assets[3])[1]
        raise AssertionError("unexpected core asset command")

    def interrupt_prepare_handoff(
        authority: ssh_module._CoreAssetTransferAuthority,
        *,
        active: bool = False,
        consume_admission: bool = False,
    ) -> None:
        nonlocal interrupted_handoffs
        if not authority.finalize_started and consume_admission and interrupted_handoffs < 16:
            if handoff_timing == "after_update":
                original_remember(
                    authority,
                    active=active,
                    consume_admission=consume_admission,
                )
            interrupted_handoffs += 1
            raise interruption
        original_remember(
            authority,
            active=active,
            consume_admission=consume_admission,
        )

    monkeypatch.setattr(transport, "_run_secret_with_remote_failure", remote_response)
    monkeypatch.setattr(
        transport,
        "_remember_core_asset_transfer",
        interrupt_prepare_handoff,
    )

    for _index in range(16):
        with pytest.raises(interruption):
            _stage(transport, assets)
        assert transport._core_asset_active_transfers == set()
        assert len(transport._core_asset_cleanup_authorities) <= 1
        assert transport._core_asset_pending_admissions == 0

    staged = _stage(transport, assets)

    assert staged.bundle_inode == 12
    assert interrupted_handoffs == 16
    assert prepare_calls == 17
    assert len(runner.calls) == 1
    assert transport._core_asset_active_transfers == set()
    assert transport._core_asset_cleanup_authorities == {}
    assert transport._core_asset_pending_admissions == 0


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("handoff_timing", ["before_update", "after_update"])
def test_finalize_authority_handoff_interruptions_remain_bounded_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
    handoff_timing: str,
) -> None:
    assets = _assets(tmp_path)
    runner = RecordingRunner()
    transport = _transport(tmp_path, runner)
    prepare_command = core_assets.build_core_asset_prepare_command(BUNDLE_ID)
    original_remember = transport._remember_core_asset_transfer
    prepare_calls = 0
    interrupted_handoffs = 0

    def remote_response(
        command: str,
        *,
        timeout_seconds: float,
        remote_failure_code: SshTransportErrorCode,
    ) -> SecretStr:
        nonlocal prepare_calls
        del timeout_seconds, remote_failure_code
        if command == prepare_command:
            prepare_calls += 1
            return _stage_responses(
                assets[1],
                assets[3],
                transfer_id=f"{prepare_calls:032x}",
            )[0]
        if shlex.quote(core_assets._REMOTE_DISCARD_SCRIPT) in command:
            return SecretStr("")
        if shlex.quote(core_assets._REMOTE_FINALIZE_SCRIPT) in command:
            return _stage_responses(assets[1], assets[3])[1]
        raise AssertionError("unexpected core asset command")

    def interrupt_finalize_handoff(
        authority: ssh_module._CoreAssetTransferAuthority,
        *,
        active: bool = False,
        consume_admission: bool = False,
    ) -> None:
        nonlocal interrupted_handoffs
        if authority.finalize_started and interrupted_handoffs < 16:
            if handoff_timing == "after_update":
                original_remember(
                    authority,
                    active=active,
                    consume_admission=consume_admission,
                )
            interrupted_handoffs += 1
            raise interruption
        original_remember(
            authority,
            active=active,
            consume_admission=consume_admission,
        )

    monkeypatch.setattr(transport, "_run_secret_with_remote_failure", remote_response)
    monkeypatch.setattr(
        transport,
        "_remember_core_asset_transfer",
        interrupt_finalize_handoff,
    )

    for _index in range(16):
        with pytest.raises(interruption):
            _stage(transport, assets)
        assert transport._core_asset_active_transfers == set()
        assert len(transport._core_asset_cleanup_authorities) <= 1
        assert transport._core_asset_pending_admissions == 0

    staged = _stage(transport, assets)

    assert staged.bundle_inode == 12
    assert interrupted_handoffs == 16
    assert prepare_calls == 17
    assert len(runner.calls) == 17
    assert transport._core_asset_active_transfers == set()
    assert transport._core_asset_cleanup_authorities == {}
    assert transport._core_asset_pending_admissions == 0


def test_concurrent_cleanup_skips_an_owned_finalize_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = _assets(tmp_path)
    transport = _transport(tmp_path, RecordingRunner())
    prepared, receipt = _stage_responses(assets[1], assets[3])
    prepare_command = core_assets.build_core_asset_prepare_command(BUNDLE_ID)
    finalize_started = threading.Event()
    allow_finalize = threading.Event()
    discard_calls: list[str] = []

    def remote_response(
        command: str,
        *,
        timeout_seconds: float,
        remote_failure_code: SshTransportErrorCode,
    ) -> SecretStr:
        del timeout_seconds, remote_failure_code
        if command == prepare_command:
            return prepared
        if shlex.quote(core_assets._REMOTE_FINALIZE_SCRIPT) in command:
            finalize_started.set()
            assert allow_finalize.wait(timeout=3)
            return receipt
        if shlex.quote(core_assets._REMOTE_DISCARD_SCRIPT) in command:
            discard_calls.append(command)
            return SecretStr("")
        raise AssertionError("unexpected core asset command")

    monkeypatch.setattr(transport, "_run_secret_with_remote_failure", remote_response)
    staged: list[StagedCoreBootstrapAssets] = []
    failures: list[BaseException] = []

    def stage() -> None:
        try:
            staged.append(_stage(transport, assets))
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=stage)
    worker.start()
    try:
        assert finalize_started.wait(timeout=3)
        assert len(transport._core_asset_active_transfers) == 1

        transport._retry_core_asset_transfer_cleanup(deadline=time.monotonic() + 1)

        assert discard_calls == []
        assert len(transport._core_asset_cleanup_authorities) == 1
    finally:
        allow_finalize.set()
        worker.join(timeout=3)

    assert not worker.is_alive()
    assert failures == []
    assert len(staged) == 1
    assert transport._core_asset_active_transfers == set()
    assert transport._core_asset_cleanup_authorities == {}


def test_stage_core_assets_rejects_tampered_finalize_without_leaking_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assets = _assets(tmp_path)
    runner = RecordingRunner()
    transport = _transport(tmp_path, runner)
    responses = _stage_responses(assets[1], assets[3])
    tampered = json.loads(responses[1].get_secret_value())
    tampered["wheel_sha256"] = "f" * 64
    _install_secret_responses(
        monkeypatch,
        transport,
        [responses[0], SecretStr(json.dumps(tampered))],
    )

    with pytest.raises(SshTransportError) as error:
        _stage(transport, assets)
    assert error.value.code is SshTransportErrorCode.CORE_ASSET_FAILED
    assert str(tmp_path) not in str(error.value)
    assert "/home/alice" not in str(error.value)
