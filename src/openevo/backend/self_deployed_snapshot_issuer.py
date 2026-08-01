"""Private issuer for release-owned Self-Deployed execution snapshots."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from openevo.backend.runtime_identity import ManagedSelfDeployedRuntimeIdentity
from openevo.evolution import revisions
from openevo.evolution.framework import canonical_digest
from openevo.evolution.revisions import (
    ExecutionModelIdentity,
    ExecutionRuntimeIdentity,
    ExecutionServingIdentity,
    ExecutionSnapshotV1,
    ExecutionTaskNetworkPolicy,
    ModelIdentitySource,
    VerifiedExecutionSnapshot,
    content_addressed_snapshot_ref,
    execution_task_network_policy_digest,
    require_verified_execution_snapshot,
)


SELF_DEPLOYED_SNAPSHOT_PRODUCER_ID = "self-deployed-snapshot-issuer-v1"
SELF_DEPLOYED_TASK_NETWORK_POLICY_ID = "openevo.task-network.v1"


@dataclass(frozen=True, slots=True)
class SelfDeployedSnapshotIssueError(ValueError):
    code: str

    def __str__(self) -> str:
        return self.code


def _issue_self_deployed_snapshot(
    *,
    runtime: ManagedSelfDeployedRuntimeIdentity,
    capture_mode: str,
    harness_id: str,
    model_ref: str,
    token_limit: int,
    task_network_allow_internet: bool,
) -> VerifiedExecutionSnapshot:
    """Seal only the exact release profile verified by the service owner."""

    if type(runtime) is not ManagedSelfDeployedRuntimeIdentity:
        raise SelfDeployedSnapshotIssueError("self_deployed_runtime_identity_unavailable")
    try:
        runtime.__post_init__()
    except (TypeError, ValueError) as exc:
        raise SelfDeployedSnapshotIssueError("self_deployed_runtime_identity_unavailable") from exc
    if capture_mode != "transcript":
        raise SelfDeployedSnapshotIssueError("self_deployed_capture_invalid")
    if harness_id != runtime.harness_id or harness_id != "codex":
        raise SelfDeployedSnapshotIssueError("self_deployed_harness_invalid")
    if model_ref != runtime.model_id:
        raise SelfDeployedSnapshotIssueError("self_deployed_model_mismatch")
    if type(task_network_allow_internet) is not bool:
        raise SelfDeployedSnapshotIssueError("task_network_policy_invalid")

    try:
        model = ExecutionModelIdentity(
            source=ModelIdentitySource.HUGGING_FACE,
            model_id=runtime.model_id,
            model_revision=runtime.model_revision,
            token_limit=token_limit,
        )
        task_network = ExecutionTaskNetworkPolicy(
            policy_id=SELF_DEPLOYED_TASK_NETWORK_POLICY_ID,
            allow_internet=task_network_allow_internet,
            policy_digest=execution_task_network_policy_digest(
                policy_id=SELF_DEPLOYED_TASK_NETWORK_POLICY_ID,
                allow_internet=task_network_allow_internet,
            ),
        )
        deployment_digest = canonical_digest(
            {
                "managed_deployment_contract_version": "1",
                "model_profile_id": runtime.model_profile_id,
                "model_profile_sha256": runtime.profile_sha256,
                "model_snapshot_manifest_sha256": (runtime.model_snapshot_manifest_sha256),
                "vllm_image": runtime.vllm_image,
                "vllm_image_config_digest": runtime.vllm_image_config_digest,
            }
        )
        snapshot = ExecutionSnapshotV1(
            execution_mode="self_deployed",
            capture_mode="transcript",
            token_level_metrics_available=False,
            model=model,
            runtime=ExecutionRuntimeIdentity(
                kind="managed_runtime",
                harness_id=runtime.harness_id,
                harness_version=runtime.harness_version,
                image_digest=runtime.runtime_image_digest,
                policy_id=runtime.runtime_policy_id,
                policy_digest=runtime.runtime_policy_digest,
                snapshot=content_addressed_snapshot_ref(
                    "runtime",
                    runtime.runtime_identity_digest,
                ),
            ),
            serving=ExecutionServingIdentity(
                kind="managed_deployment",
                deployment_id=f"vllm-{runtime.model_profile_id}",
                snapshot=content_addressed_snapshot_ref(
                    "deployment",
                    deployment_digest,
                ),
                endpoint=None,
            ),
            task_network=task_network,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise SelfDeployedSnapshotIssueError("self_deployed_snapshot_invalid") from exc

    verified = object.__new__(VerifiedExecutionSnapshot)
    object.__setattr__(verified, "snapshot", snapshot)
    object.__setattr__(verified, "producer_id", SELF_DEPLOYED_SNAPSHOT_PRODUCER_ID)
    object.__setattr__(
        verified,
        "_verification_seal",
        revisions._VERIFIED_EXECUTION_SNAPSHOT_SEAL,
    )
    return require_verified_execution_snapshot(verified)


__all__: list[str] = []
