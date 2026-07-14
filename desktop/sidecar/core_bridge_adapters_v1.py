"""Production SSH and workspace adapters for the Desktop/Core bridge v1."""

from __future__ import annotations

from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import hmac
import http.client
import json
import math
import os
import secrets
import socket
import stat
import threading
import time
from typing import BinaryIO, Callable, Iterator, Protocol, cast

import httpx

from desktop.sidecar.contracts.v1.models import WorkspaceImportRefV1
from desktop.sidecar.core_bridge_v1 import (
    CoreHostAttachmentV1,
    CoreTunnelHandleV1,
    DesktopCoreBridgeErrorV1,
)
from desktop.sidecar.remote_lifecycle import (
    DesktopRemoteLifecycle,
    RemoteConnectionFailedError,
)
from desktop.sidecar.workspace_imports import (
    WorkspaceImportError,
    WorkspaceImportIntegrityError,
    WorkspaceImportNotFoundError,
    WorkspaceImportOwnership,
    WorkspaceImportStore,
)
from openevo.backend.contracts.v1 import models as core_v1
from openevo.deployment.core_control import (
    CoreControlBootstrapError,
    CoreControlBootstrapErrorCode,
    RemoteCoreControlAttachment,
    VerifiedCoreControlTunnel,
    build_core_control_bootstrap_plan,
    execute_core_control_bootstrap,
    open_core_control_tunnel,
)
from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.ssh import SshTransportError, SshTransportErrorCode


_HOST_IDENTITY_DOMAIN = b"openevo-desktop-core-host-identity-v1\0"
_MAX_REMOTE_OPERATION_SECONDS = 300.0
_MIN_BOOTSTRAP_SECONDS = 1.0


class _CoreTunnelEndpoint(Protocol):
    @property
    def base_url(self) -> str: ...

    def verify_authority(self) -> None: ...

    def open_verified_socket(self, *, timeout_seconds: float) -> socket.socket: ...

    def close(self) -> None: ...


class _CoreSshTransport(Protocol):
    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult: ...

    def run_secret(self, command: str, *, timeout_seconds: float = 30.0) -> object: ...

    def open_core_tunnel(
        self,
        *,
        remote_port: int,
        remote_host: str = "127.0.0.1",
        wait_for_ready: bool = True,
        timeout_seconds: float = 10.0,
    ) -> _CoreTunnelEndpoint: ...


@dataclass(frozen=True, slots=True)
class CoreBootstrapConfigV1:
    """Closed remote install inputs supplied by the future release composition."""

    source_commit: str
    remote_port: int = 0
    replace_mismatched: bool = False
    wheel_path: str = field(repr=False, compare=True, default="")
    framework_lock_path: str = field(repr=False, compare=True, default="")
    service_root: str = field(repr=False, compare=True, default="")

    def __post_init__(self) -> None:
        if type(self.replace_mismatched) is not bool:
            raise TypeError("replace_mismatched must be a boolean")
        build_core_control_bootstrap_plan(
            wheel_path=self.wheel_path,
            framework_lock=self.framework_lock_path,
            service_root=self.service_root,
            source_commit=self.source_commit,
            port=self.remote_port,
            deadline_seconds=_MIN_BOOTSTRAP_SECONDS,
            replace_mismatched=self.replace_mismatched,
        )


@dataclass(frozen=True, slots=True, repr=False)
class _AttachmentAuthority:
    profile_id: str
    transport: object
    attachment: RemoteCoreControlAttachment
    bearer_identity: str
    generation: int


@dataclass(frozen=True, slots=True, repr=False)
class _ActiveTunnelAuthority:
    session_id: str
    authority: _AttachmentAuthority
    tunnel: VerifiedCoreControlTunnel


