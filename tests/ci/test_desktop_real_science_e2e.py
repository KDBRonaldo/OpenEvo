from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from types import ModuleType
from zipfile import ZipFile

import pytest

from openevo.evolution.framework.runtime import FrameworkDistributionLock


def _load_runner() -> ModuleType:
    path = Path("scripts/e2e/desktop_real_science_e2e.py").resolve()
    spec = importlib.util.spec_from_file_location("desktop_real_science_e2e", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _head(
    generation: int,
    registry_sha256: str,
    *,
    project_id: str = "project-real-e2e",
) -> dict[str, object]:
    revision_id = f"evolution-revision-{generation}"
    revision_manifest = _digest(f"revision-manifest-{generation}")
    return {
        "schema_version": "2",
        "project_head_id": f"project-head-{generation}",
        "project_id": project_id,
        "generation": generation,
        "predecessor_project_head_id": (
            None if generation == 0 else f"project-head-{generation - 1}"
        ),
        "workspace_snapshot": {
            "schema_version": "2",
            "workspace_snapshot_id": f"workspace-snapshot-{generation}",
            "project_id": project_id,
            "manifest_sha256": _digest(f"workspace-manifest-{generation}"),
            "entry_count": generation + 1,
            "byte_size": 100 + generation,
        },
        "evolution_revision": {
            "schema_version": "2",
            "evolution_revision_id": revision_id,
            "project_id": project_id,
            "manifest_sha256": revision_manifest,
            "artifact_count": 0 if generation == 0 else 3,
        },
        "runtime_context_snapshot": {
            "schema_version": "2",
            "runtime_context_snapshot_id": f"runtime-context-{generation}",
            "project_id": project_id,
            "evolution_revision_id": revision_id,
            "evolution_revision_manifest_sha256": revision_manifest,
            "registry_sha256": registry_sha256,
            "runtime_contract_sha256": _digest(f"runtime-contract-{generation}"),
            "manifest_sha256": _digest(f"runtime-manifest-{generation}"),
        },
        "effective_execution_snapshot": {
            "schema_version": "2",
            "effective_execution_snapshot_id": f"execution-snapshot-{generation}",
            "project_id": project_id,
            "execution_mode": "codex_subscription_transcript",
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "producer_id": "verified-release-producer",
            "snapshot_sha256": _digest(f"execution-snapshot-{generation}"),
        },
        "registry_sha256": registry_sha256,
        "manifest_sha256": _digest(f"project-head-manifest-{generation}"),
    }


def _capabilities(*, auto_supported: bool = True) -> dict[str, object]:
    def target(target_id: str, method_id: str) -> dict[str, object]:
        return {
            "target_id": target_id,
            "effective_default_method_id": method_id,
            "methods": [
                {
                    "method_id": method_id,
                    "support": {"overall": "supported"},
                    "default_config_json": "{}",
                }
            ],
            "selection_resolvers": [],
        }

    return {
        "targets": [
            target("text_memory", "text-memory-reflection-v1"),
            target("skill_bundle", "skill-bundle-synthesis-v1"),
            {
                "target_id": "agent_system",
                "effective_default_method_id": "agent-system-reflection-v1",
                "methods": [],
                "selection_resolvers": [
                    {
                        "selection_value": "auto",
                        "resolved_methods": [
                            {
                                "method_id": "agent-system-reflection-v1",
                                "support": {
                                    "overall": (
                                        "supported" if auto_supported else "unsupported"
                                    )
                                },
                            }
                        ],
                    }
                ],
            },
        ]
    }


class _FakeV2Api:
    def __init__(
        self,
        module: ModuleType,
        *,
        ssh_host_alias: str = "evolab",
        wrong_second_context: bool = False,
    ) -> None:
        self.module = module
        self.ssh_host_alias = ssh_host_alias
        self.registry_sha256 = _digest("registry")
        self.project_id = "project-real-e2e"
        self.heads = [
            _head(index, self.registry_sha256, project_id=self.project_id)
            for index in range(3)
        ]
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.connected = False
        self.created_task_count = 0
        self.wrong_second_context = wrong_second_context

    def _profile(self, *, connected: bool) -> dict[str, object]:
        profile: dict[str, object] = {
            "schema_version": "2",
            "profile_id": "profile-system-openssh",
            "profile_kind": "system_openssh",
            "display_name": "OpenEvo release workspace",
            "connection_authority": "system_openssh",
            "ssh_host_alias": self.ssh_host_alias,
            "connection_state": "connected" if connected else "disconnected",
            "connection_generation": 1 if connected else 0,
            "etag": '"profile-etag"',
        }
        if connected:
            profile.update(
                {
                    "core_api_major": 2,
                    "core_registry_sha256": self.registry_sha256,
                }
            )
        return profile

    def _project(self, generation: int) -> dict[str, object]:
        return {
            "schema_version": "2",
            "project_id": self.project_id,
            "profile_id": "profile-system-openssh",
            "display_name": self.module.RELEASE_PROJECT_DISPLAY_NAME,
            "state": "ready",
            "etag": f'"project-etag-{generation}"',
            "admission_etag": f'"admission-etag-{generation}"',
            "project_config_sha256": _digest("configured-project"),
            "active_project_head": self.heads[generation],
        }

    def _task(self, ordinal: int) -> dict[str, object]:
        task_id = f"task-{ordinal}"
        predecessor = self.heads[ordinal - 1]
        successor = self.heads[ordinal]
        admission = {
            "schema_version": "2",
            "task_admission_id": f"task-admission-{ordinal}",
            "task_id": task_id,
            "project_id": self.project_id,
            "predecessor_project_head": predecessor,
            "workspace_snapshot": predecessor["workspace_snapshot"],
            "admission_sha256": _digest(f"admission-{ordinal}"),
        }
        transition = {
            "schema_version": "2",
            "successor_transition_id": f"successor-transition-{ordinal}",
            "project_id": self.project_id,
            "kind": "task_successor",
            "predecessor_project_head": predecessor,
            "expected_successor_generation": ordinal,
            "successor_project_head": successor,
        }
        return {
            "schema_version": "2",
            "task_id": task_id,
            "project_id": self.project_id,
            "state": "completed",
            "etag": f'"task-etag-{ordinal}"',
            "admission": admission,
            "attempts": [{"attempt_id": f"attempt-{ordinal}"}],
            "authoritative_attempt_id": f"attempt-{ordinal}",
            "successor_transition": transition,
        }

    def request(self, method: str, route: str, **kwargs: object):
        self.calls.append((method, route, kwargs))
        if method == "GET" and route == "/desktop/v2/ssh-hosts":
            return {
                "schema_version": "2",
                "catalog_generation": 7,
                "hosts": [
                    {
                        "ssh_host_alias": self.ssh_host_alias,
                        "availability": "selectable",
                    }
                ],
            }
        if method == "POST" and route == "/desktop/v2/profiles":
            body = kwargs["body"]
            assert isinstance(body, dict)
            assert body == {
                "schema_version": "2",
                "display_name": "OpenEvo release workspace",
                "connection_authority": "system_openssh",
                "ssh_host_alias": self.ssh_host_alias,
            }
            return self._profile(connected=False)
        if method == "POST" and route.endswith("/connect"):
            self.connected = True
            return {"status": "succeeded"}
        if method == "GET" and route == "/desktop/v2/profiles/profile-system-openssh":
            return self._profile(connected=self.connected)
        if method == "POST" and route == "/desktop/v2/projects":
            return self._project(0)
        if method == "PATCH" and route == f"/desktop/v2/projects/{self.project_id}":
            body = kwargs["body"]
            assert isinstance(body, dict)
            assert body["expected_project_head_id"] == "project-head-0"
            return self._project(0)
        if method == "GET" and route.endswith("/capabilities"):
            return {
                "schema_version": "2",
                "registry_sha256": self.registry_sha256,
                "capabilities": _capabilities(),
            }
        if method == "POST" and route.endswith("/validate"):
            return {
                "schema_version": "2",
                "valid": True,
                "registry_sha256": self.registry_sha256,
                "checks": [{"status": "passed"}, {"status": "passed"}],
            }
        if method == "POST" and route == "/desktop/v2/tasks":
            self.created_task_count += 1
            body = kwargs["body"]
            assert isinstance(body, dict)
            assert body["expected_project_head_id"] == (
                f"project-head-{self.created_task_count - 1}"
            )
            return self._task(self.created_task_count)
        if method == "GET" and route.startswith("/desktop/v2/tasks/task-"):
            ordinal = int(route.removeprefix("/desktop/v2/tasks/task-").split("/")[0])
            task = self._task(ordinal)
            if route.endswith("/context"):
                admission = task["admission"]
                assert isinstance(admission, dict)
                context_head = self.heads[0] if self.wrong_second_context and ordinal == 2 else self.heads[ordinal - 1]
                return {
                    "schema_version": "2",
                    "task_id": f"task-{ordinal}",
                    "task_admission_id": f"task-admission-{ordinal}",
                    "project_head": context_head,
                    "workspace_snapshot": admission["workspace_snapshot"],
                }
            return task
        if method == "GET" and route.startswith("/desktop/v2/transitions/"):
            ordinal = int(route.rsplit("-", 1)[1])
            task = self._task(ordinal)
            return {
                "schema_version": "2",
                "state": "committed",
                "transition": task["successor_transition"],
            }
        if method == "GET" and route == f"/desktop/v2/projects/{self.project_id}":
            return self._project(self.created_task_count)
        raise AssertionError(f"unexpected request: {method} {route}")

    def page(self, route: str, *, stage: str):
        self.calls.append(("PAGE", route, {"stage": stage}))
        return [
            {"event_type": event_type}
            for event_type in sorted(self.module.REQUIRED_TASK_EVENT_TYPES)
        ]


def _workflow(module: ModuleType, api: _FakeV2Api):
    return module.DesktopScienceWorkflow(
        api,
        ssh_host_alias=api.ssh_host_alias,
        registry_sha256=api.registry_sha256,
        codex_model=module.RELEASE_CODEX_MODEL,
        reasoning_effort=module.RELEASE_REASONING_EFFORT,
        task_title="Structural v2 test",
        task_objective="Exercise the immutable successor chain.",
        poll_seconds=0.001,
        activation_timeout_seconds=1,
        run_timeout_seconds=1,
    )


def test_formal_release_runner_is_v2_system_openssh_only() -> None:
    runner = Path("scripts/e2e/desktop_real_science_e2e.py").read_text(encoding="utf-8")
    renderer = Path(
        "desktop/tests/product-browser/release-live-observability.pw.ts"
    ).read_text(encoding="utf-8")

    assert "/desktop/v2/" in runner
    assert "--ssh-host-alias" in runner
    assert '"connection_authority": "system_openssh"' in runner
    assert "/desktop/v1/" not in runner
    assert "ssh_agent" not in runner
    for forbidden_flag in (
        "--host",
        "--port",
        "--user",
        "--expected-host-key-fingerprint",
        "--host-key-algorithm",
    ):
        assert f'add_argument("{forbidden_flag}"' not in runner

    assert 'z.literal("2")' in renderer
    assert "/desktop/v2/" in renderer
    assert "/desktop/v1/" not in renderer
    assert "../../src/api/v1/" not in renderer


def test_structural_check_is_explicitly_not_an_e2e_run(tmp_path: Path) -> None:
    output = tmp_path / "must-not-exist.json"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/e2e/desktop_real_science_e2e.py",
            "--structural-check",
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "structural check passed; E2E was not run" in result.stdout
    assert not output.exists()


def test_parser_exposes_alias_not_manual_connection_fields() -> None:
    module = _load_runner()
    parser = module._parser()
    args = parser.parse_args(["--ssh-host-alias", "evolab", "--structural-check"])

    assert args.ssh_host_alias == "evolab"
    for forbidden in ("--host", "--port", "--user", "--sidecar"):
        with pytest.raises(SystemExit):
            parser.parse_args([forbidden, "value"])


def test_two_task_workflow_uses_one_project_and_reuses_successor_head() -> None:
    module = _load_runner()
    api = _FakeV2Api(module)
    workflow = _workflow(module, api)

    evidence = workflow.run()

    assert evidence["run_mode"] == "two_task_subscription_release"
    assert evidence["task_count"] == 2
    assert evidence["remote"] == {
        "connection_authority": "system_openssh",
        "catalog_selection_verified": True,
        "system_openssh_final_authority_verified": True,
        "core_api_major": 2,
        "core_registry_sha256": api.registry_sha256,
    }
    assert [item["ordinal"] for item in evidence["tasks"]] == [1, 2]
    assert evidence["tasks"][1]["predecessor_project_head"] == evidence["tasks"][0]["successor_project_head"]
    assert evidence["project"]["initial_project_head"]["generation"] == 0
    assert evidence["project"]["active_project_head"]["generation"] == 2
    assert evidence["project"]["active_project_head"]["evolution_revision"]["artifact_count"] == 3
    assert evidence["reuse"] == {
        "first_context_excluded_own_successor": True,
        "second_admission_pinned_first_successor": True,
        "second_context_pinned_first_successor": True,
        "second_runtime_context_equals_first_successor": True,
    }

    project_creates = [
        call for call in api.calls if call[:2] == ("POST", "/desktop/v2/projects")
    ]
    project_updates = [
        call
        for call in api.calls
        if call[:2] == ("PATCH", f"/desktop/v2/projects/{api.project_id}")
    ]
    task_creates = [
        call for call in api.calls if call[:2] == ("POST", "/desktop/v2/tasks")
    ]
    assert len(project_creates) == 1
    assert len(project_updates) == 1
    assert len(task_creates) == 2
    assert all("/desktop/v2/" in route for _, route, _ in api.calls)


def test_two_task_workflow_fails_closed_when_task_two_context_is_stale() -> None:
    module = _load_runner()
    api = _FakeV2Api(module, wrong_second_context=True)

    with pytest.raises(module.E2EFailure, match="task_context_invalid"):
        _workflow(module, api).run()


def test_capability_selection_requires_supported_agent_system_auto() -> None:
    module = _load_runner()
    api = _FakeV2Api(module)
    workflow = _workflow(module, api)

    selected, methods = workflow._select_release_targets(_capabilities())
    assert set(selected) == set(module.REQUIRED_TARGET_IDS)
    assert selected["agent_system"] == {
        "enabled": True,
        "method": "auto",
        "config": {"target_path": "AGENTS.md"},
    }
    assert methods["agent_system"] == "auto"

    with pytest.raises(module.E2EFailure, match="agent_system_auto_unsupported"):
        workflow._select_release_targets(_capabilities(auto_supported=False))

    missing_resolvers = _capabilities()
    agent_target = missing_resolvers["targets"][2]
    assert isinstance(agent_target, dict)
    agent_target.pop("selection_resolvers")
    with pytest.raises(module.E2EFailure, match="agent_system_auto_unsupported"):
        workflow._select_release_targets(missing_resolvers)


def test_renderer_expectations_and_result_bind_live_v2_authority() -> None:
    module = _load_runner()
    api = _FakeV2Api(module)
    workflow = _workflow(module, api)
    workflow.run()
    expectations = workflow.renderer_expectations()
    source_commit = "f" * 40
    web_digest = _digest("packaged-web")
    screenshot_digest = _digest("screenshot")
    payload = {
        "schema_version": "2",
        "kind": "openevo_desktop_live_renderer_observability",
        "outcome": "passed",
        "provider_kind": "desktop_sidecar",
        "source_commit": source_commit,
        "packaged_web_build_digest": web_digest,
        "desktop_api_major": 2,
        "renderer_ready": True,
        "builtin_sample_count": 2,
        "project_id_sha256": module._digest_text(api.project_id),
        "task_count": 2,
        "task_id_sha256": [module._digest_text("task-1"), module._digest_text("task-2")],
        "active_project_head_generation": 2,
        "evolution_artifact_count": 3,
        "system_openssh_workspace_verified": True,
        "remote_target_controls_verified": True,
        "selected_methods": expectations["method_ids"],
        "observed_route_kinds": ["desktop_v2", "packaged_web"],
        "screenshot_sha256": screenshot_digest,
    }

    assert module._validate_renderer_result(
        payload,
        expectations=expectations,
        source_commit=source_commit,
        packaged_web_build_digest=web_digest,
        screenshot_sha256=screenshot_digest,
    ) == payload

    payload["desktop_api_major"] = 1
    with pytest.raises(module.E2EFailure, match="renderer_result_identity_mismatch"):
        module._validate_renderer_result(
            payload,
            expectations=expectations,
            source_commit=source_commit,
            packaged_web_build_digest=web_digest,
            screenshot_sha256=screenshot_digest,
        )


def test_native_frame_uses_the_closed_credential_protocol() -> None:
    module = _load_runner()
    credentials = module.NativeCredentials.create()

    frame = credentials.frame()
    payload = json.loads(frame)

    assert frame.endswith(b"\n")
    assert len(frame) <= module.MAX_NATIVE_FRAME_BYTES
    assert set(payload) == {
        "protocol",
        "instance_id",
        "readiness_key",
        "session_token",
        "handoff_token",
    }
    assert payload["protocol"] == module.NATIVE_PROTOCOL
    assert credentials.session_token not in repr(credentials)


@pytest.mark.parametrize("body", [b"", b"{}"])
def test_local_api_empty_response_contract_is_explicit(body: bytes) -> None:
    module = _load_runner()

    class Response:
        status = 403

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _size: int = -1) -> bytes:
            return body

    class FakeOpener:
        def open(self, _request: object, *, timeout: float):
            assert timeout == 120.0
            return Response()

    api = module.LocalApi("http://127.0.0.1:12345", "a" * 64)
    api._opener = FakeOpener()
    if body:
        with pytest.raises(module.E2EFailure, match="unexpected_empty_response_payload"):
            api.request(
                "GET",
                "/desktop/v2/state",
                stage="unauthenticated_probe",
                expected_status=403,
                authenticated=False,
                expected_empty_body=True,
            )
    else:
        assert api.request(
            "GET",
            "/desktop/v2/state",
            stage="unauthenticated_probe",
            expected_status=403,
            authenticated=False,
            expected_empty_body=True,
        ) is None


