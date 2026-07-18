from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import threading
import time
from typing import Callable

import httpx
from pydantic import SecretStr
import pytest

from desktop.sidecar import core_bridge_adapters_v1 as adapters
from desktop.sidecar.contracts.v1.models import WorkspaceImportRefV1
from desktop.sidecar.core_bridge_adapters_v1 import (
    AdoptedWorkspaceArchiveSourceV1,
    AdoptedWorkspaceImportV1,
    CoreBootstrapConfigV1,
    DesktopCoreSshBridgeAdapterV1,
    SealedCoreBootstrapAssetV1,
    SealedManagedRuntimeArchiveV1,
    VerifiedCoreHttpTransportV1,
)
from desktop.sidecar.core_bridge_v1 import DesktopCoreBridgeErrorV1
from desktop.sidecar.remote_lifecycle import (
    DesktopRemoteLifecycle,
    RemoteConnectionFailedError,
)
from desktop.sidecar.workspace_imports import (
    WorkspaceImportOwnership,
    WorkspaceImportStore,
)
from openevo.deployment import core_control
from openevo.deployment.core_control import (
    CoreControlBootstrapError,
    CoreControlBootstrapErrorCode,
)
from openevo.deployment.core_assets import (
    CoreBootstrapAssetSnapshotError,
    snapshot_core_bootstrap_assets,
)
from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.core_runtime import CorePythonRuntimeAuthority
from openevo.deployment.ssh import (
    SshTransportError,
    SshTransportErrorCode,
    StagedCoreBootstrapAssets,
)
from openevo.runtime.managed import MANAGED_RUNTIME_ARCHIVE_RELEASE


PROFILE_ID = "profile-a"
SOURCE_COMMIT = "1" * 40
BEARER = "B" * 64
RELEASE_IDENTITY = "2" * 64
REGISTRY_DIGEST = "3" * 64
STATUS_PROOF = "4" * 64
GENERATION = "5" * 32
ARCHIVE = bytes(1024)
IMPORT_ID = "workspace-import-" + "6" * 48


