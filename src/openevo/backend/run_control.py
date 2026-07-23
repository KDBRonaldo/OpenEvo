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

TASK_OPERATION_IDS_V2 = frozenset(
    {
        "appendCoreTaskAttemptV2",
        "cancelCoreTaskAttemptV2",
        "closeCoreTaskV2",
        "getCoreTaskAdmissionV2",
        "getCoreTaskAttemptV2",
        "getCoreTaskContextV2",
        "getCoreTaskLogsV2",
        "getCoreTaskTimelineV2",
        "getCoreTaskV2",
        "listCoreTaskArtifactsV2",
        "listCoreTaskAttemptsV2",
        "listCoreTasksV2",
        "submitCoreTaskV2",
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


class CoreTaskControlError(CoreRunControlError):
    """Typed v2 Task ownership failure, kept separate from frozen v1 routes."""


class CoreRunControl(GenerationBoundRunAdmissionVerifier, Protocol):
    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object: ...

    def counts(self) -> tuple[int, int]: ...

    def close(self) -> None: ...


class CoreTaskControlV2(Protocol):
    def invoke(self, operation_id: str, arguments: Mapping[str, object]) -> object: ...

    def ownership_counts(self) -> tuple[int, int, int]: ...

    def close(self) -> None: ...


__all__ = [
    "CoreRunControl",
    "CoreRunControlError",
    "CoreTaskControlError",
    "CoreTaskControlV2",
    "RUN_OPERATION_IDS",
    "TASK_OPERATION_IDS_V2",
]
