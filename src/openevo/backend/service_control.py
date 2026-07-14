"""Dependency boundary between Core Control and managed service ownership."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class CoreServiceControlError(RuntimeError):
    """A managed service operation failed closed."""


class CoreServiceControl(Protocol):
    """Provider-facing subset of the internal service supervisor."""

    def list(self) -> tuple[Any, ...]: ...

    def authenticates_run_service(self, headers: Mapping[str, str]) -> bool: ...

    def close(self) -> None: ...


__all__ = ["CoreServiceControl", "CoreServiceControlError"]