def _write_wheel(path: Path, *, version: str = "0.1.9") -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            f"openevo-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: openevo\nVersion: {version}\n",
        )


def _write_lock(path: Path, wheel: Path, *, digest: str | None = None) -> None:
    wheel_digest = digest or hashlib.sha256(wheel.read_bytes()).hexdigest()
    payload = FrameworkDistributionLock(
        distribution_version="0.1.9",
        distribution_digest=wheel_digest,
        wheel_filename=wheel.name,
    ).model_dump(mode="json")
    path.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_wheel_lock_validation_accepts_canonical_closed_contract(tmp_path: Path) -> None:
    module = _load_runner()
    wheel = tmp_path / "openevo-0.1.9-py3-none-any.whl"
    lock = tmp_path / "framework-lock.json"
    _write_wheel(wheel)
    _write_lock(lock, wheel)

    assert module._validate_wheel_lock(wheel, lock) == (
        "openevo",
        "0.1.9",
        hashlib.sha256(wheel.read_bytes()).hexdigest(),
    )

    payload = json.loads(lock.read_text(encoding="utf-8"))
    payload["registry_digest"] = _digest("verified-registry")
    lock.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(module.E2EFailure, match="framework_lock_wheel_mismatch"):
        module._validate_wheel_lock(wheel, lock)

    _write_lock(lock, wheel, digest="0" * 64)
    with pytest.raises(module.E2EFailure, match="framework_lock_wheel_mismatch"):
        module._validate_wheel_lock(wheel, lock)