def _bootstrap_config(tmp_path: Path) -> CoreBootstrapConfigV1:
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"sealed-wheel")
    wheel_digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    framework_lock = tmp_path / "framework-lock.json"
    framework_lock.write_text(
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
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE
    runtime_archive = tmp_path / release.filename
    runtime_archive.touch()
    os.truncate(runtime_archive, release.byte_size)
    runtime_archive.chmod(0o600)
    return CoreBootstrapConfigV1(
        source_commit=SOURCE_COMMIT,
        wheel=SealedCoreBootstrapAssetV1(
            local_path=str(wheel),
            sha256=wheel_digest,
            byte_size=wheel.stat().st_size,
        ),
        framework_lock=SealedCoreBootstrapAssetV1(
            local_path=str(framework_lock),
            sha256=hashlib.sha256(framework_lock.read_bytes()).hexdigest(),
            byte_size=framework_lock.stat().st_size,
        ),
        managed_runtime_archive=SealedManagedRuntimeArchiveV1(
            local_path=str(runtime_archive),
            sha256=release.sha256,
            byte_size=release.byte_size,
            platform=release.platform,
            config_id=release.config_id,
            oci_index_id=release.oci_index_id,
        ),
    )


def _attachment_payload(*, bearer: str = BEARER, port: int = 43117) -> SecretStr:
    return SecretStr(
        json.dumps(
            {
                "schema_version": 1,
                "host": "127.0.0.1",
                "port": port,
                "release_identity": RELEASE_IDENTITY,
                "registry_digest": REGISTRY_DIGEST,
                "source_commit": SOURCE_COMMIT,
                "generation": GENERATION,
                "status_proof": STATUS_PROOF,
                "attached": False,
                "bearer_token": bearer,
                "execution_mode": "subscription",
                "capture_mode": "transcript",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _runtime() -> CorePythonRuntimeAuthority:
    values: dict[str, object] = {
        "schema_version": 1,
        "executable_path": "/home/alice/.local/share/uv/python/python3.11",
        "executable_sha256": "a" * 64,
        "device": 1,
        "inode": 10,
        "uid": 1000,
        "mode": 0o755,
        "byte_size": 4096,
        "mtime_ns": 11,
        "ctime_ns": 12,
        "version": [3, 11, 12],
    }
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    authority_id = hashlib.sha256(b"openevo-core-python-runtime-v1\0" + canonical).hexdigest()
    return CorePythonRuntimeAuthority(
        authority_id=authority_id,
        executable_path=str(values["executable_path"]),
        executable_sha256=str(values["executable_sha256"]),
        device=1,
        inode=10,
        uid=1000,
        mode=0o755,
        byte_size=4096,
        mtime_ns=11,
        ctime_ns=12,
        version=(3, 11, 12),
    )


class FakeCoreTunnel:
    base_url = "http://openevo-core.local"

    def __init__(self, *, remote_port: int, block_close: bool = False) -> None:
        self.remote_port = remote_port
        self.close_calls = 0
        self.authority_checks = 0
        self.close_entered = threading.Event()
        self.close_release = threading.Event()
        self.block_close = block_close

    def verify_authority(self) -> None:
        self.authority_checks += 1

    def open_verified_socket(self, *, timeout_seconds: float) -> object:
        del timeout_seconds
        raise AssertionError("authentication is replaced by the focused adapter test")

    def close(self) -> None:
        self.close_calls += 1
        self.close_entered.set()
        if self.block_close:
            assert self.close_release.wait(timeout=2)


class FakeCoreTransport:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.secret_commands: list[str] = []
        self.timeouts: list[float] = []
        self.tunnel_kwargs: dict[str, object] | None = None
        self.tunnel = FakeCoreTunnel(remote_port=43117)
        self.run_error: Exception | None = None
        self.tunnel_error: Exception | None = None
        self.stage_error: Exception | None = None
        self.stage_calls: list[dict[str, object]] = []
        self.runtime_preflight_error: Exception | None = None
        self.runtime_preflight_timeouts: list[float] = []
        self.after_runtime_preflight: Callable[[], None] | None = None
        self.after_stage: Callable[[], None] | None = None
        self.after_secret: Callable[[], None] | None = None
        self.managed_runtime_calls: list[dict[str, object]] = []
        self.managed_runtime_block = False
        self.managed_runtime_entered = threading.Event()
        self.managed_runtime_cancelled = threading.Event()
        self.operation_order: list[str] = []

    def select_core_python_runtime(
        self,
        *,
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> CorePythonRuntimeAuthority:
        if cancel_event is not None and cancel_event.is_set():
            raise SshTransportError(SshTransportErrorCode.CANCELLED)
        self.runtime_preflight_timeouts.append(timeout_seconds)
        if self.runtime_preflight_error is not None:
            raise self.runtime_preflight_error
        if self.after_runtime_preflight is not None:
            self.after_runtime_preflight()
        return _runtime()

    def stage_core_bootstrap_assets(self, **kwargs: object) -> StagedCoreBootstrapAssets:
        self.operation_order.append("core_assets")
        try:
            with snapshot_core_bootstrap_assets(
                wheel_path=str(kwargs["wheel_path"]),
                wheel_sha256=str(kwargs["wheel_sha256"]),
                wheel_size=int(kwargs["wheel_size"]),
                framework_lock_path=str(kwargs["framework_lock_path"]),
                framework_lock_sha256=str(kwargs["framework_lock_sha256"]),
                framework_lock_size=int(kwargs["framework_lock_size"]),
            ):
                pass
        except CoreBootstrapAssetSnapshotError:
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST) from None
        self.stage_calls.append(kwargs)
        if self.stage_error is not None:
            error = self.stage_error
            self.stage_error = None
            raise error
        if self.after_stage is not None:
            self.after_stage()
        return StagedCoreBootstrapAssets(
            service_root="/home/alice/.openevo/core",
            wheel_path=(
                f"/home/alice/.openevo/core/assets/{kwargs['bundle_id']}/"
                "openevo-0.1.0-py3-none-any.whl"
            ),
            framework_lock_path=(
                f"/home/alice/.openevo/core/assets/{kwargs['bundle_id']}/framework-lock.json"
            ),
            wheel_sha256=str(kwargs["wheel_sha256"]),
            framework_lock_sha256=str(kwargs["framework_lock_sha256"]),
            wheel_size=int(kwargs["wheel_size"]),
            framework_lock_size=int(kwargs["framework_lock_size"]),
            bundle_device=1,
            bundle_inode=2,
            wheel_device=1,
            wheel_inode=3,
            framework_lock_device=1,
            framework_lock_inode=4,
        )

    def ensure_managed_runtime(self, **kwargs: object) -> object:
        self.operation_order.append("managed_runtime")
        self.managed_runtime_calls.append(kwargs)
        if self.managed_runtime_block:
            cancel_event = kwargs.get("cancel_event")
            assert isinstance(cancel_event, threading.Event)
            self.managed_runtime_entered.set()
            assert cancel_event.wait(timeout=2)
            self.managed_runtime_cancelled.set()
            raise SshTransportError(SshTransportErrorCode.CANCELLED)
        return object()

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        del cwd, env
        self.commands.append(command)
        self.timeouts.append(timeout_seconds)
        if self.run_error is not None:
            raise self.run_error
        return RemoteCommandResult(command=command, return_code=0)

    def run_secret(self, command: str, *, timeout_seconds: float = 30.0) -> SecretStr:
        self.secret_commands.append(command)
        self.timeouts.append(timeout_seconds)
        if self.after_secret is not None:
            self.after_secret()
        return _attachment_payload()

    def open_core_tunnel(self, **kwargs: object) -> FakeCoreTunnel:
        self.tunnel_kwargs = kwargs
        if self.tunnel_error is not None:
            raise self.tunnel_error
        return self.tunnel


class FakeLifecycle(DesktopRemoteLifecycle):
    def __init__(self, profile_id: str, transport: object) -> None:
        self.profile_id = profile_id
        self.transport = transport

    def active_transport(self, profile_id: str) -> object:
        if profile_id != self.profile_id or self.transport is None:
            raise RemoteConnectionFailedError("not connected")
        return self.transport


def _adapter(
    tmp_path: Path,
    transport: FakeCoreTransport | None = None,
) -> tuple[DesktopCoreSshBridgeAdapterV1, FakeLifecycle, FakeCoreTransport]:
    active = transport or FakeCoreTransport()
    lifecycle = FakeLifecycle(PROFILE_ID, active)
    return (
        DesktopCoreSshBridgeAdapterV1(lifecycle, _bootstrap_config(tmp_path)),
        lifecycle,
        active,
    )


@pytest.fixture
def verified_tunnel_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        core_control,
        "authenticate_core_service_endpoint",
        lambda **_kwargs: STATUS_PROOF,
    )


def test_core_host_stages_sealed_assets_then_bootstraps_supported_host(
    tmp_path: Path,
) -> None:
    adapter, _lifecycle, transport = _adapter(tmp_path)

    first = adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    second = adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)

    assert first == second
    assert first.profile_id == PROFILE_ID
    assert first.remote_port == 43117
    assert first.bearer_token == BEARER
    assert first.bearer_identity.startswith("core-host-v1-")
    assert first.bearer_identity != BEARER
    assert len(transport.stage_calls) == 2
    assert len(transport.managed_runtime_calls) == 2
    assert transport.operation_order == [
        "managed_runtime",
        "core_assets",
        "managed_runtime",
        "core_assets",
    ]
    assert len(transport.runtime_preflight_timeouts) == 2
    assert transport.stage_calls[0]["wheel_sha256"] == _bootstrap_config(tmp_path).wheel.sha256
    assert "/usr/bin/python3 -I -c" in transport.commands[0]
    assert "consume-attachment" in transport.secret_commands[0]
    assert BEARER not in " ".join(transport.commands + transport.secret_commands)
    assert all(0 < timeout <= 5 for timeout in transport.timeouts)
    assert BEARER not in repr(first)
    assert BEARER not in repr(adapter)
    assert str(tmp_path) not in repr(_bootstrap_config(tmp_path))


def test_core_host_without_packaged_managed_runtime_fails_before_remote_work(
    tmp_path: Path,
) -> None:
    bootstrap = _bootstrap_config(tmp_path)
    without_runtime = CoreBootstrapConfigV1(
        source_commit=bootstrap.source_commit,
        wheel=bootstrap.wheel,
        framework_lock=bootstrap.framework_lock,
    )
    transport = FakeCoreTransport()
    adapter = DesktopCoreSshBridgeAdapterV1(
        FakeLifecycle(PROFILE_ID, transport),
        without_runtime,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as unavailable:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)

    assert unavailable.value.error.code == "managed_runtime_asset_unavailable"
    assert transport.runtime_preflight_timeouts == []
    assert transport.managed_runtime_calls == []
    assert transport.stage_calls == []


def test_core_host_cancellation_stops_runtime_before_publication_and_allows_retry(
    tmp_path: Path,
) -> None:
    transport = FakeCoreTransport()
    transport.managed_runtime_block = True
    adapter, _lifecycle, _transport = _adapter(tmp_path, transport)
    cancel_event = threading.Event()
    result: list[object] = []

    def ensure() -> None:
        try:
            result.append(
                adapter.ensure_core(
                    PROFILE_ID,
                    deadline=time.monotonic() + 5,
                    cancel_event=cancel_event,
                )
            )
        except BaseException as exc:
            result.append(exc)

    thread = threading.Thread(target=ensure)
    thread.start()
    assert transport.managed_runtime_entered.wait(timeout=1)
    cancel_event.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert transport.managed_runtime_cancelled.is_set()
    assert isinstance(result[0], DesktopCoreBridgeErrorV1)
    assert result[0].error.code == "active_project_session_superseded"
    assert transport.stage_calls == []
    assert transport.commands == []
    assert transport.secret_commands == []

    transport.managed_runtime_block = False
    retry = adapter.ensure_core(
        PROFILE_ID,
        deadline=time.monotonic() + 5,
        cancel_event=threading.Event(),
    )
    assert retry.profile_id == PROFILE_ID
    assert len(transport.stage_calls) == 1


def test_core_host_rejects_cross_profile_and_transport_replacement(tmp_path: Path) -> None:
    adapter, lifecycle, transport = _adapter(tmp_path)

    with pytest.raises(DesktopCoreBridgeErrorV1) as cross_profile:
        adapter.ensure_core("profile-b", deadline=time.monotonic() + 5)
    assert cross_profile.value.error.code == "core_profile_not_connected"
    assert transport.commands == []

    transport.after_secret = lambda: setattr(lifecycle, "transport", FakeCoreTransport())
    with pytest.raises(DesktopCoreBridgeErrorV1) as replaced:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    assert replaced.value.error.code == "core_ssh_transport_identity_changed"
    assert BEARER not in str(replaced.value)


def test_core_host_rejects_transport_replacement_after_runtime_and_asset_checks(
    tmp_path: Path,
) -> None:
    runtime_adapter, runtime_lifecycle, runtime_transport = _adapter(tmp_path)
    runtime_transport.after_runtime_preflight = lambda: setattr(
        runtime_lifecycle,
        "transport",
        FakeCoreTransport(),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as runtime_changed:
        runtime_adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    assert runtime_changed.value.error.code == "core_ssh_transport_identity_changed"
    assert runtime_transport.stage_calls == []

    asset_adapter, asset_lifecycle, asset_transport = _adapter(tmp_path)
    asset_transport.after_stage = lambda: setattr(
        asset_lifecycle,
        "transport",
        FakeCoreTransport(),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as asset_changed:
        asset_adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    assert asset_changed.value.error.code == "core_ssh_transport_identity_changed"
    assert len(asset_transport.stage_calls) == 1
    assert asset_transport.commands == []


def test_core_host_rejects_local_asset_tamper_before_upload(tmp_path: Path) -> None:
    config = _bootstrap_config(tmp_path)
    adapter = DesktopCoreSshBridgeAdapterV1(
        FakeLifecycle(PROFILE_ID, transport := FakeCoreTransport()),
        config,
    )
    Path(config.wheel.local_path).write_bytes(b"tampered")

    with pytest.raises(DesktopCoreBridgeErrorV1) as tampered:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)

    assert tampered.value.error.code == "core_bootstrap_asset_invalid"
    assert transport.stage_calls == []
    assert str(tmp_path) not in str(tampered.value)


@pytest.mark.parametrize(
    ("transport_code", "desktop_code", "message_fragment", "retryable"),
    [
        (
            SshTransportErrorCode.CORE_PYTHON_UNAVAILABLE,
            "core_python_runtime_unavailable",
            "server architecture",
            False,
        ),
        (
            SshTransportErrorCode.CORE_PYTHON_PROVISION_FAILED,
            "core_python_runtime_provision_failed",
            "download, verify, or provision",
            True,
        ),
        (
            SshTransportErrorCode.CORE_KERNEL_SYSCALL_UNSUPPORTED,
            "core_supervisor_kernel_unsupported",
            "Linux kernel",
            False,
        ),
    ],
)
def test_core_host_reports_typed_remote_runtime_failures_before_upload(
    tmp_path: Path,
    transport_code: SshTransportErrorCode,
    desktop_code: str,
    message_fragment: str,
    retryable: bool,
) -> None:
    adapter, _lifecycle, transport = _adapter(tmp_path)
    transport.runtime_preflight_error = SshTransportError(transport_code)

    with pytest.raises(DesktopCoreBridgeErrorV1) as unsupported:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)

    assert unsupported.value.error.code == desktop_code
    assert unsupported.value.error.http_status == 409
    assert unsupported.value.error.retryable is retryable
    assert message_fragment in unsupported.value.error.message
    assert "os.pidfd_open" not in unsupported.value.error.message
    assert "signal.pidfd_send_signal" not in unsupported.value.error.message
    assert transport.stage_calls == []
    assert transport.commands == []
    assert str(tmp_path) not in str(unsupported.value)


def test_core_install_failure_is_actionable_and_redacts_remote_details() -> None:
    private = "http://proxy-user:proxy-secret@127.0.0.1 private/install/path"

    mapped = adapters._bootstrap_error(
        CoreControlBootstrapError(
            CoreControlBootstrapErrorCode.INSTALL_FAILED,
            private,
            retryable=True,
        )
    )

    assert mapped.error.code == "core_bootstrap_install_failed"
    assert mapped.error.http_status == 503
    assert mapped.error.retryable is True
    assert "isolated OpenEvo Core generation" in mapped.error.message
    assert "proxy-secret" not in str(mapped)
    assert "private/install/path" not in str(mapped)


def test_core_host_normalizes_runtime_and_upload_errors_without_private_values(
    tmp_path: Path,
) -> None:
    private = f"{BEARER} {tmp_path} python3 -I -c private-command"
    runtime_adapter, _runtime_lifecycle, runtime_transport = _adapter(tmp_path)
    runtime_transport.runtime_preflight_error = RuntimeError(private)

    with pytest.raises(DesktopCoreBridgeErrorV1) as runtime_error:
        runtime_adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    assert runtime_error.value.error.code == "core_supervisor_runtime_preflight_failed"
    assert private not in str(runtime_error.value)
    assert BEARER not in str(runtime_error.value)
    assert str(tmp_path) not in str(runtime_error.value)

    upload_adapter, _upload_lifecycle, upload_transport = _adapter(tmp_path)
    upload_transport.stage_error = RuntimeError(private)

    with pytest.raises(DesktopCoreBridgeErrorV1) as upload_error:
        upload_adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    assert upload_error.value.error.code == "core_bootstrap_asset_upload_failed"
    assert private not in str(upload_error.value)
    assert BEARER not in str(upload_error.value)
    assert str(tmp_path) not in str(upload_error.value)


def test_core_host_partial_upload_retry_is_exact_and_idempotent(tmp_path: Path) -> None:
    adapter, _lifecycle, transport = _adapter(tmp_path)
    transport.stage_error = SshTransportError(SshTransportErrorCode.RSYNC_FAILED)

    with pytest.raises(DesktopCoreBridgeErrorV1) as partial:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    assert partial.value.error.code == "core_bootstrap_asset_upload_failed"

    attachment = adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    assert attachment.remote_port == 43117
    assert len(transport.stage_calls) == 2
    assert transport.stage_calls[0]["bundle_id"] == transport.stage_calls[1]["bundle_id"]


def test_core_host_maps_bootstrap_timeout_and_expired_deadline(tmp_path: Path) -> None:
    preflight_transport = FakeCoreTransport()
    preflight_transport.runtime_preflight_error = SshTransportError(SshTransportErrorCode.TIMEOUT)
    preflight_adapter, _preflight_lifecycle, _preflight_transport = _adapter(
        tmp_path,
        preflight_transport,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as preflight_timeout:
        preflight_adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    assert (
        preflight_timeout.value.error.code == "core_supervisor_runtime_preflight_deadline_exceeded"
    )
    assert preflight_transport.stage_calls == []

    transport = FakeCoreTransport()
    transport.run_error = SshTransportError(SshTransportErrorCode.TIMEOUT)
    adapter, _lifecycle, _transport = _adapter(tmp_path, transport)

    with pytest.raises(DesktopCoreBridgeErrorV1) as timeout:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    assert timeout.value.error.code == "core_bootstrap_deadline_exceeded"
    assert timeout.value.error.http_status == 504
    assert timeout.value.error.retryable is True

    with pytest.raises(DesktopCoreBridgeErrorV1) as expired:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic())
    assert expired.value.error.code == "core_bridge_adapter_deadline_exceeded"


def test_tunnel_uses_same_transport_exact_attachment_and_idempotent_close(
    verified_tunnel_auth: None,
    tmp_path: Path,
) -> None:
    del verified_tunnel_auth
    adapter, _lifecycle, transport = _adapter(tmp_path)
    attachment = adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)

    handle = adapter.open_tunnel(
        profile_id=PROFILE_ID,
        remote_port=attachment.remote_port,
        session_id="session-a",
        deadline=time.monotonic() + 5,
    )

    assert handle.endpoint == "http://127.0.0.1:1"
    assert transport.tunnel_kwargs is not None
    assert transport.tunnel_kwargs["remote_host"] == "127.0.0.1"
    assert transport.tunnel_kwargs["remote_port"] == 43117
    assert transport.tunnel.authority_checks == 1
    http_transport = adapter.new_http_transport()
    assert "private" in repr(http_transport)
    http_transport.close()
    handle.close(deadline=time.monotonic() + 1)
    handle.close(deadline=time.monotonic() + 1)
    assert handle.closed is True
    assert handle.close_failure is None
    assert transport.tunnel.close_calls == 1


def test_tunnel_rejects_attachment_and_profile_transport_mismatch(
    verified_tunnel_auth: None,
    tmp_path: Path,
) -> None:
    del verified_tunnel_auth
    adapter, lifecycle, transport = _adapter(tmp_path)
    attachment = adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)

    with pytest.raises(DesktopCoreBridgeErrorV1) as wrong_port:
        adapter.open_tunnel(
            profile_id=PROFILE_ID,
            remote_port=attachment.remote_port + 1,
            session_id="session-a",
            deadline=time.monotonic() + 5,
        )
    assert wrong_port.value.error.code == "core_attachment_identity_mismatch"

    lifecycle.transport = FakeCoreTransport()
    with pytest.raises(DesktopCoreBridgeErrorV1) as replaced:
        adapter.open_tunnel(
            profile_id=PROFILE_ID,
            remote_port=attachment.remote_port,
            session_id="session-a",
            deadline=time.monotonic() + 5,
        )
    assert replaced.value.error.code == "core_ssh_transport_identity_changed"
    assert transport.tunnel_kwargs is None


