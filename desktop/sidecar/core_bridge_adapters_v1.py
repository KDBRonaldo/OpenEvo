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
from pathlib import Path
import re
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
from openevo.deployment.core_assets import (
    MAX_CORE_WHEEL_BYTES,
    MAX_FRAMEWORK_LOCK_BYTES,
    StagedCoreBootstrapAssets,
)
from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.core_runtime import CorePythonRuntimeAuthority
from openevo.deployment.ssh import SshTransportError, SshTransportErrorCode


_HOST_IDENTITY_DOMAIN = b"openevo-desktop-core-host-identity-v1\0"
_MAX_REMOTE_OPERATION_SECONDS = 300.0
_MIN_BOOTSTRAP_SECONDS = 1.0
_MAX_HTTP_IO_SECONDS = 60.0
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_WHEEL_FILENAME_PATTERN = re.compile(r"[A-Za-z0-9_.+-]+\.whl\Z")
_BOOTSTRAP_BUNDLE_DOMAIN = b"openevo-desktop-core-bootstrap-assets-v1\0"


class _CoreTunnelEndpoint(Protocol):
    @property
    def base_url(self) -> str: ...

    def verify_authority(self) -> None: ...

    def open_verified_socket(self, *, timeout_seconds: float) -> socket.socket: ...

    def close(self) -> None: ...


class _CoreSshTransport(Protocol):
    def close(self) -> None: ...

    def select_core_python_runtime(
        self, *, timeout_seconds: float
    ) -> CorePythonRuntimeAuthority: ...

    def stage_core_bootstrap_assets(
        self,
        *,
        runtime: CorePythonRuntimeAuthority,
        wheel_path: str,
        wheel_sha256: str,
        wheel_size: int,
        framework_lock_path: str,
        framework_lock_sha256: str,
        framework_lock_size: int,
        bundle_id: str,
        timeout_seconds: float,
    ) -> StagedCoreBootstrapAssets: ...

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


@dataclass(frozen=True, slots=True, repr=False)
class SealedCoreBootstrapAssetV1:
    """Composition-supplied identity for one immutable local release asset."""

    local_path: str = field(repr=False)
    sha256: str
    byte_size: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.local_path, str)
            or not Path(self.local_path).is_absolute()
            or not _is_canonical_local_path(self.local_path)
            or not isinstance(self.sha256, str)
            or _DIGEST_PATTERN.fullmatch(self.sha256) is None
            or type(self.byte_size) is not int
            or not 0 < self.byte_size <= MAX_CORE_WHEEL_BYTES
        ):
            raise ValueError("sealed Core bootstrap asset identity is invalid")

    def __repr__(self) -> str:
        return "SealedCoreBootstrapAssetV1(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class CoreBootstrapConfigV1:
    """Closed sealed install inputs supplied by the future release composition."""

    source_commit: str
    wheel: SealedCoreBootstrapAssetV1
    framework_lock: SealedCoreBootstrapAssetV1
    remote_port: int = 0
    replace_mismatched: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_commit, str)
            or _SOURCE_COMMIT_PATTERN.fullmatch(self.source_commit) is None
            or not isinstance(self.wheel, SealedCoreBootstrapAssetV1)
            or not isinstance(self.framework_lock, SealedCoreBootstrapAssetV1)
            or type(self.remote_port) is not int
            or not 0 <= self.remote_port <= 65_535
            or type(self.replace_mismatched) is not bool
            or _WHEEL_FILENAME_PATTERN.fullmatch(Path(self.wheel.local_path).name) is None
            or Path(self.framework_lock.local_path).name != "framework-lock.json"
            or self.framework_lock.byte_size > MAX_FRAMEWORK_LOCK_BYTES
        ):
            raise ValueError("Core bootstrap configuration is invalid")

    def __repr__(self) -> str:
        return "CoreBootstrapConfigV1(<private>)"


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
        adopt_socket: Callable[[socket.socket], bool],
    ) -> None:
        super().__init__("openevo-core.local", timeout=timeout)
        self._endpoint = endpoint
        self._adopt_socket = adopt_socket
        self._socket_guard = threading.Lock()
        self._cancelled = False
        self._opened_socket: socket.socket | None = None

    def connect(self) -> None:
        timeout = self.timeout if isinstance(self.timeout, (int, float)) else 30.0
        opened = self._endpoint.open_verified_socket(timeout_seconds=timeout)
        with self._socket_guard:
            if self._cancelled or not self._adopt_socket(opened):
                try:
                    opened.close()
                finally:
                    raise OSError("Core tunnel request was cancelled")
            self.sock = opened
            self._opened_socket = opened

    def cancel(self) -> None:
        with self._socket_guard:
            self._cancelled = True
            opened = self._opened_socket
            self._opened_socket = None
        if opened is not None:
            try:
                opened.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                opened.close()
            except OSError:
                pass
        with self._socket_guard:
            super().close()

    def close(self) -> None:
        with self._socket_guard:
            super().close()


