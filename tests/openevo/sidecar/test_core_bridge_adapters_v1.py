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

from desktop.sidecar.contracts.v1.models import WorkspaceImportRefV1
from desktop.sidecar.core_bridge_adapters_v1 import (
    AdoptedWorkspaceArchiveSourceV1,
    AdoptedWorkspaceImportV1,
    CoreBootstrapConfigV1,
    DesktopCoreSshBridgeAdapterV1,
    SealedCoreBootstrapAssetV1,
    SealedDaemonBundleV1,
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
from openevo.deployment.core_control import parse_core_control_attachment
from openevo.deployment.daemon_bundle_transport import (
    DaemonBundleIdentity,
    DaemonBundleServicePredecessor,
    DaemonBundleServiceStatus,
    StagedDaemonBundle,
)
from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.ssh import (
    SshTransportError,
    SshTransportErrorCode,
)
from openevo.runtime.managed import MANAGED_RUNTIME_ARCHIVE_RELEASE


PROFILE_ID = "profile-a"
SOURCE_COMMIT = "1" * 40
BEARER = "B" * 64
RELEASE_IDENTITY = "2" * 64
REGISTRY_DIGEST = "3" * 64
STATUS_PROOF = "4" * 64
GENERATION = "5" * 32
DEPENDENCY_LOCK_DIGEST = "6" * 64
WHEEL_DIGEST = hashlib.sha256(b"sealed-wheel").hexdigest()
FRAMEWORK_LOCK_BYTES = json.dumps(
    {
        "schema_version": "1",
        "distribution": "openevo",
        "distribution_version": "0.1.0",
        "distribution_digest": WHEEL_DIGEST,
        "wheel_filename": "openevo-0.1.0-py3-none-any.whl",
    },
    separators=(",", ":"),
    sort_keys=True,
).encode("utf-8")
FRAMEWORK_LOCK_DIGEST = hashlib.sha256(FRAMEWORK_LOCK_BYTES).hexdigest()
ARCHIVE = bytes(1024)
IMPORT_ID = "workspace-import-" + "7" * 48


def _bootstrap_config(tmp_path: Path) -> CoreBootstrapConfigV1:
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"sealed-wheel")
    wheel_digest = WHEEL_DIGEST
    framework_lock = tmp_path / "framework-lock.json"
    framework_lock.write_bytes(FRAMEWORK_LOCK_BYTES)
    release = MANAGED_RUNTIME_ARCHIVE_RELEASE
    runtime_archive = tmp_path / release.filename
    runtime_archive.touch()
    os.truncate(runtime_archive, release.byte_size)
    runtime_archive.chmod(0o600)
    daemon = tmp_path / "openevo-daemon-linux-x86_64"
    daemon.write_bytes(b"\x7fELF\0sealed-openevo-daemon")
    daemon.chmod(0o700)
    daemon_digest = hashlib.sha256(daemon.read_bytes()).hexdigest()
    daemon_manifest = tmp_path / "openevo-daemon-bundle.json"
    daemon_manifest.write_bytes(b'{"schema_version":1}\n')
    daemon_manifest_digest = hashlib.sha256(daemon_manifest.read_bytes()).hexdigest()
    framework_lock_digest = FRAMEWORK_LOCK_DIGEST
    return CoreBootstrapConfigV1(
        source_commit=SOURCE_COMMIT,
        wheel=SealedCoreBootstrapAssetV1(
            local_path=str(wheel),
            sha256=wheel_digest,
            byte_size=wheel.stat().st_size,
        ),
        framework_lock=SealedCoreBootstrapAssetV1(
            local_path=str(framework_lock),
            sha256=framework_lock_digest,
            byte_size=framework_lock.stat().st_size,
        ),
        daemon_bundle=SealedDaemonBundleV1(
            local_path=str(daemon),
            sha256=daemon_digest,
            byte_size=daemon.stat().st_size,
            manifest_sha256=daemon_manifest_digest,
            release_identity=RELEASE_IDENTITY,
            registry_digest=REGISTRY_DIGEST,
            source_commit=SOURCE_COMMIT,
            wheel_sha256=wheel_digest,
            dependency_lock_sha256=DEPENDENCY_LOCK_DIGEST,
            framework_lock_sha256=framework_lock_digest,
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
        self.identity_error: Exception | None = None
        self.observation_error: Exception | None = None
        self.ensure_error: Exception | None = None
        self.service_bundle_sha256: str | None = None
        self.service_manifest_sha256: str | None = None
        self.after_stage: Callable[[], None] | None = None
        self.after_identity: Callable[[], None] | None = None
        self.after_ensure: Callable[[], None] | None = None
        self.managed_runtime_calls: list[dict[str, object]] = []
        self.managed_runtime_block = False
        self.managed_runtime_entered = threading.Event()
        self.managed_runtime_cancelled = threading.Event()
        self.operation_order: list[str] = []
        self.ensure_predecessors: list[DaemonBundleServicePredecessor] = []

    def stage_daemon_bundle(
        self,
        *,
        bundle_path: str,
        bundle_sha256: str,
        bundle_size: int,
        manifest_path: str,
        manifest_sha256: str,
        manifest_size: int,
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> StagedDaemonBundle:
        if cancel_event is not None and cancel_event.is_set():
            raise SshTransportError(SshTransportErrorCode.CANCELLED)
        self.operation_order.append("daemon_stage")
        payload = Path(bundle_path).read_bytes()
        if len(payload) != bundle_size or hashlib.sha256(payload).hexdigest() != bundle_sha256:
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST)
        manifest_payload = Path(manifest_path).read_bytes()
        if (
            len(manifest_payload) != manifest_size
            or hashlib.sha256(manifest_payload).hexdigest() != manifest_sha256
        ):
            raise SshTransportError(SshTransportErrorCode.INVALID_REQUEST)
        self.stage_calls.append(
            {
                "bundle_path": bundle_path,
                "bundle_sha256": bundle_sha256,
                "bundle_size": bundle_size,
                "manifest_path": manifest_path,
                "manifest_sha256": manifest_sha256,
                "manifest_size": manifest_size,
                "timeout_seconds": timeout_seconds,
                "cancel_event": cancel_event,
            }
        )
        if self.stage_error is not None:
            error = self.stage_error
            self.stage_error = None
            raise error
        if self.after_stage is not None:
            self.after_stage()
        return StagedDaemonBundle(
            host_profile="docker_user_container_v1",
            sha256=bundle_sha256,
            size=bundle_size,
            reused=False,
            _service_root="/home/alice/.openevo/daemon-bundles",
            _executable_path=f"/home/alice/.openevo/daemon-bundles/bundle-{bundle_sha256}",
        )

    def daemon_bundle_identity(
        self,
        bundle: StagedDaemonBundle,
        *,
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> DaemonBundleIdentity:
        del timeout_seconds
        if cancel_event is not None and cancel_event.is_set():
            raise SshTransportError(SshTransportErrorCode.CANCELLED)
        self.operation_order.append("daemon_identity")
        if self.identity_error is not None:
            raise self.identity_error
        if self.after_identity is not None:
            self.after_identity()
        return DaemonBundleIdentity(
            bundle_format="pyinstaller-onefile",
            bundle_sha256=bundle.sha256,
            bundle_size=bundle.size,
            core_distribution="openevo",
            core_version="0.1.0",
            core_wheel_sha256=WHEEL_DIGEST,
            dependency_lock_sha256=DEPENDENCY_LOCK_DIGEST,
            framework_lock_sha256=FRAMEWORK_LOCK_DIGEST,
            registry_digest=REGISTRY_DIGEST,
            release_identity=RELEASE_IDENTITY,
            source_commit=SOURCE_COMMIT,
            platform_system="linux",
            platform_architecture="x86_64",
        )

    def ensure_managed_runtime_from_daemon(
        self,
        bundle: StagedDaemonBundle,
        **kwargs: object,
    ) -> object:
        del bundle
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

    def observe_daemon_bundle_service(
        self,
        bundle: StagedDaemonBundle,
        **kwargs: object,
    ) -> DaemonBundleServicePredecessor:
        del bundle, kwargs
        self.operation_order.append("daemon_observe")
        if self.observation_error is not None:
            raise self.observation_error
        return DaemonBundleServicePredecessor(state="absent")

    def ensure_daemon_bundle(
        self,
        bundle: StagedDaemonBundle,
        **kwargs: object,
    ) -> object:
        self.operation_order.append("daemon_start")
        predecessor = kwargs["expected_predecessor"]
        assert isinstance(predecessor, DaemonBundleServicePredecessor)
        self.ensure_predecessors.append(predecessor)
        if self.ensure_error is not None:
            raise self.ensure_error
        if self.after_ensure is not None:
            self.after_ensure()
        attachment = parse_core_control_attachment(_attachment_payload())
        return (
            attachment,
            DaemonBundleServiceStatus(
                remote_port=attachment.remote_port,
                bundle_sha256=self.service_bundle_sha256 or bundle.sha256,
                canonical_manifest_sha256=(
                    self.service_manifest_sha256 or str(kwargs["canonical_manifest_sha256"])
                ),
                lifecycle_compatibility=2,
                release_identity=attachment.release_identity,
                registry_digest=attachment.registry_digest,
                source_commit=attachment.source_commit,
                generation=attachment.generation,
                attached=attachment.attached,
            ),
        )

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


def test_core_host_stages_verified_daemon_prepares_runtime_then_starts(
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
        "daemon_stage",
        "daemon_identity",
        "daemon_observe",
        "managed_runtime",
        "daemon_start",
        "daemon_stage",
        "daemon_identity",
        "daemon_observe",
        "managed_runtime",
        "daemon_start",
    ]
    assert transport.ensure_predecessors == [
        DaemonBundleServicePredecessor(state="absent"),
        DaemonBundleServicePredecessor(state="absent"),
    ]
    assert (
        transport.stage_calls[0]["bundle_sha256"]
        == _bootstrap_config(tmp_path).daemon_bundle.sha256
    )
    assert transport.commands == []
    assert transport.secret_commands == []
    assert BEARER not in " ".join(transport.commands + transport.secret_commands)
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
        daemon_bundle=bootstrap.daemon_bundle,
    )
    transport = FakeCoreTransport()
    adapter = DesktopCoreSshBridgeAdapterV1(
        FakeLifecycle(PROFILE_ID, transport),
        without_runtime,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as unavailable:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)

    assert unavailable.value.error.code == "managed_runtime_asset_unavailable"
    assert transport.managed_runtime_calls == []
    assert transport.stage_calls == []


def test_core_host_without_packaged_daemon_fails_before_remote_work(
    tmp_path: Path,
) -> None:
    bootstrap = _bootstrap_config(tmp_path)
    without_daemon = CoreBootstrapConfigV1(
        source_commit=bootstrap.source_commit,
        wheel=bootstrap.wheel,
        framework_lock=bootstrap.framework_lock,
        managed_runtime_archive=bootstrap.managed_runtime_archive,
    )
    transport = FakeCoreTransport()
    adapter = DesktopCoreSshBridgeAdapterV1(
        FakeLifecycle(PROFILE_ID, transport),
        without_daemon,
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as unavailable:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)

    assert unavailable.value.error.code == "daemon_bundle_asset_unavailable"
    assert transport.operation_order == []


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
    assert len(transport.stage_calls) == 1
    assert transport.commands == []
    assert transport.secret_commands == []
    assert "daemon_start" not in transport.operation_order

    transport.managed_runtime_block = False
    retry = adapter.ensure_core(
        PROFILE_ID,
        deadline=time.monotonic() + 5,
        cancel_event=threading.Event(),
    )
    assert retry.profile_id == PROFILE_ID
    assert len(transport.stage_calls) == 2


def test_core_host_rejects_cross_profile_and_transport_replacement(tmp_path: Path) -> None:
    adapter, lifecycle, transport = _adapter(tmp_path)

    with pytest.raises(DesktopCoreBridgeErrorV1) as cross_profile:
        adapter.ensure_core("profile-b", deadline=time.monotonic() + 5)
    assert cross_profile.value.error.code == "core_profile_not_connected"
    assert transport.commands == []

    transport.after_ensure = lambda: setattr(lifecycle, "transport", FakeCoreTransport())
    with pytest.raises(DesktopCoreBridgeErrorV1) as replaced:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    assert replaced.value.error.code == "core_ssh_transport_identity_changed"
    assert BEARER not in str(replaced.value)


def test_core_host_rejects_transport_replacement_after_daemon_checks(
    tmp_path: Path,
) -> None:
    stage_adapter, stage_lifecycle, stage_transport = _adapter(tmp_path)
    stage_transport.after_stage = lambda: setattr(
        stage_lifecycle,
        "transport",
        FakeCoreTransport(),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as stage_changed:
        stage_adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    assert stage_changed.value.error.code == "core_ssh_transport_identity_changed"
    assert len(stage_transport.stage_calls) == 1
    assert "daemon_identity" not in stage_transport.operation_order

    identity_adapter, identity_lifecycle, identity_transport = _adapter(tmp_path)
    identity_transport.after_identity = lambda: setattr(
        identity_lifecycle,
        "transport",
        FakeCoreTransport(),
    )

    with pytest.raises(DesktopCoreBridgeErrorV1) as identity_changed:
        identity_adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    assert identity_changed.value.error.code == "core_ssh_transport_identity_changed"
    assert "managed_runtime" not in identity_transport.operation_order


def test_core_host_rejects_local_daemon_tamper_before_upload(tmp_path: Path) -> None:
    config = _bootstrap_config(tmp_path)
    adapter = DesktopCoreSshBridgeAdapterV1(
        FakeLifecycle(PROFILE_ID, transport := FakeCoreTransport()),
        config,
    )
    assert config.daemon_bundle is not None
    Path(config.daemon_bundle.local_path).write_bytes(b"tampered")

    with pytest.raises(DesktopCoreBridgeErrorV1) as tampered:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)

    assert tampered.value.error.code == "daemon_bundle_identity_mismatch"
    assert transport.stage_calls == []
    assert str(tmp_path) not in str(tampered.value)


@pytest.mark.parametrize(
    ("transport_code", "desktop_code", "http_status", "retryable"),
    [
        (
            SshTransportErrorCode.INVALID_REQUEST,
            "daemon_bundle_identity_mismatch",
            409,
            False,
        ),
        (
            SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED,
            "core_ssh_authority_invalid",
            409,
            False,
        ),
        (
            SshTransportErrorCode.TIMEOUT,
            "daemon_bundle_stage_deadline_exceeded",
            504,
            True,
        ),
    ],
)
def test_core_host_reports_typed_daemon_stage_failures(
    tmp_path: Path,
    transport_code: SshTransportErrorCode,
    desktop_code: str,
    http_status: int,
    retryable: bool,
) -> None:
    adapter, _lifecycle, transport = _adapter(tmp_path)
    transport.stage_error = SshTransportError(transport_code)

    with pytest.raises(DesktopCoreBridgeErrorV1) as unsupported:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)

    assert unsupported.value.error.code == desktop_code
    assert unsupported.value.error.http_status == http_status
    assert unsupported.value.error.retryable is retryable
    assert transport.commands == []
    assert str(tmp_path) not in str(unsupported.value)


def test_core_host_normalizes_daemon_errors_without_private_values(
    tmp_path: Path,
) -> None:
    private = f"{BEARER} {tmp_path} private-command"
    stage_adapter, _stage_lifecycle, stage_transport = _adapter(tmp_path)
    stage_transport.stage_error = RuntimeError(private)

    with pytest.raises(DesktopCoreBridgeErrorV1) as stage_error:
        stage_adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    assert stage_error.value.error.code == "daemon_bundle_stage_failed"
    assert private not in str(stage_error.value)
    assert BEARER not in str(stage_error.value)
    assert str(tmp_path) not in str(stage_error.value)


def test_core_host_partial_daemon_stage_retry_is_exact_and_idempotent(
    tmp_path: Path,
) -> None:
    adapter, _lifecycle, transport = _adapter(tmp_path)
    transport.stage_error = SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)

    with pytest.raises(DesktopCoreBridgeErrorV1) as partial:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    assert partial.value.error.code == "daemon_bundle_stage_failed"

    attachment = adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    assert attachment.remote_port == 43117
    assert len(transport.stage_calls) == 2
    assert transport.stage_calls[0]["bundle_sha256"] == transport.stage_calls[1]["bundle_sha256"]