class _VerifiedEndpointHTTPConnection(http.client.HTTPConnection):
    def __init__(
        self,
        endpoint: VerifiedCoreControlTunnel,
        *,
        timeout: float,
    ) -> None:
        super().__init__("openevo-core.local", timeout=timeout)
        self._endpoint = endpoint

    def connect(self) -> None:
        timeout = self.timeout if isinstance(self.timeout, (int, float)) else 30.0
        self.sock = self._endpoint.open_verified_socket(timeout_seconds=timeout)


class _VerifiedResponseStream(httpx.SyncByteStream):
    def __init__(
        self,
        response: http.client.HTTPResponse,
        connection: _VerifiedEndpointHTTPConnection,
        endpoint: VerifiedCoreControlTunnel,
        release: Callable[[_VerifiedEndpointHTTPConnection], None],
    ) -> None:
        self._response = response
        self._connection = connection
        self._endpoint = endpoint
        self._release = release
        self._closed = False
        self._lock = threading.Lock()

    def __iter__(self) -> Iterator[bytes]:
        try:
            while True:
                chunk = self._response.read(64 * 1024)
                self._endpoint.verify_authority()
                if not chunk:
                    return
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._response.close()
        finally:
            try:
                self._connection.close()
            finally:
                self._release(self._connection)


class VerifiedCoreHttpTransportV1(httpx.BaseTransport):
    """HTTPX transport whose every connection is opened by a verified SSH tunnel."""

    def __init__(self, endpoint: VerifiedCoreControlTunnel) -> None:
        self._endpoint = endpoint
        self._lock = threading.Lock()
        self._closed = False
        self._connections: set[_VerifiedEndpointHTTPConnection] = set()

    def __repr__(self) -> str:
        return "VerifiedCoreHttpTransportV1(<private>)"

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        timeout = _request_timeout(request)
        connection = _VerifiedEndpointHTTPConnection(self._endpoint, timeout=timeout)
        with self._lock:
            if self._closed:
                raise httpx.ConnectError("Core tunnel transport is closed.", request=request)
            self._connections.add(connection)
        handed_off = False
        try:
            target = request.url.raw_path.decode("ascii")
            headers = {key: value for key, value in request.headers.multi_items()}
            body = (
                None
                if request.method in {"GET", "HEAD"}
                and "content-length" not in request.headers
                and "transfer-encoding" not in request.headers
                else request.stream
            )
            connection.request(
                request.method,
                target,
                body=body,
                headers=headers,
            )
            response = connection.getresponse()
            self._endpoint.verify_authority()
            stream = _VerifiedResponseStream(
                response,
                connection,
                self._endpoint,
                self._release_connection,
            )
            handed_off = True
            return httpx.Response(
                status_code=response.status,
                headers=response.getheaders(),
                stream=stream,
                extensions={"reason_phrase": (response.reason or "").encode("latin-1")},
                request=request,
            )
        except (OSError, UnicodeError, ValueError, http.client.HTTPException) as exc:
            raise httpx.ConnectError("Core tunnel request failed.", request=request) from exc
        finally:
            if not handed_off:
                connection.close()
                self._release_connection(connection)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.close()
            finally:
                self._release_connection(connection)

    def _release_connection(self, connection: _VerifiedEndpointHTTPConnection) -> None:
        with self._lock:
            self._connections.discard(connection)


