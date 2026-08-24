"""Closed wire models for the first OpenEvo daemon control surface."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DaemonHealthV1(_ClosedModel):
    schema_version: Literal["1"] = "1"
    status: Literal["ready"] = "ready"
    service: Literal["openevo-daemon"] = "openevo-daemon"
    api_version: Literal["daemon-v1alpha1"] = "daemon-v1alpha1"


class DaemonStatusV1(_ClosedModel):
    schema_version: Literal["1"] = "1"
    status: Literal["ready"] = "ready"
    service: Literal["openevo-daemon"] = "openevo-daemon"
    api_version: Literal["daemon-v1alpha1"] = "daemon-v1alpha1"
    pid: int = Field(ge=1)
    started_at: str = Field(min_length=20, max_length=64)
    capabilities: tuple[str, ...] = (
        "health",
        "authenticated_status",
    )


class DaemonApiErrorV1(_ClosedModel):
    schema_version: Literal["1"] = "1"
    code: Literal["authentication_required"] = "authentication_required"
    message: Literal["A valid daemon bearer token is required."] = (
        "A valid daemon bearer token is required."
    )