class _VerifiedResponseStream(httpx.SyncByteStream):
    def __init__(
        self,
        response: http.client.HTTPResponse,
        connection: _VerifiedEndpointHTTPConnection,
        endpoint: VerifiedCoreControlTunnel,
        request: httpx.Request,
        generation_is_active: Callable[[], bool],
        release: Callable[[_VerifiedResponseStream], None],
    ) -> None:
        self._response = response
        self._connection = connection
        self._endpoint = endpoint
        self._request = request
        self._generation_is_active = generation_is_active
        self._release = release
        self._closed = False
        self._condition = threading.Condition()
        self._active_reads = 0

    def __iter__(self) -> Iterator[bytes]:
        try:
            while True:
                with self._condition:
                    if self._closed:
                        return
                    self._active_reads += 1
                try:
                    chunk = self._response.read1(64 * 1024)
                    self._endpoint.verify_authority()
                    if not self._generation_is_active():
                        return
                finally:
                    with self._condition:
                        self._active_reads -= 1
                        self._condition.notify_all()
                if not chunk:
                    return
                yield chunk
        except httpx.HTTPError:
            raise
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise httpx.ReadError(
                "Core tunnel response failed.",
                request=self._request,
            ) from None
        finally:
            self.close()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
        self._connection.cancel()
        try:
            self._response.close()
        except Exception:
            pass
        finally:
            with self._condition:
                while self._active_reads:
                    self._condition.wait()
            self._release(self)