class DesktopCoreSshBridgeAdapterV1:
    """Real Core host and loopback tunnel adapters over one active SSH lifecycle."""

    def __init__(
        self,
        lifecycle: DesktopRemoteLifecycle,
        bootstrap: CoreBootstrapConfigV1,
    ) -> None:
        if not isinstance(lifecycle, DesktopRemoteLifecycle):
            raise TypeError("lifecycle must be DesktopRemoteLifecycle")
        if not isinstance(bootstrap, CoreBootstrapConfigV1):
            raise TypeError("bootstrap must be CoreBootstrapConfigV1")
        self._lifecycle = lifecycle
        self._bootstrap = bootstrap
        self._lock = threading.Lock()
        self._generation = 0
        self._authority: _AttachmentAuthority | None = None
        self._active_tunnel: _ActiveTunnelAuthority | None = None

    def __repr__(self) -> str:
        return "DesktopCoreSshBridgeAdapterV1(<private>)"

    def ensure_core(self, profile_id: str, *, deadline: float) -> CoreHostAttachmentV1:
        transport = self._active_transport(profile_id, require_tunnel=False)
        remaining = _remaining(deadline, minimum=_MIN_BOOTSTRAP_SECONDS)
        plan = build_core_control_bootstrap_plan(
            wheel_path=self._bootstrap.wheel_path,
            framework_lock=self._bootstrap.framework_lock_path,
            service_root=self._bootstrap.service_root,
            source_commit=self._bootstrap.source_commit,
            port=self._bootstrap.remote_port,
            deadline_seconds=min(remaining, _MAX_REMOTE_OPERATION_SECONDS),
            replace_mismatched=self._bootstrap.replace_mismatched,
        )
        try:
            remote = execute_core_control_bootstrap(plan, cast(_CoreSshTransport, transport))
        except CoreControlBootstrapError as exc:
            raise _bootstrap_error(exc) from None
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise _adapter_error(
                "core_bootstrap_failed",
                "OpenEvo Core could not be attached or started.",
                retryable=True,
            ) from None
        _remaining(deadline)
        self._require_same_transport(profile_id, transport)
        bearer_identity = _host_identity(profile_id, remote)
        with self._lock:
            self._generation += 1
            authority = _AttachmentAuthority(
                profile_id=profile_id,
                transport=transport,
                attachment=remote,
                bearer_identity=bearer_identity,
                generation=self._generation,
            )
            self._authority = authority
        return CoreHostAttachmentV1(
            profile_id=profile_id,
            remote_port=remote.remote_port,
            bearer_token=remote.bearer_token,
            bearer_identity=bearer_identity,
        )

    def open_tunnel(
        self,
        *,
        profile_id: str,
        remote_port: int,
        session_id: str,
        deadline: float,
    ) -> CoreTunnelHandleV1:
        authority = self._tunnel_authority(profile_id, remote_port)
        transport = self._active_transport(profile_id, require_tunnel=True)
        if transport is not authority.transport:
            raise _transport_changed_error()
        remaining = _remaining(deadline)
        verified: VerifiedCoreControlTunnel | None = None
        try:
            verified = open_core_control_tunnel(
                authority.attachment,
                cast(_CoreSshTransport, transport),
                timeout_seconds=min(remaining, 60.0),
            )
            _remaining(deadline)
            self._require_same_transport(profile_id, transport)
            with self._lock:
                if self._authority is not authority:
                    raise _transport_changed_error()
                if self._active_tunnel is not None:
                    raise _adapter_error(
                        "core_tunnel_already_active",
                        "Another private Core tunnel is still active.",
                        status=409,
                        retryable=True,
                    )
                active = _ActiveTunnelAuthority(
                    session_id=session_id,
                    authority=authority,
                    tunnel=verified,
                )
                self._active_tunnel = active
            return CoreTunnelHandleV1(
                endpoint="http://127.0.0.1:1",
                session_id=session_id,
                close_callback=lambda: self._close_active_tunnel(active),
            )
        except DesktopCoreBridgeErrorV1:
            if verified is not None:
                _close_unpublished_tunnel(verified)
            raise
        except SshTransportError as exc:
            if verified is not None:
                _close_unpublished_tunnel(verified)
            raise _ssh_tunnel_error(exc) from None
        except CoreControlBootstrapError as exc:
            if verified is not None:
                _close_unpublished_tunnel(verified)
            raise _core_tunnel_error(exc) from None
        except BaseException as exc:
            if verified is not None:
                _close_unpublished_tunnel(verified)
            if not isinstance(exc, Exception):
                raise
            raise _adapter_error(
                "core_tunnel_open_failed",
                "The private Core tunnel could not be opened.",
                retryable=True,
            ) from None

    def new_http_transport(self) -> httpx.BaseTransport:
        """Create one Core client transport for the currently published tunnel."""

        with self._lock:
            active = self._active_tunnel
        if active is None:
            raise _adapter_error(
                "core_tunnel_not_active",
                "No verified private Core tunnel is active.",
                status=409,
                retryable=True,
            )
        self._require_same_transport(active.authority.profile_id, active.authority.transport)
        active.tunnel.verify_authority()
        return VerifiedCoreHttpTransportV1(active.tunnel)

    def _close_active_tunnel(self, active: _ActiveTunnelAuthority) -> None:
        active.tunnel.close()
        with self._lock:
            if self._active_tunnel is active:
                self._active_tunnel = None

    def _tunnel_authority(self, profile_id: str, remote_port: int) -> _AttachmentAuthority:
        if not isinstance(profile_id, str) or not profile_id:
            raise _adapter_error(
                "core_profile_not_connected",
                "The requested remote profile is not connected.",
                status=409,
            )
        if type(remote_port) is not int or not 1 <= remote_port <= 65_535:
            raise _adapter_error(
                "core_attachment_identity_mismatch",
                "The Core attachment no longer matches the active remote service.",
                status=409,
            )
        with self._lock:
            authority = self._authority
        if (
            authority is None
            or authority.profile_id != profile_id
            or authority.attachment.remote_port != remote_port
        ):
            raise _adapter_error(
                "core_attachment_identity_mismatch",
                "The Core attachment no longer matches the active remote service.",
                status=409,
            )
        return authority

    def _active_transport(self, profile_id: str, *, require_tunnel: bool) -> object:
        if not isinstance(profile_id, str) or not profile_id:
            raise _adapter_error(
                "core_profile_not_connected",
                "The requested remote profile is not connected.",
                status=409,
            )
        try:
            transport = self._lifecycle.active_transport(profile_id)
        except RemoteConnectionFailedError:
            raise _adapter_error(
                "core_profile_not_connected",
                "The requested remote profile is not connected.",
                status=409,
                retryable=True,
            ) from None
        required = ("run", "run_secret") + (("open_core_tunnel",) if require_tunnel else ())
        if any(not callable(getattr(transport, name, None)) for name in required):
            raise _adapter_error(
                "core_ssh_transport_incompatible",
                "The active SSH transport cannot operate OpenEvo Core.",
            )
        return transport

    def _require_same_transport(self, profile_id: str, expected: object) -> None:
        if self._active_transport(profile_id, require_tunnel=False) is not expected:
            raise _transport_changed_error()


