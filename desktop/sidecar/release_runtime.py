"""Production composition for the release Desktop/Core runtime."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import threading
from typing import BinaryIO

from desktop.sidecar.contracts.v1 import models as local_v1
from desktop.sidecar.core_bridge_adapters_v1 import (
    AdoptedWorkspaceArchiveSourceV1,
    AdoptedWorkspaceImportV1,
    CoreBootstrapConfigV1,
    DesktopCoreSshBridgeAdapterV1,
    SealedCoreBootstrapAssetV1,
    SealedManagedRuntimeArchiveV1,
)
from desktop.sidecar.core_bridge_store_v1 import DesktopCoreBridgeStoreV1
from desktop.sidecar.core_bridge_v1 import DesktopCoreBridgeErrorV1, DesktopCoreBridgeV1
from desktop.sidecar.event_broker_v1 import DesktopEventBrokerError, DesktopEventBrokerV1
from desktop.sidecar.provider_store import DesktopProviderStore, ProviderStoreError
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
_CORE_ASSET_DIRECTORY = Path("openevo/wheels")
_MANAGED_RUNTIME_ASSET_DIRECTORY = Path("openevo/runtime-assets")
_FRAMEWORK_LOCK_NAME = "framework-lock.json"
_RELAY_MIN_BACKOFF_SECONDS = 0.1
_RELAY_MAX_BACKOFF_SECONDS = 2.0
_RELEASE_ACTIVATION_TIMEOUT_SECONDS = 900.0


class ReleaseRuntimeConfigurationError(RuntimeError):
    """Packaged release assets or runtime ownership are invalid."""


class _CoreEventSequenceGapError(RuntimeError):
    """Force replay when a stream skips past the committed event sequence."""


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
class CoreRuntimeSessionBinding:
    """Process-local authority for one provider-published Core session."""

    project: local_v1.ProjectV1
    generation: int


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
        self._publish: Callable[[], None] | None = None
        self._session_lost: (
            Callable[[CoreRuntimeSessionBinding, DesktopCoreBridgeErrorV1], None] | None
        ) = None

    def start(
        self,
        *,
        active_project: Callable[[], CoreRuntimeSessionBinding | None],
        publish: Callable[[], None],
        session_lost: Callable[
            [CoreRuntimeSessionBinding, DesktopCoreBridgeErrorV1], None
        ],
    ) -> None:
        if not callable(active_project) or not callable(publish) or not callable(session_lost):
            raise TypeError("event relay callbacks must be callable")
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("Core event relay was already started")
            self._active_project = active_project
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
            publish = self._publish
            session_lost = self._session_lost
            if active_project is None or publish is None or session_lost is None:
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
                            publish()
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
        publish: Callable[[], None],
        session_lost: Callable[
            [CoreRuntimeSessionBinding, DesktopCoreBridgeErrorV1], None
        ],
    ) -> None:
        with self._close_lock:
            if self._closed or self._stopped:
                raise RuntimeError("release Core runtime is closed")
            self._relay.start(
                active_project=active_project,
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


def bundled_core_asset_root() -> Path:
    """Return the PyInstaller extraction root for embedded Core release assets."""

    import sys

    bundle_root = getattr(sys, "_MEIPASS", None)
    if not isinstance(bundle_root, str) or not Path(bundle_root).is_absolute():
        raise ReleaseRuntimeConfigurationError(
            "release sidecar has no absolute packaged resource root"
        )
    return Path(bundle_root) / _CORE_ASSET_DIRECTORY


def bundled_managed_runtime_asset_root() -> Path:
    """Return the PyInstaller extraction root for the offline runtime archive."""

    import sys

    bundle_root = getattr(sys, "_MEIPASS", None)
    if not isinstance(bundle_root, str) or not Path(bundle_root).is_absolute():
        raise ReleaseRuntimeConfigurationError(
            "release sidecar has no absolute packaged resource root"
        )
    return Path(bundle_root) / _MANAGED_RUNTIME_ASSET_DIRECTORY


def load_core_bootstrap_config(
    asset_root: Path | str,
    *,
    runtime_asset_root: Path | str | None = None,
    source_commit: str,
) -> CoreBootstrapConfigV1:
    """Load one exact embedded wheel/lock pair through a pinned directory fd."""

    if _SOURCE_COMMIT_PATTERN.fullmatch(source_commit) is None:
        raise ReleaseRuntimeConfigurationError("Core bootstrap source commit is invalid")
    root = Path(asset_root).resolve(strict=True)
    root_fd = os.open(
        root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        root_stat = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid != os.getuid()
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
        wheel = _seal_file(root_fd, root, wheel_names[0], max_bytes=MAX_CORE_WHEEL_BYTES)
        lock = _seal_file(
            root_fd,
            root,
            _FRAMEWORK_LOCK_NAME,
            max_bytes=MAX_FRAMEWORK_LOCK_BYTES,
            retain_payload=True,
        )
        assert lock.payload is not None
        _validate_framework_lock(lock.payload, wheel.asset)
        runtime_archive = _load_managed_runtime_archive(
            runtime_asset_root
            if runtime_asset_root is not None
            else root.parent / _MANAGED_RUNTIME_ASSET_DIRECTORY.name
        )
        return CoreBootstrapConfigV1(
            source_commit=source_commit,
            wheel=wheel.asset,
            framework_lock=lock.asset,
            managed_runtime_archive=runtime_archive,
            replace_mismatched=True,
        )
    except OSError as exc:
        raise ReleaseRuntimeConfigurationError("Core release assets could not be verified") from exc
    finally:
        os.close(root_fd)


def create_release_core_runtime(
    *,
    provider_store: DesktopProviderStore,
    workspace_store: WorkspaceImportStore,
    remote_lifecycle: DesktopRemoteLifecycle,
    asset_root: Path | str,
    source_commit: str,
    startup_phase: Callable[[str], None] | None = None,
) -> DesktopReleaseCoreRuntimeV1:
    """Compose the production Core runtime used by the packaged Desktop sidecar."""

    if startup_phase is not None:
        startup_phase("core_assets")
    bootstrap = load_core_bootstrap_config(asset_root, source_commit=source_commit)
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
        adapter = DesktopCoreSshBridgeAdapterV1(remote_lifecycle, bootstrap)
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
            managed_runtime_available=bootstrap.managed_runtime_archive is not None,
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
) -> _SealedFile:
    if Path(name).name != name or name in {"", ".", ".."}:
        raise ReleaseRuntimeConfigurationError("Core release asset name is invalid")
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=root_fd,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
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
                raise ReleaseRuntimeConfigurationError("Core release asset exceeds its byte budget")
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
) -> SealedManagedRuntimeArchiveV1 | None:
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
        absolute_root = Path(os.path.abspath(root))
        current_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for part in absolute_root.parts[1:]:
                next_fd = os.open(
                    part,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_CLOEXEC
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current_fd,
                )
                os.close(current_fd)
                current_fd = next_fd
            root_fd = current_fd
        except BaseException:
            os.close(current_fd)
            raise
    except OSError as exc:
        raise ReleaseRuntimeConfigurationError(
            "managed runtime release assets could not be verified"
        ) from exc
    try:
        metadata = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
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
            require_private=True,
        ).asset
        if sealed.byte_size != release.byte_size or sealed.sha256 != release.sha256:
            raise ReleaseRuntimeConfigurationError(
                "managed runtime release archive identity is invalid"
            )
        try:
            verify_managed_runtime_archive(sealed.local_path, release=release)
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
            "Core framework lock does not bind the exact embedded wheel"
        )


__all__ = (
    "CoreRuntimeSessionBinding",
    "DesktopReleaseCoreRuntimeV1",
    "ProviderWorkspaceArchiveSourceV1",
    "ReleaseRuntimeConfigurationError",
    "bundled_core_asset_root",
    "bundled_managed_runtime_asset_root",
    "create_release_core_runtime",
    "load_core_bootstrap_config",
)
