from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LEGACY_SELF_DEPLOYED_EXECUTION_MODE = "codex_managed_local_inference"
SELF_DEPLOYED_EXECUTION_MODE = "self-deployed"
DesktopExecutionMode = Literal[
    "codex_subscription_transcript",
    "self-deployed",
]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        hide_input_in_errors=True,
    )


class SSHAuthConfig(_StrictFrozenModel):
    method: Literal["ssh_agent", "private_key", "password_ref"] = "ssh_agent"
    private_key_path: str | None = Field(default=None, repr=False)
    password_ref: str | None = Field(default=None, repr=False)
    passphrase_ref: str | None = Field(default=None, repr=False)

    @field_validator("private_key_path", "password_ref", "passphrase_ref")
    @classmethod
    def _strip_optional_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _strip_non_empty(value, f"auth.{info.field_name}")

    @model_validator(mode="after")
    def _validate_auth_fields(self) -> SSHAuthConfig:
        if self.method == "ssh_agent":
            if (
                self.private_key_path is not None
                or self.password_ref is not None
                or self.passphrase_ref is not None
            ):
                raise ValueError(
                    "ssh_agent auth must not set private_key_path, password_ref, or passphrase_ref"
                )
        elif self.method == "private_key":
            if self.private_key_path is None:
                raise ValueError("private_key auth requires private_key_path")
            if self.password_ref is not None:
                raise ValueError("private_key auth must not set password_ref")
        elif self.method == "password_ref":
            if self.password_ref is None:
                raise ValueError("password_ref auth requires password_ref")
            if self.private_key_path is not None:
                raise ValueError("password_ref auth must not set private_key_path")
            if self.passphrase_ref is not None:
                raise ValueError("password_ref auth must not set passphrase_ref")
        return self


class ProxySettings(_StrictFrozenModel):
    http_proxy: str | None = Field(default=None, repr=False)
    https_proxy: str | None = Field(default=None, repr=False)
    no_proxy: str | None = None
    docker_registry_mirror: str | None = None
    pip_index_url: str | None = Field(default=None, repr=False)
    huggingface_endpoint: str | None = None
    hf_home: str | None = None
    extra_env: dict[str, str] = Field(default_factory=dict, repr=False)

    @field_validator(
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "docker_registry_mirror",
        "pip_index_url",
        "huggingface_endpoint",
        "hf_home",
    )
    @classmethod
    def _strip_optional_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _strip_non_empty(value, f"proxy.{info.field_name}")

    @field_validator("extra_env")
    @classmethod
    def _validate_extra_env(cls, value: dict[str, str]) -> dict[str, str]:
        env: dict[str, str] = {}
        for key, item in value.items():
            env_key = _strip_non_empty(key, "proxy.extra_env key")
            env[env_key] = _strip_non_empty(item, f"proxy.extra_env.{env_key}")
        return env

    def to_env(self) -> dict[str, str]:
        env = dict(self.extra_env)
        if self.http_proxy is not None:
            env["HTTP_PROXY"] = self.http_proxy
            env["http_proxy"] = self.http_proxy
        if self.https_proxy is not None:
            env["HTTPS_PROXY"] = self.https_proxy
            env["https_proxy"] = self.https_proxy
        if self.no_proxy is not None:
            env["NO_PROXY"] = self.no_proxy
            env["no_proxy"] = self.no_proxy
        if self.pip_index_url is not None:
            env["PIP_INDEX_URL"] = self.pip_index_url
        if self.huggingface_endpoint is not None:
            env["HF_ENDPOINT"] = self.huggingface_endpoint
        if self.hf_home is not None:
            env["HF_HOME"] = self.hf_home
        return env


class RemoteProfileConfig(_StrictFrozenModel):
    version: Literal[1] = 1
    id: str = Field(min_length=1)
    name: str | None = None
    host: str = Field(min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(min_length=1)
    auth: SSHAuthConfig = Field(default_factory=SSHAuthConfig)
    proxy: ProxySettings = Field(default_factory=ProxySettings)
    workspace_root: str | None = None
    min_home_available_kb: int = Field(default=20_000_000, ge=0)
    path: Path | None = None

    @property
    def effective_workspace_root(self) -> str:
        if self.workspace_root is not None:
            return self.workspace_root
        return f"/home/{self.user}/.openevo/workspaces"

    @field_validator("id", "host", "user")
    @classmethod
    def _strip_required_text(cls, value: str, info) -> str:
        return _strip_non_empty(value, info.field_name)

    @field_validator("name", "workspace_root")
    @classmethod
    def _strip_optional_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _strip_non_empty(value, info.field_name)

    @field_validator("workspace_root")
    @classmethod
    def _validate_workspace_root(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("/"):
            raise ValueError("workspace_root must be an absolute remote path")
        return value


def load_remote_profile_config(path: Path) -> RemoteProfileConfig:
    if not path.exists():
        raise FileNotFoundError(f"Remote profile config not found: {path}")
    if not path.is_file():
        raise ValueError(f"Remote profile config path is not a file: {path}")

    try:
        with path.open(encoding="utf-8") as handle:
            loaded: Any = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ValueError(f"Remote profile config {path} must contain a top-level mapping")

    return RemoteProfileConfig.model_validate({**loaded, "path": path})


def _strip_non_empty(value: str, field_name: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def normalize_desktop_execution_mode(value: Any) -> Any:
    if value == LEGACY_SELF_DEPLOYED_EXECUTION_MODE:
        return SELF_DEPLOYED_EXECUTION_MODE
    return value
