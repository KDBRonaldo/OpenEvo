"""Wire models and canonical serialization shared by the WebUI product."""

from .models import (
    ApiErrorV2,
    AttemptRefV2,
    ContractOfferV2,
    EffectiveExecutionSnapshotRefV2,
    EvolutionRevisionRefV2,
    ProjectHeadRefV2,
    RuntimeContextSnapshotRefV2,
    SuccessorTransitionRefV2,
    TaskAdmissionRefV2,
    WorkspaceSnapshotRefV2,
)
from .snapshots import (
    canonical_contract_bytes,
    canonical_contract_sha256,
    canonical_json_bytes,
    deterministic_sha256,
    parse_contract_json_bytes,
)

__all__ = [
    "ApiErrorV2",
    "AttemptRefV2",
    "ContractOfferV2",
    "EffectiveExecutionSnapshotRefV2",
    "EvolutionRevisionRefV2",
    "ProjectHeadRefV2",
    "RuntimeContextSnapshotRefV2",
    "SuccessorTransitionRefV2",
    "TaskAdmissionRefV2",
    "WorkspaceSnapshotRefV2",
    "canonical_contract_bytes",
    "canonical_contract_sha256",
    "canonical_json_bytes",
    "deterministic_sha256",
    "parse_contract_json_bytes",
]