class VerifiedCoreHttpTransportV1(httpx.BaseTransport):
    """HTTPX transport whose every connection is opened by a verified SSH tunnel."""

    def __init__(self, endpoint: VerifiedCoreControlTunnel) -> None:
        self._endpoint = endpoint
        self._condition = threading.Condition()
        self._closed = False
        self._generation = 0
        self._inflight = 0
        self._connections: set[_VerifiedEndpointHTTPConnection] = set()
        self._streams: set[_VerifiedResponseStream] = set()

    def __repr__(self) -> str:
        return "VerifiedCoreHttpTransportV1(<private>)"

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        timeout = _request_timeout(request)
        generation = -1
        connection: _VerifiedEndpointHTTPConnection
        connection = _VerifiedEndpointHTTPConnection(
            self._endpoint,
            timeout=timeout,
            adopt_socket=lambda opened: self._adopt_socket(
                connection,
                opened,
                generation,
            ),
        )
        with self._condition:
            if self._closed:
                raise httpx.ConnectError("Core tunnel transport is closed.", request=request)
            generation = self._generation
            self._inflight += 1
            self._connections.add(connection)
        handed_off = False
        response: http.client.HTTPResponse | None = None
        try:
            target = request.url.raw_path.decode("ascii")
            headers = {key: value for key, value in request.headers.multi_items()}
            body, encode_chunked = _request_body(request)
            connection.request(
                request.method,
                target,
                body=body,
                headers=headers,
                encode_chunked=encode_chunked,
            )
            response = connection.getresponse()
            self._endpoint.verify_authority()
            stream = _VerifiedResponseStream(
                response,
                connection,
                self._endpoint,
                request,
                lambda: self._generation_is_active(generation),
                self._release_stream,
            )
            with self._condition:
                if self._closed or generation != self._generation:
                    raise OSError("Core tunnel request was cancelled")
                self._streams.add(stream)
            handed_off = True
            return httpx.Response(
                status_code=response.status,
                headers=response.getheaders(),
                stream=stream,
                extensions={"reason_phrase": (response.reason or "").encode("latin-1")},
                request=request,
            )
        except httpx.HTTPError:
            raise
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise httpx.ConnectError("Core tunnel request failed.", request=request) from None
        finally:
            if not handed_off:
                connection.cancel()
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass
                self._release_request(connection, None)

    def close(self) -> None:
        with self._condition:
            if not self._closed:
                self._closed = True
                self._generation += 1
            connections = tuple(self._connections)
            streams = tuple(self._streams)
        for connection in connections:
            connection.cancel()
        for stream in streams:
            stream.close()
        with self._condition:
            while self._inflight:
                self._condition.wait()

    def _adopt_socket(
        self,
        connection: _VerifiedEndpointHTTPConnection,
        opened: socket.socket,
        generation: int,
    ) -> bool:
        del opened
        with self._condition:
            return (
                not self._closed
                and generation == self._generation
                and connection in self._connections
            )

    def _generation_is_active(self, generation: int) -> bool:
        with self._condition:
            return not self._closed and generation == self._generation

    def _release_stream(self, stream: _VerifiedResponseStream) -> None:
        self._release_request(stream._connection, stream)

    def _release_request(
        self,
        connection: _VerifiedEndpointHTTPConnection,
        stream: _VerifiedResponseStream | None,
    ) -> None:
        with self._condition:
            was_registered = connection in self._connections
            self._connections.discard(connection)
            if stream is not None:
                self._streams.discard(stream)
            if was_registered:
                self._inflight -= 1
                self._condition.notify_all()


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
        transport = self._active_transport(
            profile_id,
            require_tunnel=False,
            require_asset_stage=True,
            require_runtime_preflight=True,
        )
        remaining = _remaining(deadline, minimum=_MIN_BOOTSTRAP_SECONDS)
        try:
            runtime = cast(_CoreSshTransport, transport).select_core_python_runtime(
                timeout_seconds=min(remaining, _MAX_REMOTE_OPERATION_SECONDS)
            )
        except SshTransportError as exc:
            raise _ssh_runtime_preflight_error(exc) from None
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise _adapter_error(
                "core_supervisor_runtime_preflight_failed",
                "The remote Core supervisor runtime could not be verified.",
                retryable=True,
            ) from None
        _remaining(deadline, minimum=_MIN_BOOTSTRAP_SECONDS)
        self._require_same_transport(profile_id, transport)
        remaining = _remaining(deadline, minimum=_MIN_BOOTSTRAP_SECONDS)
        bundle_id = _bootstrap_bundle_id(self._bootstrap)
        try:
            staged = cast(_CoreSshTransport, transport).stage_core_bootstrap_assets(
                runtime=runtime,
                wheel_path=self._bootstrap.wheel.local_path,
                wheel_sha256=self._bootstrap.wheel.sha256,
                wheel_size=self._bootstrap.wheel.byte_size,
                framework_lock_path=self._bootstrap.framework_lock.local_path,
                framework_lock_sha256=self._bootstrap.framework_lock.sha256,
                framework_lock_size=self._bootstrap.framework_lock.byte_size,
                bundle_id=bundle_id,
                timeout_seconds=min(remaining, _MAX_REMOTE_OPERATION_SECONDS),
            )
        except SshTransportError as exc:
            raise _ssh_asset_error(exc) from None
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise _adapter_error(
                "core_bootstrap_asset_upload_failed",
                "The sealed Core release assets could not be staged.",
                retryable=True,
            ) from None
        _remaining(deadline, minimum=_MIN_BOOTSTRAP_SECONDS)
        self._require_same_transport(profile_id, transport)
        _verify_staged_assets(self._bootstrap, staged, bundle_id=bundle_id)
        remaining = _remaining(deadline, minimum=_MIN_BOOTSTRAP_SECONDS)
        plan = build_core_control_bootstrap_plan(
            runtime=runtime,
            wheel_path=staged.wheel_path,
            framework_lock=staged.framework_lock_path,
            service_root=staged.service_root,
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
        try:
            active.tunnel.verify_authority()
        except SshTransportError as exc:
            raise _ssh_tunnel_error(exc) from None
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise _adapter_error(
                "core_tunnel_identity_mismatch",
                "The private Core tunnel no longer matches its authenticated attachment.",
                status=409,
            ) from None
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

    def _active_transport(
        self,
        profile_id: str,
        *,
        require_tunnel: bool,
        require_asset_stage: bool = False,
        require_runtime_preflight: bool = False,
    ) -> object:
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
        required = (
            ("run", "run_secret")
            + (("open_core_tunnel",) if require_tunnel else ())
            + (("stage_core_bootstrap_assets",) if require_asset_stage else ())
            + (("select_core_python_runtime",) if require_runtime_preflight else ())
        )
        if any(not callable(getattr(transport, name, None)) for name in required):
            raise _adapter_error(
                "core_ssh_transport_incompatible",
                "The active SSH transport cannot operate OpenEvo Core.",
            )
        return transport

    def _require_same_transport(self, profile_id: str, expected: object) -> None:
        if (
            self._active_transport(
                profile_id,
                require_tunnel=False,
                require_asset_stage=False,
                require_runtime_preflight=False,
            )
            is not expected
        ):
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


def _is_canonical_local_path(path: str) -> bool:
    parts = path.split("/")
    return (
        len(parts) >= 2 and not parts[0] and all(part not in {"", ".", ".."} for part in parts[1:])
    )


def _bootstrap_bundle_id(config: CoreBootstrapConfigV1) -> str:
    identity = json.dumps(
        {
            "framework_lock": {
                "byte_size": config.framework_lock.byte_size,
                "sha256": config.framework_lock.sha256,
            },
            "schema_version": 1,
            "source_commit": config.source_commit,
            "wheel": {
                "byte_size": config.wheel.byte_size,
                "filename": Path(config.wheel.local_path).name,
                "sha256": config.wheel.sha256,
            },
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(_BOOTSTRAP_BUNDLE_DOMAIN + identity).hexdigest()


def _verify_staged_assets(
    config: CoreBootstrapConfigV1,
    staged: object,
    *,
    bundle_id: str,
) -> None:
    if not isinstance(staged, StagedCoreBootstrapAssets):
        raise _asset_identity_error()
    expected_root = f"{staged.service_root}/assets/{bundle_id}"
    if (
        staged.wheel_path != f"{expected_root}/{Path(config.wheel.local_path).name}"
        or staged.framework_lock_path != f"{expected_root}/framework-lock.json"
        or staged.wheel_sha256 != config.wheel.sha256
        or staged.framework_lock_sha256 != config.framework_lock.sha256
    ):
        raise _asset_identity_error()


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
    configured_timeout = min(float(value) for value in candidates) if candidates else 30.0
    return min(configured_timeout, _MAX_HTTP_IO_SECONDS)


def _request_body(
    request: httpx.Request,
) -> tuple[httpx.SyncByteStream | None, bool]:
    transfer_encoding = request.headers.get("transfer-encoding")
    content_length = request.headers.get("content-length")
    if transfer_encoding is not None:
        if transfer_encoding.strip().lower() != "chunked" or content_length is not None:
            raise httpx.LocalProtocolError(
                "Core tunnel request transfer framing is unsupported.",
                request=request,
            )
        return request.stream, True
    if content_length is not None:
        try:
            length = int(content_length, 10)
        except ValueError:
            length = -1
        if length < 0 or str(length) != content_length.strip():
            raise httpx.LocalProtocolError(
                "Core tunnel request content length is invalid.",
                request=request,
            )
        return request.stream, False
    if request.method in {"GET", "HEAD"}:
        return None, False
    raise httpx.LocalProtocolError(
        "Core tunnel request body length is unknown.",
        request=request,
    )


def _bootstrap_error(exc: CoreControlBootstrapError) -> DesktopCoreBridgeErrorV1:
    status = 503
    if exc.code is CoreControlBootstrapErrorCode.INVALID_PLAN:
        status = 500
    elif exc.code is CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED:
        status = 504
    elif exc.code is CoreControlBootstrapErrorCode.RESPONSE_INVALID:
        status = 502
    messages = {
        CoreControlBootstrapErrorCode.INVALID_PLAN: "Core bootstrap settings are invalid.",
        CoreControlBootstrapErrorCode.INSTALL_FAILED: (
            "The isolated OpenEvo Core generation could not be installed."
        ),
        CoreControlBootstrapErrorCode.VERIFICATION_FAILED: (
            "The isolated OpenEvo Core generation failed release verification."
        ),
        CoreControlBootstrapErrorCode.SERVICE_FAILED: (
            "OpenEvo Core could not be attached or started."
        ),
        CoreControlBootstrapErrorCode.RESPONSE_INVALID: (
            "The authenticated Core attachment response was invalid."
        ),
        CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED: (
            "Core bootstrap exceeded its total deadline."
        ),
    }
    return _adapter_error(
        exc.code.value,
        messages[exc.code],
        status=status,
        retryable=exc.retryable,
    )


def _ssh_asset_error(exc: SshTransportError) -> DesktopCoreBridgeErrorV1:
    if exc.code in {
        SshTransportErrorCode.CORE_ASSET_FAILED,
        SshTransportErrorCode.INVALID_REQUEST,
    }:
        return _asset_identity_error()
    if exc.code is SshTransportErrorCode.TIMEOUT:
        return _adapter_error(
            "core_bootstrap_asset_upload_deadline_exceeded",
            "The sealed Core release assets were not staged before the deadline.",
            status=504,
            retryable=True,
        )
    if exc.code is SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED:
        return _adapter_error(
            "core_ssh_authority_invalid",
            "The active SSH host authority is no longer valid.",
            status=409,
        )
    return _adapter_error(
        "core_bootstrap_asset_upload_failed",
        "The sealed Core release assets could not be staged.",
        retryable=True,
    )


def _ssh_runtime_preflight_error(exc: SshTransportError) -> DesktopCoreBridgeErrorV1:
    if exc.code is SshTransportErrorCode.CORE_PYTHON_UNAVAILABLE:
        return _adapter_error(
            "core_python_runtime_unavailable",
            "OpenEvo could not select a supported Python runtime on this server architecture.",
            status=409,
            next_action=(
                "Use a supported x86-64 or AArch64 Ubuntu server, then retry activation."
            ),
        )
    if exc.code is SshTransportErrorCode.CORE_PYTHON_PROVISION_FAILED:
        return _adapter_error(
            "core_python_runtime_provision_failed",
            "OpenEvo could not download, verify, or provision remote Python 3.11.",
            status=409,
            retryable=True,
            next_action=(
                "Check the configured HTTP and HTTPS proxy or server network access, "
                "then retry activation."
            ),
        )
    if exc.code is SshTransportErrorCode.CORE_KERNEL_SYSCALL_UNSUPPORTED:
        return _adapter_error(
            "core_supervisor_kernel_unsupported",
            "The remote Linux kernel does not provide the pidfd syscalls required by Core.",
            status=409,
            next_action="Use a Linux server whose kernel supports pidfd_open and pidfd_send_signal.",
        )
    if exc.code is SshTransportErrorCode.CORE_RUNTIME_UNSUPPORTED:
        return _adapter_error(
            "core_supervisor_runtime_unsupported",
            (
                "The remote host does not satisfy Core's Linux process identity "
                "and supervision prerequisites."
            ),
            status=409,
            next_action="Use a supported Linux GPU server and retry activation.",
        )
    if exc.code is SshTransportErrorCode.TIMEOUT:
        return _adapter_error(
            "core_supervisor_runtime_preflight_deadline_exceeded",
            "The remote Core supervisor runtime preflight exceeded the deadline.",
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
            "core_supervisor_runtime_preflight_invalid",
            "The remote Core supervisor runtime preflight request is invalid.",
            status=500,
        )
    return _adapter_error(
        "core_supervisor_runtime_preflight_failed",
        "The remote Core supervisor runtime could not be verified.",
        retryable=True,
    )


def _asset_identity_error() -> DesktopCoreBridgeErrorV1:
    return _adapter_error(
        "core_bootstrap_asset_invalid",
        "The sealed Core release assets failed exact identity verification.",
        status=409,
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
    next_action: str | None = None,
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
            next_action=next_action
            or (
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
    "SealedCoreBootstrapAssetV1",
    "VerifiedCoreHttpTransportV1",
)
