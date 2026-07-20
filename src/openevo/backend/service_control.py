"""Dependency boundary between Core Control and managed service ownership."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
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


class ServiceRestartAttemptState(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ServiceRestartAttempt:
    """Durable operation-owned receipt for one automatic service restart."""

    operation_id: str
    service_id: str
    expected_service_etag: str
    state: ServiceRestartAttemptState
    service: SupervisorServiceSummary | None


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

    def restart_once(
        self,
        service_id: str,
        *,
        operation_id: str,
        expected_service_etag: str,
        total_timeout: float | None = None,
    ) -> SupervisorServiceSummary: ...

    def list_restart_attempts(self) -> tuple[ServiceRestartAttempt, ...]: ...

    def acknowledge_restart_attempt(
        self,
        operation_id: str,
        *,
        service_id: str,
        expected_service_etag: str,
    ) -> None: ...

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


__all__ = [
    "CoreServiceControl",
    "CoreServiceControlError",
    "ServiceRestartAttempt",
    "ServiceRestartAttemptState",
]