def test_tunnel_maps_ssh_timeout_without_leaking_attachment(
    verified_tunnel_auth: None,
    tmp_path: Path,
) -> None:
    del verified_tunnel_auth
    adapter, _lifecycle, transport = _adapter(tmp_path)
    attachment = adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    transport.tunnel_error = SshTransportError(SshTransportErrorCode.TIMEOUT)

    with pytest.raises(DesktopCoreBridgeErrorV1) as timeout:
        adapter.open_tunnel(
            profile_id=PROFILE_ID,
            remote_port=attachment.remote_port,
            session_id="session-a",
            deadline=time.monotonic() + 5,
        )

    assert timeout.value.error.code == "core_tunnel_deadline_exceeded"
    assert timeout.value.error.http_status == 504
    assert BEARER not in str(timeout.value)


def test_tunnel_close_timeout_is_observable_and_reuses_one_close(
    verified_tunnel_auth: None,
    tmp_path: Path,
) -> None:
    del verified_tunnel_auth
    adapter, _lifecycle, transport = _adapter(tmp_path)
    transport.tunnel = FakeCoreTunnel(remote_port=43117, block_close=True)
    attachment = adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    handle = adapter.open_tunnel(
        profile_id=PROFILE_ID,
        remote_port=attachment.remote_port,
        session_id="session-a",
        deadline=time.monotonic() + 5,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as timeout:
        handle.close(deadline=time.monotonic() + 0.01)
    assert timeout.value.error.code == "core_tunnel_close_deadline_exceeded"
    assert handle.close_failure == "deadline_exceeded"
    assert handle.closed is False
    assert transport.tunnel.close_entered.wait(timeout=1)

    transport.tunnel.close_release.set()
    handle.close(deadline=time.monotonic() + 1)
    assert handle.closed is True
    assert handle.close_failure is None
    assert transport.tunnel.close_calls == 1


def test_http_transport_opens_verified_socket_without_local_tcp_listener() -> None:
    requests: list[bytes] = []
    workers: list[threading.Thread] = []

    class SocketEndpoint:
        def __init__(self) -> None:
            self.authority_checks = 0

        def open_verified_socket(self, *, timeout_seconds: float) -> socket.socket:
            parent, child = socket.socketpair()
            parent.settimeout(timeout_seconds)

            def serve() -> None:
                with child:
                    received = bytearray()
                    while b"\r\n\r\n" not in received:
                        received.extend(child.recv(4096))
                    requests.append(bytes(received))
                    child.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: 11\r\n"
                        b"Connection: close\r\n\r\n"
                        b'{"ok":true}'
                    )

            worker = threading.Thread(target=serve)
            workers.append(worker)
            worker.start()
            return parent

        def verify_authority(self) -> None:
            self.authority_checks += 1

    endpoint = SocketEndpoint()
    transport = VerifiedCoreHttpTransportV1(endpoint)  # type: ignore[arg-type]
    with httpx.Client(base_url="http://127.0.0.1:1", transport=transport) as client:
        response = client.get("/v1/status", headers={"Authorization": f"Bearer {BEARER}"})

    for worker in workers:
        worker.join(timeout=1)
    assert response.json() == {"ok": True}
    assert b"GET /v1/status HTTP/1.1" in requests[0]
    assert f"authorization: Bearer {BEARER}".encode() in requests[0]
    assert endpoint.authority_checks >= 2


