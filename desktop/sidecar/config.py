from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import SplitResult, urlsplit, urlunsplit
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
import yaml

from openevo.projects.science import ScienceProjectConfig, load_science_project_config
from openevo.projects.science.models import EvolutionTargetsConfig
from openevo.deployment.profile import (
    DesktopExecutionMode,
    RemoteProfileConfig,
    load_remote_profile_config,
    normalize_desktop_execution_mode,
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        hide_input_in_errors=True,
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
    private_key_path: str | None = Field(default=None, repr=False)
    password_ref: str | None = Field(default=None, repr=False)
    passphrase_ref: str | None = Field(default=None, repr=False)
    workspace_root: str | None = None
    http_proxy: str | None = Field(default=None, repr=False)
    https_proxy: str | None = Field(default=None, repr=False)
    no_proxy: str | None = None
    pip_index_url: str | None = Field(default=None, repr=False)
    huggingface_endpoint: str | None = None
    hf_home: str | None = None
    execution_mode: DesktopExecutionMode = "codex_subscription_transcript"
    codex_model: str | None = None
    hf_model: str | None = None
    evolution: EvolutionTargetsConfig = Field(default_factory=EvolutionTargetsConfig)

    @model_validator(mode="before")
    @classmethod
    def _default_subscription_codex_model(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        mode = normalize_desktop_execution_mode(
            data.get("execution_mode", "codex_subscription_transcript")
        )
        data = data | {"execution_mode": mode}
        if mode == "codex_subscription_transcript" and "codex_model" not in data:
            return data | {"codex_model": "gpt-5.5"}
        return data

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
        "hf_model",
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
                    "scratch source must not set source_path, source_url, or source_branch"
                )

        if self.source_branch is not None and self.source_type != "git_repository":
            raise ValueError("source_branch is only valid for git_repository")
        if self.execution_mode == "codex_subscription_transcript":
            if self.codex_model is None:
                raise ValueError("codex_model is required for subscription mode")
            if self.hf_model is not None:
                raise ValueError("hf_model is only valid for self-deployed mode")
        elif self.execution_mode == "self-deployed":
            if self.codex_model is not None:
                raise ValueError("codex_model is only valid for subscription mode")
            if self.hf_model is None:
                raise ValueError("hf_model is required for self-deployed mode")
        return self


class DesktopProjectConfigPaths(_StrictFrozenModel):
    science_config_path: Path
    remote_profile_path: Path


class DesktopProjectConfigSummary(_StrictFrozenModel):
    project_slug: str
    valid: bool
    error: str | None = None
    project_name: str | None = None
    task_id: str | None = None
    objective: str | None = None
    source_type: str | None = None
    source_label: str | None = None
    remote_profile_id: str | None = None
    remote_host: str | None = None
    remote_user: str | None = None
    science_config_path: Path
    remote_profile_path: Path | None = None

    @model_validator(mode="after")
    def _validate_validity_error(self) -> DesktopProjectConfigSummary:
        if self.valid and self.error is not None:
            raise ValueError("valid config summaries must not include error")
        if not self.valid and self.error is None:
            raise ValueError("invalid config summaries must include error")
        return self


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
        science_config_path=(root / "projects" / _slugify(project.project.name) / "science.yaml"),
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


def list_desktop_project_configs(config_root: Path) -> tuple[DesktopProjectConfigSummary, ...]:
    root = config_root.expanduser()
    projects_root = root / "projects"
    if not projects_root.exists():
        return ()
    configs = [
        _summarize_desktop_project_config(root, science_path)
        for science_path in sorted(projects_root.glob("*/science.yaml"))
    ]
    return tuple(configs)


def load_desktop_project_config(
    config_root: Path,
    project_slug: str,
) -> tuple[ScienceProjectConfig, RemoteProfileConfig, DesktopProjectConfigPaths]:
    slug = _validate_project_slug(project_slug)
    root = config_root.expanduser()
    science_path = root / "projects" / slug / "science.yaml"
    if not science_path.exists():
        raise FileNotFoundError("Saved Desktop project config not found.")
    project = load_science_project_config(science_path)
    profile_path = root / "profiles" / f"{_slugify(project.remote_profile)}.yaml"
    profile = load_remote_profile_config(profile_path)
    if profile.id != project.remote_profile:
        raise ValueError("Saved Desktop remote profile id does not match project remote_profile.")
    paths = DesktopProjectConfigPaths(
        science_config_path=science_path,
        remote_profile_path=profile_path,
    )
    return (
        project.model_copy(update={"path": science_path}),
        profile.model_copy(update={"path": profile_path}),
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
        "execution": _execution_payload(draft),
        "evolution": draft.evolution.model_dump(mode="json"),
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


def _execution_payload(draft: DesktopProjectConfigDraft) -> dict[str, Any]:
    if draft.execution_mode == "self-deployed":
        return {
            "mode": "self-deployed",
            "hf_model": draft.hf_model,
        }
    return {
        "mode": "codex_subscription_transcript",
        "codex_model": draft.codex_model,
    }


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


def _summarize_desktop_project_config(
    config_root: Path,
    science_path: Path,
) -> DesktopProjectConfigSummary:
    project_slug = science_path.parent.name
    try:
        _validate_project_slug(project_slug)
    except ValueError as exc:
        return DesktopProjectConfigSummary(
            project_slug=project_slug,
            valid=False,
            error=str(exc),
            science_config_path=science_path,
        )

    try:
        project = load_science_project_config(science_path)
    except Exception as exc:
        return DesktopProjectConfigSummary(
            project_slug=project_slug,
            valid=False,
            error=_public_error(exc, config_root),
            science_config_path=science_path,
        )

    profile_path = config_root / "profiles" / f"{_slugify(project.remote_profile)}.yaml"
    common = _summary_project_fields(
        project_slug=project_slug,
        project=project,
        science_path=science_path,
        profile_path=profile_path,
    )
    try:
        profile = load_remote_profile_config(profile_path)
        if profile.id != project.remote_profile:
            raise ValueError(
                "Saved Desktop remote profile id does not match project remote_profile."
            )
    except Exception as exc:
        return DesktopProjectConfigSummary(
            **common,
            valid=False,
            error=_public_error(exc, config_root),
        )

    return DesktopProjectConfigSummary(
        **common,
        valid=True,
        remote_host=profile.host,
        remote_user=profile.user,
    )


def _summary_project_fields(
    *,
    project_slug: str,
    project: ScienceProjectConfig,
    science_path: Path,
    profile_path: Path,
) -> dict[str, Any]:
    return {
        "project_slug": project_slug,
        "project_name": project.project.name,
        "task_id": project.task.id,
        "objective": project.task.objective,
        "source_type": project.task.source.type,
        "source_label": _source_label(project),
        "remote_profile_id": project.remote_profile,
        "science_config_path": science_path,
        "remote_profile_path": profile_path,
    }


def _source_label(project: ScienceProjectConfig) -> str:
    source = project.task.source
    if source.type in {"local_folder", "remote_path"}:
        return source.path or source.type
    if source.type == "git_repository":
        label = _redact_url_userinfo(source.url or source.type)
        if source.branch is not None:
            return f"{label}@{source.branch}"
        return label
    return "scratch"


def _redact_url_userinfo(value: str) -> str:
    if "://" not in value:
        match = re.fullmatch(r"[^/@\s]+@([^:\s]+:.+)", value)
        if match is not None:
            return match.group(1)
        return value
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc or "@" not in parsed.netloc:
        return value
    host = parsed.netloc.rsplit("@", 1)[1]
    return urlunsplit(
        SplitResult(
            parsed.scheme,
            host,
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def _validate_project_slug(project_slug: str) -> str:
    slug = project_slug.strip()
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) is None:
        raise ValueError("Invalid Desktop project slug.")
    return slug


def _public_error(exc: Exception, config_root: Path) -> str:
    if isinstance(exc, ValidationError):
        return _validation_error_summary(exc)
    text = str(exc)
    root = config_root.expanduser()
    for prefix in (f"{root.as_posix()}/", str(root) + "/", root.as_posix(), str(root)):
        text = text.replace(prefix, "")
    return text


def _validation_error_summary(exc: ValidationError) -> str:
    messages: list[str] = []
    for error in exc.errors(include_input=False):
        loc = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", "Invalid value"))
        if loc:
            messages.append(f"{loc}: {message}")
        else:
            messages.append(message)
    return "; ".join(messages) or "Invalid config"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "item"
