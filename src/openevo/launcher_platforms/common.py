"""Closed types shared by native launcher platform adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeAlias


SshCommand: TypeAlias = str | tuple[str, ...]


class LauncherError(RuntimeError):
    """A user-actionable EvoLab launcher failure."""


@dataclass(frozen=True)
class SshConnection:
    options: tuple[str, ...]
    destination: str
    display_name: str


@dataclass(frozen=True)
class SshHostProfile:
    """A concrete Host alias discovered in the user's OpenSSH configuration."""

    alias: str
    hostname: str | None
    user: str | None
    port: int | None
    source: Path


@dataclass(frozen=True)
class SshClientEnvironment:
    """One OpenSSH installation and the config catalog it owns."""

    command: SshCommand
    profiles: tuple[SshHostProfile, ...]
    config_path: Path
    label: str


@dataclass(frozen=True)
class SshResolutionServices:
    """Platform-neutral operations used by OS-specific SSH selection policy."""

    system_environment: Callable[[Any], SshClientEnvironment]
    wsl_environment: Callable[[Any], SshClientEnvironment]
    wsl_command: Callable[[Any], tuple[tuple[str, ...], str]]
    resolve_connection: Callable[[Any], SshConnection]
    connection_with_config: Callable[[Any, SshClientEnvironment], SshConnection]
    select_from_environment: Callable[[Any, SshClientEnvironment], SshConnection]
    validate_alias: Callable[[str], str]
    select_alias: Callable[..., str]
    load_last_alias: Callable[[Path | None], str | None]
    save_last_alias: Callable[[str, Path | None], None]
