from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import time

import httpx
import pytest

from desktop.sidecar.contracts.v2 import models as local_v2
from desktop.sidecar.core_bridge_adapters_v2 import (
    CoreBootstrapConfigV2,
    DesktopCoreSshBridgeAdapterV2,
    SealedCoreBootstrapAssetV2,
    SealedDaemonBundleV2,
    SealedManagedRuntimeArchiveV2,
    VerifiedCoreHttpTransportV2,
    _ssh_error,
)
from desktop.sidecar.core_bridge_v2 import DesktopCoreBridgeErrorV2
from openevo.deployment import core_control
from openevo.deployment.daemon_bundle_transport import DaemonBundleServicePredecessor
from openevo.deployment.ssh import SshTransportError, SshTransportErrorCode
from openevo.runtime.managed import MANAGED_RUNTIME_ARCHIVE_RELEASE
from tests.openevo.sidecar.test_core_bridge_adapters_v1 import (
    DEPENDENCY_LOCK_DIGEST,
    FRAMEWORK_LOCK_BYTES,
    FRAMEWORK_LOCK_DIGEST,
    GENERATION,
    PROFILE_ID,
    REGISTRY_DIGEST,
    RELEASE_IDENTITY,
    SOURCE_COMMIT,
    STATUS_PROOF,
    WHEEL_DIGEST,
    FakeCoreTransport,
)


def _bootstrap(
    tmp_path: Path,
    *,
    replace_mismatched: bool = False,
) -> CoreBootstrapConfigV2:
    wheel = tmp_path / "openevo-0.1.10-py3-none-any.whl"
    wheel.write_bytes(b"sealed-wheel")
    framework_lock = tmp_path / "framework-lock.json"
    framework_lock.write_bytes(FRAMEWORK_LOCK_BYTES)
    daemon = tmp_path / "openevo-daemon-linux-x86_64"
    daemon.write_bytes(b"\x7fELF\0sealed-openevo-daemon")
    manifest = tmp_path / "openevo-daemon-bundle.json"
    manifest.write_bytes(b'{"schema_version":2}\n')
    runtime = tmp_path / MANAGED_RUNTIME_ARCHIVE_RELEASE.filename
    runtime.touch()
    daemon_sha256 = hashlib.sha256(daemon.read_bytes()).hexdigest()
    return CoreBootstrapConfigV2(
        source_commit=SOURCE_COMMIT,
        wheel=SealedCoreBootstrapAssetV2(
            local_path=str(wheel),
            sha256=WHEEL_DIGEST,
            byte_size=wheel.stat().st_size,
        ),
        framework_lock=SealedCoreBootstrapAssetV2(
            local_path=str(framework_lock),
            sha256=FRAMEWORK_LOCK_DIGEST,
            byte_size=framework_lock.stat().st_size,
        ),
        daemon_bundle=SealedDaemonBundleV2(
            local_path=str(daemon),
            sha256=daemon_sha256,
            byte_size=daemon.stat().st_size,
            manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
            release_identity=RELEASE_IDENTITY,
            registry_digest=REGISTRY_DIGEST,
            source_commit=SOURCE_COMMIT,
            wheel_sha256=WHEEL_DIGEST,
            dependency_lock_sha256=DEPENDENCY_LOCK_DIGEST,
            framework_lock_sha256=FRAMEWORK_LOCK_DIGEST,
        ),
        managed_runtime_archive=SealedManagedRuntimeArchiveV2(
            local_path=str(runtime),
            sha256=MANAGED_RUNTIME_ARCHIVE_RELEASE.sha256,
            byte_size=MANAGED_RUNTIME_ARCHIVE_RELEASE.byte_size,
            platform=MANAGED_RUNTIME_ARCHIVE_RELEASE.platform,
            config_id=MANAGED_RUNTIME_ARCHIVE_RELEASE.config_id,
            oci_index_id=MANAGED_RUNTIME_ARCHIVE_RELEASE.oci_index_id,
        ),
        replace_mismatched=replace_mismatched,
    )


class _Lifecycle:
    def __init__(self, transport: FakeCoreTransport, generation: int = 7) -> None:
        self.transport = transport
        self.generation = generation
        self.calls: list[tuple[str, int]] = []

    def active_transport(self, profile_id: str, generation: int) -> object:
        self.calls.append((profile_id, generation))
        if profile_id != PROFILE_ID or generation != self.generation:
            raise RuntimeError("not active")
        return self.transport


@pytest.fixture
def verified_tunnel_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        core_control,
        "authenticate_core_service_endpoint",
        lambda **_kwargs: STATUS_PROOF,
    )


