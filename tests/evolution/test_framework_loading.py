"""Security contract tests for the A2.2 distribution-backed framework loader.

Expected API contract:

* ``DistributionArtifactExpectation`` has ``distribution``,
  ``distribution_version``, and ``distribution_digest`` fields matching
  ``ImplementationRef``.
* ``verify_distribution_install`` accepts only the expectation and wheel path,
  discovers real installed metadata, and returns a ``VerifiedDistribution``
  opaque capability.
* ``load_verified_entry_point(ref, verified)`` accepts only a matching ref,
  resolves ``module:qualname`` from verified files, and validates a method
  against its declared invocation ABI or a target handler against its fixed ABI.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import sys
from typing import Any, Callable
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from openevo.evolution.framework import (
    DescriptorKind,
    ImplementationRef,
    MethodInvocationABI,
)
from openevo.evolution.framework.loading import (
    DistributionArtifactExpectation,
    FrameworkLoadError,
    VerifiedDistribution,
    load_verified_entry_point,
    verify_distribution_install,
)
from tests.framework_testkit import verify_distribution_install_for_test


DIST_NAME = "loader-fixture"
DIST_VERSION = "1.2.3"
PACKAGE = "loader_fixture"
DEFAULT_MODULE = f"{PACKAGE}.implementation"
DEFAULT_SOURCE = "def plugin(context):\n    return context\n"


def _record_hash(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"sha256={encoded}"


def _record_bytes(files: dict[str, bytes], record_path: str) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    for path, data in sorted(files.items()):
        writer.writerow((path, _record_hash(data), len(data)))
    writer.writerow((record_path, "", ""))
    return stream.getvalue().encode("utf-8")


def _wheel_files(source: str = DEFAULT_SOURCE) -> dict[str, bytes]:
    dist_info = "loader_fixture-1.2.3.dist-info"
    record_path = f"{dist_info}/RECORD"
    files = {
        f"{PACKAGE}/__init__.py": b"",
        f"{PACKAGE}/implementation.py": source.encode("utf-8"),
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\n"
            f"Name: {DIST_NAME}\n"
            f"Version: {DIST_VERSION}\n"
            "\n"
        ).encode("utf-8"),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: openevo-loader-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
            "\n"
        ).encode("utf-8"),
    }
    files[record_path] = _record_bytes(files, record_path)
    return files


def _write_wheel(path: Path, files: dict[str, bytes]) -> None:
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for relative_path, data in files.items():
            archive.writestr(relative_path, data)


class FakeDistribution:
    """Small importlib.metadata.Distribution-shaped installed metadata fake."""

    def __init__(
        self,
        root: Path,
        files: tuple[PurePosixPath, ...],
        *,
        name: str = DIST_NAME,
        version: str = DIST_VERSION,
        direct_url: dict[str, Any] | None = None,
    ) -> None:
        self.root = root
        self.files = files
        self.metadata = {"Name": name, "Version": version}
        self.version = version
        self._direct_url = direct_url

    def locate_file(self, path: str | os.PathLike[str]) -> Path:
        return self.root.joinpath(os.fspath(path))

    def read_text(self, filename: str) -> str | None:
        if filename == "direct_url.json" and self._direct_url is not None:
            return json.dumps(self._direct_url)
        dist_info = self.root / "loader_fixture-1.2.3.dist-info" / filename
        return dist_info.read_text(encoding="utf-8") if dist_info.is_file() else None


@dataclass
class InstalledWheel:
    artifact: Path
    root: Path
    distribution: FakeDistribution
    provider_calls: list[str]
    _build: Callable[[str], InstalledWheel] | None = None

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.artifact.read_bytes()).hexdigest()

    def expectation(
        self,
        *,
        distribution: str = DIST_NAME,
        version: str = DIST_VERSION,
        digest: str | None = None,
    ) -> DistributionArtifactExpectation:
        return DistributionArtifactExpectation(
            distribution=distribution,
            distribution_version=version,
            distribution_digest=digest or self.digest,
        )

    def metadata_provider(self, name: str) -> FakeDistribution:
        self.provider_calls.append(name)
        return self.distribution

    def verify(self) -> VerifiedDistribution:
        return verify_distribution_install_for_test(
            expectation=self.expectation(),
            artifact_path=self.artifact,
            metadata_provider=self.metadata_provider,
        )

    def rebuild(self, source: str) -> InstalledWheel:
        assert self._build is not None
        return self._build(source)


@pytest.fixture
def installed_wheel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> InstalledWheel:
    def build(source: str = DEFAULT_SOURCE) -> InstalledWheel:
        root = tmp_path / "site-packages"
        artifact = tmp_path / f"loader_fixture-{DIST_VERSION}-py3-none-any.whl"
        files = _wheel_files(source)
        _write_wheel(artifact, files)
        for relative_path, data in files.items():
            installed = root / relative_path
            installed.parent.mkdir(parents=True, exist_ok=True)
            installed.write_bytes(data)
        direct_url = {
            "archive_info": {"hash": f"sha256={hashlib.sha256(artifact.read_bytes()).hexdigest()}"},
            "url": artifact.resolve().as_uri(),
        }
        distribution = FakeDistribution(
            root,
            tuple(PurePosixPath(path) for path in sorted(files)),
            direct_url=direct_url,
        )
        monkeypatch.syspath_prepend(str(root))
        sys.modules.pop(PACKAGE, None)
        sys.modules.pop(DEFAULT_MODULE, None)
        return InstalledWheel(artifact, root, distribution, [])

    fixture = build()
    fixture._build = build
    yield fixture
    sys.modules.pop(PACKAGE, None)
    sys.modules.pop(DEFAULT_MODULE, None)


def _ref(
    installed: InstalledWheel,
    *,
    distribution: str = DIST_NAME,
    version: str = DIST_VERSION,
    digest: str | None = None,
    entry_point: str = f"{DEFAULT_MODULE}:plugin",
) -> ImplementationRef:
    return ImplementationRef(
        distribution=distribution,
        distribution_version=version,
        distribution_digest=digest or installed.digest,
        entry_point=entry_point,
    )


def test_verify_accepts_matching_wheel_and_installed_distribution(
    installed_wheel: InstalledWheel,
) -> None:
    verified = installed_wheel.verify()

    assert isinstance(verified, VerifiedDistribution)
    assert installed_wheel.provider_calls == [DIST_NAME]


def test_public_verifier_rejects_metadata_provider_injection(
    installed_wheel: InstalledWheel,
) -> None:
    with pytest.raises(TypeError, match="metadata_provider"):
        verify_distribution_install(  # type: ignore[call-arg]
            expectation=installed_wheel.expectation(),
            artifact_path=installed_wheel.artifact,
            metadata_provider=installed_wheel.metadata_provider,
        )


@pytest.mark.parametrize("mismatch", ["digest", "version", "distribution"])
def test_verify_rejects_artifact_identity_mismatch(
    installed_wheel: InstalledWheel,
    mismatch: str,
) -> None:
    values = {
        "distribution": DIST_NAME,
        "version": DIST_VERSION,
        "digest": installed_wheel.digest,
    }
    values[mismatch] = {
        "digest": "f" * 64,
        "version": "9.9.9",
        "distribution": "other-fixture",
    }[mismatch]

    with pytest.raises(FrameworkLoadError):
        verify_distribution_install_for_test(
            expectation=installed_wheel.expectation(**values),
            artifact_path=installed_wheel.artifact,
            metadata_provider=installed_wheel.metadata_provider,
        )


@pytest.mark.parametrize(
    "direct_url",
    [
        {"dir_info": {"editable": True}, "url": "file:///workspace/loader-fixture"},
        {"dir_info": {}, "url": "file:///workspace/loader-fixture"},
    ],
    ids=["editable", "source-tree"],
)
def test_verify_rejects_editable_or_source_tree_metadata(
    installed_wheel: InstalledWheel,
    direct_url: dict[str, Any],
) -> None:
    installed_wheel.distribution._direct_url = direct_url

    with pytest.raises(FrameworkLoadError):
        installed_wheel.verify()


@pytest.mark.parametrize("damage", ["tampered", "missing"])
def test_verify_rejects_changed_or_missing_installed_file(
    installed_wheel: InstalledWheel,
    damage: str,
) -> None:
    implementation = installed_wheel.root / PACKAGE / "implementation.py"
    if damage == "tampered":
        implementation.write_text("def plugin(context): return 'tampered'\n", encoding="utf-8")
    else:
        implementation.unlink()

    with pytest.raises(FrameworkLoadError):
        installed_wheel.verify()


def test_verify_rejects_extra_shadow_python_file(installed_wheel: InstalledWheel) -> None:
    (installed_wheel.root / PACKAGE / "shadow.py").write_text(
        "def plugin(context): return context\n",
        encoding="utf-8",
    )

    with pytest.raises(FrameworkLoadError):
        installed_wheel.verify()


def test_verify_rejects_top_level_shadow_module(installed_wheel: InstalledWheel) -> None:
    (installed_wheel.root / f"{PACKAGE}.py").write_text(
        "raise RuntimeError('shadow')\n",
        encoding="utf-8",
    )

    with pytest.raises(FrameworkLoadError):
        installed_wheel.verify()


def test_verify_rejects_symlinked_installed_file(installed_wheel: InstalledWheel) -> None:
    implementation = installed_wheel.root / PACKAGE / "implementation.py"
    outside = installed_wheel.root.parent / "outside.py"
    outside.write_bytes(implementation.read_bytes())
    implementation.unlink()
    implementation.symlink_to(outside)

    with pytest.raises(FrameworkLoadError):
        installed_wheel.verify()


def test_verify_rejects_wheel_member_path_escape(installed_wheel: InstalledWheel) -> None:
    files = _wheel_files()
    _write_wheel(installed_wheel.artifact, {**files, "../escape.py": b"escaped = True\n"})

    with pytest.raises(FrameworkLoadError):
        verify_distribution_install_for_test(
            expectation=installed_wheel.expectation(),
            artifact_path=installed_wheel.artifact,
            metadata_provider=installed_wheel.metadata_provider,
        )


def test_verify_rejects_wheel_data_importable_code(
    installed_wheel: InstalledWheel,
) -> None:
    files = _wheel_files()
    files["loader_fixture-1.2.3.data/purelib/shadow_dependency.py"] = (
        b"raise RuntimeError('shadow')\n"
    )
    _write_wheel(installed_wheel.artifact, files)
    installed_wheel.distribution._direct_url = None

    with pytest.raises(FrameworkLoadError):
        verify_distribution_install_for_test(
            expectation=installed_wheel.expectation(),
            artifact_path=installed_wheel.artifact,
            metadata_provider=installed_wheel.metadata_provider,
        )


def test_loader_returns_verified_context_callable(installed_wheel: InstalledWheel) -> None:
    plugin = load_verified_entry_point(
        _ref(installed_wheel),
        installed_wheel.verify(),
        invocation_abi=MethodInvocationABI.METHOD_CONTEXT_V1,
    )

    marker = object()
    assert plugin(marker) is marker


@pytest.mark.parametrize("mismatch", ["distribution", "version", "digest"])
def test_loader_rejects_ref_identity_mismatch(
    installed_wheel: InstalledWheel,
    mismatch: str,
) -> None:
    verified = installed_wheel.verify()
    values = {
        "distribution": DIST_NAME,
        "version": DIST_VERSION,
        "digest": installed_wheel.digest,
    }
    values[mismatch] = {
        "distribution": "other-fixture",
        "version": "9.9.9",
        "digest": "e" * 64,
    }[mismatch]

    with pytest.raises(FrameworkLoadError):
        load_verified_entry_point(
            _ref(installed_wheel, **values),
            verified,
            invocation_abi=MethodInvocationABI.METHOD_CONTEXT_V1,
        )


def test_loader_rejects_unowned_module_before_import(
    installed_wheel: InstalledWheel,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside-import"
    outside.mkdir()
    marker = tmp_path / "imported"
    (outside / "unowned_plugin.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
        "def plugin(context): return context\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(outside))

    with pytest.raises(FrameworkLoadError):
        load_verified_entry_point(
            _ref(installed_wheel, entry_point="unowned_plugin:plugin"),
            installed_wheel.verify(),
            invocation_abi=MethodInvocationABI.METHOD_CONTEXT_V1,
        )

    assert not marker.exists()
    assert "unowned_plugin" not in sys.modules


def test_loader_rejects_same_name_shadow_package_before_import(
    installed_wheel: InstalledWheel,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "same-name-shadow"
    package = outside / PACKAGE
    package.mkdir(parents=True)
    marker = tmp_path / "shadow-imported"
    (package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n",
        encoding="utf-8",
    )
    (package / "implementation.py").write_text(DEFAULT_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(outside))
    sys.modules.pop(PACKAGE, None)
    sys.modules.pop(DEFAULT_MODULE, None)

    with pytest.raises(FrameworkLoadError):
        load_verified_entry_point(
            _ref(installed_wheel),
            installed_wheel.verify(),
            invocation_abi=MethodInvocationABI.METHOD_CONTEXT_V1,
        )

    assert not marker.exists()
    assert PACKAGE not in sys.modules
    assert DEFAULT_MODULE not in sys.modules


def test_loader_rejects_changed_module_origin_after_import(
    installed_wheel: InstalledWheel,
) -> None:
    rebuilt = installed_wheel.rebuild(
        "def plugin(context):\n    return context\n"
        "__spec__.origin = '/outside/shadow.py'\n"
    )

    with pytest.raises(FrameworkLoadError):
        load_verified_entry_point(
            _ref(rebuilt),
            rebuilt.verify(),
            invocation_abi=MethodInvocationABI.METHOD_CONTEXT_V1,
        )


def test_loader_wraps_module_removed_after_distribution_verification(
    installed_wheel: InstalledWheel,
) -> None:
    verified = installed_wheel.verify()
    (installed_wheel.root / PACKAGE / "implementation.py").unlink()

    with pytest.raises(FrameworkLoadError):
        load_verified_entry_point(
            _ref(installed_wheel),
            verified,
            invocation_abi=MethodInvocationABI.METHOD_CONTEXT_V1,
        )


def test_loader_rechecks_owned_dependencies_after_distribution_verification(
    installed_wheel: InstalledWheel,
    tmp_path: Path,
) -> None:
    verified = installed_wheel.verify()
    marker = tmp_path / "changed-dependency-imported"
    (installed_wheel.root / PACKAGE / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n",
        encoding="utf-8",
    )

    with pytest.raises(FrameworkLoadError):
        load_verified_entry_point(
            _ref(installed_wheel),
            verified,
            invocation_abi=MethodInvocationABI.METHOD_CONTEXT_V1,
        )

    assert not marker.exists()


def test_loader_rejects_qualname_alias(installed_wheel: InstalledWheel) -> None:
    rebuilt = installed_wheel.rebuild(
        "def other(context):\n    return context\nplugin = other\n"
    )

    with pytest.raises(FrameworkLoadError):
        load_verified_entry_point(
            _ref(rebuilt),
            rebuilt.verify(),
            invocation_abi=MethodInvocationABI.METHOD_CONTEXT_V1,
        )


@pytest.mark.parametrize(
    ("source", "invocation_abi"),
    [
        ("def plugin():\n    return None\n", MethodInvocationABI.METHOD_CONTEXT_V1),
        ("def plugin(job):\n    return job\n", MethodInvocationABI.LEGACY_WORKER_JOB_V1),
        (
            "def plugin(context, extra):\n    return context\n",
            MethodInvocationABI.METHOD_CONTEXT_V1,
        ),
        ("def plugin(*context):\n    return context\n", MethodInvocationABI.METHOD_CONTEXT_V1),
        ("def plugin(**context):\n    return context\n", MethodInvocationABI.METHOD_CONTEXT_V1),
        ("def plugin(*, context):\n    return context\n", MethodInvocationABI.METHOD_CONTEXT_V1),
        ("def plugin(context, /):\n    return context\n", MethodInvocationABI.METHOD_CONTEXT_V1),
    ],
)
def test_loader_rejects_method_with_signature_outside_declared_abi(
    installed_wheel: InstalledWheel,
    source: str,
    invocation_abi: MethodInvocationABI,
) -> None:
    rebuilt = installed_wheel.rebuild(source)

    with pytest.raises(FrameworkLoadError):
        load_verified_entry_point(
            _ref(rebuilt),
            rebuilt.verify(),
            invocation_abi=invocation_abi,
        )


def test_loader_accepts_verified_legacy_method(installed_wheel: InstalledWheel) -> None:
    rebuilt = installed_wheel.rebuild(
        "def plugin(job, artifact_root):\n    return job, artifact_root\n"
    )

    plugin = load_verified_entry_point(
        _ref(rebuilt),
        rebuilt.verify(),
        invocation_abi=MethodInvocationABI.LEGACY_WORKER_JOB_V1,
    )

    assert plugin("job", "root") == ("job", "root")


@pytest.mark.parametrize("invocation_abi", [None, "unknown_v1"])
def test_loader_rejects_missing_or_unknown_method_abi(
    installed_wheel: InstalledWheel,
    invocation_abi: str | None,
) -> None:
    with pytest.raises(FrameworkLoadError, match="ABI"):
        load_verified_entry_point(
            _ref(installed_wheel),
            installed_wheel.verify(),
            invocation_abi=invocation_abi,
        )


def test_loader_accepts_verified_two_argument_target_handler(
    installed_wheel: InstalledWheel,
) -> None:
    rebuilt = installed_wheel.rebuild(
        "def handler(handler_input, services):\n"
        "    return handler_input, services\n"
    )

    handler = load_verified_entry_point(
        _ref(rebuilt, entry_point=f"{DEFAULT_MODULE}:handler"),
        rebuilt.verify(),
        expected_kind=DescriptorKind.TARGET_HANDLER,
        expected_id="fixture_handler",
    )

    assert handler("input", "services") == ("input", "services")


def test_loader_rejects_identity_anchor_as_target_handler(
    installed_wheel: InstalledWheel,
) -> None:
    rebuilt = installed_wheel.rebuild(
        "from openevo.evolution.framework.loading import "
        "DescriptorImplementationAnchor\n"
        "from openevo.evolution.framework import DescriptorKind\n"
        "handler = DescriptorImplementationAnchor(\n"
        "    descriptor_kind=DescriptorKind.TARGET_HANDLER,\n"
        "    descriptor_id='fixture_handler',\n"
        ")\n"
    )

    with pytest.raises(FrameworkLoadError, match="target handler"):
        load_verified_entry_point(
            _ref(rebuilt, entry_point=f"{DEFAULT_MODULE}:handler"),
            rebuilt.verify(),
            expected_kind=DescriptorKind.TARGET_HANDLER,
            expected_id="fixture_handler",
        )


@pytest.mark.parametrize(
    "source",
    [
        "def handler(handler_input):\n    return handler_input\n",
        "def handler(value, services):\n    return value, services\n",
        "def handler(handler_input, *, services):\n    return handler_input, services\n",
        "def handler(handler_input, services, /):\n    return handler_input, services\n",
        "def handler(*args):\n    return args\n",
    ],
)
def test_loader_rejects_target_handler_with_wrong_signature(
    installed_wheel: InstalledWheel,
    source: str,
) -> None:
    rebuilt = installed_wheel.rebuild(source)

    with pytest.raises(FrameworkLoadError):
        load_verified_entry_point(
            _ref(rebuilt, entry_point=f"{DEFAULT_MODULE}:handler"),
            rebuilt.verify(),
            expected_kind=DescriptorKind.TARGET_HANDLER,
            expected_id="fixture_handler",
        )
