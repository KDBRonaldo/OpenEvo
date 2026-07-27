from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

import desktop.sidecar.release_runtime as release_runtime
from tests.managed_runtime_testkit import (
    RUNTIME_FILENAME,
    write_test_managed_runtime_archive,
)
from desktop.sidecar.core_bridge_v1 import DesktopCoreBridgeErrorV1
from desktop.sidecar.core_bridge_adapters_v2 import (
    CoreBootstrapConfigV2,
    SealedCoreBootstrapAssetV2,
    SealedDaemonBundleV2,
)
from desktop.sidecar.event_broker_v1 import DesktopEventBrokerError
from desktop.sidecar.core_bridge_v2 import (
    CoreHostAttachmentV2,
    CoreTunnelHandleV2,
    DesktopCoreBridgeErrorV2,
)
from desktop.sidecar.provider_store import DesktopProviderStore, ProviderStoreError
from desktop.sidecar.provider_store_v2 import DesktopProviderStoreV2
from desktop.sidecar.release_app import create_release_desktop_local_api_app
from desktop.sidecar.release_provider import DesktopReleaseProvider
from desktop.sidecar.release_runtime import (
    CoreRuntimeSessionBinding,
    DesktopCoreEventRelayV1,
    DesktopReleaseCoreRuntimeV1,
    DesktopReleaseCoreRuntimeV2,
    ReleaseRuntimeConfigurationError,
    create_release_core_runtime,
    create_release_core_runtime_v2,
    load_core_bootstrap_config,
    load_core_bootstrap_config_v2,
)
from desktop.sidecar.remote_lifecycle import DesktopRemoteLifecycle
from desktop.sidecar.workspace_imports import WorkspaceImportStore
from openevo.backend.contracts.v1 import models as core_v1
from openevo.deployment.host_keys import ProviderKnownHostStore
from tests.openevo.sidecar.test_core_bridge_v2 import _status as _core_v2_status
from tests.openevo.sidecar.test_core_client_v2 import (
    _TOKEN as CORE_V2_TOKEN,
    _version as _core_v2_version,
)


SOURCE_COMMIT = "a" * 40


def _assets(root: Path, *, wheel_payload: bytes = b"wheel-v1") -> Path:
    root.mkdir(mode=0o700, parents=True)
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


