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
from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.ssh import SshTransportError, SshTransportErrorCode


PROFILE_ID = "profile-a"
SOURCE_COMMIT = "1" * 40
BEARER = "B" * 64
RELEASE_IDENTITY = "2" * 64
REGISTRY_DIGEST = "3" * 64
STATUS_PROOF = "4" * 64
GENERATION = "5" * 32
ARCHIVE = bytes(1024)
IMPORT_ID = "workspace-import-" + "6" * 48


def _bootstrap_config() -> CoreBootstrapConfigV1:
    return CoreBootstrapConfigV1(
        source_commit=SOURCE_COMMIT,
        wheel_path="/srv/openevo/releases/openevo.whl",
        framework_lock_path="/srv/openevo/releases/framework-lock.json",
        service_root="/srv/openevo/core",
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
        self.after_secret: Callable[[], None] | None = None

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
    transport: FakeCoreTransport | None = None,
) -> tuple[DesktopCoreSshBridgeAdapterV1, FakeLifecycle, FakeCoreTransport]:
    active = transport or FakeCoreTransport()
    lifecycle = FakeLifecycle(PROFILE_ID, active)
    return DesktopCoreSshBridgeAdapterV1(lifecycle, _bootstrap_config()), lifecycle, active


@pytest.fixture
def verified_tunnel_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        core_control,
        "authenticate_core_service_endpoint",
        lambda **_kwargs: STATUS_PROOF,
    )


def test_core_host_uses_real_bootstrap_secret_channel_and_exact_identity() -> None:
    adapter, _lifecycle, transport = _adapter()

    first = adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    second = adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)

    assert first == second
    assert first.profile_id == PROFILE_ID
    assert first.remote_port == 43117
    assert first.bearer_token == BEARER
    assert first.bearer_identity.startswith("core-host-v1-")
    assert first.bearer_identity != BEARER
    assert "python3 -I -c" in transport.commands[0]
    assert "consume-attachment" in transport.secret_commands[0]
    assert BEARER not in " ".join(transport.commands + transport.secret_commands)
    assert all(0 < timeout <= 5 for timeout in transport.timeouts)
    assert BEARER not in repr(first)
    assert BEARER not in repr(adapter)
    assert "/srv/openevo" not in repr(_bootstrap_config())


def test_core_host_rejects_cross_profile_and_transport_replacement() -> None:
    adapter, lifecycle, transport = _adapter()

    with pytest.raises(DesktopCoreBridgeErrorV1) as cross_profile:
        adapter.ensure_core("profile-b", deadline=time.monotonic() + 5)
    assert cross_profile.value.error.code == "core_profile_not_connected"
    assert transport.commands == []

    transport.after_secret = lambda: setattr(lifecycle, "transport", FakeCoreTransport())
    with pytest.raises(DesktopCoreBridgeErrorV1) as replaced:
        adapter.ensure_core(PROFILE_ID, deadline=time.monotonic() + 5)
    assert replaced.value.error.code == "core_ssh_transport_identity_changed"
    assert BEARER not in str(replaced.value)


def test_core_host_maps_bootstrap_timeout_and_expired_deadline() -> None:
    transport = FakeCoreTransport()
    transport.run_error = SshTransportError(SshTransportErrorCode.TIMEOUT)
    adapter, _lifecycle, _transport = _adapter(transport)

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
) -> None:
    del verified_tunnel_auth
    adapter, _lifecycle, transport = _adapter()
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
) -> None:
    del verified_tunnel_auth
    adapter, lifecycle, transport = _adapter()
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
) -> None:
    del verified_tunnel_auth
    adapter, _lifecycle, transport = _adapter()
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
) -> None:
    del verified_tunnel_auth
    adapter, _lifecycle, transport = _adapter()
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
