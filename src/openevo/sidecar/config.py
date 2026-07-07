from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import yaml

from openevo.science import ScienceProjectConfig
from openevo.sidecar.models import RemoteProfileConfig


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class DesktopProjectConfigDraft(_StrictFrozenModel):
    project_name: str
    task_id: str
    objective: str
    source_type: Literal["local_folder", "git_repository", "remote_path", "scratch"] = (
        "remote_path"
    )
    source_path: str | None = None
    source_url: str | None = None
    source_branch: str | None = None
    remote_profile_id: str = "default"
    remote_host: str
    remote_port: int = Field(default=22, ge=1, le=65535)
    remote_user: str
    auth_method: Literal["ssh_agent", "private_key", "password_ref"] = "ssh_agent"
    private_key_path: str | None = None
    password_ref: str | None = None
    passphrase_ref: str | None = None
    workspace_root: str | None = None
    http_proxy: str | None = None
    https_proxy: str | None = None
    no_proxy: str | None = None
    pip_index_url: str | None = None
    huggingface_endpoint: str | None = None
    hf_home: str | None = None
    codex_model: str = "gpt-5.1-codex-mini"
    text_memory: bool = True
    skill_bundle: bool = True
    agent_system: bool = True

    @field_validator(
        "project_name",
        "task_id",
        "objective",
        "source_path",
        "source_url",
        "source_branch",
        "remote_profile_id",
        "remote_host",
        "remote_user",
        "private_key_path",
        "password_ref",
        "passphrase_ref",
        "workspace_root",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "pip_index_url",
        "huggingface_endpoint",
        "hf_home",
        "codex_model",
    )
    @classmethod
    def _strip_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return text

    @model_validator(mode="after")
    def _validate_source_fields(self) -> DesktopProjectConfigDraft:
        if self.source_type in {"local_folder", "remote_path"}:
            if self.source_path is None:
                raise ValueError(f"source_path is required for {self.source_type}")
            if self.source_url is not None:
                raise ValueError(f"source_url is not valid for {self.source_type}")
        elif self.source_type == "git_repository":
            if self.source_url is None:
                raise ValueError("source_url is required for git_repository")
            if self.source_path is not None:
                raise ValueError("source_path is not valid for git_repository")
        elif self.source_type == "scratch":
            if (
                self.source_path is not None
                or self.source_url is not None
                or self.source_branch is not None
            ):
                raise ValueError(
                    "scratch source must not set source_path, source_url, or "
                    "source_branch"
                )

        if self.source_branch is not None and self.source_type != "git_repository":
            raise ValueError("source_branch is only valid for git_repository")
        return self


class DesktopProjectConfigPaths(_StrictFrozenModel):
    science_config_path: Path
    remote_profile_path: Path


def build_desktop_project_configs(
    draft: DesktopProjectConfigDraft,
) -> tuple[ScienceProjectConfig, RemoteProfileConfig]:
    project = ScienceProjectConfig.model_validate(_science_project_payload(draft))
    profile = RemoteProfileConfig.model_validate(_remote_profile_payload(draft))
    return project, profile


def save_desktop_project_config(
    draft: DesktopProjectConfigDraft,
    config_root: Path,
) -> tuple[ScienceProjectConfig, RemoteProfileConfig, DesktopProjectConfigPaths]:
    project, profile = build_desktop_project_configs(draft)
    root = config_root.expanduser()
    paths = DesktopProjectConfigPaths(
        science_config_path=(
            root / "projects" / _slugify(project.project.name) / "science.yaml"
        ),
        remote_profile_path=root / "profiles" / f"{_slugify(profile.id)}.yaml",
    )
    paths.science_config_path.parent.mkdir(parents=True, exist_ok=True)
    paths.remote_profile_path.parent.mkdir(parents=True, exist_ok=True)
    _write_yaml(paths.science_config_path, project)
    _write_yaml(paths.remote_profile_path, profile)
    return (
        project.model_copy(update={"path": paths.science_config_path}),
        profile.model_copy(update={"path": paths.remote_profile_path}),
        paths,
    )


def _science_project_payload(draft: DesktopProjectConfigDraft) -> dict[str, Any]:
    return {
        "version": 1,
        "project": {"name": draft.project_name},
        "remote_profile": draft.remote_profile_id,
        "task": {
            "id": draft.task_id,
            "objective": draft.objective,
            "source": _task_source_payload(draft),
        },
        "execution": {
            "mode": "codex_subscription_transcript",
            "codex_model": draft.codex_model,
        },
        "evolution": {
            "text_memory": draft.text_memory,
            "skill_bundle": draft.skill_bundle,
            "agent_system": draft.agent_system,
            "parametric_memory": False,
        },
    }


def _task_source_payload(draft: DesktopProjectConfigDraft) -> dict[str, Any]:
    if draft.source_type in {"local_folder", "remote_path"}:
        return {"type": draft.source_type, "path": draft.source_path}
    if draft.source_type == "git_repository":
        payload = {"type": "git_repository", "url": draft.source_url}
        if draft.source_branch is not None:
            payload["branch"] = draft.source_branch
        return payload
    return {"type": "scratch"}


def _remote_profile_payload(draft: DesktopProjectConfigDraft) -> dict[str, Any]:
    return {
        "version": 1,
        "id": draft.remote_profile_id,
        "host": draft.remote_host,
        "port": draft.remote_port,
        "user": draft.remote_user,
        "auth": _auth_payload(draft),
        "proxy": _proxy_payload(draft),
        "workspace_root": draft.workspace_root,
    }


def _auth_payload(draft: DesktopProjectConfigDraft) -> dict[str, Any]:
    return _drop_none(
        {
            "method": draft.auth_method,
            "private_key_path": draft.private_key_path,
            "password_ref": draft.password_ref,
            "passphrase_ref": draft.passphrase_ref,
        }
    )


def _proxy_payload(draft: DesktopProjectConfigDraft) -> dict[str, Any]:
    return _drop_none(
        {
            "http_proxy": draft.http_proxy,
            "https_proxy": draft.https_proxy,
            "no_proxy": draft.no_proxy,
            "pip_index_url": draft.pip_index_url,
            "huggingface_endpoint": draft.huggingface_endpoint,
            "hf_home": draft.hf_home,
        }
    )


def _drop_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _write_yaml(path: Path, model: BaseModel) -> None:
    payload = model.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"path"},
    )
    path.write_text(
        yaml.safe_dump(payload, sort_keys=True),
        encoding="utf-8",
    )


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "item"