def test_http_transport_delivers_small_chunked_sse_chunk_before_stream_end() -> None:
    first_chunk_sent = threading.Event()
    finish_stream = threading.Event()
    workers: list[threading.Thread] = []

    class SseEndpoint:
        def open_verified_socket(self, *, timeout_seconds: float) -> socket.socket:
            parent, child = socket.socketpair()
            parent.settimeout(timeout_seconds)

            def serve() -> None:
                with child:
                    received = bytearray()
                    while b"\r\n\r\n" not in received:
                        received.extend(child.recv(4096))
                    child.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: text/event-stream\r\n"
                        b"Transfer-Encoding: chunked\r\n"
                        b"Connection: close\r\n\r\n"
                        b"d\r\n: heartbeat\n\n\r\n"
                    )
                    first_chunk_sent.set()
                    assert finish_stream.wait(timeout=2)
                    child.sendall(b"0\r\n\r\n")

            worker = threading.Thread(target=serve)
            workers.append(worker)
            worker.start()
            return parent

        def verify_authority(self) -> None:
            pass

    transport = VerifiedCoreHttpTransportV1(SseEndpoint())  # type: ignore[arg-type]
    client = httpx.Client(base_url="http://127.0.0.1:1", transport=transport)
    response = client.send(client.build_request("GET", "/v1/events"), stream=True)
    delivered: list[bytes] = []
    delivery_finished = threading.Event()
    chunks = response.iter_raw()

    def read_first() -> None:
        delivered.append(next(chunks))
        delivery_finished.set()

    reader = threading.Thread(target=read_first)
    reader.start()
    try:
        assert first_chunk_sent.wait(timeout=1)
        assert delivery_finished.wait(timeout=0.25)
        assert delivered == [b": heartbeat\n\n"]
    finally:
        finish_stream.set()
        reader.join(timeout=1)
        for worker in workers:
            worker.join(timeout=1)
        chunks.close()
        response.close()
        client.close()


