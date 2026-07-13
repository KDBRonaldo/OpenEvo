"""Repository-private issuance helpers for revision ledger tests."""

from __future__ import annotations

from openevo.evolution import revisions
from openevo.evolution.revisions import ExecutionSnapshotV1, VerifiedExecutionSnapshot


def verified_execution_snapshot_for_test(
    snapshot: ExecutionSnapshotV1,
) -> VerifiedExecutionSnapshot:
    """Issue a sealed execution snapshot without creating a production producer."""

    verified = object.__new__(VerifiedExecutionSnapshot)
    object.__setattr__(verified, "snapshot", snapshot)
    object.__setattr__(verified, "producer_id", "repo-testkit")
    object.__setattr__(
        verified,
        "_verification_seal",
        revisions._VERIFIED_EXECUTION_SNAPSHOT_SEAL,
    )
    return verified


__all__ = ["verified_execution_snapshot_for_test"]