def test_adapter_binds_every_daemon_step_to_exact_profile_connection_generation(
    tmp_path: Path,
) -> None:
    transport = FakeCoreTransport()
    lifecycle = _Lifecycle(transport)
    adapter = DesktopCoreSshBridgeAdapterV2(lifecycle, _bootstrap(tmp_path))

    attachment = adapter.ensure_core(
        PROFILE_ID,
        7,
        deadline=time.monotonic() + 5,
    )

    assert attachment.profile_id == PROFILE_ID
    assert attachment.profile_connection_generation == 7
    assert attachment.remote_port == 43117
    assert len(attachment.bearer_identity) == 64
    assert transport.operation_order == [
        "daemon_stage",
        "daemon_identity",
        "daemon_observe",
        "managed_runtime",
        "daemon_start",
    ]
    assert lifecycle.calls and set(lifecycle.calls) == {(PROFILE_ID, 7)}
    assert not transport.commands
    assert not transport.secret_commands
    assert "BBBB" not in repr(attachment)
    assert str(tmp_path) not in repr(adapter)


def test_adapter_reports_explicit_monotonic_daemon_lifecycle_checkpoints(
    tmp_path: Path,
) -> None:
    transport = FakeCoreTransport()
    observed: list[tuple[str, object, bool]] = []
    adapter = DesktopCoreSshBridgeAdapterV2(
        _Lifecycle(transport),
        _bootstrap(tmp_path),
        progress_observer=lambda phase, progress, cancellable: observed.append(
            (phase, progress, cancellable)
        ),
    )

    adapter.ensure_core(PROFILE_ID, 7, deadline=time.monotonic() + 5)

    phases = [phase for phase, _progress, _cancellable in observed]
    assert phases == [
        "remote_preflight",
        "transferring",
        "transferring",
        "transferring",
        "verifying",
        "starting_daemon",
        "waiting_for_daemon",
    ]
    indices = [local_v2.LIFECYCLE_PHASES.index(phase) for phase in phases]
    assert indices == sorted(indices)
    transfers = [progress for phase, progress, _cancellable in observed if phase == "transferring"]
    assert transfers[0].completed == 0
    assert 0 < transfers[1].completed < transfers[1].total
    assert transfers[2].completed == transfers[2].total
    assert len({progress.total for progress in transfers}) == 1
    assert [cancellable for _phase, _progress, cancellable in observed] == [
        True,
        True,
        True,
        True,
        True,
        False,
        False,
    ]


def test_adapter_enters_noncancellable_phase_before_replacing_daemon(
    tmp_path: Path,
) -> None:
    old = DaemonBundleServicePredecessor(
        state="running",
        generation="8" * 32,
        release_identity="9" * 64,
        bundle_sha256="a" * 64,
        canonical_manifest_sha256="b" * 64,
        lifecycle_compatibility=6,
    )
    transport = FakeCoreTransport()
    transport.observation_results = [
        old,
        DaemonBundleServicePredecessor(state="absent"),
    ]

    def observe(phase: str, _progress: object, cancellable: bool) -> None:
        transport.operation_order.append(f"progress:{phase}:{cancellable}")

    adapter = DesktopCoreSshBridgeAdapterV2(
        _Lifecycle(transport),
        _bootstrap(tmp_path, replace_mismatched=True),
        progress_observer=observe,
    )

    adapter.ensure_core(PROFILE_ID, 7, deadline=time.monotonic() + 5)

    assert transport.operation_order.index("progress:starting_daemon:False") < (
        transport.operation_order.index("daemon_stop")
    )
    assert transport.operation_order.index("progress:waiting_for_daemon:False") < (
        transport.operation_order.index("daemon_start")
    )


