"""Release runtime composition for the verified executable evolution registry."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator

from .builtins import VerifiedExecutableRegistry, load_verified_builtin_registry
from .contracts import (
    _Contract,
    _digest,
    _distribution_name,
    _distribution_version,
)
from .loading import DistributionArtifactExpectation, verify_distribution_install


_MAX_FRAMEWORK_LOCK_BYTES = 64 * 1024


class FrameworkDistributionLock(_Contract):
    """External identity lock supplied by Desktop or maintainer automation."""

    schema_version: Literal["1"] = "1"
    distribution: Literal["openevo"] = "openevo"
    distribution_version: str
    distribution_digest: str
    wheel_filename: str

    _distribution = field_validator("distribution")(_distribution_name)
    _version = field_validator("distribution_version")(_distribution_version)
    _digest_value = field_validator("distribution_digest")(_digest)

    @field_validator("wheel_filename")
    @classmethod
    def _safe_wheel_filename(cls, value: str) -> str:
        if (
            not value
            or Path(value).name != value
            or value != value.strip()
            or not value.endswith(".whl")
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("wheel_filename must be one safe wheel basename")
        return value

    @model_validator(mode="after")
    def _matching_wheel_name(self) -> FrameworkDistributionLock:
        normalized_version = self.distribution_version.replace("-", "_")
        if not self.wheel_filename.startswith(
            f"openevo-{normalized_version}-"
        ) and not self.wheel_filename.startswith(
            f"openevo-{self.distribution_version}-"
        ):
            raise ValueError("wheel_filename does not match the locked version")
        return self


def load_framework_distribution_lock(
    lock_path: str | Path,
) -> tuple[FrameworkDistributionLock, Path]:
    """Load one bounded lock and resolve its wheel within the same directory."""

    path = Path(lock_path)
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ValueError("framework distribution lock is unavailable") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ValueError("framework distribution lock must be a regular non-symlink file")
    try:
        if path.stat().st_size > _MAX_FRAMEWORK_LOCK_BYTES:
            raise ValueError("framework distribution lock exceeds the size limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("framework distribution lock is not valid UTF-8 JSON") from exc
    lock = FrameworkDistributionLock.model_validate(payload)
    wheel_path = path.resolve(strict=True).parent / lock.wheel_filename
    return lock, wheel_path


def load_verified_framework_registry(
    lock_path: str | Path,
) -> VerifiedExecutableRegistry:
    """Verify the locked installed wheel and atomically publish its handles."""

    lock, wheel_path = load_framework_distribution_lock(lock_path)
    verified = verify_distribution_install(
        DistributionArtifactExpectation(
            distribution=lock.distribution,
            distribution_version=lock.distribution_version,
            distribution_digest=lock.distribution_digest,
        ),
        wheel_path,
    )
    return load_verified_builtin_registry(verified)


__all__ = [
    "FrameworkDistributionLock",
    "load_framework_distribution_lock",
    "load_verified_framework_registry",
]
