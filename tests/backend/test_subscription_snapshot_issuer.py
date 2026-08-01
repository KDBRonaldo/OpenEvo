from __future__ import annotations

import inspect
import json

import pytest
from pydantic import ValidationError

from openevo.backend.run_admission import (
    EffectiveExecutionSettings,
    EffectiveExecutionSnapshotUnavailable,
    resolve_genesis_execution_snapshot,
    resolve_settings_transition_execution_snapshot,
)
from openevo.backend.service_supervisor import ServiceExecutionMode, ServiceRunBinding
from openevo.evolution.framework import canonical_digest
from openevo.evolution.revisions import (
    ExecutionSnapshotV1,
    VerifiedExecutionSnapshot,
    execution_snapshot_id_for_snapshot,
    require_verified_execution_snapshot,
)
from openevo.internal_auth import InternalServiceIdentity
from openevo.projects.science.compiler import MANAGED_RUNTIME_IMAGES
from openevo.runtime.codex_isolation import (
    CODEX_SUBSCRIPTION_POLICY_ID,
    CODEX_SUBSCRIPTION_POLICY_SHA256,
)
from openevo.runtime.managed import (
    MANAGED_CODEX_VERSION,
    MANAGED_RUNTIME_RELEASES,
    require_immutable_managed_runtime_image,
)
from openevo.runtime.self_deployed import require_release_self_deployed_model_profile


def _binding(
    *,
    execution_mode: ServiceExecutionMode = ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
    codex_model: str = "gpt-5.3-codex-spark",
) -> ServiceRunBinding:
    image = MANAGED_RUNTIME_IMAGES["managed_science"]
    release = require_immutable_managed_runtime_image(
        profile="managed_science",
        image=MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest,
    )
    profile = require_release_self_deployed_model_profile("qwen3-0.6b-v1")
    self_deployed = execution_mode is ServiceExecutionMode.SELF_DEPLOYED
    return ServiceRunBinding(
        execution_mode=execution_mode,
        codex_model=profile.model_id if self_deployed else codex_model,
        runtime_image=image,
        runtime_image_immutable_reference=release.immutable_reference,
        runtime_identity_digest="1" * 64,
        generation_digest="2" * 64,
        registry_digest="3" * 64,
        framework_lock_digest="4" * 64,
        rollout_url="http://127.0.0.1:41001",
        evolution_backend_url="http://127.0.0.1:41002",
        gateway_url="http://127.0.0.1:41003",
        self_deployed_profile_id=profile.profile_id if self_deployed else None,
        self_deployed_profile_sha256=profile.profile_sha256 if self_deployed else None,
        self_deployed_model_revision=profile.model_revision if self_deployed else None,
        self_deployed_model_snapshot_sha256=(
            profile.model_snapshot_manifest_sha256 if self_deployed else None
        ),
        self_deployed_vllm_image=profile.vllm_image if self_deployed else None,
        self_deployed_vllm_image_config_digest=(
            profile.vllm_image_config_digest if self_deployed else None
        ),
        _identity=InternalServiceIdentity(
            service_id="core-control",
            generation_digest="2" * 64,
            registry_digest="3" * 64,
            framework_lock_digest="4" * 64,
            credential="credential-canary-" + "x" * 48,
        ),
    )


def _settings(**changes: object) -> EffectiveExecutionSettings:
    payload: dict[str, object] = {
        "execution_mode": "codex_subscription_transcript",
        "capture_mode": "transcript",
        "harness_id": "codex",
        "model_ref": "gpt-5.3-codex-spark",
        "token_limit": 200_000,
        "task_network_allow_internet": True,
    }
    payload.update(changes)
    return EffectiveExecutionSettings.model_validate(payload)