@dataclass(frozen=True, slots=True)
class AdoptedWorkspaceImportV1:
    """Exact private ownership authority for one already adopted import."""

    import_ref: WorkspaceImportRefV1
    ownership: WorkspaceImportOwnership

    def __post_init__(self) -> None:
        if not isinstance(self.import_ref, WorkspaceImportRefV1):
            raise TypeError("import_ref must be WorkspaceImportRefV1")
        if not isinstance(self.ownership, WorkspaceImportOwnership):
            raise TypeError("ownership must be WorkspaceImportOwnership")


class AdoptedWorkspaceArchiveSourceV1:
    """Resolve only frozen adopted import authorities through their owning store."""

    def __init__(
        self,
        store: WorkspaceImportStore,
        bindings: tuple[AdoptedWorkspaceImportV1, ...],
    ) -> None:
        if not isinstance(store, WorkspaceImportStore):
            raise TypeError("store must be WorkspaceImportStore")
        if not isinstance(bindings, tuple) or any(
            not isinstance(binding, AdoptedWorkspaceImportV1) for binding in bindings
        ):
            raise TypeError("bindings must be a tuple of AdoptedWorkspaceImportV1")
        by_id: dict[str, AdoptedWorkspaceImportV1] = {}
        for binding in bindings:
            import_id = binding.import_ref.import_id
            if import_id in by_id:
                raise ValueError("workspace import bindings must have unique import IDs")
            by_id[import_id] = binding
        self._store = store
        self._bindings = by_id

    def __repr__(self) -> str:
        return "AdoptedWorkspaceArchiveSourceV1(<private>)"

    def open_archive(self, ref: WorkspaceImportRefV1) -> AbstractContextManager[BinaryIO]:
        if not isinstance(ref, WorkspaceImportRefV1):
            raise _workspace_authority_error()
        binding = self._bindings.get(ref.import_id)
        if binding is None or binding.import_ref != ref:
            raise _workspace_authority_error()
        return self._resolve(binding)

    @contextmanager
    def _resolve(self, binding: AdoptedWorkspaceImportV1) -> Iterator[BinaryIO]:
        try:
            with self._store.resolve(
                binding.import_ref,
                ownership=binding.ownership,
            ) as stream:
                _validate_archive_stream(stream)
                yield stream
        except DesktopCoreBridgeErrorV1:
            raise
        except WorkspaceImportNotFoundError:
            raise _adapter_error(
                "workspace_import_not_found",
                "The adopted workspace archive is no longer available.",
                status=409,
            ) from None
        except WorkspaceImportIntegrityError:
            raise _adapter_error(
                "workspace_import_integrity_failed",
                "The adopted workspace archive failed integrity verification.",
                status=409,
            ) from None
        except WorkspaceImportError:
            raise _adapter_error(
                "workspace_import_unavailable",
                "The adopted workspace archive could not be opened.",
                retryable=True,
            ) from None
        except (OSError, TypeError, ValueError):
            raise _adapter_error(
                "workspace_import_stream_invalid",
                "The adopted workspace archive stream is invalid.",
                status=409,
            ) from None