def test_http_transport_frames_unknown_length_chunked_request_on_wire() -> None:
    wire: list[bytes] = []

    class RequestChunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b"abc"
            yield b"de"

    class WireEndpoint:
        def open_verified_socket(self, *, timeout_seconds: float) -> socket.socket:
            parent, child = socket.socketpair()
            parent.settimeout(timeout_seconds)

            def serve() -> None:
                with child:
                    received = bytearray()
                    while b"0\r\n\r\n" not in received:
                        received.extend(child.recv(4096))
                    wire.append(bytes(received))
                    child.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
                    )

            threading.Thread(target=serve).start()
            return parent

        def verify_authority(self) -> None:
            pass

    transport = VerifiedCoreHttpTransportV1(WireEndpoint())  # type: ignore[arg-type]
    request = httpx.Request(
        "POST",
        "http://127.0.0.1:1/v1/upload",
        headers={"Transfer-Encoding": "chunked"},
        stream=RequestChunks(),
    )
    response = transport.handle_request(request)
    try:
        assert response.read() == b"{}"
    finally:
        response.close()
        transport.close()
    assert b"\r\n3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n" in wire[0]


def test_http_transport_clamps_endpoint_io_timeout_to_sixty_seconds() -> None:
    observed_timeouts: list[float] = []

    class TimeoutEndpoint:
        def open_verified_socket(self, *, timeout_seconds: float) -> socket.socket:
            observed_timeouts.append(timeout_seconds)
            if timeout_seconds > 60:
                raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST)
            parent, child = socket.socketpair()

            def serve() -> None:
                with child:
                    received = bytearray()
                    while b"\r\n\r\n" not in received:
                        received.extend(child.recv(4096))
                    child.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
                    )

            threading.Thread(target=serve).start()
            return parent

        def verify_authority(self) -> None:
            pass

    transport = VerifiedCoreHttpTransportV1(TimeoutEndpoint())  # type: ignore[arg-type]
    with httpx.Client(
        base_url="http://127.0.0.1:1",
        transport=transport,
        timeout=300.0,
    ) as client:
        assert client.get("/v1/status").json() == {}
    assert observed_timeouts == [60.0]


