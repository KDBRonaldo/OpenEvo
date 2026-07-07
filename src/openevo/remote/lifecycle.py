from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class RemoteLifecycleStatus(StrEnum):
    PLANNED = "planned"
    STARTING = "starting"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RemoteDaemonLaunchSpec(_StrictFrozenModel):
    service_id: str
    kind: Literal[
        "openevo_backend",
        "vllm",
        "polar_gateway",
        "rollout_server",
        "evolution_worker",
        "custom",
    ]
    command: str
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    ports: dict[str, int] = Field(default_factory=dict)
    pid_file: str | None = None
    log_path: str | None = None
    health_check: str | None = None
    depends_on: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator(
        "service_id",
        "command",
        "cwd",
        "pid_file",
        "log_path",
        "health_check",
    )
    @classmethod
    def _strip_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _strip_non_empty(value, info.field_name)

    @field_validator("env")
    @classmethod
    def _validate_env(cls, value: dict[str, str]) -> dict[str, str]:
        return _strip_string_mapping(value, "env")

    @field_validator("ports")
    @classmethod
    def _validate_ports(cls, value: dict[str, int]) -> dict[str, int]:
        ports: dict[str, int] = {}
        for key, port in value.items():
            port_name = _strip_non_empty(key, "ports key")
            if port < 1 or port > 65535:
                raise ValueError(f"ports.{port_name} must be between 1 and 65535")
            ports[port_name] = port
        return ports

    @field_validator("depends_on", mode="before")
    @classmethod
    def _coerce_depends_on_tuple(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("depends_on")
    @classmethod
    def _validate_depends_on(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_strip_non_empty(item, "depends_on") for item in value)


class RemoteServiceStatus(_StrictFrozenModel):
    service_id: str
    status: RemoteLifecycleStatus
    message: str
    pid: int | None = None
    log_path: str | None = None
    health_check: str | None = None
    last_checked_at: str | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _coerce_status(cls, value) -> RemoteLifecycleStatus:
        if isinstance(value, str):
            return RemoteLifecycleStatus(value)
        return value

    @field_validator(
        "service_id",
        "message",
        "log_path",
        "health_check",
        "last_checked_at",
    )
    @classmethod
    def _strip_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _strip_non_empty(value, info.field_name)

    @field_validator("pid")
    @classmethod
    def _validate_pid(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("pid must be a positive integer")
        return value


class RemoteLifecycleEvent(_StrictFrozenModel):
    level: Literal["info", "warn", "error"]
    message: str
    source: str
    created_at: str | None = None

    @field_validator("message", "source", "created_at")
    @classmethod
    def _strip_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _strip_non_empty(value, info.field_name)


class RemoteStatusReport(_StrictFrozenModel):
    remote_profile_id: str
    project_name: str
    task_id: str
    bootstrap_ready: bool = False
    workspace_ready: bool = False
    services: tuple[RemoteServiceStatus, ...] = Field(default_factory=tuple)
    events: tuple[RemoteLifecycleEvent, ...] = Field(default_factory=tuple)
    actionable_errors: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="before")
    @classmethod
    def _ignore_dumped_ready(cls, value):
        if isinstance(value, dict) and "ready" in value:
            return {key: item for key, item in value.items() if key != "ready"}
        return value

    @field_validator("remote_profile_id", "project_name", "task_id")
    @classmethod
    def _strip_required_text(cls, value: str, info) -> str:
        return _strip_non_empty(value, info.field_name)

    @field_validator("services", "events", "actionable_errors", mode="before")
    @classmethod
    def _coerce_tuples(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("actionable_errors")
    @classmethod
    def _validate_actionable_errors(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            _strip_non_empty(item, "actionable_errors") for item in value
        )

    @computed_field
    @property
    def ready(self) -> bool:
        if not self.bootstrap_ready or not self.workspace_ready:
            return False
        if self.actionable_errors:
            return False
        return all(
            service.status
            in {RemoteLifecycleStatus.PLANNED, RemoteLifecycleStatus.RUNNING}
            for service in self.services
        )


def _strip_non_empty(value: str, field_name: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _strip_string_mapping(value: dict[str, str], field_name: str) -> dict[str, str]:
    stripped: dict[str, str] = {}
    for key, item in value.items():
        stripped_key = _strip_non_empty(key, f"{field_name} key")
        stripped[stripped_key] = _strip_non_empty(item, f"{field_name}.{stripped_key}")
    return stripped
