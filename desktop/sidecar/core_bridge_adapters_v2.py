"""Production system-SSH and verified-socket adapters for the Core v2 bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import http.client
import json
import math
from pathlib import Path
import re
import socket
import threading
import time
from typing import Callable, Iterator, Protocol, cast

import httpx

from desktop.sidecar.contracts.v2 import models as local_v2
from desktop.sidecar.core_bridge_v2 import (
    CoreHostAttachmentV2,
    CoreTunnelHandleV2,
    DesktopCoreBridgeErrorV2,
)
from openevo.deployment.core_assets import (
    MAX_CORE_WHEEL_BYTES,
    MAX_FRAMEWORK_LOCK_BYTES,
)
from openevo.deployment.core_control import (
    CoreControlBootstrapError,
    CoreControlBootstrapErrorCode,
    RemoteCoreControlAttachment,
    VerifiedCoreControlTunnel,
    open_core_control_tunnel,
)
from openevo.deployment.daemon_bundle_transport import (
    DaemonBundleIdentity,
    DaemonBundleServicePredecessor,
    DaemonBundleServiceStatus,
    StagedDaemonBundle,
)
from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.ssh import SshTransportError, SshTransportErrorCode
from openevo.runtime.managed import MANAGED_RUNTIME_ARCHIVE_RELEASE


_HOST_IDENTITY_DOMAIN = b"openevo-desktop-core-host-identity-v2\0"
_MAX_REMOTE_OPERATION_SECONDS = 300.0
_MAX_MANAGED_RUNTIME_SECONDS = 900.0
_MAX_HTTP_IO_SECONDS = 7200.0
_DIGEST = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_WHEEL_FILENAME = re.compile(r"[A-Za-z0-9_.+-]+\.whl\Z", re.ASCII)
_DAEMON_BUNDLE_FILENAME = "openevo-daemon-linux-x86_64"
_DAEMON_MANIFEST_FILENAME = "openevo-daemon-bundle.json"
LifecycleProgressObserverV2 = Callable[
    [local_v2.LifecyclePhaseV2, local_v2.LifecycleProgressV2 | None, bool],
    None,
]


class _CoreSshTransport(Protocol):
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
    ) -> StagedDaemonBundle: ...

    def daemon_bundle_identity(
        self,
        bundle: StagedDaemonBundle,
        *,
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> DaemonBundleIdentity: ...

    def observe_daemon_bundle_service(
        self,
        bundle: StagedDaemonBundle,
        *,
        canonical_manifest_sha256: str,
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> DaemonBundleServicePredecessor: ...

    def ensure_managed_runtime_from_daemon(
        self,
        bundle: StagedDaemonBundle,
        *,
        archive_path: str,
        archive_sha256: str,
        archive_size: int,
        platform: str,
        config_id: str,
        oci_index_id: str,
        aliases: tuple[str, ...],
        timeout_seconds: float,
        cancel_event: threading.Event | None = None,
    ) -> object: ...

    def stop_daemon_bundle(
        self,
        bundle: StagedDaemonBundle,
        *,
        expected_predecessor: DaemonBundleServicePredecessor,
        timeout_seconds: float,
    ) -> object: ...

    def ensure_daemon_bundle(
        self,
        bundle: StagedDaemonBundle,
        *,
        expected_predecessor: DaemonBundleServicePredecessor,
        canonical_manifest_sha256: str,
        port: int,
        timeout_seconds: float,
    ) -> tuple[RemoteCoreControlAttachment, DaemonBundleServiceStatus]: ...

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
    ) -> object: ...


class GenerationBoundRemoteLifecycleV2(Protocol):
    """The Task 21 lifecycle surface; aliases never expose host/user fields."""

    def active_transport(
        self,
        profile_id: str,
        profile_connection_generation: int,
    ) -> _CoreSshTransport: ...


@dataclass(frozen=True, slots=True, repr=False)
class SealedCoreBootstrapAssetV2:
    local_path: str = field(repr=False)
    sha256: str
    byte_size: int

    def __post_init__(self) -> None:
        if (
            type(self.local_path) is not str
            or not Path(self.local_path).is_absolute()
            or not _is_canonical_local_path(self.local_path)
            or _DIGEST.fullmatch(self.sha256) is None
            or type(self.byte_size) is not int
            or not 0 < self.byte_size <= MAX_CORE_WHEEL_BYTES
        ):
            raise ValueError("sealed Core v2 bootstrap asset is invalid")

    def __repr__(self) -> str:
        return "SealedCoreBootstrapAssetV2(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class SealedManagedRuntimeArchiveV2:
    local_path: str = field(repr=False)
    sha256: str
    byte_size: int
    platform: str
    config_id: str
    oci_index_id: str

    def __post_init__(self) -> None:
        release = MANAGED_RUNTIME_ARCHIVE_RELEASE
        if (
            type(self.local_path) is not str
            or not Path(self.local_path).is_absolute()
            or not _is_canonical_local_path(self.local_path)
            or Path(self.local_path).name != release.filename
            or self.sha256 != release.sha256
            or self.byte_size != release.byte_size
            or self.platform != release.platform
            or self.config_id != release.config_id
            or self.oci_index_id != release.oci_index_id
        ):
            raise ValueError("sealed managed runtime archive is invalid")

    def __repr__(self) -> str:
        return "SealedManagedRuntimeArchiveV2(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class SealedDaemonBundleV2:
    local_path: str = field(repr=False)
    sha256: str
    byte_size: int
    manifest_sha256: str
    release_identity: str
    registry_digest: str
    source_commit: str
    wheel_sha256: str
    dependency_lock_sha256: str
    framework_lock_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.local_path) is not str
            or not Path(self.local_path).is_absolute()
            or not _is_canonical_local_path(self.local_path)
            or Path(self.local_path).name != _DAEMON_BUNDLE_FILENAME
            or type(self.byte_size) is not int
            or not 0 < self.byte_size <= MAX_CORE_WHEEL_BYTES
            or any(
                _DIGEST.fullmatch(value) is None
                for value in (
                    self.sha256,
                    self.manifest_sha256,
                    self.release_identity,
                    self.registry_digest,
                    self.wheel_sha256,
                    self.dependency_lock_sha256,
                    self.framework_lock_sha256,
                )
            )
            or _SOURCE_COMMIT.fullmatch(self.source_commit) is None
        ):
            raise ValueError("sealed Daemon v2 bundle is invalid")

    def __repr__(self) -> str:
        return "SealedDaemonBundleV2(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class CoreBootstrapConfigV2:
    source_commit: str
    wheel: SealedCoreBootstrapAssetV2
    framework_lock: SealedCoreBootstrapAssetV2
    daemon_bundle: SealedDaemonBundleV2 | None
    managed_runtime_archive: SealedManagedRuntimeArchiveV2 | None
    remote_port: int = 0
    replace_mismatched: bool = False

    def __post_init__(self) -> None:
        daemon = self.daemon_bundle
        if (
            _SOURCE_COMMIT.fullmatch(self.source_commit) is None
            or type(self.wheel) is not SealedCoreBootstrapAssetV2
            or type(self.framework_lock) is not SealedCoreBootstrapAssetV2
            or (daemon is not None and type(daemon) is not SealedDaemonBundleV2)
            or (
                self.managed_runtime_archive is not None
                and type(self.managed_runtime_archive) is not SealedManagedRuntimeArchiveV2
            )
            or type(self.remote_port) is not int
            or not 0 <= self.remote_port <= 65_535
            or type(self.replace_mismatched) is not bool
            or _WHEEL_FILENAME.fullmatch(Path(self.wheel.local_path).name) is None
            or Path(self.framework_lock.local_path).name != "framework-lock.json"
            or self.framework_lock.byte_size > MAX_FRAMEWORK_LOCK_BYTES
            or (
                daemon is not None
                and (
                    daemon.source_commit != self.source_commit
                    or daemon.wheel_sha256 != self.wheel.sha256
                    or daemon.framework_lock_sha256 != self.framework_lock.sha256
                )
            )
        ):
            raise ValueError("Core v2 bootstrap configuration is invalid")

    def __repr__(self) -> str:
        return "CoreBootstrapConfigV2(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class _AttachmentAuthority:
    profile_id: str
    profile_connection_generation: int
    transport: _CoreSshTransport
    attachment: RemoteCoreControlAttachment
    bearer_identity: str
    adapter_generation: int


@dataclass(frozen=True, slots=True, repr=False)
class _ActiveTunnelAuthority:
    session_id: str
    authority: _AttachmentAuthority
    tunnel: VerifiedCoreControlTunnel


class DesktopCoreSshBridgeAdapterV2:
    """Stages/attaches Daemon, then owns generation-bound verified tunnels."""

    def __init__(
        self,
        lifecycle: GenerationBoundRemoteLifecycleV2,
        bootstrap: CoreBootstrapConfigV2,
        *,
        progress_observer: LifecycleProgressObserverV2 | None = None,
    ) -> None:
        if type(bootstrap) is not CoreBootstrapConfigV2:
            raise TypeError("bootstrap must be an exact CoreBootstrapConfigV2")
        if not callable(getattr(lifecycle, "active_transport", None)):
            raise TypeError("lifecycle lacks generation-bound active transport")
        if progress_observer is not None and not callable(progress_observer):
            raise TypeError("Core SSH lifecycle progress observer is invalid")
        self._lifecycle = lifecycle
        self._bootstrap = bootstrap
        self._progress_observer = progress_observer
        self._lock = threading.RLock()
        self._generation = 0
        self._authority: _AttachmentAuthority | None = None
        self._tunnels: dict[str, _ActiveTunnelAuthority] = {}
        self._latest_tunnel_session: str | None = None

    def __repr__(self) -> str:
        return "DesktopCoreSshBridgeAdapterV2(<private>)"

    def set_progress_observer(self, observer: LifecycleProgressObserverV2) -> None:
        if not callable(observer):
            raise TypeError("Core SSH lifecycle progress observer is invalid")
        with self._lock:
            if self._progress_observer is observer:
                return
            if self._authority is not None or self._progress_observer is not None:
                raise RuntimeError("Core SSH lifecycle progress observer cannot be changed")
            self._progress_observer = observer

    def ensure_core(
        self,
        profile_id: str,
        profile_connection_generation: int,
        *,
        deadline: float,
        cancel_event: threading.Event | None = None,
    ) -> CoreHostAttachmentV2:
        activation_cancel = cancel_event or threading.Event()
        _require_activation_not_cancelled(activation_cancel)
        daemon = self._bootstrap.daemon_bundle
        runtime = self._bootstrap.managed_runtime_archive
        if daemon is None or runtime is None:
            raise _adapter_error(
                "daemon_release_assets_unavailable",
                "This Desktop build does not contain the sealed Daemon and runtime assets.",
                status=409,
                action="install_repair_daemon",
            )
        transport = self._active_transport(
            profile_id, profile_connection_generation, require_tunnel=False
        )
        manifest_path = Path(daemon.local_path).with_name(_DAEMON_MANIFEST_FILENAME)
        try:
            manifest_size = manifest_path.stat().st_size
        except OSError:
            raise _adapter_error(
                "daemon_release_assets_unavailable",
                "This Desktop build does not contain the sealed Daemon manifest.",
                status=409,
                action="install_repair_daemon",
                affected_resource_id=profile_id,
            ) from None
        transfer_total = daemon.byte_size + manifest_size + runtime.byte_size
        self._observe_progress(
            "remote_preflight",
            local_v2.LifecycleProgressIndeterminateV2(kind="indeterminate"),
            cancellable=True,
        )
        self._observe_progress(
            "transferring",
            local_v2.LifecycleProgressBytesV2(
                kind="bytes",
                completed=0,
                total=transfer_total,
            ),
            cancellable=True,
        )
        try:
            staged = transport.stage_daemon_bundle(
                bundle_path=daemon.local_path,
                bundle_sha256=daemon.sha256,
                bundle_size=daemon.byte_size,
                manifest_path=str(manifest_path),
                manifest_sha256=daemon.manifest_sha256,
                manifest_size=manifest_size,
                timeout_seconds=min(_remaining(deadline), _MAX_REMOTE_OPERATION_SECONDS),
                cancel_event=activation_cancel,
            )
            self._observe_progress(
                "transferring",
                local_v2.LifecycleProgressBytesV2(
                    kind="bytes",
                    completed=daemon.byte_size + manifest_size,
                    total=transfer_total,
                ),
                cancellable=True,
            )
            _verify_staged_daemon(daemon, staged)
            self._require_same_transport(profile_id, profile_connection_generation, transport)
            identity = transport.daemon_bundle_identity(
                staged,
                timeout_seconds=min(_remaining(deadline), _MAX_REMOTE_OPERATION_SECONDS),
                cancel_event=activation_cancel,
            )
            _verify_daemon_identity(daemon, identity)
            self._require_same_transport(profile_id, profile_connection_generation, transport)
            predecessor = transport.observe_daemon_bundle_service(
                staged,
                canonical_manifest_sha256=daemon.manifest_sha256,
                timeout_seconds=min(_remaining(deadline), _MAX_REMOTE_OPERATION_SECONDS),
                cancel_event=activation_cancel,
            )
            self._require_same_transport(profile_id, profile_connection_generation, transport)
            transport.ensure_managed_runtime_from_daemon(
                staged,
                archive_path=runtime.local_path,
                archive_sha256=runtime.sha256,
                archive_size=runtime.byte_size,
                platform=runtime.platform,
                config_id=runtime.config_id,
                oci_index_id=runtime.oci_index_id,
                aliases=MANAGED_RUNTIME_ARCHIVE_RELEASE.aliases,
                timeout_seconds=min(_remaining(deadline), _MAX_MANAGED_RUNTIME_SECONDS),
                cancel_event=activation_cancel,
            )
            self._observe_progress(
                "transferring",
                local_v2.LifecycleProgressBytesV2(
                    kind="bytes",
                    completed=transfer_total,
                    total=transfer_total,
                ),
                cancellable=True,
            )
            self._observe_progress(
                "verifying",
                local_v2.LifecycleProgressIndeterminateV2(kind="indeterminate"),
                cancellable=True,
            )
            self._require_same_transport(profile_id, profile_connection_generation, transport)
            _require_activation_not_cancelled(activation_cancel)
            self._observe_progress(
                "starting_daemon",
                local_v2.LifecycleProgressIndeterminateV2(kind="indeterminate"),
                cancellable=False,
            )
            if (
                self._bootstrap.replace_mismatched
                and predecessor.state != "absent"
                and not _predecessor_matches_candidate(predecessor, daemon, identity)
            ):
                transport.stop_daemon_bundle(
                    staged,
                    expected_predecessor=predecessor,
                    timeout_seconds=min(_remaining(deadline), _MAX_REMOTE_OPERATION_SECONDS),
                )
                self._require_same_transport(profile_id, profile_connection_generation, transport)
                predecessor = transport.observe_daemon_bundle_service(
                    staged,
                    canonical_manifest_sha256=daemon.manifest_sha256,
                    timeout_seconds=min(_remaining(deadline), _MAX_REMOTE_OPERATION_SECONDS),
                )
                if predecessor.state != "absent":
                    raise _adapter_error(
                        "daemon_service_predecessor_mismatch",
                        "The OpenEvo Daemon generation changed during replacement.",
                        status=409,
                        retryable=True,
                        action="retry",
                    )
            self._observe_progress(
                "waiting_for_daemon",
                local_v2.LifecycleProgressIndeterminateV2(kind="indeterminate"),
                cancellable=False,
            )
            remote, service = transport.ensure_daemon_bundle(
                staged,
                expected_predecessor=predecessor,
                canonical_manifest_sha256=daemon.manifest_sha256,
                port=self._bootstrap.remote_port,
                timeout_seconds=min(_remaining(deadline), _MAX_REMOTE_OPERATION_SECONDS),
            )
        except DesktopCoreBridgeErrorV2:
            raise
        except SshTransportError as exc:
            raise _ssh_error(exc, affected_resource_id=profile_id) from None
        except (OSError, TypeError, ValueError):
            raise _adapter_error(
                "daemon_bootstrap_failed",
                "The sealed OpenEvo Daemon could not be prepared.",
                retryable=True,
                action="retry",
                affected_resource_id=profile_id,
            ) from None
        self._require_same_transport(profile_id, profile_connection_generation, transport)
        _verify_daemon_attachment(daemon, remote, service)
        bearer_identity = _host_identity(
            profile_id,
            profile_connection_generation,
            remote,
            service,
        )
        with self._lock:
            self._generation += 1
            authority = _AttachmentAuthority(
                profile_id=profile_id,
                profile_connection_generation=profile_connection_generation,
                transport=transport,
                attachment=remote,
                bearer_identity=bearer_identity,
                adapter_generation=self._generation,
            )
            self._authority = authority
        return CoreHostAttachmentV2(
            profile_id=profile_id,
            profile_connection_generation=profile_connection_generation,
            remote_port=remote.remote_port,
            bearer_token=remote.bearer_token,
            bearer_identity=bearer_identity,
        )

    def _observe_progress(
        self,
        phase: local_v2.LifecyclePhaseV2,
        progress: local_v2.LifecycleProgressV2 | None,
        *,
        cancellable: bool,
    ) -> None:
        observer = self._progress_observer
        if observer is not None:
            observer(phase, progress, cancellable)

    def open_tunnel(
        self,
        *,
        profile_id: str,
        profile_connection_generation: int,
        remote_port: int,
        session_id: str,
        deadline: float,
    ) -> CoreTunnelHandleV2:
        authority = self._tunnel_authority(profile_id, profile_connection_generation, remote_port)
        transport = self._active_transport(
            profile_id, profile_connection_generation, require_tunnel=True
        )
        if transport is not authority.transport:
            raise _transport_changed_error(profile_id)
        verified: VerifiedCoreControlTunnel | None = None
        try:
            for attempt in range(2):
                try:
                    verified = open_core_control_tunnel(
                        authority.attachment,
                        transport,
                        timeout_seconds=min(_remaining(deadline), 60.0),
                    )
                    break
                except CoreControlBootstrapError as exc:
                    if attempt != 0 or not exc.retryable:
                        raise
                    # A freshly published Daemon can lose one short-lived mux
                    # follower without invalidating the owned SSH generation.
                    self._require_same_transport(
                        profile_id,
                        profile_connection_generation,
                        transport,
                    )
            if verified is None:
                raise _adapter_error(
                    "core_tunnel_open_failed",
                    "The private Core tunnel could not be opened.",
                    retryable=True,
                    action="retry",
                    affected_resource_id=profile_id,
                )
            self._require_same_transport(profile_id, profile_connection_generation, transport)
            with self._lock:
                if self._authority is not authority or session_id in self._tunnels:
                    raise _transport_changed_error(profile_id)
                if len(self._tunnels) >= 2:
                    raise _adapter_error(
                        "core_tunnel_capacity_full",
                        "Another Core tunnel transition is still active.",
                        status=409,
                        retryable=True,
                        action="retry",
                        affected_resource_id=profile_id,
                    )
                active = _ActiveTunnelAuthority(
                    session_id=session_id,
                    authority=authority,
                    tunnel=verified,
                )
                self._tunnels[session_id] = active
                self._latest_tunnel_session = session_id
            return CoreTunnelHandleV2(
                endpoint="http://127.0.0.1:1",
                profile_id=profile_id,
                profile_connection_generation=profile_connection_generation,
                session_id=session_id,
                close_callback=lambda: self._close_active_tunnel(active),
            )
        except DesktopCoreBridgeErrorV2:
            if verified is not None:
                _close_unpublished_tunnel(verified)
            raise
        except (CoreControlBootstrapError, SshTransportError) as exc:
            if verified is not None:
                _close_unpublished_tunnel(verified)
            raise _tunnel_error(exc, profile_id) from None
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            if verified is not None:
                _close_unpublished_tunnel(verified)
            raise _adapter_error(
                "core_tunnel_open_failed",
                "The private Core tunnel could not be opened.",
                retryable=True,
                action="retry",
                affected_resource_id=profile_id,
            ) from None

    def new_http_transport(self) -> httpx.BaseTransport:
        with self._lock:
            session_id = self._latest_tunnel_session
            active = None if session_id is None else self._tunnels.get(session_id)
        if active is None:
            raise _adapter_error(
                "core_tunnel_not_active",
                "No verified private Core tunnel is active.",
                status=409,
                retryable=True,
                action="reconnect",
            )
        self._require_same_transport(
            active.authority.profile_id,
            active.authority.profile_connection_generation,
            active.authority.transport,
        )
        try:
            active.tunnel.verify_authority()
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise _adapter_error(
                "core_tunnel_identity_mismatch",
                "The private Core tunnel no longer matches its attachment.",
                status=409,
                action="reconnect",
                affected_resource_id=active.authority.profile_id,
            ) from None
        return VerifiedCoreHttpTransportV2(active.tunnel)

    def _close_active_tunnel(self, active: _ActiveTunnelAuthority) -> None:
        active.tunnel.close()
        with self._lock:
            self._tunnels.pop(active.session_id, None)
            if self._latest_tunnel_session == active.session_id:
                self._latest_tunnel_session = next(reversed(self._tunnels), None)

    def _tunnel_authority(
        self,
        profile_id: str,
        profile_connection_generation: int,
        remote_port: int,
    ) -> _AttachmentAuthority:
        with self._lock:
            authority = self._authority
        if (
            authority is None
            or authority.profile_id != profile_id
            or authority.profile_connection_generation != profile_connection_generation
            or authority.attachment.remote_port != remote_port
        ):
            raise _adapter_error(
                "core_attachment_identity_mismatch",
                "The Core attachment belongs to another profile generation.",
                status=409,
                action="reconnect",
                affected_resource_id=profile_id,
            )
        return authority

    def _active_transport(
        self,
        profile_id: str,
        profile_connection_generation: int,
        *,
        require_tunnel: bool,
    ) -> _CoreSshTransport:
        try:
            transport = self._lifecycle.active_transport(profile_id, profile_connection_generation)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise _adapter_error(
                "core_profile_not_connected",
                "The requested profile generation is not connected.",
                status=409,
                retryable=True,
                action="reconnect",
                affected_resource_id=profile_id,
            ) from None
        required = {
            "stage_daemon_bundle",
            "daemon_bundle_identity",
            "observe_daemon_bundle_service",
            "ensure_managed_runtime_from_daemon",
            "stop_daemon_bundle",
            "ensure_daemon_bundle",
            "run",
            "run_secret",
        }
        if require_tunnel:
            required.add("open_core_tunnel")
        if any(not callable(getattr(transport, name, None)) for name in required):
            raise _adapter_error(
                "core_ssh_transport_incompatible",
                "The active system-SSH transport cannot operate the OpenEvo Daemon.",
                status=409,
                action="install_repair_daemon",
                affected_resource_id=profile_id,
            )
        return cast(_CoreSshTransport, transport)

    def _require_same_transport(
        self,
        profile_id: str,
        profile_connection_generation: int,
        expected: _CoreSshTransport,
    ) -> None:
        if (
            self._active_transport(
                profile_id,
                profile_connection_generation,
                require_tunnel=False,
            )
            is not expected
        ):
            raise _transport_changed_error(profile_id)


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
        self._guard = threading.Lock()
        self._cancelled = False
        self._opened_socket: socket.socket | None = None

    def connect(self) -> None:
        timeout = self.timeout if isinstance(self.timeout, (int, float)) else 30.0
        opened = self._endpoint.open_verified_socket(timeout_seconds=timeout)
        with self._guard:
            if self._cancelled or not self._adopt_socket(opened):
                opened.close()
                raise OSError("Core tunnel request was cancelled")
            self.sock = opened
            self._opened_socket = opened

    def cancel(self) -> None:
        with self._guard:
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
        with self._guard:
            super().close()

    def close(self) -> None:
        with self._guard:
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
        self._condition = threading.Condition()
        self._closed = False
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
            raise httpx.ReadError("Core tunnel response failed.", request=self._request) from None
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
        with self._condition:
            while self._active_reads:
                self._condition.wait()
        self._release(self)


class VerifiedCoreHttpTransportV2(httpx.BaseTransport):
    """HTTPX transport whose sockets are all issued by one verified SSH tunnel."""

    def __init__(self, endpoint: VerifiedCoreControlTunnel) -> None:
        if type(endpoint) is not VerifiedCoreControlTunnel:
            raise TypeError("verified Core transport requires an exact tunnel")
        self._endpoint = endpoint
        self._condition = threading.Condition()
        self._closed = False
        self._generation = 0
        self._inflight = 0
        self._connections: set[_VerifiedEndpointHTTPConnection] = set()
        self._streams: set[_VerifiedResponseStream] = set()

    def __repr__(self) -> str:
        return "VerifiedCoreHttpTransportV2(<private>)"

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        _validate_transport_request(request)
        timeout = _request_timeout(request)
        generation = -1
        connection: _VerifiedEndpointHTTPConnection
        connection = _VerifiedEndpointHTTPConnection(
            self._endpoint,
            timeout=timeout,
            adopt_socket=lambda opened: self._adopt_socket(connection, opened, generation),
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
            existed = connection in self._connections
            self._connections.discard(connection)
            if stream is not None:
                self._streams.discard(stream)
            if existed:
                self._inflight -= 1
                self._condition.notify_all()


def _host_identity(
    profile_id: str,
    profile_connection_generation: int,
    attachment: RemoteCoreControlAttachment,
    service: DaemonBundleServiceStatus,
) -> str:
    public = json.dumps(
        {
            "bundle_sha256": service.bundle_sha256,
            "canonical_manifest_sha256": service.canonical_manifest_sha256,
            "generation": attachment.generation,
            "profile_connection_generation": profile_connection_generation,
            "profile_id": profile_id,
            "registry_digest": attachment.registry_digest,
            "release_identity": attachment.release_identity,
            "remote_host": attachment.remote_host,
            "remote_port": attachment.remote_port,
            "source_commit": attachment.source_commit,
            "status_proof": attachment.status_proof,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hmac.new(
        attachment.bearer_token.encode("ascii"),
        _HOST_IDENTITY_DOMAIN + public,
        hashlib.sha256,
    ).hexdigest()


def _verify_staged_daemon(expected: SealedDaemonBundleV2, actual: object) -> None:
    if type(actual) is not StagedDaemonBundle:
        raise ValueError("staged Daemon receipt type differs")
    actual.__post_init__()
    if (
        actual.host_profile != "docker_user_container_v1"
        or actual.sha256 != expected.sha256
        or actual.size != expected.byte_size
    ):
        raise ValueError("staged Daemon receipt differs")


def _verify_daemon_identity(expected: SealedDaemonBundleV2, actual: object) -> None:
    if type(actual) is not DaemonBundleIdentity or (
        actual.bundle_format != "pyinstaller-onefile"
        or actual.bundle_sha256 != expected.sha256
        or actual.bundle_size != expected.byte_size
        or actual.core_distribution != "openevo"
        or actual.core_wheel_sha256 != expected.wheel_sha256
        or actual.dependency_lock_sha256 != expected.dependency_lock_sha256
        or actual.framework_lock_sha256 != expected.framework_lock_sha256
        or actual.registry_digest != expected.registry_digest
        or actual.release_identity != expected.release_identity
        or actual.source_commit != expected.source_commit
        or actual.platform_system != "linux"
        or actual.platform_architecture != "x86_64"
    ):
        raise ValueError("Daemon bundle identity differs")


def _verify_daemon_attachment(
    expected: SealedDaemonBundleV2,
    attachment: object,
    service: object,
) -> None:
    if (
        type(attachment) is not RemoteCoreControlAttachment
        or type(service) is not DaemonBundleServiceStatus
    ):
        raise ValueError("Daemon attachment type differs")
    if (
        service.bundle_sha256 != expected.sha256
        or service.canonical_manifest_sha256 != expected.manifest_sha256
        or service.lifecycle_compatibility < 2
        or service.release_identity != expected.release_identity
        or service.registry_digest != expected.registry_digest
        or service.source_commit != expected.source_commit
        or service.generation != attachment.generation
        or service.remote_port != attachment.remote_port
        or service.attached != attachment.attached
        or attachment.release_identity != expected.release_identity
        or attachment.registry_digest != expected.registry_digest
        or attachment.source_commit != expected.source_commit
        or attachment.execution_mode != "subscription"
        or attachment.capture_mode != "transcript"
    ):
        raise ValueError("Daemon attachment identity differs")


def _predecessor_matches_candidate(
    predecessor: DaemonBundleServicePredecessor,
    expected: SealedDaemonBundleV2,
    identity: DaemonBundleIdentity,
) -> bool:
    return (
        predecessor.state == "running"
        and predecessor.release_identity == expected.release_identity == identity.release_identity
        and predecessor.bundle_sha256 == expected.sha256 == identity.bundle_sha256
        and predecessor.canonical_manifest_sha256 == expected.manifest_sha256
    )


def _request_timeout(request: httpx.Request) -> float:
    configured = request.extensions.get("timeout")
    if not isinstance(configured, dict):
        return 30.0
    values = [
        float(value)
        for key in ("connect", "read", "write", "pool")
        if isinstance((value := configured.get(key)), (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and value > 0
    ]
    return min(min(values) if values else 30.0, _MAX_HTTP_IO_SECONDS)


def _validate_transport_request(request: httpx.Request) -> None:
    if type(request) is not httpx.Request:
        raise httpx.LocalProtocolError("Core tunnel request type is invalid.")
    url = request.url
    if (
        url.scheme != "http"
        or url.host != "127.0.0.1"
        or url.port != 1
        or bool(url.username)
        or bool(url.password)
        or bool(url.fragment)
        or not url.raw_path.startswith(b"/")
        or request.headers.get_list("host") != ["127.0.0.1:1"]
    ):
        raise httpx.LocalProtocolError(
            "Core tunnel request escaped its fixed loopback origin.",
            request=request,
        )


def _request_body(
    request: httpx.Request,
) -> tuple[httpx.SyncByteStream | None, bool]:
    transfer = request.headers.get("transfer-encoding")
    content_length = request.headers.get("content-length")
    if transfer is not None:
        if transfer.strip().lower() != "chunked" or content_length is not None:
            raise httpx.LocalProtocolError(
                "Core tunnel transfer framing is unsupported.", request=request
            )
        return request.stream, True
    if content_length is not None:
        try:
            length = int(content_length, 10)
        except ValueError:
            length = -1
        if length < 0 or str(length) != content_length.strip():
            raise httpx.LocalProtocolError(
                "Core tunnel content length is invalid.", request=request
            )
        return request.stream, False
    if request.method in {"GET", "HEAD"}:
        return None, False
    raise httpx.LocalProtocolError("Core tunnel request body length is unknown.", request=request)


def _remaining(deadline: float) -> float:
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(float(deadline))
        or float(deadline) <= time.monotonic()
    ):
        raise _adapter_error(
            "core_adapter_deadline_exceeded",
            "The Core adapter operation exceeded its deadline.",
            status=504,
            retryable=True,
            action="retry",
        )
    return float(deadline) - time.monotonic()


def _is_canonical_local_path(path: str) -> bool:
    parts = path.split("/")
    return bool(
        len(parts) >= 2 and not parts[0] and all(part not in {"", ".", ".."} for part in parts[1:])
    )


def _close_unpublished_tunnel(tunnel: VerifiedCoreControlTunnel) -> None:
    try:
        tunnel.close()
    except BaseException:
        pass


def _transport_changed_error(profile_id: str) -> DesktopCoreBridgeErrorV2:
    return _adapter_error(
        "core_ssh_transport_identity_changed",
        "The system-SSH connection changed during the Core operation.",
        status=409,
        retryable=True,
        action="reconnect",
        affected_resource_id=profile_id,
    )


def _ssh_error(
    error: SshTransportError,
    *,
    affected_resource_id: str,
) -> DesktopCoreBridgeErrorV2:
    if error.code is SshTransportErrorCode.CANCELLED:
        return _activation_cancelled_error(affected_resource_id)
    if error.code is SshTransportErrorCode.HOST_KEY_VERIFICATION_FAILED:
        return _adapter_error(
            "core_ssh_authority_invalid",
            "The system-SSH host authority is no longer valid.",
            status=409,
            action="review_host_key",
            affected_resource_id=affected_resource_id,
        )
    if error.code is SshTransportErrorCode.TIMEOUT:
        return _adapter_error(
            "daemon_bootstrap_timeout",
            "The OpenEvo Daemon operation exceeded its deadline.",
            status=504,
            retryable=True,
            action="retry",
            affected_resource_id=affected_resource_id,
        )
    return _adapter_error(
        "daemon_bootstrap_failed",
        "The OpenEvo Daemon could not be prepared over system SSH.",
        retryable=True,
        action="retry",
        affected_resource_id=affected_resource_id,
    )


def _require_activation_not_cancelled(cancel_event: threading.Event) -> None:
    if not isinstance(cancel_event, threading.Event) or cancel_event.is_set():
        raise _activation_cancelled_error(None)


def _activation_cancelled_error(
    affected_resource_id: str | None,
) -> DesktopCoreBridgeErrorV2:
    return _adapter_error(
        "core_activation_cancelled",
        "The OpenEvo Daemon activation was cancelled before publication.",
        status=409,
        retryable=True,
        action="retry",
        affected_resource_id=affected_resource_id,
    )


def _tunnel_error(
    error: CoreControlBootstrapError | SshTransportError,
    profile_id: str,
) -> DesktopCoreBridgeErrorV2:
    timeout = (
        isinstance(error, CoreControlBootstrapError)
        and error.code is CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED
    ) or (isinstance(error, SshTransportError) and error.code is SshTransportErrorCode.TIMEOUT)
    return _adapter_error(
        "core_tunnel_timeout" if timeout else "core_tunnel_open_failed",
        "The private Core tunnel did not become ready."
        if timeout
        else "The private Core tunnel could not be opened.",
        status=504 if timeout else 503,
        retryable=True,
        action="retry",
        affected_resource_id=profile_id,
    )


def _adapter_error(
    code: str,
    summary: str,
    *,
    status: int = 503,
    retryable: bool = False,
    action: local_v2.DesktopActionV2 = "none",
    affected_resource_id: str | None = None,
) -> DesktopCoreBridgeErrorV2:
    return DesktopCoreBridgeErrorV2(
        min(599, max(400, status)),
        local_v2.DesktopErrorV2(
            code=code,
            summary=summary,
            retryable=retryable,
            action=action,
            affected_resource_id=affected_resource_id,
        ),
    )


__all__ = (
    "CoreBootstrapConfigV2",
    "DesktopCoreSshBridgeAdapterV2",
    "GenerationBoundRemoteLifecycleV2",
    "LifecycleProgressObserverV2",
    "SealedCoreBootstrapAssetV2",
    "SealedDaemonBundleV2",
    "SealedManagedRuntimeArchiveV2",
    "VerifiedCoreHttpTransportV2",
)