def test_private_temporary_root_resolves_system_directory_alias(tmp_path: Path) -> None:
    module = _load_runner()
    private_parent = tmp_path / "private"
    private_parent.mkdir()
    session_root = private_parent / "session"
    session_root.mkdir(mode=0o700)
    session_root.chmod(0o700)
    aliased_parent = tmp_path / "alias"
    aliased_parent.symlink_to(private_parent, target_is_directory=True)

    resolved = module._resolve_private_temporary_root(aliased_parent / "session")

    assert resolved == session_root.resolve(strict=True)
    assert not resolved.is_symlink()


def test_held_release_asset_rejects_path_replacement(tmp_path: Path) -> None:
    module = _load_runner()
    source = tmp_path / "sidecar"
    source.write_bytes(b"verified-sidecar")
    authority = module.HeldReleaseAsset.open(source)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"replacement")
    os.replace(replacement, source)

    with pytest.raises(module.E2EFailure, match="release_asset_authority_changed"):
        authority.verify_unchanged()
    with pytest.raises(module.E2EFailure, match="release_asset_authority_changed"):
        authority.copy_to(tmp_path / "launch", executable=True)
    assert not (tmp_path / "launch").exists()
    authority.close()


def test_held_release_asset_copy_is_digest_bound_and_executable(tmp_path: Path) -> None:
    module = _load_runner()
    source = tmp_path / "sidecar"
    source.write_bytes(b"verified-sidecar")
    authority = module.HeldReleaseAsset.open(source)
    launch = tmp_path / "launch"

    authority.copy_to(launch, executable=True)

    assert launch.read_bytes() == b"verified-sidecar"
    assert stat.S_IMODE(launch.stat().st_mode) == 0o500
    assert authority.evidence() == {
        "sha256": hashlib.sha256(b"verified-sidecar").hexdigest(),
        "byte_size": len(b"verified-sidecar"),
    }
    authority.close()


