from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openevo import __version__
from openevo.backend.api import BackendHTTPError, create_backend_app
from openevo.backend.models import (
    BackendError,
    MAX_EVOLUTION_PROJECT_VALIDATION_REQUEST_BYTES,
)
from openevo.evolution.framework import (
    CapabilityAudience,
    build_evolution_capabilities,
    canonical_json,
    execution_profile_for_release_mode,
)
from openevo.evolution.framework.builtins import VerifiedExecutableRegistry
from openevo.evolution.framework.contracts import DescriptorKind
from openevo.evolution.models import ArtifactRegisterRequest
from openevo.evolution.store import EvolutionStore
from tests.framework_testkit import verified_builtin_registry


def _client(
    *,
    raise_server_exceptions: bool = True,
    evolution_registry: VerifiedExecutableRegistry | None = None,
) -> TestClient:
    app = create_backend_app(evolution_registry=evolution_registry)

    @app.get("/_test/http-error")
    def _test_http_error() -> None:
        raise BackendHTTPError(
            409,
            BackendError(
                code="service_conflict",
                message="Service conflict.",
                severity="blocking",
                category="service",
                retryable=True,
                repair_action="openevo_can_retry",
            ),
        )

    @app.get("/_test/unhandled-error")
    def _test_unhandled_error() -> None:
        raise RuntimeError("boom")

    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _client_for_state_root(
    state_root: Path,
    *,
    raise_server_exceptions: bool = True,
) -> TestClient:
    return TestClient(
        create_backend_app(state_root=state_root),
        raise_server_exceptions=raise_server_exceptions,
    )


def _desktop_project_config_contract(method) -> tuple[dict, dict]:
    schema = json.loads(canonical_json(method.config_schema))
    default = json.loads(canonical_json(method.default_config))
    injected = {
        injection.field_name for injection in method.project_config_injections
    }
    for field_name in injected:
        schema["properties"].pop(field_name)
        default.pop(field_name, None)
    required = [
        field_name
        for field_name in schema.get("required", [])
        if field_name not in injected
    ]
    if required:
        schema["required"] = required
    else:
        schema.pop("required", None)
    return schema, default


