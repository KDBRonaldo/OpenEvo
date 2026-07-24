from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from openevo.evolution import artifact_payloads, context_projection
from openevo.evolution.artifact_payloads import ArtifactPayloadService
from openevo.evolution.framework import (
    EvolutionExecutionProfile,
    RuntimeDestinationRoots,
    TargetConsumptionLimits,
    canonical_json,
)
from openevo.evolution.framework import builtin_handlers
from tests.framework_testkit import verified_builtin_registry


def _projection_types():
    from openevo.evolution.context_projection import (
        ContextProjectionResolveRequest,
        ContextProjectionResolver,
    )

    return ContextProjectionResolveRequest, ContextProjectionResolver


@pytest.fixture(scope="module")
def executable_registry(tmp_path_factory: pytest.TempPathFactory):
    return verified_builtin_registry(tmp_path_factory.mktemp("verified-registry"))


@pytest.fixture
def artifact_root(tmp_path: Path) -> Path:
    root = tmp_path / "managed-artifacts"
    (root / "payloads").mkdir(parents=True)
    (root / "metadata").mkdir()
    return root


def _request(
    *,
    auth_mode: str | None = None,
    base_model: str | None = "model-a",
    destination_roots: RuntimeDestinationRoots | None = None,
    target_limits: dict[str, TargetConsumptionLimits] | None = None,
):
    Request, _ = _projection_types()
    agent: dict[str, Any] = {"harness": "codex"}
    if auth_mode is not None:
        agent["settings"] = {"auth_mode": auth_mode}
    execution_mode = "subscription" if auth_mode is not None else "self_deployed"
    return Request(
        task_id="task-context-projection",
        instruction="Continue the task.",
        agent=agent,
        base_model=base_model,
        policy_version="policy-7",
        rollout_step=3,
        metadata={"task_tags": ["parser"]},
        execution_profile=EvolutionExecutionProfile(
            execution_mode=execution_mode,
            capture_mode="transcript",
            harness_id="codex",
            runtime_capabilities=("adapter_serving", "multi_adapter_application"),
        ),
        destination_roots=destination_roots
        or RuntimeDestinationRoots(
            target_data="/openevo/session/evolution",
            harness_skills="/openevo/session/evolution/skills",
            harness_instruction="/workspace/repository",
        ),
        target_limits=target_limits or {},
    )


def _artifact_row(
    artifact_root: Path,
    *,
    artifact_id: str,
    artifact_type: str,
    payload: Path,
    quality: float,
    manifest: dict[str, object] | None = None,
    compatibility: dict[str, object] | None = None,
    created_at: str = "2026-01-01T00:00:00+00:00",
) -> dict[str, object]:
    metadata_path = artifact_root / "metadata" / f"{artifact_id}.json"
    metadata_path.write_text(
        json.dumps({"manifest": manifest or {}}, sort_keys=True),
        encoding="utf-8",
    )
    return {
        "artifact_id": artifact_id,
        "type": artifact_type,
        "name": artifact_id,
        "uri": payload.as_uri(),
        "manifest_path": str(metadata_path),
        "manifest_json": json.dumps(
            manifest or {},
            sort_keys=True,
            allow_nan=False,
        ),
        "compatibility_json": json.dumps(
            compatibility or {"task_tags": ["parser"]}, sort_keys=True
        ),
        "scores_json": json.dumps({"quality": quality}),
        "created_at": created_at,
        "promoted": 1,
        "state": "active",
    }


def _memory(
    artifact_root: Path,
    artifact_id: str,
    text: str,
    quality: float,
) -> dict[str, object]:
    path = artifact_root / "payloads" / f"{artifact_id}.md"
    path.write_text(text, encoding="utf-8")
    return _artifact_row(
        artifact_root,
        artifact_id=artifact_id,
        artifact_type="text_memory",
        payload=path,
        quality=quality,
        manifest={"content_path": path.name},
    )


def _skill(
    artifact_root: Path,
    artifact_id: str,
    quality: float,
) -> dict[str, object]:
    path = artifact_root / "payloads" / artifact_id
    path.mkdir()
    (path / "SKILL.md").write_text(f"# {artifact_id}\n", encoding="utf-8")
    return _artifact_row(
        artifact_root,
        artifact_id=artifact_id,
        artifact_type="skill_bundle",
        payload=path,
        quality=quality,
    )


