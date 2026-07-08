"""OpenEvo Science project config models."""

from __future__ import annotations

from openevo.science.compiler import (
    MANAGED_RUNTIME_IMAGES,
    PreparedWorkspace,
    compile_science_project,
)
from openevo.science.models import (
    EnvironmentConfig,
    EvolutionTargetsConfig,
    ExecutionConfig,
    ProjectInfo,
    ScienceProjectConfig,
    ScienceTaskConfig,
    TaskSourceConfig,
    load_science_project_config,
)

__all__ = [
    "EnvironmentConfig",
    "EvolutionTargetsConfig",
    "ExecutionConfig",
    "MANAGED_RUNTIME_IMAGES",
    "PreparedWorkspace",
    "ProjectInfo",
    "ScienceProjectConfig",
    "ScienceTaskConfig",
    "TaskSourceConfig",
    "compile_science_project",
    "load_science_project_config",
]
