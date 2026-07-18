"""Data models for runtime configuration and execution."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo.runtime.managed import (
    ManagedRuntimeProfile,
    require_managed_runtime_binding,
)


def validate_runtime_session_target(value: str, *, label: str) -> str:
    """Require one canonical absolute target below the session bind root."""

    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or "\x00" in value
    ):
        raise ValueError(f"{label} must be an absolute path under /openevo/session")
    parts = value.split("/")
    if (
        parts[:3] != ["", "openevo", "session"]
        or len(parts) < 4
        or any(part in {"", ".", ".."} for part in parts[1:])
    ):
        raise ValueError(f"{label} must be canonical and remain under /openevo/session")
    return value


class ExecInput(BaseModel):
    """Command specification for runtime execution."""

    model_config = ConfigDict(extra="forbid")

    command: str
    cwd: str | None = None
    env: dict[str, str] | None = None


class PrepareAction(BaseModel):
    """One ordered step in the runtime preparation recipe.

    Interleaves uploads and shell commands in exact order needed by the task.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["upload_file", "upload_dir", "exec"]
    source: str | None = None
    target: str | None = None
    command: str | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None

    @model_validator(mode="after")
    def _validate_fields(self) -> PrepareAction:
        if self.type in ("upload_file", "upload_dir"):
            if not self.source or not self.target:
                raise ValueError(f"{self.type} requires source and target")
            validate_runtime_session_target(self.target, label="prepare target")
            for field_name in ("command", "cwd", "env"):
                if getattr(self, field_name) is not None:
                    raise ValueError(f"{self.type} must not set {field_name}")
        elif self.type == "exec":
            if not self.command:
                raise ValueError("exec requires command")
            for field_name in ("source", "target"):
                if getattr(self, field_name) is not None:
                    raise ValueError(f"exec must not set {field_name}")
        return self


class ExecResult(BaseModel):
    """Result of a command executed inside a runtime."""

    stdout: str | None = None
    stderr: str | None = None
    return_code: int


class RuntimeSpec(BaseModel):
    """Container runtime configuration for one rollout session."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["docker", "apptainer", "bubblewrap"] = "docker"
    profile: ManagedRuntimeProfile | None = None
    container_user: Literal["image", "host"] = "image"
    image: str
    prepare: list[PrepareAction] = Field(default_factory=list)
    eval_prepare: list[PrepareAction] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    network: str | None = "host"
    workdir: str | None = None
    cpus: int | None = None
    memory_mb: int | None = None
    storage_mb: int | None = None
    gpus: int = 0
    allow_internet: bool = True
    import_path: str | None = None
    kwargs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("image")
    @classmethod
    def _validate_image(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("runtime image must be non-empty")
        return normalized

    @field_validator("workdir")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("workdir must be non-empty when provided")
        return normalized

    @model_validator(mode="after")
    def _validate_managed_runtime_binding(self) -> RuntimeSpec:
        managed = require_managed_runtime_binding(
            profile=self.profile,
            image=self.image,
            backend=self.backend,
            container_user=self.container_user,
        )
        if managed and (self.import_path is not None or self.kwargs):
            raise ValueError(
                "Core-managed runtime profiles forbid custom runtime loaders and options"
            )
        return self
