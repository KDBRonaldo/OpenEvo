"""Dependency boundary between Core Control and managed service ownership."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from openevo.backend.service_supervisor import (
        ServiceExecutionMode,
        ServiceGroupSnapshot,
        ServiceRunBinding,
        SupervisorLogEntry,
        SupervisorServiceSummary,
    )


class CoreServiceControlError(RuntimeError):
    """A managed service operation failed closed."""


class CoreServiceControl(Protocol):
    """Provider-facing subset of the internal service supervisor."""

    def ensure(
        self,
        execution_mode: ServiceExecutionMode,
        *,
        model_ref: str | None = None,
        codex_model: str | None = None,
        runtime_image: str | None = None,
        total_timeout: float | None = None,
    ) -> ServiceGroupSnapshot: ...

    def list(self) -> tuple[SupervisorServiceSummary, ...]: ...

    def get(self, service_id: str) -> SupervisorServiceSummary: ...

    def restart(
        self,
        service_id: str,
        *,
        operation_id: str,
        total_timeout: float | None = None,
    ) -> SupervisorServiceSummary: ...

    def logs(
        self,
        service_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[SupervisorLogEntry, ...]: ...

    def run_binding(self) -> ServiceRunBinding: ...

    def cancel(self, *, total_timeout: float | None = None) -> None: ...

    def authenticates_run_service(self, headers: Mapping[str, str]) -> bool: ...

    def close(self) -> None: ...


__all__ = ["CoreServiceControl", "CoreServiceControlError"]
