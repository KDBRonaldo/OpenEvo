"""One four-axis method support evaluator shared by planning and capabilities."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from .contracts import (
    EvolutionExecutionProfile,
    _Contract,
    _optional_stable_id,
    _text,
    _unique_ids,
)
from .descriptors import EvolutionMethodDescriptor


class SupportState(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"


class MethodSupportOverall(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"


class AxisSupport(_Contract):
    state: SupportState
    reason_code: str | None = None
    message: str = Field(max_length=4096)
    missing_requirements: tuple[str, ...] = Field(default=(), max_length=256)

    _reason = field_validator("reason_code")(_optional_stable_id)
    _message = field_validator("message")(_text)
    _missing = field_validator("missing_requirements")(_unique_ids)

    @model_validator(mode="after")
    def _state_payload(self) -> AxisSupport:
        if self.state is SupportState.SUPPORTED:
            if self.reason_code is not None or self.missing_requirements:
                raise ValueError("supported axis cannot include a failure reason")
        elif self.reason_code is None:
            raise ValueError("unsupported/unavailable axis requires a reason code")
        return self


class MethodSupport(_Contract):
    overall: MethodSupportOverall
    execution: AxisSupport
    capture: AxisSupport
    harness: AxisSupport
    runtime: AxisSupport

    @model_validator(mode="after")
    def _overall_matches_axes(self) -> MethodSupport:
        axes = (self.execution, self.capture, self.harness, self.runtime)
        expected = (
            MethodSupportOverall.UNSUPPORTED
            if any(axis.state is SupportState.UNSUPPORTED for axis in axes)
            else MethodSupportOverall.UNAVAILABLE
            if any(axis.state is SupportState.UNAVAILABLE for axis in axes)
            else MethodSupportOverall.SUPPORTED
        )
        if self.overall is not expected:
            raise ValueError("method support overall state does not match axes")
        return self

    def failure_messages(self) -> tuple[str, ...]:
        return tuple(
            axis.message
            for axis in (
                self.execution,
                self.capture,
                self.harness,
                self.runtime,
            )
            if axis.state is not SupportState.SUPPORTED
        )


def _supported() -> AxisSupport:
    return AxisSupport(state=SupportState.SUPPORTED, message="Supported.")


def evaluate_method_support(
    method: EvolutionMethodDescriptor,
    profile: EvolutionExecutionProfile,
) -> MethodSupport:
    if profile.execution_mode not in method.execution_modes:
        execution = AxisSupport(
            state=SupportState.UNSUPPORTED,
            reason_code="unsupported_execution_mode",
            message=(
                f"method {method.id!r} does not support execution mode "
                f"{profile.execution_mode.value!r}"
            ),
        )
    else:
        execution = _supported()

    if profile.capture_mode not in method.capture_modes:
        capture = AxisSupport(
            state=SupportState.UNSUPPORTED,
            reason_code="unsupported_capture_mode",
            message=(
                f"method {method.id!r} does not support capture mode "
                f"{profile.capture_mode.value!r}"
            ),
        )
    else:
        capture = _supported()

    if profile.harness_id not in method.supported_harness_ids:
        harness = AxisSupport(
            state=SupportState.UNSUPPORTED,
            reason_code="unsupported_harness",
            message=(
                f"method {method.id!r} does not support harness "
                f"{profile.harness_id!r}"
            ),
        )
    else:
        missing_harness = tuple(
            sorted(
                set(method.harness_requirements).difference(
                    profile.harness_capabilities
                )
            )
        )
        harness = (
            AxisSupport(
                state=SupportState.UNAVAILABLE,
                reason_code="missing_harness_capabilities",
                message=(
                    f"method {method.id!r} requires unavailable harness capabilities: "
                    + ", ".join(missing_harness)
                ),
                missing_requirements=missing_harness,
            )
            if missing_harness
            else _supported()
        )

    missing_runtime = tuple(
        sorted(
            set(method.runtime_requirements).difference(
                profile.runtime_capabilities
            )
        )
    )
    runtime = (
        AxisSupport(
            state=SupportState.UNAVAILABLE,
            reason_code="missing_runtime_capabilities",
            message=(
                f"method {method.id!r} requires unavailable runtime capabilities: "
                + ", ".join(missing_runtime)
            ),
            missing_requirements=missing_runtime,
        )
        if missing_runtime
        else _supported()
    )

    axes = (execution, capture, harness, runtime)
    overall = (
        MethodSupportOverall.UNSUPPORTED
        if any(axis.state is SupportState.UNSUPPORTED for axis in axes)
        else MethodSupportOverall.UNAVAILABLE
        if any(axis.state is SupportState.UNAVAILABLE for axis in axes)
        else MethodSupportOverall.SUPPORTED
    )
    return MethodSupport(
        overall=overall,
        execution=execution,
        capture=capture,
        harness=harness,
        runtime=runtime,
    )


__all__ = [
    "AxisSupport",
    "MethodSupport",
    "MethodSupportOverall",
    "SupportState",
    "evaluate_method_support",
]
