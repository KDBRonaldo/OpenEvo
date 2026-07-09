from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any


DEFAULT_AGENT_SYSTEM_TARGET_PATH = "AGENTS.md"
ROOT_AGENT_SYSTEM_FILES = {
    "AGENTS.md",
    "agents.md",
    "CLAUDE.md",
    "GEMINI.md",
}


def normalize_agent_system_target_path(value: Any | None) -> str:
    if value is None:
        raw = DEFAULT_AGENT_SYSTEM_TARGET_PATH
    elif not isinstance(value, str):
        raise ValueError("agent_system target_path must be a string")
    else:
        raw = value.strip().replace("\\", "/")

    if not raw:
        raise ValueError("agent_system target_path must be non-empty")

    path = PurePosixPath(raw)
    if path.is_absolute():
        raise ValueError(f"agent_system target_path must be relative: {value}")

    parts = [part for part in path.parts if part != "."]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"agent_system target_path must not escape the runtime workdir: {value}")

    normalized = PurePosixPath(*parts).as_posix()
    if not is_allowed_agent_system_target_path(normalized):
        raise ValueError("agent_system target_path must be a supported harness instruction path")
    return normalized


def is_allowed_agent_system_target_path(value: str) -> bool:
    path = PurePosixPath(value)
    parts = path.parts
    if len(parts) == 1:
        return path.name in ROOT_AGENT_SYSTEM_FILES
    return (
        len(parts) == 3
        and parts[0] == ".openhands"
        and parts[1] == "microagents"
        and path.suffix == ".md"
        and bool(path.name)
    )
