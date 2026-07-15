"""Dependency boundary between Core Control routes and science run ownership."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from openevo.internal_auth import GenerationBoundRunAdmissionVerifier


RUN_OPERATION_IDS = frozenset(
    {
        "cancelCoreRunV1",
        "createCoreRunV1",
        "deleteCoreRunV1",
        "listCoreRunArtifactsV1",
        "getCoreRunContextV1",
        "getCoreRunLogsV1",
        "getCoreRunTimelineV1",
        "getCoreRunV1",
        "listCoreRunsV1",
        "retryCoreRunV1",
    }
)


class CoreRunControlError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable


class CoreRunControl(GenerationBoundRunAdmissionVerifier, Protocol):
    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object: ...

    def counts(self) -> tuple[int, int]: ...

    def close(self) -> None: ...


__all__ = ["CoreRunControl", "CoreRunControlError", "RUN_OPERATION_IDS"]
