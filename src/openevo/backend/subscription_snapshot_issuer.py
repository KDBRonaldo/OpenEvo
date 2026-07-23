"""Private production issuer for verified Codex Subscription execution snapshots."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import ValidationError

from openevo.backend.runtime_identity import ManagedSubscriptionRuntimeIdentity
from openevo.codex_models import codex_cli_model_name, validate_codex_model_ref
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


SUBSCRIPTION_SNAPSHOT_PRODUCER_ID = "subscription-snapshot-issuer-v1"
SUBSCRIPTION_TASK_NETWORK_POLICY_ID = "openevo.task-network.v1"
_SUBSCRIPTION_MODEL_REVISION = "subscription-managed"
_SUBSCRIPTION_SERVING_ID = "codex-subscription"


@dataclass(frozen=True, slots=True)
class SubscriptionSnapshotIssueError(ValueError):
    """Closed internal issuer failure mapped by the run-admission owner."""

    code: str

    def __str__(self) -> str:
        return self.code


def _issue_subscription_snapshot(
    *,
    runtime: ManagedSubscriptionRuntimeIdentity,
    capture_mode: str,
    harness_id: str,
    model_ref: str,
    token_limit: int,
    task_network_allow_internet: bool,
) -> VerifiedExecutionSnapshot:
    """Seal only the complete release-owned Subscription composition."""

    if type(runtime) is not ManagedSubscriptionRuntimeIdentity:
        raise SubscriptionSnapshotIssueError("managed_runtime_identity_unavailable")
    try:
        runtime.__post_init__()
    except (TypeError, ValueError) as exc:
        raise SubscriptionSnapshotIssueError(
            "managed_runtime_identity_unavailable"
        ) from exc
    if capture_mode != "transcript":
        raise SubscriptionSnapshotIssueError("subscription_capture_invalid")
    if harness_id != runtime.harness_id or harness_id != "codex":
        raise SubscriptionSnapshotIssueError("subscription_harness_invalid")
    if type(task_network_allow_internet) is not bool:
        raise SubscriptionSnapshotIssueError("task_network_policy_invalid")
    try:
        validated_model = validate_codex_model_ref(
            model_ref,
            field_name="Subscription model",
        )
        if validated_model != model_ref:
            raise ValueError("Subscription model is not canonical")
        model_id = codex_cli_model_name(validated_model)
        model = ExecutionModelIdentity(
            source=ModelIdentitySource.SUBSCRIPTION,
            model_id=model_id,
            model_revision=_SUBSCRIPTION_MODEL_REVISION,
            token_limit=token_limit,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise SubscriptionSnapshotIssueError("subscription_model_invalid") from exc
    if model.model_id != runtime.codex_model:
        raise SubscriptionSnapshotIssueError("managed_runtime_model_mismatch")

    try:
        task_network = ExecutionTaskNetworkPolicy(
            policy_id=SUBSCRIPTION_TASK_NETWORK_POLICY_ID,
            allow_internet=task_network_allow_internet,
            policy_digest=execution_task_network_policy_digest(
                policy_id=SUBSCRIPTION_TASK_NETWORK_POLICY_ID,
                allow_internet=task_network_allow_internet,
            ),
        )
        serving_digest = canonical_digest(
            {
                "subscription_serving_contract_version": "1",
                "kind": "subscription",
                "provider": "codex",
                "model_id": model.model_id,
                "endpoint": None,
            }
        )
        snapshot = ExecutionSnapshotV1(
            execution_mode="subscription",
            capture_mode="transcript",
            token_level_metrics_available=False,
            model=model,
            runtime=ExecutionRuntimeIdentity(
                kind="subscription_client",
                harness_id=runtime.harness_id,
                harness_version=runtime.harness_version,
                image_digest=runtime.image_digest,
                policy_id=runtime.runtime_policy_id,
                policy_digest=runtime.runtime_policy_digest,
                snapshot=content_addressed_snapshot_ref(
                    "runtime",
                    runtime.runtime_identity_digest,
                ),
            ),
            serving=ExecutionServingIdentity(
                kind="subscription",
                deployment_id=_SUBSCRIPTION_SERVING_ID,
                snapshot=content_addressed_snapshot_ref("deployment", serving_digest),
                endpoint=None,
            ),
            task_network=task_network,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise SubscriptionSnapshotIssueError("subscription_snapshot_invalid") from exc

    verified = object.__new__(VerifiedExecutionSnapshot)
    object.__setattr__(verified, "snapshot", snapshot)
    object.__setattr__(verified, "producer_id", SUBSCRIPTION_SNAPSHOT_PRODUCER_ID)
    object.__setattr__(
        verified,
        "_verification_seal",
        revisions._VERIFIED_EXECUTION_SNAPSHOT_SEAL,
    )
    return require_verified_execution_snapshot(verified)


# The issuer intentionally has no public constructor or injectable publication
# hook. Project-head orchestration enters through the two run_admission resolvers.
__all__: list[str] = []