def test_core_host_maps_daemon_timeout_and_expired_deadline(tmp_path: Path) -> None:
    transport = FakeCoreTransport()
    transport.ensure_error = SshTransportError(SshTransportErrorCode.TIMEOUT)
    adapter, _lifecycle, _transport = _adapter(tmp_path, transport)

    with pytest.raises(DesktopCoreBridgeErrorV1) as timeout:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    assert timeout.value.error.code == "daemon_bundle_start_deadline_exceeded"
    assert timeout.value.error.http_status == 504
    assert timeout.value.error.retryable is True

    with pytest.raises(DesktopCoreBridgeErrorV1) as expired:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic())
    assert expired.value.error.code == "core_bridge_adapter_deadline_exceeded"


def test_core_host_preserves_daemon_predecessor_conflict(tmp_path: Path) -> None:
    transport = FakeCoreTransport()
    transport.ensure_error = SshTransportError(
        SshTransportErrorCode.DAEMON_SERVICE_PREDECESSOR_MISMATCH
    )
    adapter, _lifecycle, _transport = _adapter(tmp_path, transport)

    with pytest.raises(DesktopCoreBridgeErrorV1) as conflict:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)

    assert conflict.value.error.code == "daemon_service_predecessor_mismatch"
    assert conflict.value.error.http_status == 409
    assert conflict.value.error.retryable is True
    assert BEARER not in str(conflict.value)


def test_core_host_rejects_attachment_with_different_exact_bundle(
    tmp_path: Path,
) -> None:
    transport = FakeCoreTransport()
    transport.service_bundle_sha256 = "9" * 64
    adapter, _lifecycle, _transport = _adapter(tmp_path, transport)

    with pytest.raises(DesktopCoreBridgeErrorV1) as mismatch:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)

    assert mismatch.value.error.code == "daemon_bundle_identity_mismatch"
    assert mismatch.value.error.retryable is False


def test_core_host_preserves_nonretryable_daemon_update_required(
    tmp_path: Path,
) -> None:
    transport = FakeCoreTransport()
    transport.ensure_error = SshTransportError(SshTransportErrorCode.DAEMON_UPDATE_REQUIRED)
    adapter, _lifecycle, _transport = _adapter(tmp_path, transport)

    with pytest.raises(DesktopCoreBridgeErrorV1) as update:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)

    assert update.value.error.code == "daemon_update_required"
    assert update.value.error.http_status == 409
    assert update.value.error.retryable is False


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