def test_subscription_genesis_issues_complete_verified_snapshot() -> None:
    binding = _binding()
    verified = resolve_genesis_execution_snapshot(
        settings=_settings(),
        service_binding=binding,
    )

    assert require_verified_execution_snapshot(verified) is verified
    assert verified.producer_id == "subscription-snapshot-issuer-v1"
    snapshot = verified.snapshot
    assert snapshot.execution_mode == "subscription"
    assert snapshot.capture_mode == "transcript"
    assert snapshot.token_level_metrics_available is False
    assert snapshot.model.source == "subscription"
    assert snapshot.model.model_id == "gpt-5.3-codex-spark"
    assert snapshot.model.model_revision == "subscription-managed"
    assert snapshot.model.token_limit == 200_000
    assert snapshot.runtime.kind == "subscription_client"
    assert snapshot.runtime.harness_id == "codex"
    assert snapshot.runtime.harness_version == MANAGED_CODEX_VERSION
    assert snapshot.runtime.snapshot.content_digest == binding.runtime_identity_digest
    runtime_release = require_immutable_managed_runtime_image(
        profile="managed_science",
        image=binding.runtime_image_immutable_reference,
    )
    assert snapshot.runtime.image_digest == runtime_release.trusted_digest.removeprefix("sha256:")
    assert snapshot.runtime.policy_id == CODEX_SUBSCRIPTION_POLICY_ID
    assert snapshot.runtime.policy_digest == CODEX_SUBSCRIPTION_POLICY_SHA256
    assert snapshot.task_network.allow_internet is True
    assert snapshot.task_network.policy_id == "openevo.task-network.v1"
    assert snapshot.serving.kind == "subscription"
    assert snapshot.serving.endpoint is None
    assert execution_snapshot_id_for_snapshot(snapshot) == (f"exec-{canonical_digest(snapshot)}")

    encoded = json.dumps(snapshot.model_dump(mode="json"), sort_keys=True)
    for forbidden in (
        "credential-canary",
        binding.rollout_url,
        binding.evolution_backend_url,
        binding.gateway_url,
        "SSH_AUTH_SOCK",
        "Authorization",
    ):
        assert forbidden not in encoded


def test_genesis_and_settings_transition_share_the_same_canonical_identity() -> None:
    settings = _settings()
    binding = _binding()

    genesis = resolve_genesis_execution_snapshot(
        settings=settings,
        service_binding=binding,
    )
    transitioned = resolve_settings_transition_execution_snapshot(
        settings=settings,
        service_binding=binding,
    )

    assert transitioned.snapshot == genesis.snapshot
    assert canonical_digest(transitioned.snapshot) == canonical_digest(genesis.snapshot)
    assert transitioned.producer_id == genesis.producer_id


def test_task_network_policy_is_part_of_the_snapshot_identity() -> None:
    binding = _binding()
    enabled = resolve_genesis_execution_snapshot(
        settings=_settings(task_network_allow_internet=True),
        service_binding=binding,
    )
    disabled = resolve_settings_transition_execution_snapshot(
        settings=_settings(task_network_allow_internet=False),
        service_binding=binding,
    )

    assert enabled.snapshot.task_network.allow_internet is True
    assert disabled.snapshot.task_network.allow_internet is False
    assert enabled.snapshot.task_network.policy_digest != (
        disabled.snapshot.task_network.policy_digest
    )
    assert canonical_digest(enabled.snapshot) != canonical_digest(disabled.snapshot)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"capture_mode": "proxy"}, "subscription_capture_invalid"),
        ({"harness_id": "claude-code"}, "subscription_harness_invalid"),
    ],
)
def test_unsupported_execution_profiles_fail_with_typed_unavailable(
    changes: dict[str, object],
    code: str,
) -> None:
    with pytest.raises(EffectiveExecutionSnapshotUnavailable) as failure:
        resolve_genesis_execution_snapshot(
            settings=_settings(**changes),
            service_binding=_binding(),
        )

    assert failure.value.code == code


def test_unavailable_or_mismatched_managed_runtime_fails_closed() -> None:
    with pytest.raises(EffectiveExecutionSnapshotUnavailable) as missing:
        resolve_genesis_execution_snapshot(
            settings=_settings(),
            service_binding=None,
        )
    assert missing.value.code == "managed_runtime_identity_unavailable"
    assert missing.value.retryable is True

    with pytest.raises(EffectiveExecutionSnapshotUnavailable) as wrong_mode:
        resolve_genesis_execution_snapshot(
            settings=_settings(),
            service_binding=_binding(execution_mode=ServiceExecutionMode.SELF_DEPLOYED),
        )
    assert wrong_mode.value.code == "managed_runtime_identity_unavailable"

    with pytest.raises(EffectiveExecutionSnapshotUnavailable) as wrong_model:
        resolve_genesis_execution_snapshot(
            settings=_settings(),
            service_binding=_binding(codex_model="gpt-5.5"),
        )
    assert wrong_model.value.code == "managed_runtime_model_mismatch"