def test_http_transport_close_blocks_late_socket_adoption_and_bearer_send() -> None:
    open_entered = threading.Event()
    release_open = threading.Event()
    close_started = threading.Event()
    close_returned = threading.Event()
    wire = bytearray()

    class BlockingEndpoint:
        def open_verified_socket(self, *, timeout_seconds: float) -> socket.socket:
            del timeout_seconds
            open_entered.set()
            assert release_open.wait(timeout=2)
            parent, child = socket.socketpair()

            def observe() -> None:
                with child:
                    child.settimeout(0.25)
                    try:
                        wire.extend(child.recv(4096))
                    except (TimeoutError, OSError):
                        pass

            threading.Thread(target=observe).start()
            return parent

        def verify_authority(self) -> None:
            pass

    transport = VerifiedCoreHttpTransportV1(BlockingEndpoint())  # type: ignore[arg-type]
    request_failed = threading.Event()

    def request() -> None:
        try:
            with httpx.Client(
                base_url="http://127.0.0.1:1",
                transport=transport,
            ) as client:
                client.get(
                    "/v1/status",
                    headers={"Authorization": f"Bearer {BEARER}"},
                )
        except httpx.HTTPError:
            request_failed.set()

    def close() -> None:
        close_started.set()
        transport.close()
        close_returned.set()

    requester = threading.Thread(target=request)
    requester.start()
    assert open_entered.wait(timeout=1)
    closer = threading.Thread(target=close)
    closer.start()
    assert close_started.wait(timeout=1)
    assert not close_returned.wait(timeout=0.1)
    release_open.set()
    requester.join(timeout=1)
    closer.join(timeout=1)

    assert request_failed.is_set()
    assert close_returned.is_set()
    assert bytes(wire) == b""


