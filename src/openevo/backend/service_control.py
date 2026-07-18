"""Dependency boundary between Core Control and managed service ownership."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from openevo.backend.service_supervisor import (
        ServiceExecutionMode,
        ServiceGroupSnapshot,
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

    def list(self) -> tuple[Any, ...]: ...

    def authenticates_run_service(self, headers: Mapping[str, str]) -> bool: ...

    def close(self) -> None: ...


__all__ = ["CoreServiceControl", "CoreServiceControlError"]
