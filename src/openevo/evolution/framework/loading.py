"""Verified loading for evolution framework implementations.

The artifact digest is supplied by a release descriptor or an explicit
maintainer plugin lock.  This module never derives an expected identity from
the code it is about to trust.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os
import re
import stat
import sys
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email.parser import Parser
from importlib import metadata
from importlib.machinery import PathFinder
from pathlib import Path, PurePosixPath
from types import MappingProxyType, ModuleType
from typing import Protocol

from pydantic import field_validator

from .contracts import (
    DescriptorKind,
    ImplementationRef,
    MethodInvocationABI,
    _Contract,
    _digest,
    _distribution_name,
    _distribution_version,
    _stable_id,
    canonical_digest,
)


_MAX_WHEEL_FILES = 100_000
_MAX_WHEEL_UNCOMPRESSED_BYTES = 32 * 1024 * 1024 * 1024
_DIST_INFO_METADATA = re.compile(r"[^/]+\.dist-info/METADATA\Z", re.ASCII)
_IMPORTABLE_SUFFIXES = (".py", ".pyi", ".so", ".pyd", ".dll", ".dylib")


class FrameworkLoadError(RuntimeError):
    """Raised when an implementation cannot be tied to its locked artifact."""


class DistributionArtifactExpectation(_Contract):
    """External identity from a Core release descriptor or plugin lock."""

    distribution: str
    distribution_version: str
    distribution_digest: str

    _name = field_validator("distribution")(_distribution_name)
    _version = field_validator("distribution_version")(_distribution_version)
    _sha = field_validator("distribution_digest")(_digest)


class DescriptorImplementationAnchor(_Contract):
    """Non-executable identity anchor used before a target-handler cutover."""

    descriptor_kind: DescriptorKind
    descriptor_id: str

    _id = field_validator("descriptor_id")(_stable_id)

    def __call__(self) -> DescriptorImplementationAnchor:
        """Return the immutable anchor for explicit catalog introspection."""

        return self


class InstalledDistribution(Protocol):
    """Subset of ``importlib.metadata.Distribution`` used by the verifier."""

    version: str
    metadata: Mapping[str, str]

    def locate_file(self, path: str | os.PathLike[str]) -> Path: ...

    def read_text(self, filename: str) -> str | None: ...


_DistributionProvider = Callable[
    [str], InstalledDistribution | Sequence[InstalledDistribution]
]


_VERIFIED_DISTRIBUTION_SEAL = object()


@dataclass(frozen=True, slots=True, init=False)
class VerifiedDistribution:
    """A wheel identity bound to one fully checked installed file inventory."""

    expectation: DistributionArtifactExpectation
    install_root: Path
    inventory: Mapping[str, str]
    inventory_digest: str
    _verification_seal: object = field(repr=False, compare=False)

    def __new__(cls, *_args: object, **_kwargs: object) -> VerifiedDistribution:
        raise TypeError(
            "VerifiedDistribution is issued only by verify_distribution_install"
        )

    def implementation_ref(
        self,
        entry_point: str,
        *,
        contract_version: str = "1",
    ) -> ImplementationRef:
        return ImplementationRef(
            distribution=self.expectation.distribution,
            distribution_version=self.expectation.distribution_version,
            distribution_digest=self.expectation.distribution_digest,
            entry_point=entry_point,
            contract_version=contract_version,
        )


def _publish_verified_distribution(
    *,
    expectation: DistributionArtifactExpectation,
    install_root: Path,
    inventory: Mapping[str, str],
    inventory_digest: str,
) -> VerifiedDistribution:
    verified = object.__new__(VerifiedDistribution)
    object.__setattr__(verified, "expectation", expectation)
    object.__setattr__(verified, "install_root", install_root)
    object.__setattr__(verified, "inventory", inventory)
    object.__setattr__(verified, "inventory_digest", inventory_digest)
    object.__setattr__(verified, "_verification_seal", _VERIFIED_DISTRIBUTION_SEAL)
    return verified


def _require_verified_distribution(verified: VerifiedDistribution) -> None:
    if (
        type(verified) is not VerifiedDistribution
        or getattr(verified, "_verification_seal", None)
        is not _VERIFIED_DISTRIBUTION_SEAL
    ):
        raise FrameworkLoadError(
            "distribution evidence was not issued by verify_distribution_install"
        )


def _canonical_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_distribution_provider(name: str) -> Sequence[InstalledDistribution]:
    expected = _canonical_distribution_name(name)
    return tuple(
        distribution
        for distribution in metadata.distributions()
        if _canonical_distribution_name(str(distribution.metadata.get("Name") or ""))
        == expected
    )


def _safe_wheel_member(name: str) -> str:
    if not name or "\\" in name or name.startswith("/"):
        raise FrameworkLoadError("wheel contains an unsafe member path")
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in path.parts) or str(path) != name:
        raise FrameworkLoadError("wheel contains an unsafe member path")
    return name


def _read_wheel_inventory(
    artifact_path: Path,
    expectation: DistributionArtifactExpectation,
) -> dict[str, str]:
    try:
        archive = zipfile.ZipFile(artifact_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise FrameworkLoadError("distribution artifact is not a readable wheel") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > _MAX_WHEEL_FILES:
            raise FrameworkLoadError("wheel file count exceeds the verification limit")
        if sum(info.file_size for info in infos) > _MAX_WHEEL_UNCOMPRESSED_BYTES:
            raise FrameworkLoadError("wheel size exceeds the verification limit")

        names: set[str] = set()
        metadata_members: list[str] = []
        inventory: dict[str, str] = {}
        for info in infos:
            name = _safe_wheel_member(info.filename.rstrip("/"))
            if name in names:
                raise FrameworkLoadError("wheel contains duplicate member paths")
            names.add(name)
            if info.flag_bits & 0x1:
                raise FrameworkLoadError("encrypted wheel members are not supported")
            if info.is_dir():
                continue
            if _DIST_INFO_METADATA.fullmatch(name):
                metadata_members.append(name)
            parts = PurePosixPath(name).parts
            first = parts[0]
            if first.endswith(".data"):
                if (
                    len(parts) >= 3
                    and parts[1] in {"purelib", "platlib"}
                    and parts[-1].endswith(_IMPORTABLE_SUFFIXES)
                ):
                    raise FrameworkLoadError(
                        "wheel .data directory contains importable code"
                    )
                continue
            if first.endswith(".dist-info"):
                continue
            with archive.open(info) as stream:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            inventory[name] = digest.hexdigest()

        if len(metadata_members) != 1:
            raise FrameworkLoadError("wheel must contain exactly one distribution METADATA file")
        try:
            message = Parser().parsestr(
                archive.read(metadata_members[0]).decode("utf-8")
            )
        except (KeyError, UnicodeDecodeError) as exc:
            raise FrameworkLoadError("wheel distribution METADATA is invalid") from exc
        wheel_name = _canonical_distribution_name(str(message.get("Name") or ""))
        wheel_version = str(message.get("Version") or "")
        if wheel_name != expectation.distribution:
            raise FrameworkLoadError("wheel distribution name does not match its lock")
        if wheel_version != expectation.distribution_version:
            raise FrameworkLoadError("wheel distribution version does not match its lock")
        if not inventory or not any(path.endswith(".py") for path in inventory):
            raise FrameworkLoadError("wheel contains no importable Python implementation")
        return dict(sorted(inventory.items()))


def _path_has_symlink(root: Path, relative_path: str) -> bool:
    current = root
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError:
            return False
        if stat.S_ISLNK(mode):
            return True
    return False


def _verify_installed_inventory(
    distribution: InstalledDistribution,
    inventory: Mapping[str, str],
) -> tuple[Path, Mapping[str, str]]:
    try:
        root = Path(distribution.locate_file("")).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FrameworkLoadError("installed distribution root is unavailable") from exc
    if not root.is_dir():
        raise FrameworkLoadError("installed distribution root is not a directory")

    return root, _verify_inventory_at_root(root, inventory)


def _verify_inventory_at_root(
    root: Path,
    inventory: Mapping[str, str],
) -> Mapping[str, str]:
    if not root.is_dir():
        raise FrameworkLoadError("installed distribution root is not a directory")

    for relative_path, expected_digest in inventory.items():
        if _path_has_symlink(root, relative_path):
            raise FrameworkLoadError("installed distribution contains a symlink")
        candidate = root.joinpath(*PurePosixPath(relative_path).parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError, RuntimeError) as exc:
            raise FrameworkLoadError("installed distribution file is missing or escapes its root") from exc
        try:
            current_digest = _sha256_file(resolved) if resolved.is_file() else None
        except OSError as exc:
            raise FrameworkLoadError("installed distribution file is unreadable") from exc
        if current_digest != expected_digest:
            raise FrameworkLoadError("installed distribution file does not match the locked wheel")

    top_levels = {PurePosixPath(path).parts[0] for path in inventory}
    for top_level in top_levels:
        root_member = root / top_level
        if root_member.is_file():
            continue
        if not root_member.is_dir():
            raise FrameworkLoadError("installed top-level package is missing")
        for candidate in root_member.rglob("*"):
            if "__pycache__" in candidate.parts:
                continue
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_symlink():
                raise FrameworkLoadError("installed distribution contains a symlink")
            if candidate.is_file() and candidate.name.endswith(_IMPORTABLE_SUFFIXES):
                if relative not in inventory:
                    raise FrameworkLoadError(
                        "installed distribution contains untracked importable code"
                    )
        for suffix in _IMPORTABLE_SUFFIXES:
            shadow = root / f"{top_level}{suffix}"
            if shadow.exists() or shadow.is_symlink():
                raise FrameworkLoadError(
                    "installed distribution contains a top-level shadow module"
                )

    return MappingProxyType(dict(sorted(inventory.items())))


def _reverify_distribution_inventory(verified: VerifiedDistribution) -> None:
    _require_verified_distribution(verified)
    frozen_inventory = _verify_inventory_at_root(
        verified.install_root,
        verified.inventory,
    )
    inventory_digest = canonical_digest(
        [
            {"path": path, "sha256": digest}
            for path, digest in frozen_inventory.items()
        ]
    )
    if inventory_digest != verified.inventory_digest:
        raise FrameworkLoadError("verified distribution inventory identity changed")


def verify_distribution_install(
    expectation: DistributionArtifactExpectation,
    artifact_path: str | Path,
) -> VerifiedDistribution:
    """Bind a pinned wheel to its uniquely discovered installed distribution."""

    return _verify_distribution_install(
        expectation,
        artifact_path,
        metadata_provider=_default_distribution_provider,
    )


def _verify_distribution_install(
    expectation: DistributionArtifactExpectation,
    artifact_path: str | Path,
    *,
    metadata_provider: _DistributionProvider,
) -> VerifiedDistribution:
    """Internal verifier with an injectable metadata source for test isolation."""

    artifact = Path(artifact_path)
    try:
        mode = artifact.lstat().st_mode
    except OSError as exc:
        raise FrameworkLoadError("distribution artifact is unavailable") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise FrameworkLoadError("distribution artifact must be a regular non-symlink file")
    if _sha256_file(artifact) != expectation.distribution_digest:
        raise FrameworkLoadError("distribution artifact SHA-256 does not match its lock")

    inventory = _read_wheel_inventory(artifact, expectation)
    if _sha256_file(artifact) != expectation.distribution_digest:
        raise FrameworkLoadError("distribution artifact changed during verification")
    provided = metadata_provider(expectation.distribution)
    candidates = (
        tuple(provided)
        if isinstance(provided, Sequence) and not isinstance(provided, str | bytes)
        else (provided,)
    )
    matching = tuple(
        candidate
        for candidate in candidates
        if _canonical_distribution_name(str(candidate.metadata.get("Name") or ""))
        == expectation.distribution
    )
    if len(matching) != 1:
        raise FrameworkLoadError("expected exactly one installed distribution")
    distribution = matching[0]
    if str(distribution.version) != expectation.distribution_version:
        raise FrameworkLoadError("installed distribution version does not match its lock")

    try:
        direct_url_text = distribution.read_text("direct_url.json")
    except (OSError, UnicodeDecodeError) as exc:
        raise FrameworkLoadError("installed distribution metadata is unreadable") from exc
    if direct_url_text:
        try:
            direct_url = json.loads(direct_url_text)
        except json.JSONDecodeError as exc:
            raise FrameworkLoadError("installed distribution direct_url metadata is invalid") from exc
        if isinstance(direct_url, dict):
            if "dir_info" in direct_url:
                raise FrameworkLoadError(
                    "editable or source-tree distributions are not valid release installs"
                )
            archive_info = direct_url.get("archive_info")
            if isinstance(archive_info, dict):
                archive_hash = archive_info.get("hash")
                if (
                    isinstance(archive_hash, str)
                    and archive_hash
                    != f"sha256={expectation.distribution_digest}"
                ):
                    raise FrameworkLoadError(
                        "installed distribution archive hash does not match its lock"
                    )

    root, frozen_inventory = _verify_installed_inventory(distribution, inventory)
    return _publish_verified_distribution(
        expectation=expectation,
        install_root=root,
        inventory=frozen_inventory,
        inventory_digest=canonical_digest(
            [
                {"path": path, "sha256": digest}
                for path, digest in frozen_inventory.items()
            ]
        ),
    )


def _module_inventory_path(
    verified: VerifiedDistribution,
    module_name: str,
) -> str:
    module_path = module_name.replace(".", "/")
    candidates = (
        f"{module_path}.py",
        f"{module_path}/__init__.py",
    )
    matches = tuple(path for path in candidates if path in verified.inventory)
    if len(matches) != 1:
        raise FrameworkLoadError("entry-point module is not uniquely owned by the distribution")
    return matches[0]


def _module_origin(module: ModuleType) -> Path | None:
    value = getattr(module, "__file__", None)
    if not isinstance(value, str) or not value:
        return None
    try:
        return Path(value).resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def _verify_loaded_prefixes(verified: VerifiedDistribution, module_name: str) -> None:
    parts = module_name.split(".")
    for index in range(1, len(parts) + 1):
        prefix = ".".join(parts[:index])
        loaded = sys.modules.get(prefix)
        if loaded is None:
            continue
        try:
            member = _module_inventory_path(verified, prefix)
        except FrameworkLoadError:
            paths = getattr(loaded, "__path__", ())
            try:
                if paths and all(
                    Path(path).resolve(strict=True).is_relative_to(verified.install_root)
                    for path in paths
                ):
                    continue
            except (OSError, RuntimeError):
                pass
            raise FrameworkLoadError("an entry-point package is shadowed in sys.modules")
        if _module_origin(loaded) != (verified.install_root / member).resolve(strict=True):
            raise FrameworkLoadError("an entry-point module is shadowed in sys.modules")


def _verify_import_resolution(verified: VerifiedDistribution, module_name: str) -> None:
    """Resolve every package layer without importing it and reject shadowing."""

    parts = module_name.split(".")
    search_path: Sequence[str] | None = None
    for index in range(1, len(parts) + 1):
        prefix = ".".join(parts[:index])
        spec = PathFinder.find_spec(prefix, search_path)
        if spec is None:
            raise FrameworkLoadError("entry-point module cannot be resolved")
        member = _module_inventory_path(verified, prefix)
        expected_origin = (verified.install_root / member).resolve(strict=True)
        origin = spec.origin
        try:
            resolved_origin = (
                Path(origin).resolve(strict=True)
                if isinstance(origin, str) and origin
                else None
            )
        except (OSError, RuntimeError):
            resolved_origin = None
        if resolved_origin != expected_origin:
            raise FrameworkLoadError("entry-point import would resolve outside the install")

        if index < len(parts):
            locations = spec.submodule_search_locations
            if not locations:
                raise FrameworkLoadError("entry-point parent is not a package")
            try:
                resolved_locations = tuple(
                    Path(location).resolve(strict=True) for location in locations
                )
            except (OSError, RuntimeError) as exc:
                raise FrameworkLoadError(
                    "entry-point package search path is unavailable"
                ) from exc
            expected_location = expected_origin.parent
            if resolved_locations != (expected_location,):
                raise FrameworkLoadError(
                    "entry-point package search path is outside the install"
                )
            search_path = tuple(str(location) for location in resolved_locations)


def _verify_implementation_ref(
    verified: VerifiedDistribution,
    implementation: ImplementationRef,
) -> None:
    expected = verified.expectation
    if (
        implementation.distribution != expected.distribution
        or implementation.distribution_version != expected.distribution_version
        or implementation.distribution_digest != expected.distribution_digest
    ):
        raise FrameworkLoadError("entry-point implementation identity does not match its install")


def _verify_callable_entry_point(
    value: object,
    *,
    module_name: str,
    attribute_path: str,
    expected_parameters: tuple[str, ...],
    label: str,
) -> None:
    if not callable(value):
        raise FrameworkLoadError(f"{label} entry point is not callable")
    if getattr(value, "__module__", None) != module_name:
        raise FrameworkLoadError(f"{label} entry point is re-exported from another module")
    if getattr(value, "__qualname__", None) != attribute_path:
        raise FrameworkLoadError(f"{label} entry-point qualified name does not match")
    try:
        parameters = tuple(inspect.signature(value).parameters.values())
    except (TypeError, ValueError) as exc:
        raise FrameworkLoadError(f"{label} entry-point signature is unavailable") from exc
    if (
        tuple(parameter.name for parameter in parameters) != expected_parameters
        or any(
            parameter.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD
            for parameter in parameters
        )
    ):
        raise FrameworkLoadError(f"{label} entry-point signature does not match")


def load_verified_entry_point(
    implementation: ImplementationRef,
    verified: VerifiedDistribution,
    *,
    expected_kind: DescriptorKind | str = DescriptorKind.METHOD,
    expected_id: str | None = None,
    invocation_abi: MethodInvocationABI | str | None = None,
) -> object:
    """Load one entry point only after proving module ownership and identity."""

    _verify_implementation_ref(verified, implementation)
    _reverify_distribution_inventory(verified)
    try:
        kind = DescriptorKind(expected_kind)
    except ValueError as exc:
        raise FrameworkLoadError("entry-point descriptor kind is invalid") from exc
    method_parameters: tuple[str, ...] | None = None
    if kind is DescriptorKind.METHOD:
        try:
            method_abi = MethodInvocationABI(invocation_abi)
        except (TypeError, ValueError) as exc:
            raise FrameworkLoadError("method invocation ABI is missing or invalid") from exc
        method_parameters = {
            MethodInvocationABI.LEGACY_WORKER_JOB_V1: ("job", "artifact_root"),
            MethodInvocationABI.METHOD_CONTEXT_V1: ("context",),
        }[method_abi]
    elif invocation_abi is not None:
        raise FrameworkLoadError("invocation ABI is only valid for method entry points")
    try:
        module_name, attribute_path = implementation.entry_point.split(":", 1)
    except (ValueError, TypeError) as exc:
        raise FrameworkLoadError("entry-point syntax is invalid") from exc
    descriptor_id = expected_id or attribute_path
    try:
        _stable_id(descriptor_id)
    except ValueError as exc:
        raise FrameworkLoadError("entry-point descriptor ID is invalid") from exc

    member = _module_inventory_path(verified, module_name)
    try:
        expected_origin = (verified.install_root / member).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FrameworkLoadError("entry-point module is unavailable") from exc
    if _path_has_symlink(verified.install_root, member):
        raise FrameworkLoadError("entry-point module contains a symlink")
    try:
        current_digest = _sha256_file(expected_origin)
    except OSError as exc:
        raise FrameworkLoadError("entry-point module is unreadable") from exc
    if current_digest != verified.inventory[member]:
        raise FrameworkLoadError("entry-point module changed after distribution verification")
    _verify_import_resolution(verified, module_name)
    _verify_loaded_prefixes(verified, module_name)

    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise FrameworkLoadError("entry-point module import failed") from exc
    if _module_origin(module) != expected_origin:
        raise FrameworkLoadError("imported entry-point module is outside the verified install")
    module_spec = getattr(module, "__spec__", None)
    spec_origin = getattr(module_spec, "origin", None)
    try:
        normalized_spec_origin = (
            Path(spec_origin).resolve(strict=True)
            if isinstance(spec_origin, str) and spec_origin
            else None
        )
    except (OSError, RuntimeError):
        normalized_spec_origin = None
    if normalized_spec_origin != expected_origin:
        raise FrameworkLoadError("imported entry-point module spec is outside the install")
    try:
        imported_digest = _sha256_file(expected_origin)
    except OSError as exc:
        raise FrameworkLoadError("entry-point module became unreadable during import") from exc
    if imported_digest != verified.inventory[member]:
        raise FrameworkLoadError("entry-point module changed while it was imported")

    value: object = module
    try:
        for attribute in attribute_path.split("."):
            value = getattr(value, attribute)
    except AttributeError as exc:
        raise FrameworkLoadError("entry-point attribute does not exist") from exc

    if kind is DescriptorKind.METHOD:
        if method_parameters is None:  # The ABI validation above is exhaustive.
            raise FrameworkLoadError("method invocation ABI is missing or invalid")
        _verify_callable_entry_point(
            value,
            module_name=module_name,
            attribute_path=attribute_path,
            expected_parameters=method_parameters,
            label="method",
        )
    elif kind is DescriptorKind.TARGET_HANDLER and not isinstance(
        value, DescriptorImplementationAnchor
    ):
        _verify_callable_entry_point(
            value,
            module_name=module_name,
            attribute_path=attribute_path,
            expected_parameters=("handler_input", "services"),
            label="target handler",
        )
    elif not isinstance(value, DescriptorImplementationAnchor):
        raise FrameworkLoadError("descriptor entry point is not an identity anchor")
    elif value.descriptor_kind is not kind or value.descriptor_id != descriptor_id:
        raise FrameworkLoadError("descriptor entry-point anchor does not match")
    return value


__all__ = [
    "DescriptorImplementationAnchor",
    "DistributionArtifactExpectation",
    "FrameworkLoadError",
    "InstalledDistribution",
    "VerifiedDistribution",
    "load_verified_entry_point",
    "verify_distribution_install",
]