def _write_summary_for_artifact(state_root: Path, run_id: str, artifact_id: str) -> None:
    summary_path = state_root / "runs" / run_id / "summary.json"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(
        json.dumps(
            {
                "mode": "run",
                "status": "completed",
                "experiment_id": "biology-components",
                "experiment_name": "Biology Components",
                "run_id": run_id,
                "round_count": 1,
                "tasks": [
                    {
                        "task_id": "folding-baseline",
                        "rounds": [
                            {
                                "round_index": 0,
                                "artifact_ids": {"text_memory": [artifact_id]},
                                "jobs": [
                                    {
                                        "artifact_type": "text_memory",
                                        "method": "text_memory_reflector",
                                        "worker_status": "succeeded",
                                        "artifact_ids": [artifact_id],
                                        "approved_artifact_ids": [artifact_id],
                                        "promotion_status": "approved",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _run_create_payload(
    project_id: str,
    *,
    execution_mode: str = "codex_subscription_transcript",
) -> dict:
    return {
        "schema_version": "1",
        "idempotency_key": f"idem-{project_id}-{execution_mode}",
        "project_id": project_id,
        "project_snapshot_id": f"snapshot-{project_id}",
        "workspace_snapshot_ref": f"workspace://{project_id}/snapshot",
        "task": {
            "id": "folding-baseline",
            "objective": "Improve the folding baseline.",
            "source": {"type": "scratch"},
        },
        "execution_mode": execution_mode,
        "capture_mode": "transcript",
        "artifact_families": ["text_memory", "skill_bundle", "agent_system"],
        "method_ids": [
            "text_memory_expel_reflector",
            "skill_bundle_reflector",
            "agent_system_gepa_reflector",
        ],
        "runtime": {"kind": "managed_science"},
        "model": {"name": "gpt-5.1-codex-mini"},
    }


def test_backend_health() -> None:
    client = _client()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize(
    ("release_mode", "generic_execution_mode"),
    [
        ("codex_subscription_transcript", "subscription"),
        ("self-deployed", "self_deployed"),
    ],
)
def test_backend_capabilities_are_registry_snapshot_projection(
    release_mode: str,
    generic_execution_mode: str,
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / release_mode)
    client = _client(evolution_registry=registry)
    response = client.get("/capabilities", params={"execution_mode": release_mode})

    assert response.status_code == 200
    payload = response.json()
    profile = execution_profile_for_release_mode(release_mode)
    expected = build_evolution_capabilities(
        registry.snapshot,
        profile=profile,
        audience=CapabilityAudience.DESKTOP,
        core_version=__version__,
    ).model_dump(mode="json")
    assert payload == expected
    assert payload["schema_version"] == "1"
    assert payload["core_version"] == __version__
    assert payload["registry_digest"] == registry.snapshot.registry_digest
    assert payload["evaluated_profile"] == {
        "execution_mode": generic_execution_mode,
        "capture_mode": "transcript",
        "harness_id": "codex",
        "harness_capabilities": [],
        "runtime_capabilities": [],
    }

    expected_target_ids = {
        target_id
        for target_id, target in registry.snapshot.targets.items()
        if target.exposure.value == "desktop"
    }
    targets = payload["targets"]
    assert {target["target_id"] for target in targets} == expected_target_ids
    assert "evolution_methods" not in payload
    for target in targets:
        target_descriptor = registry.snapshot.targets[target["target_id"]]
        handler_descriptor = registry.snapshot.target_handlers[
            target_descriptor.handler_id
        ]
        assert target["configured_default_method_id"] == (
            target_descriptor.default_method_id
        )
        configured_default = next(
            method
            for method in target["methods"]
            if method["method_id"] == target_descriptor.default_method_id
        )
        assert target["effective_default_method_id"] == (
            target_descriptor.default_method_id
            if configured_default["support"]["overall"] == "supported"
            else None
        )
        assert target["configured_default_support"] == configured_default["support"]
        assert target["implementation_identity_digest"] == (
            registry.snapshot.identity_digest_for(
                DescriptorKind.TARGET,
                target_descriptor.id,
            )
        )
        assert target["handler_identity_digest"] == (
            registry.snapshot.identity_digest_for(
                DescriptorKind.TARGET_HANDLER,
                handler_descriptor.id,
            )
        )
        expected_method_ids = {
            method_id
            for method_id, method in registry.snapshot.methods.items()
            if method.target_id == target["target_id"]
            and method.exposure.value == "desktop"
        }
        assert {method["method_id"] for method in target["methods"]} == expected_method_ids
        assert {method["method_id"] for method in target["accepted_methods"]} == {
            method_id
            for method_id, method in registry.snapshot.methods.items()
            if method.target_id == target["target_id"]
        }
        for method in target["methods"]:
            method_descriptor = registry.snapshot.methods[method["method_id"]]
            assert set(method["support"]) == {
                "overall",
                "execution",
                "capture",
                "harness",
                "runtime",
            }
            assert method["implementation_identity_digest"] == (
                registry.snapshot.identity_digest_for(
                    DescriptorKind.METHOD,
                    method_descriptor.id,
                )
            )
            expected_schema, expected_default = _desktop_project_config_contract(
                method_descriptor
            )
            assert method["config_schema_json"] == canonical_json(expected_schema)
            assert method["default_config_json"] == canonical_json(expected_default)
            for injection in method_descriptor.project_config_injections:
                field_name = injection.field_name
                assert field_name in method_descriptor.config_schema["properties"]
                assert field_name not in expected_schema["properties"]


def test_backend_capabilities_openapi_declares_typed_errors() -> None:
    operation = create_backend_app().openapi()["paths"]["/capabilities"]["get"]

    for status_code in ("422", "503"):
        assert operation["responses"][status_code]["content"][
            "application/json"
        ]["schema"] == {"$ref": "#/components/schemas/BackendError"}


def test_backend_capabilities_fail_closed_without_verified_registry() -> None:
    response = _client().get(
        "/capabilities",
        params={"execution_mode": "codex_subscription_transcript"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "code": "evolution_registry_unavailable",
        "message": "Verified evolution capabilities are unavailable.",
        "severity": "blocking",
        "category": "service",
        "retryable": True,
        "repair_action": "openevo_can_retry",
        "details": {},
        "logs_ref": None,
    }


def test_backend_validates_resolver_config_against_every_concrete_method(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    response = _client(evolution_registry=registry).post(
        "/evolution/project-validation",
        json={
            "execution_mode": "codex_subscription_transcript",
            "expected_registry_digest": registry.snapshot.registry_digest,
            "agent_model": "gpt-5.1-codex-mini",
            "targets": {
                "agent_system": {
                    "enabled": True,
                    "method": "auto",
                    "config": {"target_path": "AGENTS.md"},
                }
            },
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "valid": True,
        "registry_digest": registry.snapshot.registry_digest,
    }


@pytest.mark.parametrize(
    ("target_id", "method"),
    [
        ("text_memory", "text_memory"),
        ("agent_system", "auto"),
    ],
    ids=["hidden-method", "resolver"],
)
def test_backend_rejects_invalid_hidden_or_resolver_project_config(
    tmp_path: Path,
    target_id: str,
    method: str,
) -> None:
    registry = verified_builtin_registry(tmp_path / method)
    response = _client(evolution_registry=registry).post(
        "/evolution/project-validation",
        json={
            "execution_mode": "self-deployed",
            "expected_registry_digest": registry.snapshot.registry_digest,
            "agent_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            "targets": {
                target_id: {
                    "enabled": True,
                    "method": method,
                    "config": {"unexpected": True},
                }
            },
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "evolution_project_invalid"
    assert response.json()["details"] == {
        "target_id": target_id,
        "selection": method,
        "reason_code": "invalid_method_config_or_profile",
        "registry_digest": registry.snapshot.registry_digest,
    }


def test_backend_rejects_project_validation_after_registry_change(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    response = _client(evolution_registry=registry).post(
        "/evolution/project-validation",
        json={
            "execution_mode": "codex_subscription_transcript",
            "expected_registry_digest": "0" * 64,
            "agent_model": "gpt-5.1-codex-mini",
            "targets": {},
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "evolution_registry_changed"
    assert response.json()["details"] == {
        "registry_digest": registry.snapshot.registry_digest
    }


def test_backend_bounds_project_validation_structure(tmp_path: Path) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    deeply_nested: dict[str, object] = {}
    for _ in range(40):
        deeply_nested = {"next": deeply_nested}

    response = _client(evolution_registry=registry).post(
        "/evolution/project-validation",
        json={
            "execution_mode": "self-deployed",
            "expected_registry_digest": registry.snapshot.registry_digest,
            "agent_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            "targets": {
                "text_memory": {
                    "enabled": False,
                    "method": "text_memory",
                    "config": deeply_nested,
                }
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "request_validation_error"

    response = _client(evolution_registry=registry).post(
        "/evolution/project-validation",
        json={
            "execution_mode": "self-deployed",
            "expected_registry_digest": registry.snapshot.registry_digest,
            "agent_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            "targets": {
                f"target_{index}": {"enabled": False}
                for index in range(129)
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "request_validation_error"


def test_backend_rejects_oversized_project_validation_body(tmp_path: Path) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    client = _client(evolution_registry=registry)
    response = client.post(
        "/evolution/project-validation",
        content=json.dumps(
            {
                "execution_mode": "self-deployed",
                "expected_registry_digest": registry.snapshot.registry_digest,
                "agent_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
                "targets": {},
                "padding": "x" * (1024 * 1024),
            }
        ),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["code"] == "request_body_too_large"

    response = client.post(
        "/evolution/project-validation",
        content=iter((b"{", b"x" * (1024 * 1024 + 1))),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "request_body_too_large"


def test_backend_accepts_project_validation_resource_boundaries(
    tmp_path: Path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    client = _client(evolution_registry=registry)
    targets = {
        "boundary_a": {
            "enabled": False,
            "method": None,
            "config": {"padding": ""},
        },
        "boundary_b": {
            "enabled": False,
            "method": None,
            "config": {"padding": ""},
        },
    }
    payload = {
        "execution_mode": "self-deployed",
        "expected_registry_digest": registry.snapshot.registry_digest,
        "agent_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        "targets": targets,
    }
    empty_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    padding_bytes = MAX_EVOLUTION_PROJECT_VALIDATION_REQUEST_BYTES - len(empty_body)
    first_padding = padding_bytes // 2
    targets["boundary_a"]["config"]["padding"] = "x" * first_padding
    targets["boundary_b"]["config"]["padding"] = "x" * (
        padding_bytes - first_padding
    )
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    assert len(body) == MAX_EVOLUTION_PROJECT_VALIDATION_REQUEST_BYTES
    response = client.post(
        "/evolution/project-validation",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200

    deepest_valid: dict[str, object] = {}
    for _ in range(15):
        deepest_valid = {"next": deepest_valid}
    payload["targets"] = {
        "boundary_depth": {
            "enabled": False,
            "method": None,
            "config": {
                **deepest_valid,
                "template": '{["escaped"]}' * 64,
            },
        }
    }
    response = client.post("/evolution/project-validation", json=payload)
    assert response.status_code == 200


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"execution_mode": "subscription"},
        {"execution_mode": "self_deployed"},
        {"execution_mode": "unknown"},
    ],
)
def test_backend_capabilities_require_valid_release_execution_mode(
    params: dict,
    tmp_path: Path,
) -> None:
    response = _client(
        evolution_registry=verified_builtin_registry(tmp_path / "registry")
    ).get(
        "/capabilities",
        params=params,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "request_validation_error"


def test_backend_rejects_unverified_registry_injection() -> None:
    class FakeRegistry:
        snapshot = object()

    with pytest.raises(TypeError, match="verified registry loader"):
        create_backend_app(evolution_registry=FakeRegistry())  # type: ignore[arg-type]


def test_backend_route_surface_is_present() -> None:
    routes = {
        (route.path, next(iter(route.methods - {"HEAD", "OPTIONS"}), None))
        for route in create_backend_app().routes
        if hasattr(route, "methods")
    }
    assert ("/status", "GET") in routes
    assert ("/environment", "GET") in routes
    assert ("/environment/doctor", "POST") in routes
    assert ("/environment/repair", "POST") in routes
    assert ("/projects", "POST") in routes
    assert ("/projects", "GET") in routes
    assert ("/projects/{project_id}", "GET") in routes
    assert ("/projects/{project_id}", "PATCH") in routes
    assert ("/runs", "POST") in routes
    assert ("/runs", "GET") in routes
    assert ("/runs/{run_id}", "GET") in routes
    assert ("/runs/{run_id}/cancel", "POST") in routes
    assert ("/runs/{run_id}/retry", "POST") in routes
    assert ("/runs/{run_id:path}/timeline", "GET") in routes
    assert ("/runs/{run_id:path}/logs", "GET") in routes
    assert ("/runs/{run_id:path}/artifacts", "GET") in routes
    assert ("/artifacts/{artifact_id}", "GET") in routes
    assert ("/artifacts/{artifact_id:path}/content", "GET") in routes
    assert ("/artifacts/{artifact_id:path}/diff", "GET") in routes
    assert ("/services", "GET") in routes
    assert ("/services/{service_id}/logs", "GET") in routes
    assert ("/services/{service_id}/restart", "POST") in routes
    assert ("/services/{service_id}/stop", "POST") in routes
    assert ("/capabilities", "GET") in routes


def test_backend_project_run_artifact_flow() -> None:
    client = _client()

    project = client.post(
        "/projects",
        json={"name": "science demo", "workspace_root": "/srv/openevo/workspaces/demo"},
    )
    assert project.status_code == 200
    project_id = project.json()["id"]

    run = client.post(
        "/runs",
        json=_run_create_payload(project_id),
    )
    assert run.status_code == 200
    run_id = run.json()["id"]

    timeline = client.get(f"/runs/{run_id}/timeline")
    assert timeline.status_code == 200
    assert timeline.json()[0]["phase"] == "created"

    artifacts = client.get(f"/runs/{run_id}/artifacts")
    assert artifacts.status_code == 200
    artifact_id = artifacts.json()[0]["id"]

    content = client.get(f"/artifacts/{artifact_id}/content")
    assert content.status_code == 200
    assert content.json()["artifact_type"] in {"text_memory", "skill_bundle", "agent_system"}


def test_backend_reads_sidecar_run_state_from_state_root(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    store = EvolutionStore(
        db_path=state_root / "evolution" / "evolution.db",
        artifact_root=state_root / "evolution" / "artifacts",
    )
    store.initialize()
    artifact_dir = state_root / "evolution" / "artifacts" / "manual-memory"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "memory.md").write_text(
        "# Learned Memory\n\n- Prefer stable folds.\n",
        encoding="utf-8",
    )
    artifact = store.register_artifact(
        ArtifactRegisterRequest(
            type="text_memory",
            name="Learned memory",
            uri=artifact_dir.as_uri(),
            manifest={"content_path": "memory.md"},
            lineage={"method": "text_memory_reflector"},
            promoted=True,
        )
    )
    run_id = "run_20260709120000000000"
    summary_path = state_root / "runs" / run_id / "summary.json"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(
        json.dumps(
            {
                "mode": "run",
                "status": "completed",
                "experiment_id": "biology-components",
                "experiment_name": "Biology Components",
                "run_id": run_id,
                "round_count": 1,
                "tasks": [
                    {
                        "task_id": "folding-baseline",
                        "rounds": [
                            {
                                "round_index": 0,
                                "policy_version": "policy-r0",
                                "rollout_status": "completed",
                                "dataset_status": "ready",
                                "artifact_ids": {
                                    "text_memory": [artifact.artifact_id],
                                },
                                "jobs": [
                                    {
                                        "artifact_type": "text_memory",
                                        "method": "text_memory_reflector",
                                        "worker_status": "succeeded",
                                        "artifact_ids": [artifact.artifact_id],
                                        "approved_artifact_ids": [artifact.artifact_id],
                                        "promotion_status": "approved",
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "summary_path": str(summary_path),
            }
        ),
        encoding="utf-8",
    )
    client = _client_for_state_root(state_root)

    timeline = client.get(f"/runs/{run_id}/timeline")
    artifacts = client.get(f"/runs/{run_id}/artifacts")
    content = client.get(f"/artifacts/{artifact.artifact_id}/content")
    diff = client.get(f"/artifacts/{artifact.artifact_id}/diff")

    assert timeline.status_code == 200
    assert any(
        artifact.artifact_id in event["artifact_ids"]
        for event in timeline.json()
    )
    assert artifacts.status_code == 200
    artifact_payload = artifacts.json()[0]
    assert artifact_payload["id"] == artifact.artifact_id
    assert artifact_payload["run_id"] == run_id
    assert artifact_payload["artifact_type"] == "text_memory"
    assert artifact_payload["title"] == "Learned memory"
    assert artifact_payload["promoted"] is True
    assert artifact_payload["lineage"]["method"] == "text_memory_reflector"
    assert content.status_code == 200
    assert content.json()["content"] == "# Learned Memory\n\n- Prefer stable folds.\n"
    assert content.json()["metadata"]["target_path"] == "memory.md"
    assert diff.status_code == 200
    assert diff.json()["after"] == "# Learned Memory\n\n- Prefer stable folds.\n"


def test_backend_rejects_artifact_content_outside_state_root(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    (outside_root / "secret.txt").write_text("secret", encoding="utf-8")
    store = EvolutionStore(
        db_path=state_root / "evolution" / "evolution.db",
        artifact_root=state_root / "evolution" / "artifacts",
    )
    store.initialize()
    artifact = store.register_artifact(
        ArtifactRegisterRequest(
            type="text_memory",
            name="Outside memory",
            uri=outside_root.as_uri(),
            manifest={"content_path": "secret.txt"},
            promoted=True,
        )
    )
    _write_summary_for_artifact(state_root, "run_20260709121000000000", artifact.artifact_id)
    client = _client_for_state_root(state_root)

    content = client.get(f"/artifacts/{artifact.artifact_id}/content")
    diff = client.get(f"/artifacts/{artifact.artifact_id}/diff")

    assert content.status_code == 404
    assert content.json()["code"] == "artifact_content_not_found"
    assert "secret" not in content.text
    assert diff.status_code == 404
    assert diff.json()["code"] == "artifact_content_not_found"
    assert "secret" not in diff.text


def test_backend_rejects_unreadable_artifact_content_uris(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    artifact_root = state_root / "evolution" / "artifacts"
    artifact_dir = artifact_root / "manual-memory"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "memory.md").write_text("# Memory\n", encoding="utf-8")
    (artifact_root / "secret.txt").write_text("secret", encoding="utf-8")
    store = EvolutionStore(
        db_path=state_root / "evolution" / "evolution.db",
        artifact_root=artifact_root,
    )
    store.initialize()
    artifacts = [
        store.register_artifact(
            ArtifactRegisterRequest(
                type="text_memory",
                name="Remote URI memory",
                uri="https://example.test/memory.md",
                manifest={"content_path": "memory.md"},
                promoted=True,
            )
        ),
        store.register_artifact(
            ArtifactRegisterRequest(
                type="text_memory",
                name="Netloc URI memory",
                uri=f"file://gpu.example.test{artifact_dir}/memory.md",
                manifest={"content_path": "memory.md"},
                promoted=True,
            )
        ),
        store.register_artifact(
            ArtifactRegisterRequest(
                type="text_memory",
                name="Traversal memory",
                uri=artifact_dir.as_uri(),
                manifest={"content_path": "../secret.txt"},
                promoted=True,
            )
        ),
    ]
    for index, artifact in enumerate(artifacts):
        _write_summary_for_artifact(
            state_root,
            f"run_2026070912200000000{index}",
            artifact.artifact_id,
        )
    client = _client_for_state_root(state_root)

    for artifact in artifacts:
        response = client.get(f"/artifacts/{artifact.artifact_id}/content")

        assert response.status_code == 404
        assert response.json()["code"] == "artifact_content_not_found"
        assert "secret" not in response.text


def test_backend_invalid_state_ids_return_typed_client_errors(tmp_path: Path) -> None:
    clients = [
        _client(raise_server_exceptions=False),
        _client_for_state_root(tmp_path / "state", raise_server_exceptions=False),
    ]

    for client in clients:
        responses = [
            (client.get("/runs/bad%5Cid/timeline"), "invalid_run_id", "run"),
            (client.get("/runs/bad%2Fid/timeline"), "invalid_run_id", "run"),
            (client.get("/artifacts/bad%5Cid/content"), "invalid_artifact_id", "artifact"),
            (client.get("/artifacts/bad%2Fid/content"), "invalid_artifact_id", "artifact"),
        ]
        for response, code, category in responses:
            assert response.status_code == 400
            assert response.json()["code"] == code
            assert response.json()["category"] == category


@pytest.mark.parametrize(
    "execution_mode",
    ["codex_subscription_transcript", "self-deployed"],
)
def test_backend_accepts_release_execution_modes(execution_mode: str) -> None:
    client = _client()
    project = client.post(
        "/projects",
        json={"name": "science demo", "workspace_root": "/srv/openevo/workspaces/demo"},
    )
    assert project.status_code == 200
    project_id = project.json()["id"]

    response = client.post(
        "/runs",
        json=_run_create_payload(project_id, execution_mode=execution_mode),
    )
    assert response.status_code == 200
    assert response.json()["execution_mode"] == execution_mode


def test_backend_run_create_rejects_benchmark_only_fields() -> None:
    client = _client()
    project = client.post(
        "/projects",
        json={"name": "science demo", "workspace_root": "/srv/openevo/workspaces/demo"},
    )
    assert project.status_code == 200
    project_id = project.json()["id"]

    top_level = _run_create_payload(project_id) | {"benchmark_task_id": "tb-1"}
    top_level_response = client.post("/runs", json=top_level)
    assert top_level_response.status_code == 422
    assert top_level_response.json()["code"] == "request_validation_error"

    nested = _run_create_payload(project_id)
    nested["task"]["metadata"] = {"benchmark_task_id": "tb-1"}
    nested_response = client.post("/runs", json=nested)
    assert nested_response.status_code == 422
    assert nested_response.json()["code"] == "request_validation_error"


def test_backend_run_create_requires_subscription_transcript_capture() -> None:
    client = _client()
    project = client.post(
        "/projects",
        json={"name": "science demo", "workspace_root": "/srv/openevo/workspaces/demo"},
    )
    assert project.status_code == 200
    project_id = project.json()["id"]

    payload = _run_create_payload(project_id) | {"capture_mode": "proxy"}

    response = client.post("/runs", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "request_validation_error"
    assert "subscription" in json.dumps(response.json()["details"]["errors"])


def test_backend_run_create_rejects_client_token_metric_claim() -> None:
    client = _client()
    project = client.post(
        "/projects",
        json={"name": "science demo", "workspace_root": "/srv/openevo/workspaces/demo"},
    )
    assert project.status_code == 200
    project_id = project.json()["id"]

    payload = _run_create_payload(project_id) | {
        "token_level_metrics_available": True,
    }

    response = client.post("/runs", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "request_validation_error"
    assert "token_level_metrics_available" in json.dumps(body["details"]["errors"])


def test_backend_run_create_requires_external_beta_contract_fields() -> None:
    client = _client()
    project = client.post(
        "/projects",
        json={"name": "science demo", "workspace_root": "/srv/openevo/workspaces/demo"},
    )
    assert project.status_code == 200
    project_id = project.json()["id"]

    response = client.post(
        "/runs",
        json={"project_id": project_id, "execution_mode": "codex_subscription_transcript"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "request_validation_error"
    error_text = json.dumps(body["details"]["errors"])
    assert "schema_version" in error_text
    assert "task" in error_text
    assert "method_ids" in error_text


def test_backend_typed_error_model() -> None:
    client = _client()
    response = client.get("/projects/missing-project")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "project_not_found"
    assert body["severity"] == "blocking"
    assert body["category"] == "project"
    assert body["retryable"] is False
    assert body["repair_action"] == "user_action_required"


def test_backend_validation_errors_use_typed_error_model() -> None:
    client = _client()
    response = client.post("/runs", json={})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "request_validation_error"
    assert body["severity"] == "blocking"
    assert body["category"] == "internal"
    assert body["retryable"] is False
    assert body["repair_action"] == "openevo_can_reconfigure"
    assert "errors" in body["details"]


def test_backend_http_errors_use_typed_error_model() -> None:
    client = _client()
    response = client.get("/_test/http-error")
    assert response.status_code == 409
    assert response.json()["code"] == "service_conflict"
    assert response.json()["repair_action"] == "openevo_can_retry"


def test_default_http_errors_use_typed_error_model() -> None:
    client = _client()
    not_found = client.get("/missing-route")
    assert not_found.status_code == 404
    assert not_found.json()["code"] == "http_error"
    assert not_found.json()["severity"] == "blocking"
    assert not_found.json()["category"] == "internal"
    assert not_found.json()["repair_action"] == "user_action_required"

    method_not_allowed = client.delete("/projects")
    assert method_not_allowed.status_code == 405
    assert method_not_allowed.json()["code"] == "http_error"
    assert method_not_allowed.json()["severity"] == "blocking"
    assert method_not_allowed.json()["category"] == "internal"


def test_backend_unhandled_errors_use_typed_error_model() -> None:
    client = _client(raise_server_exceptions=False)
    response = client.get("/_test/unhandled-error")
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_server_error"
    assert body["severity"] == "blocking"
    assert body["category"] == "internal"
    assert body["retryable"] is True
    assert body["repair_action"] == "openevo_can_retry"


def test_environment_doctor_and_repair_contract() -> None:
    client = _client()
    doctor = client.post("/environment/doctor", json={"repair": False})
    assert doctor.status_code == 200
    assert doctor.json()["checks"][0]["category"] in {"python", "docker", "codex", "network"}

    repair = client.post("/environment/repair", json={"actions": ["clear_stale_state"]})
    assert repair.status_code == 200
    assert repair.json()["status"] in {"ok", "needs_user_action"}