def _agent_system(
    artifact_root: Path,
    artifact_id: str,
    text: str,
    quality: float,
) -> dict[str, object]:
    directory = artifact_root / "payloads" / artifact_id
    directory.mkdir()
    path = directory / "AGENTS.md"
    path.write_text(text, encoding="utf-8")
    return _artifact_row(
        artifact_root,
        artifact_id=artifact_id,
        artifact_type="agent_system",
        payload=path,
        quality=quality,
        manifest={"content_path": "AGENTS.md", "target_path": "AGENTS.md"},
    )


def _adapter(
    artifact_root: Path,
    artifact_id: str,
    quality: float,
    *,
    base_model: str = "model-a",
) -> dict[str, object]:
    path = artifact_root / "payloads" / artifact_id
    path.mkdir()
    (path / "adapter.bin").write_bytes(b"non-empty-adapter")
    return _artifact_row(
        artifact_root,
        artifact_id=artifact_id,
        artifact_type="parametric_memory",
        payload=path,
        quality=quality,
        manifest={
            "adapter_id": artifact_id,
            "adapter_format": "lora",
            "base_model": base_model,
        },
        compatibility={"task_tags": ["parser"], "base_model": [base_model]},
    )


def _projection(response: object, target_id: str):
    return next(item for item in response.projections if item.target_id == target_id)


