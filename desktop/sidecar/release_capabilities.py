from __future__ import annotations

from desktop.sidecar.contracts.v1.models import (
    ExecutionModeCapabilitiesV1,
    ExecutionModeCapabilityV1,
)


RELEASE_EXECUTION_MODE_CAPABILITIES_V1 = ExecutionModeCapabilitiesV1(
    modes=(
        ExecutionModeCapabilityV1(
            mode="codex_subscription_transcript",
            display_name="Subscription",
            support_state="supported",
            message="Available in this OpenEvo Desktop release.",
        ),
        ExecutionModeCapabilityV1(
            mode="self-deployed",
            display_name="Self-deployed",
            support_state="unavailable",
            reason_code="self_deployed_release_unavailable",
            message=(
                "Self-deployed execution is not available in this OpenEvo Desktop release. "
                "Choose Subscription to save or run this project."
            ),
        ),
    )
)


__all__ = ("RELEASE_EXECUTION_MODE_CAPABILITIES_V1",)