def test_http_transport_close_cancels_and_waits_for_active_chunk_stream() -> None:
    response_started = threading.Event()
    peer_closed = threading.Event()
    workers: list[threading.Thread] = []

    class StreamingEndpoint:
        def open_verified_socket(self, *, timeout_seconds: float) -> socket.socket:
            parent, child = socket.socketpair()
            parent.settimeout(timeout_seconds)

            def serve() -> None:
                with child:
                    received = bytearray()
                    while b"\r\n\r\n" not in received:
                        received.extend(child.recv(4096))
                    child.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: text/event-stream\r\n"
                        b"Transfer-Encoding: chunked\r\n"
                        b"Connection: close\r\n\r\n"
                    )
                    response_started.set()
                    assert child.recv(1) == b""
                    peer_closed.set()

            worker = threading.Thread(target=serve)
            workers.append(worker)
            worker.start()
            return parent

        def verify_authority(self) -> None:
            pass

    transport = VerifiedCoreHttpTransportV1(StreamingEndpoint())  # type: ignore[arg-type]
    request = httpx.Request("GET", "http://127.0.0.1:1/v1/events")
    response = transport.handle_request(request)
    chunks = response.iter_raw()
    read_failed = threading.Event()
    delivered: list[bytes] = []

    def read() -> None:
        try:
            delivered.append(next(chunks))
        except httpx.ReadError:
            read_failed.set()

    reader = threading.Thread(target=read)
    reader.start()
    assert response_started.wait(timeout=1)

    transport.close()

    reader.join(timeout=1)
    for worker in workers:
        worker.join(timeout=1)
    assert peer_closed.is_set()
    assert read_failed.is_set()
    assert delivered == []
    response.close()