def test_verified_handlers_execute_and_target_ranks_preserve_global_order(
    artifact_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    patched_registry = dict(builtin_handlers.BUILTIN_HANDLER_REGISTRY)
    for handler_id in ("text_memory_handler", "skill_bundle_handler"):
        original = patched_registry[handler_id]

        def make_recording_handler(original_handler, entry_point_name):
            def recording_handler(handler_input, services):
                calls[handler_input.target_id] = handler_input
                return original_handler(handler_input, services)

            recording_handler.__module__ = builtin_handlers.__name__
            recording_handler.__qualname__ = entry_point_name
            return recording_handler

        recording_handler = make_recording_handler(original, handler_id)
        monkeypatch.setattr(builtin_handlers, handler_id, recording_handler)
        patched_registry[handler_id] = recording_handler
    monkeypatch.setattr(
        builtin_handlers,
        "BUILTIN_HANDLER_REGISTRY",
        patched_registry,
    )
    registry = verified_builtin_registry(tmp_path / "recording-registry")
    _, Resolver = _projection_types()
    rows = [
        _skill(artifact_root, "skill-high", 0.9),
        _memory(artifact_root, "memory-middle", "Remember this.", 0.8),
        _skill(artifact_root, "skill-low", 0.7),
    ]

    response = Resolver(artifact_root, registry).resolve(
        _request(), rows, context_id="ctx-ranking"
    )

    assert response.projection_contract_version == "1"
    assert response.context_id == "ctx-ranking"
    assert response.registry_digest == registry.snapshot.registry_digest
    assert response.base_model == "model-a"
    assert response.destination_roots == _request().destination_roots
    assert set(calls) == {"text_memory", "skill_bundle"}
    assert [item.artifact_id for item in calls["skill_bundle"].ranked_artifacts] == [
        "skill-high",
        "skill-low",
    ]
    assert [item.rank_index for item in calls["skill_bundle"].ranked_artifacts] == [0, 1]
    assert [item.artifact_id for item in calls["text_memory"].ranked_artifacts] == [
        "memory-middle"
    ]
    assert response.selection.artifact_ids == (
        "skill-high",
        "memory-middle",
        "skill-low",
    )
    assert _projection(response, "skill_bundle").artifact_ids == (
        "skill-high",
        "skill-low",
    )


def test_snapshot_failure_skips_only_the_bad_artifact(
    artifact_root: Path,
    executable_registry,
) -> None:
    missing = _artifact_row(
        artifact_root,
        artifact_id="missing-memory",
        artifact_type="text_memory",
        payload=artifact_root / "payloads" / "missing.md",
        quality=1.0,
        manifest={"content_path": "missing.md"},
    )
    valid = _memory(artifact_root, "valid-memory", "usable", 0.5)
    _, Resolver = _projection_types()

    response = Resolver(artifact_root, executable_registry).resolve(
        _request(), [missing, valid], context_id="ctx-snapshot-skip"
    )

    assert _projection(response, "text_memory").artifact_ids == ("valid-memory",)
    assert response.selection.artifact_ids == ("valid-memory",)
    assert response.selection.skipped_artifact_ids == ("missing-memory",)
    assert response.selection.skipped_artifacts[0].reason == "payload_policy_rejected"


def test_remote_uri_skip_has_bounded_reason_code(
    artifact_root: Path,
    executable_registry,
) -> None:
    remote = _adapter(artifact_root, "adapter-remote", 1.0)
    remote["uri"] = "hf://organization/model@immutable-revision"
    _, Resolver = _projection_types()

    response = Resolver(artifact_root, executable_registry).resolve(
        _request(), [remote], context_id="ctx-remote-skip"
    )

    assert response.projections == ()
    assert response.selection.skipped_artifact_ids == ("adapter-remote",)
    assert response.selection.skipped_artifacts[0].reason == (
        "unsupported_uri_scheme"
    )
    encoded = json.dumps(response.model_dump(mode="json"), sort_keys=True)
    assert "organization/model" not in encoded


def test_typed_skip_is_emitted_only_after_compatibility_filtering(
    artifact_root: Path,
    executable_registry,
) -> None:
    incompatible = _memory(artifact_root, "incompatible-remote", "unused", 1.0)
    incompatible["uri"] = "hf://organization/private@revision"
    incompatible["compatibility_json"] = json.dumps(
        {"task_tags": ["other-task"]},
        sort_keys=True,
    )
    _, Resolver = _projection_types()

    response = Resolver(artifact_root, executable_registry).resolve(
        _request(), [incompatible], context_id="ctx-incompatible-skip"
    )

    assert response.projections == ()
    assert response.selection.artifact_ids == ()
    assert response.selection.skipped_artifacts == ()


def test_malformed_file_uri_is_a_typed_payload_skip(
    artifact_root: Path,
    executable_registry,
) -> None:
    malformed = _memory(artifact_root, "malformed-uri", "unused", 1.0)
    malformed["uri"] = "file://[invalid/path"
    _, Resolver = _projection_types()

    response = Resolver(artifact_root, executable_registry).resolve(
        _request(), [malformed], context_id="ctx-malformed-uri"
    )

    assert response.projections == ()
    assert response.selection.skipped_artifact_ids == ("malformed-uri",)
    assert response.selection.skipped_artifacts[0].reason == (
        "payload_policy_rejected"
    )


def test_target_candidate_attempt_budget_applies_before_payload_io(
    artifact_root: Path,
    executable_registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _artifact_row(
            artifact_root,
            artifact_id=f"missing-{index}",
            artifact_type="text_memory",
            payload=artifact_root / "payloads" / f"missing-{index}.md",
            quality=float(10 - index),
            manifest={"content_path": f"missing-{index}.md"},
        )
        for index in range(3)
    ]
    monkeypatch.setattr(
        context_projection,
        "MAX_CONTEXT_CANDIDATES_PER_TARGET",
        2,
        raising=False,
    )
    _, Resolver = _projection_types()

    with pytest.raises(ValueError, match="payload attempt budget"):
        Resolver(artifact_root, executable_registry).resolve(
            _request(), rows, context_id="ctx-candidate-budget"
        )


def test_remote_and_unbound_candidates_do_not_consume_payload_attempt_budget(
    artifact_root: Path,
    executable_registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = _memory(artifact_root, "memory-remote", "remote", 1.0)
    remote["uri"] = "hf://organization/memory@revision"
    unbound = _memory(artifact_root, "memory-unbound", "unbound", 0.9)
    unbound["manifest_json"] = None
    valid = _memory(artifact_root, "memory-valid", "valid", 0.8)
    monkeypatch.setattr(
        context_projection,
        "MAX_CONTEXT_CANDIDATES_PER_TARGET",
        1,
    )
    _, Resolver = _projection_types()

    response = Resolver(artifact_root, executable_registry).resolve(
        _request(),
        [remote, unbound, valid],
        context_id="ctx-cheap-skips",
    )

    assert response.selection.artifact_ids == ("memory-valid",)
    assert response.selection.skipped_artifact_ids == (
        "memory-remote",
        "memory-unbound",
    )


@pytest.mark.parametrize("field", ["compatibility_json", "scores_json"])
def test_candidate_metadata_is_bounded_before_ranking(
    artifact_root: Path,
    executable_registry,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    row = _memory(artifact_root, "memory-oversized-metadata", "memory", 1.0)
    row[field] = json.dumps(
        {"padding": "x" * context_projection.MAX_CONTRACT_JSON_BYTES}
    )

    def bounded_ranking(rows):
        assert rows == []
        return []

    monkeypatch.setattr(context_projection, "sort_candidates", bounded_ranking)
    _, Resolver = _projection_types()

    response = Resolver(artifact_root, executable_registry).resolve(
        _request(),
        [row],
        context_id=f"ctx-oversized-{field}",
    )

    assert response.selection.artifact_ids == ()
    if field == "compatibility_json":
        assert response.selection.skipped_artifacts == ()
    else:
        assert response.selection.skipped_artifact_ids == (
            "memory-oversized-metadata",
        )
        assert response.selection.skipped_artifacts[0].reason == (
            "metadata_policy_rejected"
        )


def test_semantically_invalid_promoted_artifact_fails_projection_closed(
    artifact_root: Path,
    executable_registry,
) -> None:
    invalid_skill = artifact_root / "payloads" / "invalid-skill"
    invalid_skill.mkdir()
    (invalid_skill / "README.md").write_text("not a skill", encoding="utf-8")
    row = _artifact_row(
        artifact_root,
        artifact_id="invalid-skill",
        artifact_type="skill_bundle",
        payload=invalid_skill,
        quality=1.0,
    )
    memory = _memory(artifact_root, "valid-memory-alongside", "memory", 0.5)
    _, Resolver = _projection_types()

    with pytest.raises(ValueError, match="requires root SKILL.md"):
        Resolver(artifact_root, executable_registry).resolve(
            _request(),
            [row, memory],
            context_id="ctx-invalid-promoted-artifact",
        )


def test_projection_uses_immutable_registered_manifest_not_mutable_file(
    artifact_root: Path,
    executable_registry,
    tmp_path: Path,
) -> None:
    row = _agent_system(
        artifact_root,
        "agent-unsafe-manifest",
        "Do not trust an external manifest.",
        1.0,
    )
    manifest_path = Path(str(row["manifest_path"]))
    manifest_path.unlink()
    outside = tmp_path / "outside-manifest.json"
    outside.write_text(
        json.dumps(
            {
                "manifest": {
                    "content_path": "AGENTS.md",
                    "target_path": "CLAUDE.md",
                }
            }
        ),
        encoding="utf-8",
    )
    manifest_path.symlink_to(outside)
    _, Resolver = _projection_types()

    response = Resolver(artifact_root, executable_registry).resolve(
        _request(), [row], context_id="ctx-unsafe-manifest"
    )

    projection = _projection(response, "agent_system")
    assert projection.artifact_ids == ("agent-unsafe-manifest",)
    assert [
        item.destination_relative_path for item in projection.staged_payloads
    ] == [
        "agent_system.md",
        "AGENTS.md",
    ]


def test_semantically_invalid_registered_manifest_fails_closed(
    artifact_root: Path,
    executable_registry,
) -> None:
    row = _memory(artifact_root, "memory-invalid-manifest", "memory", 1.0)
    row["manifest_json"] = "{not-json"
    _, Resolver = _projection_types()

    with pytest.raises(ValueError, match="registered artifact manifest"):
        Resolver(artifact_root, executable_registry).resolve(
            _request(),
            [row],
            context_id="ctx-invalid-registered-manifest",
        )


def test_unbound_legacy_manifest_is_quarantined_without_blocking_valid_artifacts(
    artifact_root: Path,
    executable_registry,
) -> None:
    legacy = _memory(artifact_root, "memory-legacy-unbound", "legacy", 1.0)
    legacy["manifest_json"] = None
    legacy["compatibility_json"] = "not-json"
    legacy["scores_json"] = "x" * (
        context_projection.MAX_ARTIFACT_ROUTING_JSON_BYTES + 1
    )
    valid = _memory(artifact_root, "memory-current-bound", "current", 0.5)
    _, Resolver = _projection_types()

    response = Resolver(artifact_root, executable_registry).resolve(
        _request(),
        [legacy, valid],
        context_id="ctx-unbound-legacy-manifest",
    )

    assert response.selection.artifact_ids == ("memory-current-bound",)
    assert response.selection.skipped_artifacts == ()


def test_full_ranking_contract_is_preserved_in_projection_selection(
    artifact_root: Path,
    executable_registry,
) -> None:
    low_quality = _memory(artifact_root, "memory-low-quality", "low", 0.4)
    low_quality["scores_json"] = json.dumps(
        {"quality": 0.4, "heldout_reward_delta": 1.0}
    )
    reward_only = _memory(artifact_root, "memory-reward-only", "reward", 0.0)
    reward_only["scores_json"] = json.dumps({"heldout_reward_delta": 0.8})
    earlier = _memory(artifact_root, "memory-earlier", "earlier", 0.6)
    earlier["created_at"] = "2026-01-01T00:00:00+00:00"
    later_a = _memory(artifact_root, "memory-later-a", "later a", 0.6)
    later_a["created_at"] = "2026-01-02T00:00:00+00:00"
    later_b = _memory(artifact_root, "memory-later-b", "later b", 0.6)
    later_b["created_at"] = "2026-01-02T00:00:00+00:00"
    _, Resolver = _projection_types()

    response = Resolver(artifact_root, executable_registry).resolve(
        _request(),
        [low_quality, earlier, later_a, reward_only, later_b],
        context_id="ctx-full-ranking",
    )

    assert response.selection.artifact_ids == (
        "memory-reward-only",
        "memory-later-b",
        "memory-later-a",
        "memory-earlier",
        "memory-low-quality",
    )
    assert _projection(response, "text_memory").artifact_ids == (
        response.selection.artifact_ids
    )


def test_explicit_context_artifact_ids_remain_a_strict_allowlist(
    artifact_root: Path,
    executable_registry,
) -> None:
    first = _memory(artifact_root, "memory-allowed", "allowed", 0.5)
    second = _memory(artifact_root, "memory-excluded", "excluded", 1.0)
    payload = _request().model_dump(mode="json")
    payload["metadata"]["evolution"] = {
        "context_artifact_ids": ["memory-allowed"]
    }
    Request, Resolver = _projection_types()

    response = Resolver(artifact_root, executable_registry).resolve(
        Request.model_validate(payload),
        [first, second],
        context_id="ctx-explicit-allowlist",
    )

    assert response.selection.artifact_ids == ("memory-allowed",)
    assert _projection(response, "text_memory").artifact_ids == (
        "memory-allowed",
    )

    payload["metadata"]["evolution"]["context_artifact_ids"] = []
    empty_response = Resolver(artifact_root, executable_registry).resolve(
        Request.model_validate(payload),
        [first, second],
        context_id="ctx-explicit-empty-allowlist",
    )
    assert empty_response.projections == ()
    assert empty_response.selection.artifact_ids == ()


def test_explicit_context_artifact_ids_preserve_revision_order(
    artifact_root: Path,
    executable_registry,
) -> None:
    memory = _memory(artifact_root, "memory-explicit", "remember", 0.9)
    skill = _skill(artifact_root, "skill-explicit", 0.8)
    agent_system = _agent_system(
        artifact_root,
        "agent-system-explicit",
        "Apply the evolved procedure.",
        0.7,
    )
    requested_order = (
        "agent-system-explicit",
        "skill-explicit",
        "memory-explicit",
    )
    payload = _request().model_dump(mode="json")
    payload["metadata"]["evolution"] = {"context_artifact_ids": list(requested_order)}
    Request, Resolver = _projection_types()

    response = Resolver(artifact_root, executable_registry).resolve(
        Request.model_validate(payload),
        [memory, skill, agent_system],
        context_id="ctx-explicit-revision-order",
    )

    assert response.selection.artifact_ids == requested_order
    assert {
        projection.target_id: projection.artifact_ids for projection in response.projections
    } == {
        "agent_system": ("agent-system-explicit",),
        "skill_bundle": ("skill-explicit",),
        "text_memory": ("memory-explicit",),
    }


@pytest.mark.parametrize(
    "auth_mode",
    ["subscription", "chatgpt_subscription", "claude_subscription"],
)
def test_subscription_auth_suppresses_adapter_target_before_handler_call(
    artifact_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_mode: str,
) -> None:
    calls = 0
    original = builtin_handlers.parametric_memory_handler

    def recording_adapter_handler(handler_input, services):
        nonlocal calls
        calls += 1
        return original(handler_input, services)

    recording_adapter_handler.__module__ = builtin_handlers.__name__
    recording_adapter_handler.__qualname__ = "parametric_memory_handler"
    monkeypatch.setattr(
        builtin_handlers,
        "parametric_memory_handler",
        recording_adapter_handler,
    )
    patched_registry = dict(builtin_handlers.BUILTIN_HANDLER_REGISTRY)
    patched_registry["parametric_memory_handler"] = recording_adapter_handler
    monkeypatch.setattr(
        builtin_handlers,
        "BUILTIN_HANDLER_REGISTRY",
        patched_registry,
    )
    registry = verified_builtin_registry(tmp_path / f"registry-{auth_mode}")
    _, Resolver = _projection_types()

    response = Resolver(artifact_root, registry).resolve(
        _request(auth_mode=auth_mode),
        [_adapter(artifact_root, f"adapter-{auth_mode}", 1.0)],
        context_id=f"ctx-{auth_mode}",
    )

    assert calls == 0
    assert response.projections == ()
    assert response.selection.artifact_ids == ()


def test_adapter_selection_requires_matching_request_base_model(
    artifact_root: Path,
    executable_registry,
) -> None:
    matching = _adapter(artifact_root, "adapter-match", 0.6, base_model="model-a")
    mismatched = _adapter(artifact_root, "adapter-other", 0.9, base_model="model-b")
    _, Resolver = _projection_types()

    response = Resolver(artifact_root, executable_registry).resolve(
        _request(base_model="model-a"),
        [mismatched, matching],
        context_id="ctx-model",
    )

    projection = _projection(response, "parametric_memory")
    assert projection.artifact_ids == ("adapter-match",)
    assert projection.adapters[0].base_model == "model-a"
    assert len(projection.adapters[0].source_payload_digest) == 64
    assert projection.adapters[0].source_size_bytes == len(b"non-empty-adapter")
    assert response.base_model == "model-a"
    assert response.selection.artifact_ids == ("adapter-match",)

    adapter_path = Path(str(matching["uri"]).removeprefix("file://"))
    (adapter_path / "adapter.bin").write_bytes(b"changed-adapter")
    with ArtifactPayloadService(artifact_root) as payloads:
        changed = payloads.issue_snapshot(
            artifact_id="adapter-match",
            artifact_type="parametric_memory",
            name="adapter-match",
            uri=str(matching["uri"]),
            manifest={
                "adapter_id": "adapter-match",
                "adapter_format": "lora",
                "base_model": "model-a",
            },
            scores={},
            rank_index=0,
        )
    assert (
        changed.payload_manifest_digest
        != projection.adapters[0].source_payload_digest
    )


def test_direct_resolver_filters_unpromoted_rows_and_rejects_profile_drift(
    artifact_root: Path,
    executable_registry,
) -> None:
    unpromoted = _memory(artifact_root, "memory-unpromoted", "draft", 1.0)
    unpromoted["promoted"] = 0
    _, Resolver = _projection_types()
    resolver = Resolver(artifact_root, executable_registry)

    response = resolver.resolve(
        _request(), [unpromoted], context_id="ctx-unpromoted"
    )
    assert response.projections == ()
    assert response.selection.artifact_ids == ()

    payload = _request(auth_mode="subscription").model_dump(mode="json")
    payload["execution_profile"]["execution_mode"] = "self_deployed"
    Request, _ = _projection_types()
    with pytest.raises(ValueError, match="auth mode"):
        resolver.resolve(
            Request.model_validate(payload),
            [],
            context_id="ctx-auth-drift",
        )

    payload = _request().model_dump(mode="json")
    payload["agent"]["harness"] = "other"
    with pytest.raises(ValueError, match="agent harness"):
        resolver.resolve(
            Request.model_validate(payload),
            [],
            context_id="ctx-harness-drift",
        )


def test_memory_separator_and_utf8_clipping_use_shared_target_budget(
    artifact_root: Path,
    executable_registry,
) -> None:
    limits = TargetConsumptionLimits(max_text_chars=6, max_text_bytes=8)
    rows = [
        _memory(artifact_root, "memory-first", "A界B", 0.9),
        _memory(artifact_root, "memory-second", "CDEF", 0.8),
    ]
    _, Resolver = _projection_types()

    response = Resolver(artifact_root, executable_registry).resolve(
        _request(target_limits={"text_memory": limits}),
        rows,
        context_id="ctx-clipping",
    )

    projection = _projection(response, "text_memory")
    assert projection.artifact_ids == ("memory-first", "memory-second")
    assert projection.instructions[0].text == "A界B\n\nC"
    assert projection.renderer.data.markdown == "A界B\n\nC"
    assert len(projection.instructions[0].text) == 6
    assert len(projection.instructions[0].text.encode("utf-8")) == 8


def test_response_serialization_never_exposes_uri_or_payload_handle(
    artifact_root: Path,
    executable_registry,
) -> None:
    rows = [
        _memory(artifact_root, "private-memory", "private bytes", 0.9),
        _skill(artifact_root, "private-skill", 0.8),
        _agent_system(artifact_root, "private-agent", "private rules", 0.7),
        _adapter(artifact_root, "private-adapter", 0.6),
    ]
    _, Resolver = _projection_types()

    response = Resolver(artifact_root, executable_registry).resolve(
        _request(), rows, context_id="ctx-public-contract"
    )
    encoded = json.dumps(response.model_dump(mode="json"), sort_keys=True)

    assert "file://" not in encoded
    assert "payload_handle" not in encoded
    assert str(artifact_root) not in encoded
    assert all(str(row["uri"]) not in encoded for row in rows)

    payload = response.model_dump(mode="json")
    payload["memory"] = {}
    with pytest.raises(ValidationError, match="extra_forbidden"):
        type(response).model_validate(payload)
    payload = response.model_dump(mode="json")
    payload["projection_contract_version"] = "2"
    with pytest.raises(ValidationError, match="literal_error"):
        type(response).model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", b"task-bytes"),
        ("rollout_step", "3"),
    ],
)
def test_projection_request_rejects_scalar_type_coercion(
    field: str,
    value: object,
) -> None:
    Request, _ = _projection_types()
    payload = _request().model_dump(mode="python")
    payload[field] = value

    with pytest.raises(ValidationError):
        Request.model_validate(payload)


def test_projection_request_target_limits_are_deeply_immutable() -> None:
    request = _request(
        target_limits={"text_memory": TargetConsumptionLimits(max_artifacts=1)}
    )

    with pytest.raises(TypeError, match="immutable"):
        request.target_limits["text_memory"] = TargetConsumptionLimits(
            max_artifacts=2
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent", {"harness": "codex", "env": {"API_TOKEN": "secret"}}),
        ("metadata", {"task_tags": ["parser"], "api_token": "secret"}),
        ("agent", {"settings": {"auth_mode": "subscription"}}),
        ("instruction", "x" * (context_projection.MAX_CONTRIBUTION_TEXT + 1)),
    ],
    ids=("agent-secret", "metadata-secret", "missing-harness", "instruction-limit"),
)
def test_projection_request_rejects_unbounded_or_sensitive_shape(
    field: str,
    value: object,
) -> None:
    Request, _ = _projection_types()
    payload = _request().model_dump(mode="json")
    payload[field] = value

    with pytest.raises(ValidationError):
        Request.model_validate(payload)