def test_adapter_cancellation_stops_managed_runtime_stream_before_daemon_start(
    tmp_path: Path,
) -> None:
    transport = FakeCoreTransport()
    transport.managed_runtime_block = True
    adapter = DesktopCoreSshBridgeAdapterV2(_Lifecycle(transport), _bootstrap(tmp_path))
    cancel_event = threading.Event()
    result: list[object] = []

    def ensure() -> None:
        try:
            result.append(
                adapter.ensure_core(
                    PROFILE_ID,
                    7,
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
    assert isinstance(result[0], DesktopCoreBridgeErrorV2)
    assert result[0].error.code == "core_activation_cancelled"
    assert transport.stage_calls[0]["cancel_event"] is cancel_event
    assert transport.managed_runtime_calls[0]["cancel_event"] is cancel_event
    assert "daemon_start" not in transport.operation_order


def test_adapter_rejects_stale_profile_generation_before_remote_work(
    tmp_path: Path,
) -> None:
    transport = FakeCoreTransport()
    adapter = DesktopCoreSshBridgeAdapterV2(_Lifecycle(transport), _bootstrap(tmp_path))

    with pytest.raises(DesktopCoreBridgeErrorV2) as caught:
        adapter.ensure_core(PROFILE_ID, 6, deadline=time.monotonic() + 5)

    assert caught.value.error.code == "core_profile_not_connected"
    assert not transport.operation_order


def test_adapter_classifies_daemon_stage_follower_failure_as_retryable_connection(
    tmp_path: Path,
) -> None:
    transport = FakeCoreTransport()
    transport.stage_error = SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)
    adapter = DesktopCoreSshBridgeAdapterV2(_Lifecycle(transport), _bootstrap(tmp_path))

    with pytest.raises(DesktopCoreBridgeErrorV2) as caught:
        adapter.ensure_core(PROFILE_ID, 7, deadline=time.monotonic() + 5)

    assert caught.value.error.code == "core_connection_failed"
    assert caught.value.status_code == 503
    assert caught.value.error.retryable is True
    assert caught.value.error.action == "reconnect"
    assert caught.value.error.affected_resource_id == PROFILE_ID


def test_ssh_error_preserves_nonretryable_daemon_update_required() -> None:
    error = _ssh_error(
        SshTransportError(SshTransportErrorCode.DAEMON_UPDATE_REQUIRED),
        affected_resource_id=PROFILE_ID,
    )

    assert error.error.code == "daemon_update_required"
    assert error.status_code == 409
    assert error.error.retryable is False
    assert error.error.action == "install_repair_daemon"
    assert error.error.affected_resource_id == PROFILE_ID


def test_adapter_opens_only_verified_tunnel_transport_and_closes_owned_session(
    tmp_path: Path,
    verified_tunnel_auth: None,
) -> None:
    transport = FakeCoreTransport()
    adapter = DesktopCoreSshBridgeAdapterV2(_Lifecycle(transport), _bootstrap(tmp_path))
    attachment = adapter.ensure_core(PROFILE_ID, 7, deadline=time.monotonic() + 5)
    tunnel = adapter.open_tunnel(
        profile_id=PROFILE_ID,
        profile_connection_generation=7,
        remote_port=attachment.remote_port,
        session_id="session-1",
        deadline=time.monotonic() + 5,
    )

    http_transport = adapter.new_http_transport()
    assert type(http_transport) is VerifiedCoreHttpTransportV2
    assert tunnel.endpoint == "http://127.0.0.1:1"
    assert transport.tunnel_kwargs == {
        "remote_port": 43117,
        "remote_host": "127.0.0.1",
        "wait_for_ready": True,
        "timeout_seconds": pytest.approx(5, abs=0.2),
    }
    with pytest.raises(httpx.LocalProtocolError):
        http_transport.handle_request(httpx.Request("GET", "http://example.test/v2/tasks"))
    http_transport.close()
    tunnel.close(deadline=time.monotonic() + 5)
    assert transport.tunnel.close_calls == 1
    with pytest.raises(DesktopCoreBridgeErrorV2):
        adapter.new_http_transport()


def test_adapter_classifies_verified_tunnel_ssh_failure_as_retryable_connection(
    tmp_path: Path,
    verified_tunnel_auth: None,
) -> None:
    transport = FakeCoreTransport()
    adapter = DesktopCoreSshBridgeAdapterV2(_Lifecycle(transport), _bootstrap(tmp_path))
    attachment = adapter.ensure_core(PROFILE_ID, 7, deadline=time.monotonic() + 5)
    tunnel = adapter.open_tunnel(
        profile_id=PROFILE_ID,
        profile_connection_generation=7,
        remote_port=attachment.remote_port,
        session_id="session-ssh-failure",
        deadline=time.monotonic() + 5,
    )

    def fail_authority_check() -> None:
        raise SshTransportError(SshTransportErrorCode.CONNECTION_FAILED)

    transport.tunnel.verify_authority = fail_authority_check  # type: ignore[method-assign]

    with pytest.raises(DesktopCoreBridgeErrorV2) as caught:
        adapter.new_http_transport()

    assert caught.value.error.code == "core_connection_failed"
    assert caught.value.error.retryable is True
    assert caught.value.error.action == "reconnect"
    assert caught.value.error.affected_resource_id == PROFILE_ID
    tunnel.close(deadline=time.monotonic() + 5)


def test_adapter_retries_one_retryable_verified_tunnel_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeCoreTransport()
    lifecycle = _Lifecycle(transport)
    adapter = DesktopCoreSshBridgeAdapterV2(lifecycle, _bootstrap(tmp_path))
    attachment = adapter.ensure_core(PROFILE_ID, 7, deadline=time.monotonic() + 5)
    attempts: list[float] = []

    def open_tunnel_once_then_succeed(
        _attachment: object,
        active_transport: object,
        *,
        timeout_seconds: float,
    ) -> object:
        assert active_transport is transport
        attempts.append(timeout_seconds)
        if len(attempts) == 1:
            raise core_control.CoreControlBootstrapError(
                core_control.CoreControlBootstrapErrorCode.SERVICE_FAILED,
                "transient mux follower failure",
                retryable=True,
            )
        return transport.tunnel

    monkeypatch.setattr(
        "desktop.sidecar.core_bridge_adapters_v2.open_core_control_tunnel",
        open_tunnel_once_then_succeed,
    )

    tunnel = adapter.open_tunnel(
        profile_id=PROFILE_ID,
        profile_connection_generation=7,
        remote_port=attachment.remote_port,
        session_id="session-retry",
        deadline=time.monotonic() + 5,
    )

    assert len(attempts) == 2
    assert all(0 < timeout <= 5 for timeout in attempts)
    tunnel.close(deadline=time.monotonic() + 5)
    assert transport.tunnel.close_calls == 1


def test_adapter_retry_fails_closed_if_system_ssh_transport_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeCoreTransport()
    lifecycle = _Lifecycle(transport)
    adapter = DesktopCoreSshBridgeAdapterV2(lifecycle, _bootstrap(tmp_path))
    attachment = adapter.ensure_core(PROFILE_ID, 7, deadline=time.monotonic() + 5)
    attempts = 0

    def fail_first_tunnel(
        _attachment: object,
        active_transport: object,
        *,
        timeout_seconds: float,
    ) -> object:
        nonlocal attempts
        del active_transport, timeout_seconds
        attempts += 1
        lifecycle.transport = FakeCoreTransport()
        raise core_control.CoreControlBootstrapError(
            core_control.CoreControlBootstrapErrorCode.SERVICE_FAILED,
            "transient mux follower failure",
            retryable=True,
        )

    monkeypatch.setattr(
        "desktop.sidecar.core_bridge_adapters_v2.open_core_control_tunnel",
        fail_first_tunnel,
    )

    with pytest.raises(DesktopCoreBridgeErrorV2) as caught:
        adapter.open_tunnel(
            profile_id=PROFILE_ID,
            profile_connection_generation=7,
            remote_port=attachment.remote_port,
            session_id="session-transport-changed",
            deadline=time.monotonic() + 5,
        )

    assert caught.value.error.code == "core_ssh_transport_identity_changed"
    assert attempts == 1


def test_adapter_does_not_retry_non_retryable_tunnel_identity_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = FakeCoreTransport()
    adapter = DesktopCoreSshBridgeAdapterV2(_Lifecycle(transport), _bootstrap(tmp_path))
    attachment = adapter.ensure_core(PROFILE_ID, 7, deadline=time.monotonic() + 5)
    attempts = 0

    def reject_tunnel_identity(
        _attachment: object,
        active_transport: object,
        *,
        timeout_seconds: float,
    ) -> object:
        nonlocal attempts
        del active_transport, timeout_seconds
        attempts += 1
        raise core_control.CoreControlBootstrapError(
            core_control.CoreControlBootstrapErrorCode.RESPONSE_INVALID,
            "verified tunnel identity mismatch",
            retryable=False,
        )

    monkeypatch.setattr(
        "desktop.sidecar.core_bridge_adapters_v2.open_core_control_tunnel",
        reject_tunnel_identity,
    )

    with pytest.raises(DesktopCoreBridgeErrorV2) as caught:
        adapter.open_tunnel(
            profile_id=PROFILE_ID,
            profile_connection_generation=7,
            remote_port=attachment.remote_port,
            session_id="session-identity-mismatch",
            deadline=time.monotonic() + 5,
        )

    assert caught.value.error.code == "core_tunnel_open_failed"
    assert attempts == 1


def test_adapter_source_has_no_v1_contract_or_shared_backend_fallback() -> None:
    source = Path("desktop/sidecar/core_bridge_adapters_v2.py").read_text(encoding="utf-8")
    assert "contracts.v1" not in source
    assert "core_bridge_v1" not in source
    assert "backend_url" not in source
    assert "shared_backend" not in source
    assert json.dumps({"generation": GENERATION}) not in source