def _daemon_assets(
    root: Path,
    wheel_root: Path,
    *,
    source_commit: str = SOURCE_COMMIT,
) -> Path:
    root.mkdir(mode=0o700, parents=True)
    bundle = root / "openevo-daemon-linux-x86_64"
    bundle.write_bytes(b"self-contained-daemon")
    wheel = next(wheel_root.glob("openevo-*.whl"))
    lock_path = wheel_root / "framework-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    manifest = {
        "artifact": {
            "filename": bundle.name,
            "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
            "size": bundle.stat().st_size,
        },
        "build_environment_distributions": [],
        "core": {
            "framework_lock": {
                "filename": lock_path.name,
                "sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
            },
            "registry_digest": "3" * 64,
            "wheel": {
                "filename": wheel.name,
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "size": wheel.stat().st_size,
                "version": lock["distribution_version"],
            },
        },
        "dependency_lock": {"filename": "uv.lock", "sha256": "4" * 64},
        "platform": {"architecture": "x86_64", "system": "linux"},
        "release": {"identity": "2" * 64, "source_commit": source_commit},
        "runtime": {
            "format": "pyinstaller-onefile",
            "python": {"implementation": "CPython", "version": "3.11.15"},
            "system_python_required": False,
            "target_pypi_required": False,
        },
        "schema_version": 1,
        "smoke": {
            "backend_readiness": "passed",
            "controlled_exit": "passed",
            "identity": "passed",
        },
    }
    (root / "openevo-daemon-bundle.json").write_text(
        json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root


def _write_release_assets_manifest(root: Path, *, source_commit: str = SOURCE_COMMIT) -> None:
    files = []
    for path in sorted(
        (
            root / "core/framework-lock.json",
            *tuple((root / "core").glob("openevo-*.whl")),
            root / "daemon/openevo-daemon-bundle.json",
            root / "daemon/openevo-daemon-linux-x86_64",
            root / "runtime" / RUNTIME_FILENAME,
        ),
        key=lambda candidate: candidate.relative_to(root).as_posix(),
    ):
        payload = path.read_bytes()
        files.append(
            {
                "relative_path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "byte_size": len(payload),
            }
        )
    (root / "release-assets.json").write_text(
        json.dumps(
            {
                "files": files,
                "schema_version": 1,
                "source_commit": source_commit,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _packaged_release_assets(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, object]:
    from desktop.sidecar import core_bridge_adapters_v1

    root.mkdir(mode=0o700, parents=True)
    wheel_root = _assets(root / "core")
    _daemon_assets(root / "daemon", wheel_root)
    runtime_root = root / "runtime"
    runtime_root.mkdir(mode=0o700)
    runtime_archive = runtime_root / RUNTIME_FILENAME
    expected = write_test_managed_runtime_archive(runtime_archive)
    runtime_archive.chmod(0o644)
    monkeypatch.setattr(release_runtime, "MANAGED_RUNTIME_ARCHIVE_RELEASE", expected)
    monkeypatch.setattr(core_bridge_adapters_v1, "MANAGED_RUNTIME_ARCHIVE_RELEASE", expected)
    _write_release_assets_manifest(root)
    return wheel_root, expected


def test_load_core_bootstrap_config_binds_exact_packaged_pair(tmp_path: Path) -> None:
    root = _assets(tmp_path / "assets")

    config = load_core_bootstrap_config(root, source_commit=SOURCE_COMMIT)

    assert config.source_commit == SOURCE_COMMIT
    assert config.replace_mismatched is True
    assert config.wheel.local_path == str(root.resolve() / "openevo-0.1.0-py3-none-any.whl")
    assert config.wheel.sha256 == hashlib.sha256(b"wheel-v1").hexdigest()
    assert config.framework_lock.local_path == str(root.resolve() / "framework-lock.json")


def test_load_core_bootstrap_config_binds_exact_daemon_bundle(tmp_path: Path) -> None:
    wheel_root = _assets(tmp_path / "openevo" / "wheels")
    daemon_root = _daemon_assets(tmp_path / "openevo" / "daemon", wheel_root)

    config = load_core_bootstrap_config(
        wheel_root,
        daemon_asset_root=daemon_root,
        source_commit=SOURCE_COMMIT,
    )

    assert config.daemon_bundle is not None
    assert config.daemon_bundle.source_commit == SOURCE_COMMIT
    assert config.daemon_bundle.wheel_sha256 == config.wheel.sha256
    assert config.daemon_bundle.framework_lock_sha256 == config.framework_lock.sha256
    assert str(tmp_path) not in repr(config.daemon_bundle)


def test_v2_bootstrap_loader_reissues_only_exact_v2_asset_identities(
    tmp_path: Path,
) -> None:
    wheel_root = _assets(tmp_path / "openevo" / "wheels")
    daemon_root = _daemon_assets(tmp_path / "openevo" / "daemon", wheel_root)

    config = load_core_bootstrap_config_v2(
        wheel_root,
        daemon_asset_root=daemon_root,
        source_commit=SOURCE_COMMIT,
    )

    assert type(config) is CoreBootstrapConfigV2
    assert type(config.wheel) is SealedCoreBootstrapAssetV2
    assert type(config.framework_lock) is SealedCoreBootstrapAssetV2
    assert type(config.daemon_bundle) is SealedDaemonBundleV2
    assert config.daemon_bundle.source_commit == SOURCE_COMMIT
    assert "V1" not in repr(config)


@pytest.mark.parametrize("mutation", ["binary", "manifest", "source_commit", "extra"])
def test_daemon_bundle_rejects_release_identity_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    wheel_root = _assets(tmp_path / "openevo" / "wheels")
    daemon_root = _daemon_assets(tmp_path / "openevo" / "daemon", wheel_root)
    manifest_path = daemon_root / "openevo-daemon-bundle.json"
    if mutation == "binary":
        (daemon_root / "openevo-daemon-linux-x86_64").write_bytes(b"changed")
    elif mutation == "manifest":
        manifest_path.write_text(manifest_path.read_text() + "\n", encoding="utf-8")
    elif mutation == "source_commit":
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        value["release"]["source_commit"] = "f" * 40
        manifest_path.write_text(
            json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        (daemon_root / "unexpected").write_text("no", encoding="utf-8")

    with pytest.raises(ReleaseRuntimeConfigurationError):
        load_core_bootstrap_config(
            wheel_root,
            daemon_asset_root=daemon_root,
            source_commit=SOURCE_COMMIT,
        )


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


def test_load_core_bootstrap_config_without_runtime_archive_is_not_release_ready(
    tmp_path: Path,
) -> None:
    root = _assets(tmp_path / "assets")

    config = load_core_bootstrap_config(root, source_commit=SOURCE_COMMIT)

    assert config.managed_runtime_archive is None


def test_load_core_bootstrap_config_seals_exact_candidate_runtime_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.sidecar import core_bridge_adapters_v1

    wheel_root = _assets(tmp_path / "openevo" / "wheels")
    runtime_root = tmp_path / "openevo" / "runtime-assets"
    runtime_root.mkdir(mode=0o700)
    archive = runtime_root / RUNTIME_FILENAME
    expected = write_test_managed_runtime_archive(archive)
    monkeypatch.setattr(release_runtime, "MANAGED_RUNTIME_ARCHIVE_RELEASE", expected)
    monkeypatch.setattr(core_bridge_adapters_v1, "MANAGED_RUNTIME_ARCHIVE_RELEASE", expected)

    config = load_core_bootstrap_config(
        wheel_root,
        runtime_asset_root=runtime_root,
        source_commit=SOURCE_COMMIT,
    )

    assert config.managed_runtime_archive is not None
    assert config.managed_runtime_archive.sha256 == expected.sha256
    assert config.managed_runtime_archive.byte_size == expected.byte_size
    assert str(runtime_root) not in repr(config)


@pytest.mark.parametrize("mutation", ["symlink", "hardlink", "writable", "tamper"])
def test_candidate_runtime_archive_rejects_unsealed_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    wheel_root = _assets(tmp_path / "openevo" / "wheels")
    runtime_root = tmp_path / "openevo" / "runtime-assets"
    runtime_root.mkdir(mode=0o700)
    archive = runtime_root / RUNTIME_FILENAME
    expected = write_test_managed_runtime_archive(archive)
    monkeypatch.setattr(release_runtime, "MANAGED_RUNTIME_ARCHIVE_RELEASE", expected)
    if mutation == "symlink":
        target = runtime_root / "target"
        archive.rename(target)
        archive.symlink_to(target)
    elif mutation == "hardlink":
        os.link(archive, runtime_root / "second-link")
    elif mutation == "writable":
        archive.chmod(0o666)
    else:
        archive.write_bytes(b"tampered-runtime")

    with pytest.raises(ReleaseRuntimeConfigurationError):
        load_core_bootstrap_config(
            wheel_root,
            runtime_asset_root=runtime_root,
            source_commit=SOURCE_COMMIT,
        )


def test_packaged_release_manifest_binds_all_lazy_bootstrap_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_root = tmp_path / "openevo-release-assets"
    wheel_root, expected_runtime = _packaged_release_assets(release_root, monkeypatch)

    config = load_core_bootstrap_config(
        wheel_root,
        release_assets_root=release_root,
        source_commit=SOURCE_COMMIT,
        packaged_resource_assets=True,
    )

    assert config.daemon_bundle is not None
    assert config.managed_runtime_archive is not None
    assert config.managed_runtime_archive.sha256 == expected_runtime.sha256


def test_packaged_release_assets_accept_root_owned_read_only_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_root = tmp_path / "openevo-release-assets"
    wheel_root, _expected_runtime = _packaged_release_assets(release_root, monkeypatch)
    owner_id = release_root.stat().st_uid
    if owner_id != 0:
        pytest.skip("root-owned packaged media fixture requires a root test process")
    monkeypatch.setattr(release_runtime.os, "getuid", lambda: owner_id + 1)

    config = load_core_bootstrap_config(
        wheel_root,
        release_assets_root=release_root,
        source_commit=SOURCE_COMMIT,
        packaged_resource_assets=True,
    )

    assert config.managed_runtime_archive is not None


@pytest.mark.parametrize("linked_entry", ("root", "core"))
def test_packaged_release_assets_reject_linked_resource_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    linked_entry: str,
) -> None:
    real_root = tmp_path / "real-release-assets"
    _packaged_release_assets(real_root, monkeypatch)
    release_root = tmp_path / "openevo-release-assets"
    if linked_entry == "root":
        release_root.symlink_to(real_root, target_is_directory=True)
    else:
        release_root.mkdir(mode=0o700)
        for name in ("daemon", "runtime", "release-assets.json"):
            source = real_root / name
            destination = release_root / name
            if source.is_dir():
                source.rename(destination)
            else:
                destination.write_bytes(source.read_bytes())
        (release_root / "core").symlink_to(real_root / "core", target_is_directory=True)

    with pytest.raises(ReleaseRuntimeConfigurationError):
        load_core_bootstrap_config(
            release_root / "core",
            release_assets_root=release_root,
            source_commit=SOURCE_COMMIT,
            packaged_resource_assets=True,
        )


@pytest.mark.parametrize(
    "mutation",
    ("source_commit", "file_digest", "extra_root", "noncanonical"),
)
def test_packaged_release_manifest_rejects_composition_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    release_root = tmp_path / "openevo-release-assets"
    wheel_root, _expected_runtime = _packaged_release_assets(release_root, monkeypatch)
    manifest_path = release_root / "release-assets.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "source_commit":
        manifest["source_commit"] = "f" * 40
    elif mutation == "file_digest":
        manifest["files"][0]["sha256"] = "f" * 64
    elif mutation == "extra_root":
        (release_root / "unexpected").mkdir()
    else:
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if mutation not in {"extra_root", "noncanonical"}:
        manifest_path.write_text(
            json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )

    with pytest.raises(ReleaseRuntimeConfigurationError):
        load_core_bootstrap_config(
            wheel_root,
            release_assets_root=release_root,
            source_commit=SOURCE_COMMIT,
            packaged_resource_assets=True,
        )


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
    assert runtime.core_bridge._activation_timeout == 900.0
    assert runtime.core_bridge._timeout == 60.0
    assert runtime.managed_runtime_available is False
    runtime.start(
        active_project=lambda: None,
        refresh_authority=lambda _binding: None,
        publish=lambda _binding: None,
        session_lost=lambda _binding, _error: None,
    )
    runtime.close()
    runtime.close()
    lifecycle.close()
    workspace_store.close()
    provider_store.close()


def test_release_runtime_does_not_read_remote_assets_before_first_core_use(
    tmp_path: Path,
) -> None:
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
    missing_assets = tmp_path / "assets-that-must-not-be-opened"

    runtime = create_release_core_runtime(
        provider_store=provider_store,
        workspace_store=workspace_store,
        remote_lifecycle=lifecycle,
        asset_root=missing_assets / "core",
        release_assets_root=missing_assets,
        daemon_asset_root=missing_assets / "daemon",
        runtime_asset_root=missing_assets / "runtime",
        source_commit=SOURCE_COMMIT,
        packaged_resource_assets=True,
    )

    assert not missing_assets.exists()
    with pytest.raises(DesktopCoreBridgeErrorV1) as exc_info:
        runtime.core_bridge._host_service.ensure_core(
            "profile-1",
            deadline=time.monotonic() + 1,
        )
    assert exc_info.value.error.code == "release_assets_initialization_failed"
    assert exc_info.value.error.retryable is True

    runtime.close()
    lifecycle.close()
    workspace_store.close()
    provider_store.close()


def test_v2_release_runtime_is_deferred_and_owns_only_v2_bridge_state(
    tmp_path: Path,
) -> None:
    provider_store = DesktopProviderStoreV2(tmp_path / "state" / "provider-v2")
    missing_assets = tmp_path / "assets-that-must-not-be-opened-v2"
    lifecycle = SimpleNamespace(active_transport=lambda *_args: object())

    runtime = create_release_core_runtime_v2(
        provider_store=provider_store,
        remote_lifecycle=lifecycle,
        asset_root=missing_assets / "core",
        release_assets_root=missing_assets,
        daemon_asset_root=missing_assets / "daemon",
        runtime_asset_root=missing_assets / "runtime",
        source_commit=SOURCE_COMMIT,
        packaged_resource_assets=True,
    )
    try:
        assert type(runtime) is DesktopReleaseCoreRuntimeV2
        assert runtime.bridge_store.state_root.name == "core-bridge-v2"
        assert not missing_assets.exists()
        with pytest.raises(DesktopCoreBridgeErrorV2) as exc_info:
            runtime.core_bridge._host_service.ensure_core(
                "profile-1",
                2,
                deadline=time.monotonic() + 1,
            )
        assert exc_info.value.error.code == "release_assets_initialization_failed"
    finally:
        runtime.close()
        provider_store.close()


class _ProfileConnectorAdapterV2:
    def __init__(self, *, health: str = "healthy") -> None:
        self.health = health
        self.requests: list[httpx.Request] = []
        self.closed: list[str] = []
        self.ensure_deadlines: list[float] = []

    def ensure_core(
        self,
        profile_id: str,
        profile_connection_generation: int,
        *,
        deadline: float,
    ) -> CoreHostAttachmentV2:
        assert deadline > time.monotonic()
        self.ensure_deadlines.append(deadline)
        return CoreHostAttachmentV2(
            profile_id=profile_id,
            profile_connection_generation=profile_connection_generation,
            remote_port=8765,
            bearer_token=CORE_V2_TOKEN,
            bearer_identity=hashlib.sha256(CORE_V2_TOKEN.encode()).hexdigest(),
        )

    def open_tunnel(
        self,
        *,
        profile_id: str,
        profile_connection_generation: int,
        remote_port: int,
        session_id: str,
        deadline: float,
    ) -> CoreTunnelHandleV2:
        assert remote_port == 8765
        assert deadline > time.monotonic()
        return CoreTunnelHandleV2(
            endpoint="http://127.0.0.1:49201",
            profile_id=profile_id,
            profile_connection_generation=profile_connection_generation,
            session_id=session_id,
            close_callback=lambda: self.closed.append(session_id),
        )

    def new_http_transport(self) -> httpx.BaseTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.path == "/version":
                return httpx.Response(200, json=_core_v2_version())
            if request.url.path == "/health":
                return httpx.Response(
                    200,
                    json={
                        "schema_version": "2",
                        "status": self.health,
                        "checked_at": "2026-07-23T06:00:00Z",
                    },
                )
            if request.url.path == "/v2/system/status":
                return httpx.Response(200, json=_core_v2_status())
            raise AssertionError(f"unexpected connector request: {request.url.path}")

        return httpx.MockTransport(handler)


def test_v2_profile_connector_negotiates_then_closes_its_temporary_tunnel() -> None:
    adapter = _ProfileConnectorAdapterV2()
    connector = release_runtime.DesktopCoreProfileConnectorV2(adapter)

    version = connector.connect_profile("profile-1", 3)

    assert version.release_version == "0.1.10"
    assert [request.url.path for request in adapter.requests] == [
        "/version",
        "/health",
        "/v2/system/status",
    ]
    assert "authorization" not in adapter.requests[0].headers
    assert "authorization" not in adapter.requests[1].headers
    assert adapter.requests[2].headers["authorization"] == f"Bearer {CORE_V2_TOKEN}"
    assert len(adapter.closed) == 1


def test_v2_profile_connector_default_deadline_covers_managed_runtime_install() -> None:
    adapter = _ProfileConnectorAdapterV2()
    connector = release_runtime.DesktopCoreProfileConnectorV2(adapter)
    before = time.monotonic()

    connector.connect_profile("profile-1", 3)

    assert len(adapter.ensure_deadlines) == 1
    assert adapter.ensure_deadlines[0] - before >= 899.0


def test_v2_profile_connector_refuses_degraded_daemon_readiness() -> None:
    adapter = _ProfileConnectorAdapterV2(health="degraded")
    connector = release_runtime.DesktopCoreProfileConnectorV2(adapter)

    with pytest.raises(DesktopCoreBridgeErrorV2) as exc_info:
        connector.connect_profile("profile-1", 3)

    assert exc_info.value.error.code == "core_not_ready"
    assert len(adapter.closed) == 1


def test_deferred_release_asset_initialization_retries_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    original_loader = release_runtime.load_core_bootstrap_config
    calls = 0

    def load(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ReleaseRuntimeConfigurationError("injected first load failure")
        return original_loader(*args, **kwargs)

    monkeypatch.setattr(release_runtime, "load_core_bootstrap_config", load)
    runtime = create_release_core_runtime(
        provider_store=provider_store,
        workspace_store=workspace_store,
        remote_lifecycle=lifecycle,
        asset_root=assets,
        source_commit=SOURCE_COMMIT,
    )
    deferred = runtime.core_bridge._host_service

    with pytest.raises(DesktopCoreBridgeErrorV1):
        deferred._resolve()
    resolved = deferred._resolve()

    assert isinstance(resolved, release_runtime.DesktopCoreSshBridgeAdapterV1)
    assert deferred._resolve() is resolved
    assert calls == 2

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
        build_version="0.1.8",
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
        "service_control",
        "diagnostics",
        "maintenance",
    ]


def test_core_event_relay_skips_heartbeat_and_invalidates_on_change() -> None:
    heartbeat = core_v1.HeartbeatEventV1(
        id="heartbeat-1",
        sequence=1,
        occurred_at="2026-07-14T12:00:00Z",
        event="heartbeat.v1",
        payload=core_v1.HeartbeatPayloadV1(active_run_count=0),
    )
    project_change = core_v1.ProjectUpdatedEventV1.model_construct(
        id="project-change-1",
        sequence=3,
        occurred_at="2026-07-14T12:00:02Z",
        event="project.updated.v1",
        change=None,
        payload=None,
    )
    frames = (
        SimpleNamespace(id="heartbeat-1", data=SimpleNamespace(root=heartbeat)),
        SimpleNamespace(
            id="run-change-1",
            data=SimpleNamespace(root=SimpleNamespace(sequence=2, event="run.updated.v1")),
        ),
        SimpleNamespace(id="project-change-1", data=SimpleNamespace(root=project_change)),
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
    complete = threading.Event()
    publish_count = 0
    refresh_count = 0

    binding = CoreRuntimeSessionBinding(project=project, generation=1)  # type: ignore[arg-type]

    def active_project():
        return None if complete.is_set() else binding

    def refresh_authority(candidate: CoreRuntimeSessionBinding) -> None:
        nonlocal refresh_count
        assert candidate is binding
        refresh_count += 1

    def publish(candidate: CoreRuntimeSessionBinding) -> None:
        nonlocal publish_count
        assert candidate is binding
        publish_count += 1
        if publish_count == 2:
            complete.set()

    relay.start(
        active_project=active_project,
        refresh_authority=refresh_authority,
        publish=publish,
        session_lost=lambda _binding, _error: None,
    )
    assert complete.wait(timeout=2)
    relay.request_stop()
    relay.join()

    assert publish_count == 2
    assert refresh_count == 1
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
        refresh_authority=lambda _binding: None,
        publish=lambda _binding: None,
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

    def publish(candidate: CoreRuntimeSessionBinding) -> None:
        nonlocal publication_attempt
        assert candidate is binding
        publication_attempt += 1
        if publication_attempt == 1:
            raise DesktopEventBrokerError("injected publication failure")
        published.append(f"publication-{publication_attempt}")
        if len(published) == 5:
            complete.set()

    relay.start(
        active_project=active_project,
        refresh_authority=lambda _binding: None,
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


def test_core_event_relay_replays_project_update_after_callback_failures() -> None:
    project_change = core_v1.ProjectUpdatedEventV1.model_construct(
        id="project-change-1",
        sequence=1,
        occurred_at="2026-07-14T12:00:00Z",
        event="project.updated.v1",
        change=None,
        payload=None,
    )
    frame = SimpleNamespace(
        id="project-change-1",
        data=SimpleNamespace(root=project_change),
    )

    class EventContext:
        def __enter__(self):
            return iter((frame,))

        def __exit__(self, *_exc: object) -> None:
            return None

    class Bridge:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        def events(self, _project: object, *, last_event_id: str | None = None):
            self.calls.append(last_event_id)
            return EventContext()

    bridge = Bridge()
    relay = DesktopCoreEventRelayV1(bridge)  # type: ignore[arg-type]
    project = SimpleNamespace(project_id="project-1", etag='"' + "a" * 64 + '"')
    binding = CoreRuntimeSessionBinding(project=project, generation=1)  # type: ignore[arg-type]
    complete = threading.Event()
    refresh_attempts = 0
    publish_attempts = 0

    def active_project():
        return None if complete.is_set() else binding

    def refresh_authority(candidate: CoreRuntimeSessionBinding) -> None:
        nonlocal refresh_attempts
        assert candidate is binding
        refresh_attempts += 1
        if refresh_attempts == 1:
            raise ProviderStoreError("injected authority persistence failure")

    def publish(candidate: CoreRuntimeSessionBinding) -> None:
        nonlocal publish_attempts
        assert candidate is binding
        publish_attempts += 1
        if publish_attempts == 1:
            raise DesktopEventBrokerError("injected invalidation publication failure")
        complete.set()

    relay.start(
        active_project=active_project,
        refresh_authority=refresh_authority,
        publish=publish,
        session_lost=lambda _binding, _error: None,
    )
    assert complete.wait(timeout=2)
    relay.request_stop()
    relay.join()

    assert refresh_attempts == 3
    assert publish_attempts == 2
    assert bridge.calls[:3] == [None, None, None]