def _host_identity(profile_id: str, attachment: RemoteCoreControlAttachment) -> str:
    public_identity = json.dumps(
        {
            "capture_mode": attachment.capture_mode,
            "execution_mode": attachment.execution_mode,
            "generation": attachment.generation,
            "profile_id": profile_id,
            "registry_digest": attachment.registry_digest,
            "release_identity": attachment.release_identity,
            "remote_host": attachment.remote_host,
            "remote_port": attachment.remote_port,
            "source_commit": attachment.source_commit,
            "status_proof": attachment.status_proof,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    digest = hmac.new(
        attachment.bearer_token.encode("ascii"),
        _HOST_IDENTITY_DOMAIN + public_identity,
        hashlib.sha256,
    ).hexdigest()
    return f"core-host-v1-{digest}"


def _remaining(deadline: float, *, minimum: float = 0.0) -> float:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise _deadline_error()
    value = float(deadline)
    if not math.isfinite(value):
        raise _deadline_error()
    remaining = value - time.monotonic()
    if remaining <= minimum:
        raise _deadline_error()
    return remaining


def _validate_archive_stream(stream: BinaryIO) -> None:
    descriptor = stream.fileno()
    metadata = os.fstat(descriptor)
    flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    if (
        not isinstance(descriptor, int)
        or descriptor < 0
        or not stat.S_ISREG(metadata.st_mode)
        or flags & os.O_ACCMODE != os.O_RDONLY
        or not stream.readable()
        or stream.writable()
        or not isinstance(getattr(stream, "name", None), int)
    ):
        raise ValueError("workspace archive stream is not verified read-only storage")


def _close_unpublished_tunnel(tunnel: VerifiedCoreControlTunnel) -> None:
    try:
        tunnel.close()
    except BaseException:
        pass


def _request_timeout(request: httpx.Request) -> float:
    configured = request.extensions.get("timeout")
    if not isinstance(configured, dict):
        return 30.0
    candidates = [
        value
        for key in ("connect", "read", "write", "pool")
        if isinstance((value := configured.get(key)), (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and value > 0
    ]
    return min(float(value) for value in candidates) if candidates else 30.0


def _bootstrap_error(exc: CoreControlBootstrapError) -> DesktopCoreBridgeErrorV1:
    status = 503
    if exc.code is CoreControlBootstrapErrorCode.INVALID_PLAN:
        status = 500
    elif exc.code is CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED:
        status = 504
    elif exc.code is CoreControlBootstrapErrorCode.RESPONSE_INVALID:
        status = 502
    return _adapter_error(
        exc.code.value,
        str(exc),
        status=status,
        retryable=exc.retryable,
    )


def _ssh_tunnel_error(exc: SshTransportError) -> DesktopCoreBridgeErrorV1:
    if exc.code is SshTransportErrorCode.TIMEOUT:
        return _adapter_error(
            "core_tunnel_deadline_exceeded",
            "The private Core tunnel did not open before the deadline.",
            status=504,
            retryable=True,
        )
    if exc.code is SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED:
        return _adapter_error(
            "core_ssh_authority_invalid",
            "The active SSH host authority is no longer valid.",
            status=409,
        )
    if exc.code is SshTransportErrorCode.INVALID_REQUEST:
        return _adapter_error(
            "core_tunnel_request_invalid",
            "The private Core tunnel request is invalid.",
            status=500,
        )
    return _adapter_error(
        "core_tunnel_open_failed",
        "The private Core tunnel could not be opened.",
        retryable=True,
    )


def _core_tunnel_error(exc: CoreControlBootstrapError) -> DesktopCoreBridgeErrorV1:
    if exc.code is CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED:
        return _adapter_error(
            "core_tunnel_deadline_exceeded",
            "The private Core tunnel did not open before the deadline.",
            status=504,
            retryable=exc.retryable,
        )
    if exc.code is CoreControlBootstrapErrorCode.RESPONSE_INVALID:
        return _adapter_error(
            "core_tunnel_identity_mismatch",
            "The private Core tunnel did not match its authenticated attachment.",
            status=409,
        )
    return _adapter_error(
        "core_tunnel_open_failed",
        "The private Core tunnel could not be opened.",
        retryable=exc.retryable,
    )


def _deadline_error() -> DesktopCoreBridgeErrorV1:
    return _adapter_error(
        "core_bridge_adapter_deadline_exceeded",
        "The Desktop Core adapter deadline expired.",
        status=504,
        retryable=True,
    )


def _transport_changed_error() -> DesktopCoreBridgeErrorV1:
    return _adapter_error(
        "core_ssh_transport_identity_changed",
        "The active SSH connection changed during the Core operation.",
        status=409,
        retryable=True,
    )


def _workspace_authority_error() -> DesktopCoreBridgeErrorV1:
    return _adapter_error(
        "workspace_import_authority_mismatch",
        "The workspace import does not match an adopted ownership authority.",
        status=409,
    )


def _adapter_error(
    code: str,
    message: str,
    *,
    status: int = 503,
    retryable: bool = False,
) -> DesktopCoreBridgeErrorV1:
    return DesktopCoreBridgeErrorV1(
        core_v1.ApiErrorV1(
            request_id=f"desktop-core-adapter-{secrets.token_hex(8)}",
            code=code,
            http_status=status,
            message=message,
            severity=core_v1.ErrorSeverity.BLOCKING,
            category=core_v1.ErrorCategory.SERVICE,
            retryable=retryable,
            repair_action=(
                core_v1.RepairAction.OPENEVO_CAN_RETRY
                if retryable
                else core_v1.RepairAction.UNSUPPORTED
            ),
            next_action=(
                "Retry after the active remote connection is ready."
                if retryable
                else "Reconnect the remote profile and activate the saved project."
            ),
        )
    )


__all__ = (
    "AdoptedWorkspaceArchiveSourceV1",
    "AdoptedWorkspaceImportV1",
    "CoreBootstrapConfigV1",
    "DesktopCoreSshBridgeAdapterV1",
    "VerifiedCoreHttpTransportV1",
)
