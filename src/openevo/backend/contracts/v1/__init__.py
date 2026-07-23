"""Core Control API v1 closed contract models and schema source."""

from typing import TYPE_CHECKING, Any

from .app import (
    CoreControlApiProviderV1,
    CoreControlHTTPError,
    core_control_contract_app,
    create_core_control_contract_app,
)
from .models import (
    ApiErrorV1,
    ArtifactContentV1,
    ArtifactSummaryV1,
    AttemptV1,
    CapabilitiesResponseV1,
    EventEnvelopeV1,
    ExecutionMode,
    RunCreateV1,
    RunStatus,
    RunSummaryV1,
    RunV1,
)
from .snapshots import (
    build_events_schema_document,
    build_openapi_document,
    canonical_json_bytes,
    deterministic_sha256,
    events_schema_sha256,
    openapi_sha256,
)
if TYPE_CHECKING:
    from .provider import CoreControlProviderV1


def __getattr__(name: str) -> Any:
    """Load the business provider lazily to keep model/store imports acyclic."""

    if name in {"CoreControlProviderV1", "create_core_control_app"}:
        from .provider import CoreControlProviderV1, create_core_control_app

        return {
            "CoreControlProviderV1": CoreControlProviderV1,
            "create_core_control_app": create_core_control_app,
        }[name]
    raise AttributeError(name)

__all__ = [
    "ApiErrorV1",
    "ArtifactContentV1",
    "ArtifactSummaryV1",
    "AttemptV1",
    "CapabilitiesResponseV1",
    "CoreControlApiProviderV1",
    "CoreControlHTTPError",
    "CoreControlProviderV1",
    "EventEnvelopeV1",
    "ExecutionMode",
    "RunCreateV1",
    "RunStatus",
    "RunSummaryV1",
    "RunV1",
    "build_events_schema_document",
    "build_openapi_document",
    "canonical_json_bytes",
    "core_control_contract_app",
    "create_core_control_app",
    "create_core_control_contract_app",
    "deterministic_sha256",
    "events_schema_sha256",
    "openapi_sha256",
]
