"""Production composition for the release Desktop/Core runtime."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys
import threading
from typing import Any, BinaryIO

import httpx

from desktop.sidecar.contracts.v1 import models as local_v1
from desktop.sidecar.core_bridge_adapters_v1 import (
    AdoptedWorkspaceArchiveSourceV1,
    AdoptedWorkspaceImportV1,
    CoreBootstrapConfigV1,
    DesktopCoreSshBridgeAdapterV1,
    SealedCoreBootstrapAssetV1,
    SealedDaemonBundleV1,
    SealedManagedRuntimeArchiveV1,
)
from desktop.sidecar.core_bridge_store_v1 import DesktopCoreBridgeStoreV1
from desktop.sidecar.core_bridge_v1 import (
    CoreHostAttachmentV1,
    CoreTunnelHandleV1,
    DesktopCoreBridgeErrorV1,
    DesktopCoreBridgeV1,
)
from desktop.sidecar.event_broker_v1 import DesktopEventBrokerError, DesktopEventBrokerV1
from desktop.sidecar.legacy_v1_import import (
    LegacyV1ImportReport,
    import_legacy_v1_state,
)
from desktop.sidecar.provider_store import DesktopProviderStore, ProviderStoreError
from desktop.sidecar.provider_store_v2 import DesktopProviderStoreV2
from desktop.sidecar.remote_lifecycle import DesktopRemoteLifecycle
from desktop.sidecar.workspace_identity import ownership_for_native_import
from desktop.sidecar.workspace_imports import (
    WorkspaceImportNotFoundError,
    WorkspaceImportStore,
)
from openevo.backend.contracts.v1 import models as core_v1
from openevo.deployment.core_assets import MAX_CORE_WHEEL_BYTES, MAX_FRAMEWORK_LOCK_BYTES
from openevo.runtime.managed import (
    MANAGED_RUNTIME_ARCHIVE_RELEASE,
    ManagedRuntimeArchiveVerificationError,
    verify_managed_runtime_archive,
)


_LOGGER = logging.getLogger(__name__)
_SOURCE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MAX_ASSET_DIRECTORY_ENTRIES = 8
_RELEASE_ASSETS_MANIFEST_NAME = "release-assets.json"
_RELEASE_ASSETS_MANIFEST_MAX_BYTES = 1024 * 1024
_CORE_ASSET_DIRECTORY_NAME = "core"
_DAEMON_ASSET_DIRECTORY_NAME = "daemon"
_MANAGED_RUNTIME_ASSET_DIRECTORY_NAME = "runtime"
_FRAMEWORK_LOCK_NAME = "framework-lock.json"
_DAEMON_BUNDLE_NAME = "openevo-daemon-linux-x86_64"
_DAEMON_MANIFEST_NAME = "openevo-daemon-bundle.json"
_MAX_DAEMON_MANIFEST_BYTES = 1024 * 1024
_RELAY_MIN_BACKOFF_SECONDS = 0.1
_RELAY_MAX_BACKOFF_SECONDS = 2.0
_RELEASE_ACTIVATION_TIMEOUT_SECONDS = 900.0
_V2_PROVIDER_STATE_DIRECTORY = "provider-v2"


class ReleaseRuntimeConfigurationError(RuntimeError):
    """Packaged release assets or runtime ownership are invalid."""


class _CoreEventSequenceGapError(RuntimeError):
    """Force replay when a stream skips past the committed event sequence."""


class _DeferredCoreSshBridgeAdapterV1:
    """Load and verify remote release assets only when Core is first needed."""

    def __init__(
        self,
        lifecycle: DesktopRemoteLifecycle,
        bootstrap_loader: Callable[[], CoreBootstrapConfigV1],
    ) -> None:
        if not isinstance(lifecycle, DesktopRemoteLifecycle):
            raise TypeError("lifecycle must be DesktopRemoteLifecycle")
        if not callable(bootstrap_loader):
            raise TypeError("bootstrap_loader must be callable")
        self._lifecycle = lifecycle
        self._bootstrap_loader = bootstrap_loader
        self._lock = threading.Lock()
        self._adapter: DesktopCoreSshBridgeAdapterV1 | None = None

    def ensure_core(
        self,
        profile_id: str,
        *,
        deadline: float,
        cancel_event: threading.Event | None = None,
    ) -> CoreHostAttachmentV1:
        return self._resolve().ensure_core(
            profile_id,
            deadline=deadline,
            cancel_event=cancel_event,
        )

    def open_tunnel(
        self,
        *,
        profile_id: str,
        remote_port: int,
        session_id: str,
        deadline: float,
    ) -> CoreTunnelHandleV1:
        return self._resolve().open_tunnel(
            profile_id=profile_id,
            remote_port=remote_port,
            session_id=session_id,
            deadline=deadline,
        )

    def new_http_transport(self) -> httpx.BaseTransport:
        return self._resolve().new_http_transport()

    def _resolve(self) -> DesktopCoreSshBridgeAdapterV1:
        with self._lock:
            if self._adapter is not None:
                return self._adapter
            try:
                bootstrap = self._bootstrap_loader()
            except (OSError, ReleaseRuntimeConfigurationError) as exc:
                raise DesktopCoreBridgeErrorV1(
                    core_v1.ApiErrorV1(
                        request_id="release-assets-initialization",
                        code="release_assets_initialization_failed",
                        http_status=503,
                        message=("OpenEvo Desktop could not verify its remote release assets."),
                        severity=core_v1.ErrorSeverity.BLOCKING,
                        category=core_v1.ErrorCategory.SERVICE,
                        retryable=True,
                        repair_action=core_v1.RepairAction.OPENEVO_CAN_RETRY,
                        next_action=(
                            "Retry remote project activation. If the problem continues, "
                            "reinstall OpenEvo Desktop."
                        ),
                    )
                ) from exc
            adapter = DesktopCoreSshBridgeAdapterV1(self._lifecycle, bootstrap)
            self._adapter = adapter
            return adapter


def _collect_cleanup_failure(
    cleanup: Callable[[], None],
    first_failure: BaseException | None,
) -> BaseException | None:
    try:
        cleanup()
    except BaseException as exc:
        if first_failure is None:
            return exc
    return first_failure


def _cleanup_after_primary_failure(cleanup: Callable[[], None]) -> None:
    _collect_cleanup_failure(cleanup, None)


@dataclass(frozen=True, slots=True)
class _SealedFile:
    asset: SealedCoreBootstrapAssetV1
    payload: bytes | None


@dataclass(frozen=True, slots=True)
class _ReleaseAssetsManifest:
    root: Path
    files: tuple[dict[str, object], ...]
    allowed_owner_ids: frozenset[int]


def _canonical_absolute_directory(path: Path | str) -> Path:
    absolute = Path(os.path.abspath(Path(path)))
    if sys.platform == "darwin" and len(absolute.parts) > 1:
        alias = Path("/") / absolute.parts[1]
        if alias in {Path("/etc"), Path("/tmp"), Path("/var")}:
            try:
                metadata = alias.lstat()
                target = alias.resolve(strict=True)
            except OSError:
                pass
            else:
                expected = Path("/private") / alias.name
                if stat.S_ISLNK(metadata.st_mode) and target == expected:
                    absolute = target.joinpath(*absolute.parts[2:])
    return absolute


def _open_directory_without_symlinks(path: Path | str) -> tuple[Path, int]:
    absolute = _canonical_absolute_directory(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    current_fd = os.open(os.sep, flags)
    try:
        for part in absolute.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return absolute, current_fd
    except BaseException:
        os.close(current_fd)
        raise


@dataclass(frozen=True, slots=True)
class CoreRuntimeSessionBinding:
    """Process-local authority for one provider-published Core session."""

    project: local_v1.ProjectV1
    generation: int


@dataclass(slots=True)
class ReleaseLocalStateV2:
    """Own the isolated v2 provider store and its read-only v1 import report."""

    provider_store: DesktopProviderStoreV2
    legacy_import: LegacyV1ImportReport
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self.provider_store.close()
        self._closed = True


def create_release_local_state_v2(
    state_root: Path | str,
    *,
    clock: Callable[[], datetime] | None = None,
) -> ReleaseLocalStateV2:
    """Create fresh v2 authority without mutating retained v1 state."""

    root = Path(os.path.abspath(os.fspath(Path(state_root).expanduser())))
    try:
        metadata = os.lstat(root)
    except FileNotFoundError:
        root.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(root, 0o700)
        except FileExistsError:
            metadata = os.lstat(root)
        else:
            os.chmod(root, 0o700, follow_symlinks=False)
            metadata = os.lstat(root)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise ReleaseRuntimeConfigurationError("Desktop v2 state root is not owner-private")
    provider = DesktopProviderStoreV2(
        root / _V2_PROVIDER_STATE_DIRECTORY,
        clock=clock,
    )
    try:
        imported = import_legacy_v1_state(provider, root)
        return ReleaseLocalStateV2(
            provider_store=provider,
            legacy_import=imported,
        )
    except BaseException:
        provider.close()
        raise


class ProviderWorkspaceArchiveSourceV1:
    """Resolve the current durable project/import binding for every read."""

    def __init__(
        self,
        provider_store: DesktopProviderStore,
        workspace_store: WorkspaceImportStore,
    ) -> None:
        self._provider_store = provider_store
        self._workspace_store = workspace_store

    @contextmanager
    def open_archive(self, ref: local_v1.WorkspaceImportRefV1) -> Iterator[BinaryIO]:
        if not isinstance(ref, local_v1.WorkspaceImportRefV1):
            raise TypeError("workspace import reference has the wrong type")
        with self._provider_store.workspace_import_reference_guard():
            matches = [
                (project_id, source)
                for project_id, source in self._provider_store.native_workspace_sources()
                if source.import_ref is not None and source.import_ref.import_id == ref.import_id
            ]
            if len(matches) != 1 or matches[0][1].import_ref != ref:
                raise WorkspaceImportNotFoundError(
                    "workspace import is not bound to exactly one saved project"
                )
            project_id, _source = matches[0]
            binding = AdoptedWorkspaceImportV1(
                import_ref=ref,
                ownership=ownership_for_native_import(ref, project_id=project_id),
            )
            source = AdoptedWorkspaceArchiveSourceV1(self._workspace_store, (binding,))
            with source.open_archive(ref) as stream:
                yield stream


class DesktopCoreEventRelayV1:
    """Relay accepted Core changes into Desktop snapshot invalidations."""

    def __init__(self, bridge: DesktopCoreBridgeV1) -> None:
        self._bridge = bridge
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_project: Callable[[], CoreRuntimeSessionBinding | None] | None = None
        self._refresh_authority: Callable[[CoreRuntimeSessionBinding], None] | None = None
        self._publish: Callable[[CoreRuntimeSessionBinding], None] | None = None
        self._session_lost: (
            Callable[[CoreRuntimeSessionBinding, DesktopCoreBridgeErrorV1], None] | None
        ) = None

    def start(
        self,
        *,
        active_project: Callable[[], CoreRuntimeSessionBinding | None],
        refresh_authority: Callable[[CoreRuntimeSessionBinding], None],
        publish: Callable[[CoreRuntimeSessionBinding], None],
        session_lost: Callable[[CoreRuntimeSessionBinding, DesktopCoreBridgeErrorV1], None],
    ) -> None:
        if (
            not callable(active_project)
            or not callable(refresh_authority)
            or not callable(publish)
            or not callable(session_lost)
        ):
            raise TypeError("event relay callbacks must be callable")
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("Core event relay was already started")
            self._active_project = active_project
            self._refresh_authority = refresh_authority
            self._publish = publish
            self._session_lost = session_lost
            self._thread = threading.Thread(
                target=self._run,
                name="openevo-core-event-relay",
                daemon=True,
            )
            self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    def join(self) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def _run(self) -> None:
        identity: tuple[str, str, int] | None = None
        last_event_id: str | None = None
        last_event_sequence: int | None = None
        backoff = _RELAY_MIN_BACKOFF_SECONDS
        while not self._stop.is_set():
            active_project = self._active_project
            refresh_authority = self._refresh_authority
            publish = self._publish
            session_lost = self._session_lost
            if (
                active_project is None
                or refresh_authority is None
                or publish is None
                or session_lost is None
            ):
                return
            binding: CoreRuntimeSessionBinding | None = None
            try:
                binding = active_project()
                if binding is None:
                    identity = None
                    last_event_id = None
                    last_event_sequence = None
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, _RELAY_MAX_BACKOFF_SECONDS)
                    continue
                project = binding.project
                current_identity = (project.project_id, project.etag, binding.generation)
                if current_identity != identity:
                    identity = current_identity
                    last_event_id = None
                    last_event_sequence = None
                with self._bridge.events(project, last_event_id=last_event_id) as events:
                    backoff = _RELAY_MIN_BACKOFF_SECONDS
                    for frame in events:
                        if self._stop.is_set():
                            return
                        event = frame.data.root
                        if not isinstance(event, core_v1.HeartbeatEventV1):
                            if isinstance(event, core_v1.ProjectUpdatedEventV1):
                                refresh_authority(binding)
                            publish(binding)
                        if (
                            last_event_sequence is None
                            or event.sequence == last_event_sequence + 1
                        ):
                            last_event_id = frame.id
                            last_event_sequence = event.sequence
                        elif event.sequence > last_event_sequence:
                            raise _CoreEventSequenceGapError(
                                "Core event stream skipped the committed sequence"
                            )
            except DesktopCoreBridgeErrorV1 as exc:
                if binding is not None:
                    session_lost(binding, exc)
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, _RELAY_MAX_BACKOFF_SECONDS)
            except (
                _CoreEventSequenceGapError,
                DesktopEventBrokerError,
                ProviderStoreError,
                OSError,
            ):
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, _RELAY_MAX_BACKOFF_SECONDS)
            except Exception:
                _LOGGER.exception("Desktop Core event relay failed")
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, _RELAY_MAX_BACKOFF_SECONDS)


class DesktopReleaseCoreRuntimeV1:
    """Own the process-local release bridge, relay, broker, and bridge store."""

    def __init__(
        self,
        *,
        bridge: DesktopCoreBridgeV1,
        event_broker: DesktopEventBrokerV1,
        bridge_store: DesktopCoreBridgeStoreV1,
        managed_runtime_available: bool = False,
    ) -> None:
        self.core_bridge = bridge
        self.event_broker = event_broker
        self._bridge_store = bridge_store
        self.managed_runtime_available = managed_runtime_available
        self._relay = DesktopCoreEventRelayV1(bridge)
        self._close_lock = threading.RLock()
        self._stopped = False
        self._closed = False

    def start(
        self,
        *,
        active_project: Callable[[], CoreRuntimeSessionBinding | None],
        refresh_authority: Callable[[CoreRuntimeSessionBinding], None],
        publish: Callable[[CoreRuntimeSessionBinding], None],
        session_lost: Callable[[CoreRuntimeSessionBinding, DesktopCoreBridgeErrorV1], None],
    ) -> None:
        with self._close_lock:
            if self._closed or self._stopped:
                raise RuntimeError("release Core runtime is closed")
            self._relay.start(
                active_project=active_project,
                refresh_authority=refresh_authority,
                publish=publish,
                session_lost=session_lost,
            )

    def stop(self) -> None:
        with self._close_lock:
            if self._stopped:
                return
            self._stopped = True
            failure = _collect_cleanup_failure(self._relay.request_stop, None)
            failure = _collect_cleanup_failure(self.core_bridge.close, failure)
            failure = _collect_cleanup_failure(self._relay.join, failure)
            if failure is not None:
                raise failure

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            failure = _collect_cleanup_failure(self.stop, None)
            failure = _collect_cleanup_failure(self.event_broker.close, failure)
            failure = _collect_cleanup_failure(self._bridge_store.close, failure)
            if failure is not None:
                raise failure


def _load_release_assets_manifest(
    asset_root: Path | str,
    *,
    source_commit: str,
    allowed_owner_ids: frozenset[int],
) -> _ReleaseAssetsManifest:
    try:
        root, root_fd = _open_directory_without_symlinks(asset_root)
    except OSError as exc:
        raise ReleaseRuntimeConfigurationError(
            "packaged release asset root is unavailable"
        ) from exc
    try:
        metadata = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in allowed_owner_ids
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise ReleaseRuntimeConfigurationError(
                "packaged release asset root is not owner controlled"
            )
        names = tuple(sorted(entry.name for entry in os.scandir(root_fd)))
        if names != (
            _CORE_ASSET_DIRECTORY_NAME,
            _DAEMON_ASSET_DIRECTORY_NAME,
            _RELEASE_ASSETS_MANIFEST_NAME,
            _MANAGED_RUNTIME_ASSET_DIRECTORY_NAME,
        ):
            raise ReleaseRuntimeConfigurationError(
                "packaged release asset root has an unexpected inventory"
            )
        sealed = _seal_file(
            root_fd,
            root,
            _RELEASE_ASSETS_MANIFEST_NAME,
            max_bytes=_RELEASE_ASSETS_MANIFEST_MAX_BYTES,
            retain_payload=True,
            allowed_owner_ids=allowed_owner_ids,
        )
    except OSError as exc:
        raise ReleaseRuntimeConfigurationError(
            "packaged release asset manifest could not be verified"
        ) from exc
    finally:
        os.close(root_fd)
    assert sealed.payload is not None

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        parsed: dict[str, object] = {}
        for key, value in pairs:
            if key in parsed:
                raise ValueError("duplicate release asset manifest key")
            parsed[key] = value
        return parsed

    try:
        value = json.loads(
            sealed.payload.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReleaseRuntimeConfigurationError(
            "packaged release asset manifest is invalid"
        ) from exc
    canonical = (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    files = value.get("files") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != {"files", "schema_version", "source_commit"}
        or value.get("schema_version") != 1
        or value.get("source_commit") != source_commit
        or sealed.payload != canonical
        or not isinstance(files, list)
        or len(files) != 5
    ):
        raise ReleaseRuntimeConfigurationError(
            "packaged release asset manifest does not bind this release"
        )
    paths: list[str] = []
    for entry in files:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"relative_path", "sha256", "byte_size"}
            or not isinstance(entry.get("relative_path"), str)
            or not isinstance(entry.get("sha256"), str)
            or _DIGEST_PATTERN.fullmatch(entry["sha256"]) is None
            or type(entry.get("byte_size")) is not int
            or not 0
            < entry["byte_size"]
            <= max(
                MAX_CORE_WHEEL_BYTES,
                MAX_FRAMEWORK_LOCK_BYTES,
                _MAX_DAEMON_MANIFEST_BYTES,
                MANAGED_RUNTIME_ARCHIVE_RELEASE.byte_size,
            )
        ):
            raise ReleaseRuntimeConfigurationError(
                "packaged release asset manifest entry is invalid"
            )
        relative_path = entry["relative_path"]
        parts = Path(relative_path).parts
        if (
            Path(relative_path).is_absolute()
            or len(parts) != 2
            or parts[0]
            not in {
                _CORE_ASSET_DIRECTORY_NAME,
                _DAEMON_ASSET_DIRECTORY_NAME,
                _MANAGED_RUNTIME_ASSET_DIRECTORY_NAME,
            }
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ReleaseRuntimeConfigurationError(
                "packaged release asset manifest path is invalid"
            )
        paths.append(relative_path)
    if paths != sorted(paths) or len(set(paths)) != len(paths):
        raise ReleaseRuntimeConfigurationError(
            "packaged release asset manifest inventory is not canonical"
        )
    return _ReleaseAssetsManifest(
        root=root,
        files=tuple(files),
        allowed_owner_ids=allowed_owner_ids,
    )


def _validate_release_assets_manifest_inventory(
    manifest: _ReleaseAssetsManifest,
    config: CoreBootstrapConfigV1,
) -> None:
    daemon = config.daemon_bundle
    runtime = config.managed_runtime_archive
    if daemon is None or runtime is None:
        raise ReleaseRuntimeConfigurationError("packaged release asset inventory is incomplete")
    daemon_root = manifest.root / _DAEMON_ASSET_DIRECTORY_NAME
    try:
        canonical_daemon_root, daemon_root_fd = _open_directory_without_symlinks(daemon_root)
    except OSError as exc:
        raise ReleaseRuntimeConfigurationError("packaged Daemon manifest is unavailable") from exc
    try:
        daemon_manifest = _seal_file(
            daemon_root_fd,
            canonical_daemon_root,
            _DAEMON_MANIFEST_NAME,
            max_bytes=_MAX_DAEMON_MANIFEST_BYTES,
            allowed_owner_ids=manifest.allowed_owner_ids,
        ).asset
    except OSError as exc:
        raise ReleaseRuntimeConfigurationError(
            "packaged Daemon manifest could not be verified"
        ) from exc
    finally:
        os.close(daemon_root_fd)
    if daemon_manifest.sha256 != daemon.manifest_sha256:
        raise ReleaseRuntimeConfigurationError(
            "packaged Daemon manifest changed during release verification"
        )
    expected = tuple(
        sorted(
            (
                {
                    "relative_path": f"core/{_FRAMEWORK_LOCK_NAME}",
                    "sha256": config.framework_lock.sha256,
                    "byte_size": config.framework_lock.byte_size,
                },
                {
                    "relative_path": f"core/{Path(config.wheel.local_path).name}",
                    "sha256": config.wheel.sha256,
                    "byte_size": config.wheel.byte_size,
                },
                {
                    "relative_path": f"daemon/{_DAEMON_MANIFEST_NAME}",
                    "sha256": daemon_manifest.sha256,
                    "byte_size": daemon_manifest.byte_size,
                },
                {
                    "relative_path": f"daemon/{_DAEMON_BUNDLE_NAME}",
                    "sha256": daemon.sha256,
                    "byte_size": daemon.byte_size,
                },
                {
                    "relative_path": f"runtime/{Path(runtime.local_path).name}",
                    "sha256": runtime.sha256,
                    "byte_size": runtime.byte_size,
                },
            ),
            key=lambda entry: str(entry["relative_path"]),
        )
    )
    if manifest.files != expected:
        raise ReleaseRuntimeConfigurationError(
            "packaged release asset manifest differs from the verified files"
        )


def load_core_bootstrap_config(
    asset_root: Path | str,
    *,
    release_assets_root: Path | str | None = None,
    daemon_asset_root: Path | str | None = None,
    runtime_asset_root: Path | str | None = None,
    source_commit: str,
    packaged_resource_assets: bool = False,
) -> CoreBootstrapConfigV1:
    """Load one exact embedded wheel/lock pair through a pinned directory fd."""

    if _SOURCE_COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise ReleaseRuntimeConfigurationError("Core bootstrap source commit is invalid")
    allowed_owner_ids = (
        frozenset({0, os.getuid()}) if packaged_resource_assets else frozenset({os.getuid()})
    )
    release_manifest: _ReleaseAssetsManifest | None = None
    if packaged_resource_assets:
        if release_assets_root is None:
            raise ReleaseRuntimeConfigurationError(
                "packaged release assets require their canonical root"
            )
        release_manifest = _load_release_assets_manifest(
            release_assets_root,
            source_commit=source_commit,
            allowed_owner_ids=allowed_owner_ids,
        )
        expected_core_root = release_manifest.root / _CORE_ASSET_DIRECTORY_NAME
        expected_daemon_root = release_manifest.root / _DAEMON_ASSET_DIRECTORY_NAME
        expected_runtime_root = release_manifest.root / _MANAGED_RUNTIME_ASSET_DIRECTORY_NAME
        if _canonical_absolute_directory(asset_root) != expected_core_root:
            raise ReleaseRuntimeConfigurationError(
                "packaged Core assets are outside the release asset root"
            )
        if daemon_asset_root is not None and (
            _canonical_absolute_directory(daemon_asset_root) != expected_daemon_root
        ):
            raise ReleaseRuntimeConfigurationError(
                "packaged Daemon assets are outside the release asset root"
            )
        if runtime_asset_root is not None and (
            _canonical_absolute_directory(runtime_asset_root) != expected_runtime_root
        ):
            raise ReleaseRuntimeConfigurationError(
                "packaged runtime assets are outside the release asset root"
            )
        daemon_asset_root = expected_daemon_root
        runtime_asset_root = expected_runtime_root
    try:
        root, root_fd = _open_directory_without_symlinks(asset_root)
    except OSError as exc:
        raise ReleaseRuntimeConfigurationError(
            "Core release asset directory could not be verified"
        ) from exc
    try:
        root_stat = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid not in allowed_owner_ids
            or stat.S_IMODE(root_stat.st_mode) & 0o022
        ):
            raise ReleaseRuntimeConfigurationError(
                "Core release asset directory is not private owner-controlled storage"
            )
        names: list[str] = []
        with os.scandir(root_fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > _MAX_ASSET_DIRECTORY_ENTRIES:
                    raise ReleaseRuntimeConfigurationError(
                        "Core release asset directory exceeds its entry budget"
                    )
        wheel_names = sorted(name for name in names if name.endswith(".whl"))
        if len(wheel_names) != 1 or set(names) != {wheel_names[0], _FRAMEWORK_LOCK_NAME}:
            raise ReleaseRuntimeConfigurationError(
                "Core release assets must contain exactly one wheel and framework lock"
            )
        wheel = _seal_file(
            root_fd,
            root,
            wheel_names[0],
            max_bytes=MAX_CORE_WHEEL_BYTES,
            allowed_owner_ids=allowed_owner_ids,
        )
        lock = _seal_file(
            root_fd,
            root,
            _FRAMEWORK_LOCK_NAME,
            max_bytes=MAX_FRAMEWORK_LOCK_BYTES,
            retain_payload=True,
            allowed_owner_ids=allowed_owner_ids,
        )
        assert lock.payload is not None
        _validate_framework_lock(lock.payload, wheel.asset)
        daemon_bundle = _load_daemon_bundle(
            daemon_asset_root
            if daemon_asset_root is not None
            else root.parent / _DAEMON_ASSET_DIRECTORY_NAME,
            wheel=wheel.asset,
            framework_lock=lock.asset,
            framework_lock_payload=lock.payload,
            source_commit=source_commit,
            allowed_owner_ids=allowed_owner_ids,
        )
        runtime_archive = _load_managed_runtime_archive(
            runtime_asset_root
            if runtime_asset_root is not None
            else root.parent / _MANAGED_RUNTIME_ASSET_DIRECTORY_NAME,
            allowed_owner_ids=allowed_owner_ids,
            require_private=not packaged_resource_assets,
        )
        config = CoreBootstrapConfigV1(
            source_commit=source_commit,
            wheel=wheel.asset,
            framework_lock=lock.asset,
            daemon_bundle=daemon_bundle,
            managed_runtime_archive=runtime_archive,
            replace_mismatched=True,
        )
        if release_manifest is not None:
            _validate_release_assets_manifest_inventory(release_manifest, config)
        return config
    except OSError as exc:
        raise ReleaseRuntimeConfigurationError(
            "Core release assets could not be verified"
        ) from exc
    finally:
        os.close(root_fd)


def create_release_core_runtime(
    *,
    provider_store: DesktopProviderStore,
    workspace_store: WorkspaceImportStore,
    remote_lifecycle: DesktopRemoteLifecycle,
    asset_root: Path | str,
    source_commit: str,
    release_assets_root: Path | str | None = None,
    daemon_asset_root: Path | str | None = None,
    runtime_asset_root: Path | str | None = None,
    packaged_resource_assets: bool = False,
    startup_phase: Callable[[str], None] | None = None,
) -> DesktopReleaseCoreRuntimeV1:
    """Compose the production Core runtime used by the packaged Desktop sidecar."""

    if startup_phase is not None:
        startup_phase("core_bridge_store")
    bridge_store = DesktopCoreBridgeStoreV1(provider_store.state_root / "core-bridge-v1")
    broker: DesktopEventBrokerV1 | None = None
    bridge: DesktopCoreBridgeV1 | None = None
    try:
        if startup_phase is not None:
            startup_phase("event_broker")
        broker = DesktopEventBrokerV1()
        if startup_phase is not None:
            startup_phase("core_adapter")
        adapter = _DeferredCoreSshBridgeAdapterV1(
            remote_lifecycle,
            lambda: load_core_bootstrap_config(
                asset_root,
                release_assets_root=release_assets_root,
                daemon_asset_root=daemon_asset_root,
                runtime_asset_root=runtime_asset_root,
                source_commit=source_commit,
                packaged_resource_assets=packaged_resource_assets,
            ),
        )
        archive_source = ProviderWorkspaceArchiveSourceV1(provider_store, workspace_store)
        if startup_phase is not None:
            startup_phase("core_bridge")
        bridge = DesktopCoreBridgeV1(
            host_service=adapter,
            tunnel_factory=adapter,
            persistence=bridge_store,
            archive_source=archive_source,
            transport_factory=adapter.new_http_transport,
            activation_timeout=_RELEASE_ACTIVATION_TIMEOUT_SECONDS,
        )
        if startup_phase is not None:
            startup_phase("core_runtime")
        return DesktopReleaseCoreRuntimeV1(
            bridge=bridge,
            event_broker=broker,
            bridge_store=bridge_store,
            managed_runtime_available=runtime_asset_root is not None,
        )
    except BaseException:
        if bridge is not None:
            _cleanup_after_primary_failure(bridge.close)
        if broker is not None:
            _cleanup_after_primary_failure(broker.close)
        _cleanup_after_primary_failure(bridge_store.close)
        raise


def _seal_file(
    root_fd: int,
    root: Path,
    name: str,
    *,
    max_bytes: int,
    retain_payload: bool = False,
    require_private: bool = False,
    allowed_owner_ids: frozenset[int] | None = None,
) -> _SealedFile:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise ReleaseRuntimeConfigurationError("Core release asset name is invalid")
    if allowed_owner_ids is None:
        allowed_owner_ids = frozenset({os.getuid()})
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=root_fd,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in allowed_owner_ids
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or (require_private and stat.S_IMODE(metadata.st_mode) & 0o077)
            or not 0 < metadata.st_size <= max_bytes
        ):
            raise ReleaseRuntimeConfigurationError("Core release asset identity is invalid")
        digest = hashlib.sha256()
        payload_parts: list[bytes] = []
        observed = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - observed))
            if not chunk:
                break
            observed += len(chunk)
            if observed > max_bytes:
                raise ReleaseRuntimeConfigurationError(
                    "Core release asset exceeds its byte budget"
                )
            digest.update(chunk)
            if retain_payload:
                payload_parts.append(chunk)
        current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if (
            observed != metadata.st_size
            or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
        ):
            raise ReleaseRuntimeConfigurationError("Core release asset changed during sealing")
        return _SealedFile(
            asset=SealedCoreBootstrapAssetV1(
                local_path=str(root / name),
                sha256=digest.hexdigest(),
                byte_size=observed,
            ),
            payload=b"".join(payload_parts) if retain_payload else None,
        )
    finally:
        os.close(descriptor)


def _load_managed_runtime_archive(
    asset_root: Path | str,
    *,
    allowed_owner_ids: frozenset[int] | None = None,
    require_private: bool = True,
) -> SealedManagedRuntimeArchiveV1 | None:
    if allowed_owner_ids is None:
        allowed_owner_ids = frozenset({os.getuid()})
    root = Path(asset_root)
    try:
        root_lstat = root.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(root_lstat.st_mode):
        raise ReleaseRuntimeConfigurationError(
            "managed runtime release asset directory is invalid"
        )
    try:
        absolute_root, root_fd = _open_directory_without_symlinks(root)
    except OSError as exc:
        raise ReleaseRuntimeConfigurationError(
            "managed runtime release assets could not be verified"
        ) from exc
    try:
        metadata = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in allowed_owner_ids
            or stat.S_IMODE(metadata.st_mode) & (0o077 if require_private else 0o022)
        ):
            raise ReleaseRuntimeConfigurationError(
                "managed runtime release asset directory is not private owner-controlled storage"
            )
        names = tuple(entry.name for entry in os.scandir(root_fd))
        release = MANAGED_RUNTIME_ARCHIVE_RELEASE
        if names != (release.filename,):
            raise ReleaseRuntimeConfigurationError(
                "managed runtime release assets must contain the exact archive"
            )
        sealed = _seal_file(
            root_fd,
            absolute_root,
            release.filename,
            max_bytes=release.byte_size,
            require_private=require_private,
            allowed_owner_ids=allowed_owner_ids,
        ).asset
        if sealed.byte_size != release.byte_size or sealed.sha256 != release.sha256:
            raise ReleaseRuntimeConfigurationError(
                "managed runtime release archive identity is invalid"
            )
        try:
            verify_managed_runtime_archive(
                sealed.local_path,
                release=release,
                allowed_owner_ids=allowed_owner_ids,
                require_private=require_private,
            )
        except ManagedRuntimeArchiveVerificationError as exc:
            raise ReleaseRuntimeConfigurationError(
                "managed runtime release archive contents are invalid"
            ) from exc
        return SealedManagedRuntimeArchiveV1(
            local_path=sealed.local_path,
            sha256=sealed.sha256,
            byte_size=sealed.byte_size,
            platform=release.platform,
            config_id=release.config_id,
            oci_index_id=release.oci_index_id,
        )
    except OSError as exc:
        raise ReleaseRuntimeConfigurationError(
            "managed runtime release assets could not be verified"
        ) from exc
    finally:
        os.close(root_fd)


def _load_daemon_bundle(
    asset_root: Path | str,
    *,
    wheel: SealedCoreBootstrapAssetV1,
    framework_lock: SealedCoreBootstrapAssetV1,
    framework_lock_payload: bytes,
    source_commit: str,
    allowed_owner_ids: frozenset[int] | None = None,
) -> SealedDaemonBundleV1 | None:
    if allowed_owner_ids is None:
        allowed_owner_ids = frozenset({os.getuid()})
    root = Path(asset_root)
    try:
        root_lstat = root.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(root_lstat.st_mode):
        raise ReleaseRuntimeConfigurationError("Daemon release asset directory is invalid")
    try:
        root, root_fd = _open_directory_without_symlinks(root)
    except OSError as exc:
        raise ReleaseRuntimeConfigurationError(
            "Daemon release assets could not be verified"
        ) from exc
    try:
        metadata = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in allowed_owner_ids
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise ReleaseRuntimeConfigurationError(
                "Daemon release asset directory is not owner-controlled storage"
            )
        names = tuple(sorted(entry.name for entry in os.scandir(root_fd)))
        if names != (_DAEMON_MANIFEST_NAME, _DAEMON_BUNDLE_NAME):
            raise ReleaseRuntimeConfigurationError(
                "Daemon release assets must contain exactly the bundle and manifest"
            )
        binary = _seal_file(
            root_fd,
            root,
            _DAEMON_BUNDLE_NAME,
            max_bytes=MAX_CORE_WHEEL_BYTES,
            allowed_owner_ids=allowed_owner_ids,
        )
        manifest = _seal_file(
            root_fd,
            root,
            _DAEMON_MANIFEST_NAME,
            max_bytes=_MAX_DAEMON_MANIFEST_BYTES,
            retain_payload=True,
            allowed_owner_ids=allowed_owner_ids,
        )
        assert manifest.payload is not None
        value = _validate_daemon_manifest(
            manifest.payload,
            bundle=binary.asset,
            wheel=wheel,
            framework_lock=framework_lock,
            framework_lock_payload=framework_lock_payload,
            source_commit=source_commit,
        )
        return SealedDaemonBundleV1(
            local_path=binary.asset.local_path,
            sha256=binary.asset.sha256,
            byte_size=binary.asset.byte_size,
            manifest_sha256=manifest.asset.sha256,
            release_identity=value["release"]["identity"],
            registry_digest=value["core"]["registry_digest"],
            source_commit=source_commit,
            wheel_sha256=wheel.sha256,
            dependency_lock_sha256=value["dependency_lock"]["sha256"],
            framework_lock_sha256=framework_lock.sha256,
        )
    except OSError as exc:
        raise ReleaseRuntimeConfigurationError(
            "Daemon release assets could not be verified"
        ) from exc
    finally:
        os.close(root_fd)


def _validate_daemon_manifest(
    payload: bytes,
    *,
    bundle: SealedCoreBootstrapAssetV1,
    wheel: SealedCoreBootstrapAssetV1,
    framework_lock: SealedCoreBootstrapAssetV1,
    framework_lock_payload: bytes,
    source_commit: str,
) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicates)
        lock = json.loads(
            framework_lock_payload.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReleaseRuntimeConfigurationError("Daemon release manifest is invalid") from exc
    canonical = (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
        if isinstance(value, dict)
        else ""
    ).encode("utf-8")
    expected_top = {
        "artifact",
        "build_environment_distributions",
        "core",
        "dependency_lock",
        "platform",
        "release",
        "runtime",
        "schema_version",
        "smoke",
    }
    if not isinstance(value, dict) or set(value) != expected_top or payload != canonical:
        raise ReleaseRuntimeConfigurationError(
            "Daemon release manifest does not use the canonical closed schema"
        )
    artifact = value.get("artifact")
    core = value.get("core")
    dependency_lock = value.get("dependency_lock")
    platform_value = value.get("platform")
    release = value.get("release")
    runtime = value.get("runtime")
    smoke = value.get("smoke")
    distributions = value.get("build_environment_distributions")
    if (
        not isinstance(lock, dict)
        or not isinstance(artifact, dict)
        or set(artifact) != {"filename", "sha256", "size"}
        or artifact
        != {
            "filename": _DAEMON_BUNDLE_NAME,
            "sha256": bundle.sha256,
            "size": bundle.byte_size,
        }
        or not isinstance(core, dict)
        or set(core) != {"framework_lock", "registry_digest", "wheel"}
        or not isinstance(core.get("framework_lock"), dict)
        or core["framework_lock"]
        != {
            "filename": _FRAMEWORK_LOCK_NAME,
            "sha256": framework_lock.sha256,
        }
        or not isinstance(core.get("wheel"), dict)
        or core["wheel"]
        != {
            "filename": Path(wheel.local_path).name,
            "sha256": wheel.sha256,
            "size": wheel.byte_size,
            "version": lock.get("distribution_version"),
        }
        or _DIGEST_PATTERN.fullmatch(str(core.get("registry_digest"))) is None
        or not isinstance(dependency_lock, dict)
        or set(dependency_lock) != {"filename", "sha256"}
        or dependency_lock.get("filename") != "uv.lock"
        or _DIGEST_PATTERN.fullmatch(str(dependency_lock.get("sha256"))) is None
        or platform_value != {"architecture": "x86_64", "system": "linux"}
        or not isinstance(release, dict)
        or set(release) != {"identity", "source_commit"}
        or release.get("source_commit") != source_commit
        or _DIGEST_PATTERN.fullmatch(str(release.get("identity"))) is None
        or not isinstance(runtime, dict)
        or set(runtime) != {"format", "python", "system_python_required", "target_pypi_required"}
        or runtime.get("format") != "pyinstaller-onefile"
        or runtime.get("system_python_required") is not False
        or runtime.get("target_pypi_required") is not False
        or not isinstance(runtime.get("python"), dict)
        or set(runtime["python"]) != {"implementation", "version"}
        or runtime["python"].get("implementation") != "CPython"
        or not isinstance(runtime["python"].get("version"), str)
        or not runtime["python"]["version"].startswith("3.11.")
        or value.get("schema_version") != 1
        or smoke
        != {
            "backend_readiness": "passed",
            "controlled_exit": "passed",
            "identity": "passed",
        }
        or not isinstance(distributions, list)
        or len(distributions) > 4096
        or any(
            not isinstance(item, dict)
            or set(item) != {"name", "version"}
            or not isinstance(item.get("name"), str)
            or not 0 < len(item["name"]) <= 256
            or not isinstance(item.get("version"), str)
            or not 0 < len(item["version"]) <= 256
            for item in distributions
        )
    ):
        raise ReleaseRuntimeConfigurationError(
            "Daemon release manifest does not bind the exact Desktop release"
        )
    return value


def _validate_framework_lock(
    payload: bytes,
    wheel: SealedCoreBootstrapAssetV1,
) -> None:
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseRuntimeConfigurationError("Core framework lock is invalid") from exc
    expected_keys = {
        "schema_version",
        "distribution",
        "distribution_version",
        "distribution_digest",
        "wheel_filename",
    }
    canonical = (
        json.dumps(parsed, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
        if isinstance(parsed, dict)
        else ""
    ).encode("utf-8")
    if (
        not isinstance(parsed, dict)
        or set(parsed) != expected_keys
        or parsed.get("schema_version") != "1"
        or parsed.get("distribution") != "openevo"
        or not isinstance(parsed.get("distribution_version"), str)
        or not parsed["distribution_version"]
        or parsed.get("distribution_digest") != wheel.sha256
        or _DIGEST_PATTERN.fullmatch(str(parsed.get("distribution_digest"))) is None
        or parsed.get("wheel_filename") != Path(wheel.local_path).name
        or payload != canonical
    ):
        raise ReleaseRuntimeConfigurationError(
            "Core framework lock does not bind the exact packaged wheel"
        )


__all__ = (
    "CoreRuntimeSessionBinding",
    "DesktopReleaseCoreRuntimeV1",
    "ProviderWorkspaceArchiveSourceV1",
    "ReleaseLocalStateV2",
    "ReleaseRuntimeConfigurationError",
    "create_release_core_runtime",
    "create_release_local_state_v2",
    "load_core_bootstrap_config",
)
