"""Test-only construction of a real verified built-in registry."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable
from zipfile import ZIP_DEFLATED, ZipFile

from openevo import __version__
from openevo.evolution.framework import (
    DistributionArtifactExpectation,
    VerifiedDistribution,
)
from openevo.evolution.framework import builtins
from openevo.evolution.framework.builtins import load_verified_builtin_registry
from openevo.evolution.framework.loading import _verify_distribution_install


class _SourceDistribution:
    metadata = {"Name": "openevo"}
    version = __version__

    def __init__(self, install_root: Path) -> None:
        self._install_root = install_root

    def locate_file(self, path: str) -> Path:
        return self._install_root / path

    def read_text(self, _filename: str) -> None:
        return None


def verify_distribution_install_for_test(
    expectation: DistributionArtifactExpectation,
    artifact_path: Path,
    metadata_provider: Callable[[str], Any],
) -> VerifiedDistribution:
    """Exercise verification against an explicit test-only metadata source."""

    return _verify_distribution_install(
        expectation,
        artifact_path,
        metadata_provider=metadata_provider,
    )


def verified_builtin_registry(temp_root: Path):
    temp_root.mkdir(parents=True, exist_ok=True)
    install_root = Path(builtins.__file__).resolve().parents[3]
    artifact = temp_root / f"openevo-{__version__}-py3-none-any.whl"
    with ZipFile(artifact, "w", compression=ZIP_DEFLATED) as wheel:
        for path in sorted((install_root / "openevo").rglob("*")):
            if path.is_file() and path.name.endswith(
                (".py", ".pyi", ".so", ".pyd", ".dll", ".dylib")
            ):
                wheel.write(path, path.relative_to(install_root).as_posix())
        wheel.writestr(
            f"openevo-{__version__}.dist-info/METADATA",
            f"Name: openevo\nVersion: {__version__}\n",
        )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    verified = verify_distribution_install_for_test(
        DistributionArtifactExpectation(
            distribution="openevo",
            distribution_version=__version__,
            distribution_digest=digest,
        ),
        artifact,
        lambda _name: _SourceDistribution(install_root),
    )
    return load_verified_builtin_registry(verified)


__all__ = ["verified_builtin_registry", "verify_distribution_install_for_test"]