def _candidate_arguments(module: ModuleType, tmp_path: Path):
    app = tmp_path / "OpenEvo Desktop.app"
    macos = app / "Contents/MacOS"
    macos.mkdir(parents=True)
    (macos / "openevo-desktop-sidecar").write_bytes(b"sidecar")
    (macos / "openevo-ssh-askpass").write_bytes(b"helper")
    web = tmp_path / "packaged-web"
    web.mkdir()
    values = [
        "--ssh-host-alias",
        "evolab",
        "--app-bundle",
        str(app),
        "--core-wheel",
        str(tmp_path / "openevo.whl"),
        "--framework-lock",
        str(tmp_path / "framework-lock.json"),
        "--daemon-bundle",
        str(tmp_path / "daemon"),
        "--daemon-manifest",
        str(tmp_path / "daemon.json"),
        "--managed-runtime-archive",
        str(tmp_path / "runtime.tar"),
        "--release-candidate-manifest",
        str(tmp_path / "release-candidate.json"),
        "--app-bundle-smoke",
        str(tmp_path / "app-bundle-smoke.json"),
        "--packaged-web-manifest",
        str(tmp_path / "packaged-web-manifest.json"),
        "--playwright-candidate-evidence",
        str(tmp_path / "playwright-evidence.json"),
        "--packaged-web-root",
        str(web),
    ]
    return module._parser().parse_args(values)