def _ownership(*, project_id: str = "project-a") -> WorkspaceImportOwnership:
    return WorkspaceImportOwnership(
        project_id=project_id,
        operation_id="workspace-operation-a",
        idempotency_key="workspace-idempotency-a",
    )


def _ingest_workspace(
    tmp_path: Path,
) -> tuple[WorkspaceImportStore, WorkspaceImportRefV1, WorkspaceImportOwnership]:
    store = WorkspaceImportStore(tmp_path / "imports")
    ownership = _ownership()
    source_path = tmp_path / "workspace.tar"
    source_path.write_bytes(ARCHIVE)
    with source_path.open("rb") as source:
        ref = store.ingest(source, ownership=ownership, import_id=IMPORT_ID)
    return store, ref, ownership


def test_archive_source_yields_verified_unlinked_read_only_stream(tmp_path: Path) -> None:
    store, ref, ownership = _ingest_workspace(tmp_path)
    source = AdoptedWorkspaceArchiveSourceV1(
        store,
        (AdoptedWorkspaceImportV1(ref, ownership),),
    )

    try:
        with source.open_archive(ref) as stream:
            descriptor = stream.fileno()
            assert isinstance(stream.name, int)
            assert stream.name == descriptor
            assert fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
            assert stream.read() == ARCHIVE
            assert hashlib.sha256(ARCHIVE).hexdigest() == ref.content_sha256
            with pytest.raises(io.UnsupportedOperation):
                stream.write(b"x")
        assert stream.closed is True
        assert str(tmp_path) not in repr(source)
    finally:
        store.close()


def test_archive_source_rejects_unbound_ref_and_wrong_ownership(tmp_path: Path) -> None:
    store, ref, ownership = _ingest_workspace(tmp_path)
    changed_ref = WorkspaceImportRefV1(
        import_id=ref.import_id,
        content_sha256="7" * 64,
        byte_size=ref.byte_size,
        entry_count=ref.entry_count,
        extracted_byte_size=ref.extracted_byte_size,
    )
    exact = AdoptedWorkspaceArchiveSourceV1(
        store,
        (AdoptedWorkspaceImportV1(ref, ownership),),
    )
    wrong_owner = AdoptedWorkspaceArchiveSourceV1(
        store,
        (AdoptedWorkspaceImportV1(ref, _ownership(project_id="project-b")),),
    )

    try:
        with pytest.raises(DesktopCoreBridgeErrorV1) as unbound:
            exact.open_archive(changed_ref)
        assert unbound.value.error.code == "workspace_import_authority_mismatch"

        with pytest.raises(DesktopCoreBridgeErrorV1) as mismatch:
            with wrong_owner.open_archive(ref):
                pass
        assert mismatch.value.error.code == "workspace_import_integrity_failed"
        assert str(tmp_path) not in str(mismatch.value)
    finally:
        store.close()