@pytest.mark.parametrize(
    ("metadata", "error_fragment"),
    [
        (
            {"task_tags": ["x" * (context_projection.MAX_CONTEXT_TASK_TAG_LENGTH + 1)]},
            "task tag",
        ),
        (
            {
                "evolution": {
                    "context_artifact_ids": [
                        "x"
                        * (context_projection.MAX_CONTEXT_ARTIFACT_ID_LENGTH + 1)
                    ]
                }
            },
            "context artifact ID",
        ),
    ],
)
def test_projection_request_bounds_metadata_elements(
    metadata: dict[str, object],
    error_fragment: str,
) -> None:
    Request, _ = _projection_types()
    payload = _request().model_dump(mode="json")
    payload["metadata"] = metadata

    with pytest.raises(ValidationError, match=error_fragment):
        Request.model_validate(payload)


def test_projection_request_enforces_total_canonical_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Request, _ = _projection_types()
    payload = _request().model_dump(mode="json")
    payload["metadata"] = {"task_tags": ["bounded-tag"]}
    encoded_size = len(canonical_json(payload).encode("utf-8"))
    monkeypatch.setattr(
        context_projection,
        "MAX_CONTEXT_PROJECTION_REQUEST_BYTES",
        encoded_size - 1,
    )

    with pytest.raises(ValidationError, match="request exceeds the byte budget"):
        Request.model_validate(payload)