def test_runtime_arguments_require_exact_candidate_app_and_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner()
    monkeypatch.setattr(module.sys, "platform", "darwin")
    args = _candidate_arguments(module, tmp_path)

    module._validate_runtime_arguments(args)

    args.ssh_host_alias = "bad alias"
    with pytest.raises(module.E2EFailure, match="ssh_host_alias_invalid"):
        module._validate_runtime_arguments(args)
    args.ssh_host_alias = "evolab"
    args.app_bundle = None
    with pytest.raises(module.E2EFailure, match="exact_candidate_inputs_required"):
        module._validate_runtime_arguments(args)


def test_runtime_arguments_pin_release_model_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner()
    monkeypatch.setattr(module.sys, "platform", "darwin")
    args = _candidate_arguments(module, tmp_path)
    args.codex_model = "gpt-5"

    with pytest.raises(module.E2EFailure, match="release_model_profile_required"):
        module._validate_runtime_arguments(args)
    with pytest.raises(SystemExit):
        module._parser().parse_args(["--reasoning-effort", "medium"])


def _version_payload() -> dict[str, object]:
    contract = json.loads(Path("desktop/release-contract.json").read_text(encoding="utf-8"))
    v019 = contract["v019"]
    features = v019["required_desktop_feature_flags"]
    feature_digest = hashlib.sha256(
        json.dumps(
            features,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return {
        "schema_version": "2",
        "api_name": "openevo-desktop-local-api",
        "preferred_major": 2,
        "supported_majors": [2],
        "mutation_major": 2,
        "openapi_sha256": v019["accepted_desktop_openapi_digests"][0],
        "event_schema_sha256": v019["accepted_desktop_event_schema_digests"][0],
        "release_version": "0.1.9",
        "build_id": _digest("build-id"),
        "source_commit": "f" * 40,
        "build_channel": "release",
        "provider_kind": "desktop_sidecar",
        "feature_flags": features,
        "feature_set_sha256": feature_digest,
        "required_core_api_major": 2,
        "mutation_compatible": True,
    }


def test_release_negotiation_is_v2_only_and_session_authenticated() -> None:
    module = _load_runner()

    class FakeApi:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def request(self, _method: str, route: str, **kwargs: object):
            self.calls.append((route, kwargs))
            if route == "/version":
                return _version_payload()
            if route == "/desktop/v2/state" and kwargs.get("authenticated") is not False:
                return {"schema_version": "2"}
            return {}

    api = FakeApi()
    identity = module._release_identity(api)

    assert identity["mutation_major"] == 2
    assert identity["v2_only_negotiation_verified"] is True
    assert identity["authenticated_session_probe"] is True
    assert identity["unauthenticated_session_rejected"] is True
    assert [route for route, _ in api.calls] == [
        "/version",
        "/openevo-api/desktop/shell",
        "/desktop/v2/state",
        "/desktop/v2/state",
    ]
    assert api.calls[-1][1]["authenticated"] is False


def test_release_negotiation_rejects_non_v2_provider() -> None:
    module = _load_runner()

    class FakeApi:
        def request(self, _method: str, route: str, **_kwargs: object):
            assert route == "/version"
            payload = _version_payload()
            payload["mutation_major"] = 1
            return payload

    with pytest.raises(module.E2EFailure, match="not_release_desktop_sidecar"):
        module._release_identity(FakeApi())


@pytest.mark.parametrize(
    ("payload", "private_values", "code"),
    [
        ({"password": "redacted"}, (), "forbidden_evidence_field"),
        ({"failure": {"stage": "run", "code": "bad value"}}, (), "invalid_evidence_code"),
        ({"kind": "/Users/example/private"}, (), "host_path_in_evidence"),
        ({"kind": "literal-evolab"}, ("evolab",), "secret_in_evidence"),
        ({"kind": "https://example.invalid"}, (), "sensitive_text_in_evidence"),
    ],
)
def test_evidence_privacy_is_closed(
    payload: dict[str, object],
    private_values: tuple[str, ...],
    code: str,
) -> None:
    module = _load_runner()
    with pytest.raises(module.E2EFailure, match=code):
        module._audit_evidence(payload, private_values=private_values)


def test_process_environments_keep_proxy_only_for_asset_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_runner()
    proxy = {
        "HTTPS_PROXY": "http://proxy.example:8443",
        "http_proxy": "http://proxy.example:8080",
        "NO_PROXY": "localhost,127.0.0.1",
    }
    for name, value in proxy.items():
        monkeypatch.setenv(name, value)

    build = module._release_asset_build_environment()
    assert {name: build[name] for name in proxy} == proxy
    assert all(name not in module._sidecar_environment() for name in proxy)
    assert all(name not in module._renderer_environment() for name in proxy)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_timeout_arguments_require_finite_positive_values(value: str) -> None:
    module = _load_runner()
    with pytest.raises(SystemExit):
        module._parser().parse_args(["--poll-seconds", value])


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("--poll-seconds", "31"),
        ("--progress-seconds", "61"),
        ("--inter-task-delay-seconds", "301"),
        ("--activation-timeout-seconds", "1801"),
        ("--run-timeout-seconds", "10801"),
        ("--overall-timeout-seconds", "21601"),
        ("--renderer-timeout-seconds", "601"),
    ],
)
def test_timeout_arguments_have_closed_upper_bounds(argument: str, value: str) -> None:
    module = _load_runner()
    with pytest.raises(SystemExit):
        module._parser().parse_args([argument, value])


