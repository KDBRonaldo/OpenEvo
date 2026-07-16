from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import desktop.sidecar.release_runtime as release_runtime
from desktop.sidecar.core_bridge_v1 import DesktopCoreBridgeErrorV1
from desktop.sidecar.event_broker_v1 import DesktopEventBrokerError
from desktop.sidecar.provider_store import DesktopProviderStore
from desktop.sidecar.release_app import create_release_desktop_local_api_app
from desktop.sidecar.release_provider import DesktopReleaseProvider
from desktop.sidecar.release_runtime import (
    CoreRuntimeSessionBinding,
    DesktopCoreEventRelayV1,
    DesktopReleaseCoreRuntimeV1,
    ReleaseRuntimeConfigurationError,
    bundled_core_asset_root,
    create_release_core_runtime,
    load_core_bootstrap_config,
)
from desktop.sidecar.remote_lifecycle import DesktopRemoteLifecycle
from desktop.sidecar.workspace_imports import WorkspaceImportStore
from openevo.backend.contracts.v1 import models as core_v1
from openevo.deployment.host_keys import ProviderKnownHostStore


SOURCE_COMMIT = "a" * 40


def _assets(root: Path, *, wheel_payload: bytes = b"wheel-v1") -> Path:
    root.mkdir(mode=0o700)
    wheel_name = "openevo-0.1.0-py3-none-any.whl"
    wheel = root / wheel_name
    wheel.write_bytes(wheel_payload)
    lock = {
        "schema_version": "1",
        "distribution": "openevo",
        "distribution_version": "0.1.0",
        "distribution_digest": hashlib.sha256(wheel_payload).hexdigest(),
        "wheel_filename": wheel_name,
    }
    (root / "framework-lock.json").write_text(
        json.dumps(lock, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root


def test_load_core_bootstrap_config_binds_exact_packaged_pair(tmp_path: Path) -> None:
    root = _assets(tmp_path / "assets")

    config = load_core_bootstrap_config(root, source_commit=SOURCE_COMMIT)

    assert config.source_commit == SOURCE_COMMIT
    assert config.replace_mismatched is True
    assert config.wheel.local_path == str(root.resolve() / "openevo-0.1.0-py3-none-any.whl")
    assert config.wheel.sha256 == hashlib.sha256(b"wheel-v1").hexdigest()
    assert config.framework_lock.local_path == str(root.resolve() / "framework-lock.json")


@pytest.mark.parametrize("mutation", ["extra", "lock", "world_writable", "symlink"])
def test_load_core_bootstrap_config_rejects_unsealed_assets(
    tmp_path: Path,
    mutation: str,
) -> None:
    root = _assets(tmp_path / "assets")
    if mutation == "extra":
        (root / "extra.txt").write_text("unexpected", encoding="utf-8")
    elif mutation == "lock":
        payload = json.loads((root / "framework-lock.json").read_text(encoding="utf-8"))
        payload["distribution_digest"] = "b" * 64
        (root / "framework-lock.json").write_text(
            json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
    elif mutation == "world_writable":
        root.chmod(0o777)
    else:
        wheel = root / "openevo-0.1.0-py3-none-any.whl"
        wheel.unlink()
        wheel.symlink_to(root / "framework-lock.json")

    with pytest.raises(ReleaseRuntimeConfigurationError):
        load_core_bootstrap_config(root, source_commit=SOURCE_COMMIT)


def test_bundled_asset_root_requires_absolute_pyinstaller_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert bundled_core_asset_root() == tmp_path / "openevo" / "wheels"
    monkeypatch.setattr(sys, "_MEIPASS", "relative")
    with pytest.raises(ReleaseRuntimeConfigurationError):
        bundled_core_asset_root()


def test_release_runtime_composes_and_closes_owned_resources(tmp_path: Path) -> None:
    assets = _assets(tmp_path / "assets")
    provider_store = DesktopProviderStore(tmp_path / "state")
    workspace_store = WorkspaceImportStore(
        provider_store.state_root / "workspace-imports",
        reconcile_on_open=False,
    )
    lifecycle = DesktopRemoteLifecycle(
        ProviderKnownHostStore(
            provider_store.state_root / "ssh-host-keys",
            secure_ancestor=provider_store.state_root,
        )
    )
    runtime = create_release_core_runtime(
        provider_store=provider_store,
        workspace_store=workspace_store,
        remote_lifecycle=lifecycle,
        asset_root=assets,
        source_commit=SOURCE_COMMIT,
    )
    runtime.start(
        active_project=lambda: None,
        publish=lambda: None,
        session_lost=lambda _binding, _error: None,
    )
    runtime.close()
    runtime.close()
    lifecycle.close()
    workspace_store.close()
    provider_store.close()


def test_release_runtime_cleanup_does_not_replace_composition_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = RuntimeError("runtime composition canary")
    cleanup_canary = RuntimeError("bridge store cleanup canary")

    class FailingBridgeStore:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            raise cleanup_canary

    bridge_store = FailingBridgeStore()
    monkeypatch.setattr(
        release_runtime,
        "load_core_bootstrap_config",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        release_runtime,
        "DesktopCoreBridgeStoreV1",
        lambda *_args, **_kwargs: bridge_store,
    )

    def startup_phase(phase: str) -> None:
        if phase == "event_broker":
            raise canary

    with pytest.raises(RuntimeError) as exc_info:
        create_release_core_runtime(
            provider_store=SimpleNamespace(state_root=tmp_path),
            workspace_store=SimpleNamespace(),
            remote_lifecycle=SimpleNamespace(),
            asset_root=tmp_path / "unused-assets",
            source_commit=SOURCE_COMMIT,
            startup_phase=startup_phase,
        )

    assert exc_info.value is canary
    assert exc_info.value is not cleanup_canary
    assert bridge_store.close_calls == 1


def test_release_runtime_close_attempts_every_owned_resource_and_keeps_first_failure() -> None:
    calls: list[str] = []
    bridge_canary = RuntimeError("bridge close canary")
    broker_canary = RuntimeError("broker close canary")
    store_canary = RuntimeError("bridge store close canary")

    class Bridge:
        def close(self) -> None:
            calls.append("bridge")
            raise bridge_canary

    class Broker:
        def close(self) -> None:
            calls.append("broker")
            raise broker_canary

    class BridgeStore:
        def close(self) -> None:
            calls.append("store")
            raise store_canary

    class Relay:
        def request_stop(self) -> None:
            calls.append("relay_stop")

        def join(self) -> None:
            calls.append("relay_join")

    runtime = DesktopReleaseCoreRuntimeV1(
        bridge=Bridge(),
        event_broker=Broker(),
        bridge_store=BridgeStore(),
    )
    runtime._relay = Relay()

    with pytest.raises(RuntimeError) as exc_info:
        runtime.close()

    assert exc_info.value is bridge_canary
    assert exc_info.value is not broker_canary
    assert exc_info.value is not store_canary
    assert calls == ["relay_stop", "bridge", "relay_join", "broker", "store"]

    runtime.close()
    assert calls == ["relay_stop", "bridge", "relay_join", "broker", "store"]


def test_release_runtime_close_is_linearized_across_threads() -> None:
    calls: list[str] = []
    bridge_entered = threading.Event()
    release_bridge = threading.Event()
    second_close_returned = threading.Event()

    class Bridge:
        def close(self) -> None:
            calls.append("bridge_enter")
            bridge_entered.set()
            assert release_bridge.wait(2)
            calls.append("bridge_exit")

    class Closable:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            calls.append(self.name)

    class Relay:
        def request_stop(self) -> None:
            calls.append("relay_stop")

        def join(self) -> None:
            calls.append("relay_join")

    runtime = DesktopReleaseCoreRuntimeV1(
        bridge=Bridge(),
        event_broker=Closable("broker"),
        bridge_store=Closable("store"),
    )
    runtime._relay = Relay()
    failures: list[BaseException] = []

    def close_runtime(*, completed: threading.Event | None = None) -> None:
        try:
            runtime.close()
        except BaseException as exc:
            failures.append(exc)
        finally:
            if completed is not None:
                completed.set()

    first = threading.Thread(target=close_runtime)
    second = threading.Thread(
        target=close_runtime,
        kwargs={"completed": second_close_returned},
    )
    first.start()
    assert bridge_entered.wait(2)
    second.start()
    assert not second_close_returned.wait(0.1)
    assert calls == ["relay_stop", "bridge_enter"]

    release_bridge.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == []
    assert calls == ["relay_stop", "bridge_enter", "bridge_exit", "relay_join", "broker", "store"]


def test_release_provider_close_attempts_all_resources_and_keeps_first_failure() -> None:
    calls: list[str] = []
    bridge_canary = RuntimeError("runtime stop canary")
    broker_canary = RuntimeError("runtime close canary")

    def close(name: str, failure: BaseException | None = None) -> None:
        calls.append(name)
        if failure is not None:
            raise failure

    runtime = SimpleNamespace(
        stop=lambda: close("runtime_stop", bridge_canary),
        close=lambda: close("runtime_close", broker_canary),
    )
    provider = object.__new__(DesktopReleaseProvider)
    provider._close_lock = threading.RLock()
    provider._closed = False
    provider._project_executor = SimpleNamespace(close=lambda: close("executor"))
    provider._core_runtime = runtime
    provider._core_bridge = None
    provider._event_broker = None
    provider._remote_lifecycle = SimpleNamespace(close=lambda: close("lifecycle"))
    provider._store = SimpleNamespace(close=lambda: close("store"))
    provider._workspace_import_store = SimpleNamespace(close=lambda: close("workspace"))

    with pytest.raises(RuntimeError) as exc_info:
        provider.close()

    assert exc_info.value is bridge_canary
    assert exc_info.value is not broker_canary
    assert calls == [
        "executor",
        "runtime_stop",
        "runtime_close",
        "lifecycle",
        "store",
        "workspace",
    ]

    provider.close()
    assert calls == [
        "executor",
        "runtime_stop",
        "runtime_close",
        "lifecycle",
        "store",
        "workspace",
    ]


def test_asset_directory_budget_is_checked_without_accepting_late_entries(
    tmp_path: Path,
) -> None:
    root = _assets(tmp_path / "assets")
    for index in range(9):
        (root / f"extra-{index}").write_bytes(b"x")

    with pytest.raises(ReleaseRuntimeConfigurationError, match="entry budget"):
        load_core_bootstrap_config(root, source_commit=SOURCE_COMMIT)


def test_asset_root_must_be_owned_by_current_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _assets(tmp_path / "assets")
    monkeypatch.setattr(os, "getuid", lambda: os.stat(root).st_uid + 1)
    with pytest.raises(ReleaseRuntimeConfigurationError, match="owner-controlled"):
        load_core_bootstrap_config(root, source_commit=SOURCE_COMMIT)


def test_release_app_composes_full_remote_feature_surface(tmp_path: Path) -> None:
    assets = _assets(tmp_path / "assets")
    token = "desktop-session-token-0000000000000011"
    app = create_release_desktop_local_api_app(
        state_root=tmp_path / "state",
        session_token=token,
        instance_id="1" * 32,
        readiness_key=b"r" * 32,
        source_commit=SOURCE_COMMIT,
        build_channel="test",
        core_assets_root=assets,
    )

    with TestClient(app) as client:
        response = client.get(
            "/version",
            headers={"X-OpenEvo-Desktop-Session": token},
        )

    assert response.status_code == 200
    assert response.json()["feature_flags"] == [
        "remote_profiles",
        "project_validation",
        "operation_events",
        "run_observability",
        "artifact_inspection",
    ]


def test_core_event_relay_skips_heartbeat_and_invalidates_on_change() -> None:
    heartbeat = core_v1.HeartbeatEventV1(
        id="heartbeat-1",
        sequence=1,
        occurred_at="2026-07-14T12:00:00Z",
        event="heartbeat.v1",
        payload=core_v1.HeartbeatPayloadV1(active_run_count=0),
    )
    frames = (
        SimpleNamespace(id="heartbeat-1", data=SimpleNamespace(root=heartbeat)),
        SimpleNamespace(id="run-change-1", data=SimpleNamespace(root=object())),
    )

    class EventContext:
        def __enter__(self):
            return iter(frames)

        def __exit__(self, *_exc: object) -> None:
            return None

    class Bridge:
        def __init__(self) -> None:
            self.calls: list[tuple[object, str | None]] = []

        def events(self, project: object, *, last_event_id: str | None = None):
            self.calls.append((project, last_event_id))
            return EventContext()

    bridge = Bridge()
    relay = DesktopCoreEventRelayV1(bridge)  # type: ignore[arg-type]
    project = SimpleNamespace(project_id="project-1", etag='"' + "a" * 64 + '"')
    published = threading.Event()
    publish_count = 0

    binding = CoreRuntimeSessionBinding(project=project, generation=1)  # type: ignore[arg-type]

    def active_project():
        return None if published.is_set() else binding

    def publish() -> None:
        nonlocal publish_count
        publish_count += 1
        published.set()

    relay.start(
        active_project=active_project,
        publish=publish,
        session_lost=lambda _binding, _error: None,
    )
    assert published.wait(timeout=2)
    relay.request_stop()
    relay.join()

    assert publish_count == 1
    assert bridge.calls == [(project, None)]


def test_core_event_relay_reports_typed_session_loss_with_captured_authority() -> None:
    error = DesktopCoreBridgeErrorV1(
        core_v1.ApiErrorV1(
            request_id="relay-client-closed",
            code="core_client_closed",
            http_status=503,
            message="The Core client is closed.",
            severity=core_v1.ErrorSeverity.BLOCKING,
            category=core_v1.ErrorCategory.SERVICE,
            retryable=True,
            repair_action=core_v1.RepairAction.OPENEVO_CAN_RETRY,
            next_action="Reactivate the project.",
        )
    )

    class Bridge:
        def events(self, _project: object, *, last_event_id: str | None = None):
            del last_event_id
            raise error

    project = SimpleNamespace(project_id="project-1", etag='"' + "a" * 64 + '"')
    binding = CoreRuntimeSessionBinding(project=project, generation=7)  # type: ignore[arg-type]
    lost = threading.Event()
    observed: list[tuple[CoreRuntimeSessionBinding, DesktopCoreBridgeErrorV1]] = []
    relay = DesktopCoreEventRelayV1(Bridge())  # type: ignore[arg-type]

    def active_project():
        return None if lost.is_set() else binding

    def session_lost(
        candidate: CoreRuntimeSessionBinding,
        exc: DesktopCoreBridgeErrorV1,
    ) -> None:
        observed.append((candidate, exc))
        lost.set()

    relay.start(
        active_project=active_project,
        publish=lambda: None,
        session_lost=session_lost,
    )
    assert lost.wait(timeout=2)
    relay.request_stop()
    relay.join()

    assert observed == [(binding, error)]


def test_core_event_relay_commits_cursor_only_after_publication_and_replays_after_fault() -> None:
    frame_1 = SimpleNamespace(id="event-1", data=SimpleNamespace(root=SimpleNamespace(sequence=1)))
    frame_2 = SimpleNamespace(id="event-2", data=SimpleNamespace(root=SimpleNamespace(sequence=2)))
    frame_3 = SimpleNamespace(id="event-3", data=SimpleNamespace(root=SimpleNamespace(sequence=3)))

    class EventContext:
        def __init__(self, frames: tuple[object, ...], failure: BaseException | None = None):
            self._frames = frames
            self._failure = failure

        def __enter__(self):
            def stream():
                yield from self._frames
                if self._failure is not None:
                    raise self._failure

            return stream()

        def __exit__(self, *_exc: object) -> None:
            return None

    class Bridge:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        def events(self, _project: object, *, last_event_id: str | None = None):
            self.calls.append(last_event_id)
            if len(self.calls) == 1:
                return EventContext((frame_1,))
            if len(self.calls) == 2:
                return EventContext(
                    (frame_1, frame_1, frame_3),
                    OSError("stream interrupted"),
                )
            if len(self.calls) == 3:
                return EventContext((frame_2,), OSError("stream interrupted again"))
            if len(self.calls) == 4:
                return EventContext((frame_3,))
            return EventContext(())

    bridge = Bridge()
    relay = DesktopCoreEventRelayV1(bridge)  # type: ignore[arg-type]
    project = SimpleNamespace(project_id="project-1", etag='"' + "a" * 64 + '"')
    binding = CoreRuntimeSessionBinding(project=project, generation=1)  # type: ignore[arg-type]
    published: list[str] = []
    complete = threading.Event()
    publication_attempt = 0

    def active_project():
        return None if complete.is_set() else binding

    def publish() -> None:
        nonlocal publication_attempt
        publication_attempt += 1
        if publication_attempt == 1:
            raise DesktopEventBrokerError("injected publication failure")
        published.append(f"publication-{publication_attempt}")
        if len(published) == 5:
            complete.set()

    relay.start(
        active_project=active_project,
        publish=publish,
        session_lost=lambda _binding, _error: None,
    )
    assert complete.wait(timeout=5)
    relay.request_stop()
    relay.join()

    assert published == [
        "publication-2",
        "publication-3",
        "publication-4",
        "publication-5",
        "publication-6",
    ]
    assert bridge.calls[:4] == [None, None, "event-1", "event-2"]