def test_self_deployed_genesis_issues_exact_hugging_face_and_vllm_snapshot() -> None:
    profile = require_release_self_deployed_model_profile("qwen3-0.6b-v1")
    binding = _binding(execution_mode=ServiceExecutionMode.SELF_DEPLOYED)
    verified = resolve_genesis_execution_snapshot(
        settings=_settings(
            execution_mode="self-deployed",
            model_ref=profile.model_id,
            token_limit=8192,
        ),
        service_binding=binding,
    )

    assert verified.producer_id == "self-deployed-snapshot-issuer-v1"
    snapshot = verified.snapshot
    assert snapshot.execution_mode == "self_deployed"
    assert snapshot.capture_mode == "transcript"
    assert snapshot.token_level_metrics_available is False
    assert snapshot.model.source == "hugging_face"
    assert snapshot.model.model_id == profile.model_id
    assert snapshot.model.model_revision == profile.model_revision
    assert snapshot.model.token_limit == 8192
    assert snapshot.runtime.kind == "managed_runtime"
    assert snapshot.runtime.snapshot.content_digest == binding.runtime_identity_digest
    assert snapshot.serving.kind == "managed_deployment"
    assert snapshot.serving.deployment_id == "vllm-qwen3-0.6b-v1"
    assert snapshot.serving.endpoint is None


def test_self_deployed_snapshot_rejects_wrong_binding_or_model() -> None:
    profile = require_release_self_deployed_model_profile("qwen3-0.6b-v1")
    settings = _settings(
        execution_mode="self-deployed",
        model_ref=profile.model_id,
        token_limit=8192,
    )
    with pytest.raises(EffectiveExecutionSnapshotUnavailable) as wrong_binding:
        resolve_genesis_execution_snapshot(settings=settings, service_binding=_binding())
    assert wrong_binding.value.code == "self_deployed_runtime_identity_unavailable"

    with pytest.raises(EffectiveExecutionSnapshotUnavailable) as wrong_model:
        resolve_genesis_execution_snapshot(
            settings=_settings(
                execution_mode="self-deployed",
                model_ref="Qwen/Qwen3-1.7B",
                token_limit=8192,
            ),
            service_binding=_binding(execution_mode=ServiceExecutionMode.SELF_DEPLOYED),
        )
    assert wrong_model.value.code == "self_deployed_model_mismatch"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", {"id": "gpt-5.3-codex-spark"}),
        ("runtime", {"image": "caller-controlled"}),
        ("serving_endpoint", "https://example.invalid/v1"),
        ("environment", {"TOKEN": "secret"}),
        ("credentials", {"api_key": "secret"}),
        ("host_path", "/home/user/private"),
    ],
)
def test_desired_execution_settings_reject_open_or_secret_bearing_fields(
    field: str,
    value: object,
) -> None:
    payload = _settings().model_dump(mode="json")
    payload[field] = value

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EffectiveExecutionSettings.model_validate(payload)


@pytest.mark.parametrize(
    "model_ref",
    [
        "/home/user/private-model",
        "file:///srv/model",
        "https://user:token@example.invalid/model",
        "../private-model",
    ],
)
def test_subscription_issuer_rejects_model_paths_and_uris(model_ref: str) -> None:
    with pytest.raises(EffectiveExecutionSnapshotUnavailable) as failure:
        resolve_genesis_execution_snapshot(
            settings=_settings(model_ref=model_ref),
            service_binding=_binding(codex_model=model_ref),
        )

    assert failure.value.code == "subscription_model_invalid"


def test_verified_snapshot_and_production_issuer_have_no_public_injection_constructor() -> None:
    raw = ExecutionSnapshotV1.model_construct()
    with pytest.raises(TypeError, match="verified producer"):
        VerifiedExecutionSnapshot(snapshot=raw, producer_id="caller")

    for resolver in (
        resolve_genesis_execution_snapshot,
        resolve_settings_transition_execution_snapshot,
    ):
        assert "issuer" not in inspect.signature(resolver).parameters
