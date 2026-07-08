from __future__ import annotations

import hashlib
import posixpath
import re
import shlex
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openevo.science import PreparedWorkspace, ScienceProjectConfig
from openevo.sidecar.models import RemoteProfileConfig


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class WorkspacePreparationAction(_StrictFrozenModel):
    type: Literal["upload_dir", "git_clone", "use_remote_path"]
    task_id: str
    source: str | None = None
    target: str
    branch: str | None = None
    command: str | None = None
    source_fingerprint: str | None = None

    @field_validator(
        "task_id",
        "source",
        "target",
        "branch",
        "command",
        "source_fingerprint",
    )
    @classmethod
    def _strip_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return text


class WorkspacePreparationPlan(_StrictFrozenModel):
    project_name: str
    remote_profile_id: str
    workspace_root: str
    actions: tuple[WorkspacePreparationAction, ...] = Field(default_factory=tuple)

    @field_validator("actions", mode="before")
    @classmethod
    def _coerce_actions_tuple(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("project_name", "remote_profile_id", "workspace_root")
    @classmethod
    def _strip_required_text(cls, value: str, info) -> str:
        text = value.strip()
        if not text:
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return text

    def to_prepared_workspaces(self) -> dict[str, PreparedWorkspace]:
        return {
            action.task_id: PreparedWorkspace(
                path=action.target,
                source_fingerprint=action.source_fingerprint,
            )
            for action in self.actions
        }


def plan_workspace_preparation(
    project: ScienceProjectConfig,
    profile: RemoteProfileConfig,
) -> WorkspacePreparationPlan:
    source = project.task.source
    workspace_root = profile.effective_workspace_root
    base = {
        "project_name": project.project.name,
        "remote_profile_id": profile.id,
        "workspace_root": workspace_root,
    }

    if source.type == "scratch":
        return WorkspacePreparationPlan.model_validate(base | {"actions": []})

    if source.type == "remote_path":
        remote_path = str(source.path)
        if not remote_path.startswith("/"):
            raise ValueError("task.source.remote_path must be an absolute remote path")
        return WorkspacePreparationPlan.model_validate(
            base
            | {
                "actions": [
                    {
                        "type": "use_remote_path",
                        "task_id": project.task.id,
                        "source": remote_path,
                        "target": remote_path,
                    }
                ]
            }
        )

    if source.type == "local_folder":
        resolved_source = _resolve_local_path(project, str(source.path))
        fingerprint = _source_fingerprint(
            source_type=source.type,
            source=resolved_source,
            branch=None,
        )
        target = _target_workspace(project, profile, fingerprint)
        return WorkspacePreparationPlan.model_validate(
            base
            | {
                "actions": [
                    {
                        "type": "upload_dir",
                        "task_id": project.task.id,
                        "source": resolved_source,
                        "target": target,
                        "source_fingerprint": fingerprint,
                    }
                ]
            }
        )

    if source.type == "git_repository":
        git_url = str(source.url)
        fingerprint = _source_fingerprint(
            source_type=source.type,
            source=git_url,
            branch=source.branch,
        )
        target = _target_workspace(project, profile, fingerprint)
        command = _git_clone_command(git_url, target, source.branch)
        return WorkspacePreparationPlan.model_validate(
            base
            | {
                "actions": [
                    {
                        "type": "git_clone",
                        "task_id": project.task.id,
                        "source": git_url,
                        "target": target,
                        "branch": source.branch,
                        "command": command,
                        "source_fingerprint": fingerprint,
                    }
                ]
            }
        )

    raise ValueError(f"Unsupported task source type: {source.type}")


def _resolve_local_path(project: ScienceProjectConfig, source_path: str) -> str:
    path = Path(source_path).expanduser()
    if not path.is_absolute():
        base = project.path.parent if project.path is not None else Path.cwd()
        path = base / path
    return str(path.resolve(strict=False))


def _target_workspace(
    project: ScienceProjectConfig,
    profile: RemoteProfileConfig,
    source_fingerprint: str,
) -> str:
    root = profile.effective_workspace_root.rstrip("/") or "/"
    fingerprint_segment = source_fingerprint.removeprefix("sha256:")[:16]
    return posixpath.join(
        root,
        _slugify(project.project.name),
        _slugify(project.task.id),
        fingerprint_segment,
    )


def _source_fingerprint(
    *,
    source_type: str,
    source: str,
    branch: str | None,
) -> str:
    digest = hashlib.sha256()
    digest.update(source_type.encode("utf-8"))
    digest.update(b"\0")
    digest.update(source.encode("utf-8"))
    digest.update(b"\0")
    digest.update((branch or "").encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def _git_clone_command(url: str, target: str, branch: str | None) -> str:
    parts = ["git", "clone", "--depth", "1"]
    if branch is not None:
        parts.extend(["--branch", branch])
    parts.extend(["--", url, target])
    return " ".join(shlex.quote(part) for part in parts)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "item"