@pytest.mark.parametrize(
    "limits",
    [
        {"max_artifacts": "1"},
        {"max_adapters": True},
    ],
)
def test_projection_request_rejects_nested_limit_coercion(
    limits: dict[str, object],
) -> None:
    Request, _ = _projection_types()
    payload = _request().model_dump(mode="json")
    payload["target_limits"] = {"text_memory": limits}

    with pytest.raises(ValidationError):
        Request.model_validate(payload)


def test_zero_artifact_limit_disables_target_without_invoking_handler(
    artifact_root: Path,
    executable_registry,
) -> None:
    _, Resolver = _projection_types()

    response = Resolver(artifact_root, executable_registry).resolve(
        _request(
            target_limits={
                "text_memory": TargetConsumptionLimits(max_artifacts=0)
            }
        ),
        [_memory(artifact_root, "disabled-memory", "unused", 1.0)],
        context_id="ctx-zero-artifact-limit",
    )

    assert response.projections == ()
    assert response.selection.artifact_ids == ()
    assert response.selection.skipped_artifacts == ()


def test_projection_propagates_request_wide_payload_budget_exhaustion(
    artifact_root: Path,
    executable_registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _memory(artifact_root, "memory-budget-first", "123", 1.0),
        _memory(artifact_root, "memory-budget-second", "456", 0.9),
    ]
    monkeypatch.setattr(
        artifact_payloads,
        "MAX_PAYLOAD_TOTAL_BYTES",
        5,
    )
    _, Resolver = _projection_types()

    with pytest.raises(
        artifact_payloads.ArtifactPayloadBudgetExceeded,
        match="aggregate total bytes",
    ):
        Resolver(artifact_root, executable_registry).resolve(
            _request(),
            rows,
            context_id="ctx-request-payload-budget",
        )


def test_context_wide_destination_conflicts_fail_closed(
    artifact_root: Path,
    executable_registry,
) -> None:
    roots = RuntimeDestinationRoots(
        target_data="/runtime",
        harness_skills="/runtime",
        harness_instruction="/workspace/repository",
    )
    rows = [
        _memory(artifact_root, "memory-source", "memory", 0.9),
        _skill(artifact_root, "memory.md", 0.8),
    ]
    _, Resolver = _projection_types()

    with pytest.raises(ValueError, match="cross-target destination conflict"):
        Resolver(artifact_root, executable_registry).resolve(
            _request(destination_roots=roots), rows, context_id="ctx-conflict"
        )


@pytest.mark.parametrize("registry_kind", ["missing", "unverified"])
def test_resolver_requires_verified_executable_registry(
    artifact_root: Path,
    executable_registry,
    registry_kind: str,
) -> None:
    _, Resolver = _projection_types()
    registry = None if registry_kind == "missing" else executable_registry.snapshot

    with pytest.raises((TypeError, ValueError), match="verified|registry"):
        Resolver(artifact_root, registry)
