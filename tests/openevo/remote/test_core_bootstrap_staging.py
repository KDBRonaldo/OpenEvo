from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import signal
import struct
import subprocess
import sys
import time
from types import SimpleNamespace

from pydantic import SecretStr
import pytest

from openevo.deployment.host_keys import ProviderKnownHostStore
from openevo.deployment import core_assets, core_runtime
from openevo.deployment.profile import RemoteProfileConfig
from openevo.deployment.ssh import (
    SshRemoteExecutorTransport,
    SshTransportError,
    SshTransportErrorCode,
    StagedCoreBootstrapAssets,
)


WHEEL_NAME = "openevo-0.1.0-py3-none-any.whl"
BUNDLE_ID = "a" * 64


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
) -> list[SecretStr]:
    root = "/home/alice/.openevo/core"
    incoming = f"{root}/asset-staging/incoming-{BUNDLE_ID}"
    final = f"{root}/assets/{BUNDLE_ID}"
    return [
        SecretStr(
            json.dumps(
                {
                    "schema_version": 1,
                    "service_root": root,
                    "incoming_root": incoming,
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
    incoming = service_root / "asset-staging" / f"incoming-{BUNDLE_ID}"
    assert prepared == {
        "schema_version": 1,
        "service_root": str(service_root),
        "incoming_root": str(incoming),
    }
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
    assert stat_mode(final_root) == 0o700
    assert stat_mode(final_root / WHEEL_NAME) == 0o600

    _execute_remote_script(
        core_assets._REMOTE_PREPARE_SCRIPT,
        [BUNDLE_ID],
        home=home,
        monkeypatch=monkeypatch,
    )
    capsys.readouterr()
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
    assert (final_root / WHEEL_NAME).read_bytes() == assets[0].read_bytes()


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
    capsys.readouterr()
    service_root = home / ".openevo" / "core"
    incoming = service_root / "asset-staging" / f"incoming-{BUNDLE_ID}"
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
    assert len(secret_calls) == 2
    assert all(0 < timeout <= 20 for _command, timeout in secret_calls)
    assert len(runner.calls) == 1
    argv, timeout = runner.calls[0]
    assert argv[0] == "rsync"
    assert "--chmod=F600,D700" in argv
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


def test_core_staging_closed_payloads_reject_boolean_numeric_aliases() -> None:
    root = "/home/alice/.openevo/core"
    with pytest.raises(ValueError):
        core_assets.parse_core_asset_prepare(
            SecretStr(
                json.dumps(
                    {
                        "schema_version": True,
                        "service_root": root,
                        "incoming_root": (f"{root}/asset-staging/incoming-{BUNDLE_ID}"),
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
    responses = _stage_responses(assets[1], assets[3])
    responses = [responses[0], responses[0], responses[1]]
    _install_secret_responses(monkeypatch, transport, responses)

    with pytest.raises(SshTransportError) as partial:
        _stage(transport, assets)
    assert partial.value.code is SshTransportErrorCode.RSYNC_FAILED

    staged = _stage(transport, assets)
    assert staged.wheel_path.endswith(f"/{BUNDLE_ID}/{WHEEL_NAME}")
    assert len(runner.calls) == 2


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