def test_progress_reporter_is_redacted_and_deadline_is_fail_closed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_runner()
    reporter = module.ProgressReporter(interval_seconds=60, overall_timeout_seconds=0.01)

    reporter.emit("task_1", "running", force=True)
    output = capsys.readouterr()
    assert output.out == ""
    assert "stage=task_1 state=running" in output.err
    assert "remaining_seconds=" in output.err

    time.sleep(0.02)
    with pytest.raises(module.E2EFailure, match="overall_timeout"):
        reporter.remaining("task_1")
    reporter.stop_deadline_enforcement()
    assert reporter.remaining("cleanup") == float("inf")


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_process_group_cleanup_works_without_reaping_the_group_leader() -> None:
    module = _load_runner()
    code = """
import os, signal, sys, time
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    sys.stdout.close()
    while True:
        time.sleep(1)
print(child, flush=True)
os._exit(0)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        start_new_session=True,
        text=True,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline().strip())
    process.stdout.close()

    assert module._wait_for_build_process_group(
        process,
        process_group_id=process.pid,
        timeout_seconds=2,
    ) == 0
    assert process.returncode == 0

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        # A killed orphan can remain briefly as a zombie under the host init;
        # it cannot execute and therefore no longer owns release resources.
        ps = subprocess.run(
            ["/bin/ps", "-o", "state=", "-p", str(child_pid)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert not ps.stdout.strip() or ps.stdout.strip().startswith("Z")
