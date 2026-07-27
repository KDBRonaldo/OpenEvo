from __future__ import annotations

import dataclasses
import errno
import importlib.util
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
from types import ModuleType
from zipfile import ZipFile

import pytest


def _load_builder() -> ModuleType:
    path = Path("desktop/packaging/build_sidecar.py").resolve()
    spec = importlib.util.spec_from_file_location("openevo_build_sidecar", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_core_wheel(path: Path, *, name: str = "openevo", version: str = "0.1.0") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as archive:
        archive.writestr(
            f"openevo-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )


def _write_export_inputs(builder: ModuleType, root: Path) -> tuple[Path, Path]:
    wheel = root / "openevo-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    lock = builder._write_core_framework_lock(wheel, version="0.1.0")
    return wheel, lock


def _write_repo_skeleton(repo: Path) -> None:
    _write_product_web(repo / "desktop/dist")
    _write_product_web(repo / "desktop/packaging/web")
    (repo / "desktop/packaging/product-web-policy.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "forbidden_text": [
                    "dashboard",
                    "benchmark",
                    "developer mode",
                    "developer_mode",
                    "contract_simulator",
                    "scaffold",
                    "dry-run",
                    "dry_run",
                    "host path",
                    "host_path",
                    "host-path",
                    "command",
                ],
            }
        ),
        encoding="utf-8",
    )
    (repo / "desktop/packaging/sidecar_entry.py").write_text("", encoding="utf-8")
    (repo / "desktop/release-contract.json").write_text(
        '{"schema_version":"test-release-authority"}',
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# OpenEvo\n", encoding="utf-8")
    (repo / "LICENSE").write_text("test license\n", encoding="utf-8")
    (repo / "src/openevo").mkdir(parents=True)
    (repo / "src/openevo/__init__.py").write_text("", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "openevo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")


def _write_daemon_inputs(builder: ModuleType, repo: Path) -> tuple[Path, Path]:
    bundle = repo / builder.DAEMON_BUNDLE_BASENAME
    bundle.write_bytes(b"verified linux daemon")
    bundle.chmod(0o700)
    manifest = repo / builder.DAEMON_MANIFEST_BASENAME
    payload = {
        "artifact": {
            "filename": bundle.name,
            "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
            "size": bundle.stat().st_size,
        },
        "build_environment_distributions": [{"name": "openevo", "version": "0.1.0"}],
        "core": {
            "framework_lock": {
                "filename": "framework-lock.json",
                "sha256": "b" * 64,
            },
            "registry_digest": "a" * 64,
            "wheel": {
                "filename": "openevo-0.1.0-py3-none-any.whl",
                "sha256": "c" * 64,
                "size": 1,
                "version": "0.1.0",
            },
        },
        "dependency_lock": {
            "filename": "uv.lock",
            "sha256": hashlib.sha256((repo / "uv.lock").read_bytes()).hexdigest(),
        },
        "platform": {"architecture": "x86_64", "system": "linux"},
        "release": {
            "identity": "d" * 64,
            "source_commit": builder._BUILD_SOURCE_COMMIT,
        },
        "runtime": {
            "format": "pyinstaller-onefile",
            "python": {"implementation": "CPython", "version": "3.11.15"},
            "system_python_required": False,
            "target_pypi_required": False,
        },
        "schema_version": 1,
        "smoke": {
            "backend_readiness": "passed",
            "controlled_exit": "passed",
            "identity": "passed",
        },
    }
    manifest.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    return bundle, manifest


def _write_product_web(root: Path, *, javascript: str = "product workspace") -> None:
    files = {
        "assets/app.js": javascript.encode(),
        "index.html": b'<main id="root"></main>',
    }
    for name, payload in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    entries = [
        {
            "path": name,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "byte_size": len(payload),
        }
        for name, payload in sorted(files.items())
    ]
    build_digest = hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()
    (root / ".openevo-product-web.json").write_text(
        json.dumps(
            {"schema_version": "1", "build_digest": build_digest, "files": entries},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_thin_mach_o(path: Path, *, architecture: str = "arm64") -> None:
    cpu_type = {
        "arm64": 0x0100_000C,
        "x86_64": 0x0100_0007,
    }[architecture]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        struct.pack(
            "<IiiIIIII",
            0xFEED_FACF,
            cpu_type,
            0,
            2,
            0,
            0,
            0,
            0,
        )
        + b"native askpass helper"
    )
    path.chmod(0o755)


def _write_x86_64_elf(path: Path) -> None:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    struct.pack_into("<H", header, 18, 62)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header) + b"native askpass helper")
    path.chmod(0o755)


def test_validate_core_wheel_rejects_project_identity_mismatch(tmp_path: Path) -> None:
    builder = _load_builder()
    wheel = tmp_path / "openevo-9.9.9-py3-none-any.whl"
    _write_core_wheel(wheel, version="9.9.9")

    with pytest.raises(RuntimeError, match="does not match pyproject"):
        builder._validate_core_wheel(wheel, name="openevo", version="0.1.0")


def test_core_framework_lock_is_canonical_and_bound_to_exact_wheel(tmp_path: Path) -> None:
    builder = _load_builder()
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(wheel)

    framework_lock = builder._write_core_framework_lock(wheel, version="0.1.0")

    expected = {
        "schema_version": "1",
        "distribution": "openevo",
        "distribution_version": "0.1.0",
        "distribution_digest": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "wheel_filename": wheel.name,
    }
    assert framework_lock == tmp_path / "framework-lock.json"
    assert framework_lock.read_bytes() == (
        json.dumps(expected, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    assert (
        builder._validated_framework_lock(framework_lock.read_bytes()).model_dump(mode="json")
        == expected
    )
    assert (
        builder._load_exact_framework_lock(
            framework_lock,
            wheel,
            version="0.1.0",
        ).model_dump(mode="json")
        == expected
    )


def test_managed_runtime_archive_contract_is_exact_and_rejects_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    release = builder.MANAGED_RUNTIME_ARCHIVE_RELEASE
    assert release.filename == ("openevo-science-runtime-0.1.1-linux-amd64.tar.gz")
    assert release.byte_size == 352_236_726
    assert release.sha256 == ("ad9c5ebd69b5785b94dd52dc077d93ababfa9cf8cbcbf92940f60bee48a91149")
    archive = tmp_path / release.filename
    archive.write_bytes(b"managed subscription runtime")
    archive.chmod(0o600)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    release = dataclasses.replace(
        release,
        byte_size=archive.stat().st_size,
        sha256=digest,
        asset_api_digest=f"sha256:{digest}",
    )
    monkeypatch.setattr(builder, "MANAGED_RUNTIME_ARCHIVE_RELEASE", release)
    calls: list[tuple[Path, object]] = []

    def verify(path: Path, *, release: object) -> None:
        calls.append((path, release))

    monkeypatch.setattr(builder, "verify_managed_runtime_archive", verify)

    assert builder._validate_managed_runtime_archive(archive) == (
        archive.stat().st_size,
        digest,
    )
    assert calls == [(archive, release)]

    monkeypatch.setattr(
        builder,
        "verify_managed_runtime_archive",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("tampered")),
    )
    with pytest.raises(RuntimeError, match="managed runtime archive"):
        builder._validate_managed_runtime_archive(archive)


def test_release_build_requires_managed_runtime_before_cleaning_owned_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    repo = tmp_path / "repo"
    _write_repo_skeleton(repo)
    marker = repo / "desktop/packaging/sidecar-dist/keep"
    marker.parent.mkdir()
    marker.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(builder, "_repo_root", lambda: repo)

    with pytest.raises(RuntimeError, match="release sidecar build requires"):
        builder.build_sidecar(clean=True, release_build=True)

    assert marker.read_text(encoding="utf-8") == "keep"


def test_release_build_requires_both_daemon_inputs_before_cleaning_owned_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    repo = tmp_path / "repo"
    _write_repo_skeleton(repo)
    marker = repo / "desktop/packaging/sidecar-dist/keep"
    marker.parent.mkdir()
    marker.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(builder, "_repo_root", lambda: repo)

    with pytest.raises(RuntimeError, match="exact Core wheel, framework lock"):
        builder.build_sidecar(
            clean=True,
            managed_runtime_archive=tmp_path / "runtime.tar.gz",
            release_build=True,
        )

    assert marker.read_text(encoding="utf-8") == "keep"


def test_daemon_inputs_must_be_a_complete_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    repo = tmp_path / "repo"
    _write_repo_skeleton(repo)
    monkeypatch.setattr(builder, "_repo_root", lambda: repo)

    with pytest.raises(RuntimeError, match="must be provided together"):
        builder.build_sidecar(
            clean=False,
            daemon_bundle=tmp_path / builder.DAEMON_BUNDLE_BASENAME,
        )


def test_external_core_inputs_are_validated_as_one_release_pair(
    tmp_path: Path,
) -> None:
    builder = _load_builder()
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(wheel)
    framework_lock = builder._write_core_framework_lock(wheel, version="0.1.0")

    opened_wheel, opened_lock = builder._open_core_release_input_pair(
        wheel,
        framework_lock,
    )
    try:
        assert opened_wheel.sha256 == hashlib.sha256(wheel.read_bytes()).hexdigest()
        assert opened_lock.name == "framework-lock.json"
    finally:
        opened_lock.close()
        opened_wheel.close()

    framework_lock.write_text(
        framework_lock.read_text(encoding="utf-8").replace(
            opened_wheel.sha256,
            "0" * 64,
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="bind"):
        builder._open_core_release_input_pair(
            wheel,
            framework_lock,
        )


def test_release_cli_passes_explicit_runtime_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    archive = tmp_path / builder.MANAGED_RUNTIME_ARCHIVE_RELEASE.filename
    daemon_bundle = tmp_path / builder.DAEMON_BUNDLE_BASENAME
    daemon_manifest = tmp_path / builder.DAEMON_MANIFEST_BASENAME
    output = tmp_path / "core"
    target = tmp_path / "sidecar"
    captured: dict[str, object] = {}

    def fake_build_sidecar(**kwargs: object) -> Path:
        captured.update(kwargs)
        return target

    monkeypatch.setattr(builder, "build_sidecar", fake_build_sidecar)

    assert (
        builder.main(
            [
                "--release-build",
                "--managed-runtime-archive",
                str(archive),
                "--daemon-bundle",
                str(daemon_bundle),
                "--daemon-manifest",
                str(daemon_manifest),
                "--core-wheel-output-dir",
                str(output),
            ]
        )
        == 0
    )
    assert captured == {
        "clean": True,
        "core_wheel_output_dir": output,
        "core_wheel": None,
        "core_framework_lock": None,
        "managed_runtime_archive": archive,
        "daemon_bundle": daemon_bundle,
        "daemon_manifest": daemon_manifest,
        "release_build": True,
    }


def test_embedded_managed_runtime_archive_is_closed_and_source_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    executable = tmp_path / "sidecar"
    executable.write_bytes(b"sidecar")
    release = builder.MANAGED_RUNTIME_ARCHIVE_RELEASE
    archive = tmp_path / release.filename
    archive.write_bytes(b"managed subscription runtime")
    archive.chmod(0o600)
    identity = (archive.stat().st_size, hashlib.sha256(archive.read_bytes()).hexdigest())
    release = dataclasses.replace(
        release,
        byte_size=identity[0],
        sha256=identity[1],
        asset_api_digest=f"sha256:{identity[1]}",
    )
    monkeypatch.setattr(builder, "MANAGED_RUNTIME_ARCHIVE_RELEASE", release)
    monkeypatch.setattr(builder, "verify_managed_runtime_archive", lambda *_args, **_kwargs: None)
    member = (builder.MANAGED_RUNTIME_ARCHIVE_ROOT / release.filename).as_posix()
    monkeypatch.setattr(builder, "_archive_member_names", lambda _: (member,))
    monkeypatch.setattr(
        builder,
        "_archive_member_digest",
        lambda *_args, **_kwargs: identity,
    )

    builder._validate_embedded_managed_runtime_archive(executable, archive)

    monkeypatch.setattr(
        builder,
        "_archive_member_names",
        lambda _: (member, f"{builder.MANAGED_RUNTIME_ARCHIVE_ROOT}/stale.tar.gz"),
    )
    with pytest.raises(RuntimeError, match="exact managed runtime archive"):
        builder._validate_embedded_managed_runtime_archive(executable, archive)


def test_sidecar_archive_excludes_all_remote_release_asset_roots_and_limits_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    executable = tmp_path / "sidecar"
    executable.write_bytes(b"local sidecar")
    monkeypatch.setattr(
        builder, "_archive_member_names", lambda _: ("desktop/packaging/web/index.html",)
    )
    builder._validate_sidecar_excludes_remote_release_assets(executable)

    for root in builder.REMOTE_RELEASE_ARCHIVE_ROOTS:
        monkeypatch.setattr(
            builder, "_archive_member_names", lambda _, root=root: (f"{root}/payload",)
        )
        with pytest.raises(RuntimeError, match="must not embed remote release assets"):
            builder._validate_sidecar_excludes_remote_release_assets(executable)

    executable.write_bytes(b"x" * (builder.MAX_SIDECAR_BINARY_BYTES + 1))
    monkeypatch.setattr(builder, "_archive_member_names", lambda _: ())
    with pytest.raises(RuntimeError, match="local-only archive size limit"):
        builder._validate_sidecar_excludes_remote_release_assets(executable)


def test_embedded_daemon_inputs_are_closed_and_source_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    repo = tmp_path / "repo"
    _write_repo_skeleton(repo)
    bundle, manifest = _write_daemon_inputs(builder, repo)
    executable = repo / "sidecar"
    executable.write_bytes(b"sidecar")
    bundle_source, manifest_source, _value = builder._open_daemon_release_input_pair(
        bundle,
        manifest,
        repo=repo,
    )
    expected_members = (
        f"{builder.DAEMON_ARCHIVE_ROOT.as_posix()}/{builder.DAEMON_BUNDLE_BASENAME}",
        f"{builder.DAEMON_ARCHIVE_ROOT.as_posix()}/{builder.DAEMON_MANIFEST_BASENAME}",
    )
    monkeypatch.setattr(builder, "_archive_member_names", lambda _: expected_members)
    monkeypatch.setattr(
        builder,
        "_archive_member_digest",
        lambda _executable, member, *, expected_size: (
            expected_size,
            bundle_source.sha256
            if member.endswith(builder.DAEMON_BUNDLE_BASENAME)
            else manifest_source.sha256,
        ),
    )
    try:
        builder._validate_embedded_daemon_release_inputs(
            executable,
            bundle_source,
            manifest_source,
        )

        monkeypatch.setattr(
            builder,
            "_archive_member_names",
            lambda _: (*expected_members, "openevo/daemon/stale"),
        )
        with pytest.raises(RuntimeError, match="exactly the verified Daemon"):
            builder._validate_embedded_daemon_release_inputs(
                executable,
                bundle_source,
                manifest_source,
            )

        monkeypatch.setattr(builder, "_archive_member_names", lambda _: expected_members)
        monkeypatch.setattr(
            builder,
            "_archive_member_digest",
            lambda _executable, _member, *, expected_size: (expected_size, "f" * 64),
        )
        with pytest.raises(RuntimeError, match="differs from its source"):
            builder._validate_embedded_daemon_release_inputs(
                executable,
                bundle_source,
                manifest_source,
            )
    finally:
        manifest_source.close()
        bundle_source.close()


def test_daemon_manifest_must_bind_embedded_core_wheel_and_lock(tmp_path: Path) -> None:
    builder = _load_builder()
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(wheel)
    lock = builder._write_core_framework_lock(wheel, version="0.1.0")
    manifest: dict[str, object] = {
        "core": {
            "framework_lock": {
                "filename": lock.name,
                "sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
            },
            "registry_digest": "a" * 64,
            "wheel": {
                "filename": wheel.name,
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "size": wheel.stat().st_size,
                "version": "0.1.0",
            },
        }
    }

    builder._validate_daemon_manifest_core(
        manifest,
        wheel=wheel,
        framework_lock=lock,
        version="0.1.0",
    )
    manifest["core"]["wheel"]["sha256"] = "f" * 64
    with pytest.raises(RuntimeError, match="embedded Core wheel and lock"):
        builder._validate_daemon_manifest_core(
            manifest,
            wheel=wheel,
            framework_lock=lock,
            version="0.1.0",
        )


def test_core_wheel_and_lock_build_are_reproducible(tmp_path: Path) -> None:
    builder = _load_builder()
    repo = Path.cwd()
    _, version = builder._project_identity(repo)

    first = builder._build_core_wheel(repo, tmp_path / "first")
    second = builder._build_core_wheel(repo, tmp_path / "second")
    first_lock = builder._core_framework_lock_bytes(first, version=version)
    second_lock = builder._core_framework_lock_bytes(second, version=version)

    assert first.read_bytes() == second.read_bytes()
    assert first_lock == second_lock


def test_core_wheel_contains_exact_control_contract_snapshots(tmp_path: Path) -> None:
    builder = _load_builder()
    repo = Path.cwd()
    wheel = builder._build_core_wheel(repo, tmp_path / "build")
    snapshots = (
        "backend/contracts/v1/events.schema.json",
        "backend/contracts/v1/openapi.json",
        "backend/contracts/v2/events.schema.json",
        "backend/contracts/v2/openapi.json",
    )

    with ZipFile(wheel) as archive:
        for relative_path in snapshots:
            assert archive.read(f"openevo/{relative_path}") == (
                repo / "src/openevo" / relative_path
            ).read_bytes()


def test_validate_core_wheel_rejects_nested_wheel(tmp_path: Path) -> None:
    builder = _load_builder()
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(wheel)
    with ZipFile(wheel, "a") as archive:
        archive.writestr("openevo/wheels/stale.whl", b"stale")

    with pytest.raises(RuntimeError, match="must not contain nested wheels"):
        builder._validate_core_wheel(wheel, name="openevo", version="0.1.0")


def test_validate_core_wheel_rejects_terminal_bench_automation(tmp_path: Path) -> None:
    builder = _load_builder()
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(wheel)
    with ZipFile(wheel, "a") as archive:
        archive.writestr("openevo_terminal_bench/cli.py", b"")

    with pytest.raises(RuntimeError, match="Terminal Bench automation"):
        builder._validate_core_wheel(wheel, name="openevo", version="0.1.0")


def test_validate_core_wheel_rejects_removed_terminal_bench_modules(
    tmp_path: Path,
) -> None:
    builder = _load_builder()
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(wheel)
    legacy_modules = {
        "openevo/evolution/terminal_bench_bridge.py",
        "openevo/evolution/terminal_bench_local_parametric.py",
        "openevo/evolution/terminal_bench_per_task.py",
        "openevo/evolution/terminal_bench_task_local_parametric.py",
    }
    with ZipFile(wheel, "a") as archive:
        for name in legacy_modules:
            archive.writestr(name, b"")

    with pytest.raises(RuntimeError, match="removed Terminal Bench Core modules") as exc:
        builder._validate_core_wheel(wheel, name="openevo", version="0.1.0")

    assert all(path in str(exc.value) for path in legacy_modules)


def test_validate_core_wheel_allows_unrelated_similar_module_name(
    tmp_path: Path,
) -> None:
    builder = _load_builder()
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(wheel)
    with ZipFile(wheel, "a") as archive:
        archive.writestr("openevo/evolution/terminal_bench_bridge_v2.py", b"")

    builder._validate_core_wheel(wheel, name="openevo", version="0.1.0")


def test_product_web_build_requires_exact_audited_dist_and_packaged_assets(
    tmp_path: Path,
) -> None:
    builder = _load_builder()
    repo = tmp_path / "repo"
    _write_repo_skeleton(repo)

    digest = builder._validate_product_web_build(repo / "desktop")
    assert len(digest) == 64

    (repo / "desktop/packaging/web/assets/app.js").write_text(
        "stale product workspace", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="manifest digest differs|does not exactly match"):
        builder._validate_product_web_build(repo / "desktop")


@pytest.mark.parametrize(
    "forbidden",
    [
        "dashboard",
        "benchmark",
        "developer mode",
        "developer_mode",
        "contract_simulator",
        "scaffold",
        "dry-run",
        "dry_run",
        "host path",
        "host_path",
        "host-path",
        "command",
    ],
)
def test_product_web_build_rejects_forbidden_static_text(
    tmp_path: Path,
    forbidden: str,
) -> None:
    builder = _load_builder()
    repo = tmp_path / "repo"
    _write_repo_skeleton(repo)
    _write_product_web(repo / "desktop/dist", javascript=f"product {forbidden} text")
    _write_product_web(repo / "desktop/packaging/web", javascript=f"product {forbidden} text")

    with pytest.raises(RuntimeError, match="forbidden product text"):
        builder._validate_product_web_build(repo / "desktop")


def test_product_web_policy_rejects_every_non_release_provider_kind() -> None:
    policy = json.loads(
        Path("desktop/packaging/product-web-policy.json").read_text(encoding="utf-8")
    )

    assert {"contract_simulator", "scaffold", "dry_run"}.issubset(policy["forbidden_text"])
    schemas = Path("desktop/src/api/v1/schemas.ts").read_text(encoding="utf-8")
    assert '["dry", "run"].join("_")' not in schemas


def test_product_web_policy_allows_sanitized_lifecycle_process_log_sources(
    tmp_path: Path,
) -> None:
    builder = _load_builder()
    repo = tmp_path / "repo"
    _write_repo_skeleton(repo)
    lifecycle_sources = "ssh_stdout ssh_stderr daemon_stdout daemon_stderr"
    _write_product_web(repo / "desktop/dist", javascript=lifecycle_sources)
    _write_product_web(repo / "desktop/packaging/web", javascript=lifecycle_sources)

    assert len(builder._validate_product_web_build(repo / "desktop")) == 64
    policy = json.loads(
        Path("desktop/packaging/product-web-policy.json").read_text(encoding="utf-8")
    )
    assert "stdout" not in policy["forbidden_text"]
    assert "stderr" not in policy["forbidden_text"]
    assert {"command", "host_path", "host-path"}.issubset(policy["forbidden_text"])


def test_packaged_product_graph_excludes_non_release_provider_code() -> None:
    release_schema = Path("desktop/src/api/v1/providerKinds.release.ts").read_text(
        encoding="utf-8"
    )
    vite_config = Path("desktop/vite.config.ts").read_text(encoding="utf-8")
    packaged_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(Path("desktop/packaging/web").rglob("*"))
        if path.is_file()
        and path.suffix.lower() in {".css", ".html", ".js", ".json", ".map", ".txt"}
    )

    assert 'z.literal("desktop_sidecar")' in release_schema
    assert "providerKinds.release.ts" in vite_config
    for forbidden in (
        "contract_simulator",
        "scaffold",
        "dry_run",
        "FixtureDesktopProductProvider",
        "createFixtureDesktopProductProvider",
    ):
        assert forbidden not in release_schema
        assert forbidden not in packaged_text


def test_sidecar_archive_product_web_matches_audited_build_and_rejects_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    repo = tmp_path / "repo"
    _write_repo_skeleton(repo)
    executable = repo / "sidecar"
    executable.write_bytes(b"sidecar")
    digest = builder._validate_product_web_build(repo / "desktop")
    source = builder._product_web_files(repo / "desktop/packaging/web")
    payloads = {f"desktop/packaging/web/{name}": value for name, value in source.items()}
    monkeypatch.setattr(builder, "_archive_member_names", lambda _: tuple(payloads))
    monkeypatch.setattr(builder, "_archive_member_bytes", lambda _, name: payloads[name])

    builder._validate_embedded_product_web(executable, repo / "desktop", digest)

    payloads["desktop/packaging/web/assets/app.js"] = b"tampered"
    with pytest.raises(RuntimeError, match="differs from the audited build"):
        builder._validate_embedded_product_web(executable, repo / "desktop", digest)


def test_sidecar_archive_embeds_the_exact_release_authority_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    executable = tmp_path / "sidecar"
    executable.write_bytes(b"sidecar")
    manifest = tmp_path / "release-contract.json"
    source = b'{"schema_version":"test-release-authority"}'
    manifest.write_bytes(source)
    payloads = {"desktop/release-contract.json": source}
    monkeypatch.setattr(builder, "_archive_member_names", lambda _: tuple(payloads))
    monkeypatch.setattr(builder, "_archive_member_bytes", lambda _, name: payloads[name])

    builder._validate_embedded_release_contract(executable, manifest)

    payloads["desktop/release-contract.json"] = b'{"schema_version":"tampered"}'
    with pytest.raises(RuntimeError, match="release authority manifest differs"):
        builder._validate_embedded_release_contract(executable, manifest)


@pytest.mark.parametrize("clean", [False, True])
@pytest.mark.parametrize("release_build", [False, True])
def test_build_sidecar_uses_isolated_source_and_preserves_repository_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean: bool,
    release_build: bool,
) -> None:
    builder = _load_builder()
    repo = tmp_path / "repo"
    _write_repo_skeleton(repo)
    stale_stage = repo / "src/openevo/wheels"
    stale_stage.mkdir(parents=True)
    (stale_stage / "stale.whl").write_text("stale", encoding="utf-8")
    generic_build = repo / "build"
    generic_build.mkdir()
    (generic_build / "user-output.txt").write_text("keep", encoding="utf-8")
    (repo / "src/openevo/openevo.egg-info").mkdir()
    (repo / "src/openevo/openevo.egg-info/PKG-INFO").write_text("stale", encoding="utf-8")
    runtime_archive = repo / builder.MANAGED_RUNTIME_ARCHIVE_RELEASE.filename
    runtime_archive.write_bytes(b"managed subscription runtime")
    runtime_archive.chmod(0o600)
    daemon_bundle, daemon_manifest = _write_daemon_inputs(builder, repo)
    authoritative_wheel = repo / "authoritative-core/openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(authoritative_wheel)
    authoritative_lock = builder._write_core_framework_lock(
        authoritative_wheel,
        version="0.1.0",
    )
    commands: list[str] = []
    pyinstaller_root = repo / "fd-bound-pyinstaller"

    def fake_run(command, *, check, cwd, **kwargs):
        assert check is True
        if command[:3] == ["npm", "run", "build:openevo"]:
            assert not kwargs
            assert Path(cwd) == repo / "desktop"
            commands.append("product-web")
        elif command[2] == "build":
            env = kwargs.pop("env")
            assert not kwargs
            assert env["SOURCE_DATE_EPOCH"] == str(builder._BUILD_SOURCE_DATE_EPOCH)
            commands.append("build")
            source = Path(cwd)
            assert source != repo
            assert command[3:6] == ["--wheel", "--no-isolation", "--outdir"]
            assert sorted(path.name for path in source.iterdir()) == [
                "LICENSE",
                "README.md",
                "pyproject.toml",
                "src",
            ]
            assert not (source / "src/openevo/wheels").exists()
            assert not (source / "src/openevo/openevo.egg-info").exists()
            output_dir = Path(command[command.index("--outdir") + 1])
            _write_core_wheel(output_dir / "openevo-0.1.0-py3-none-any.whl")
        elif command[:2] == ["cargo", "build"]:
            env = kwargs.pop("env")
            assert not kwargs
            commands.append("cargo")
            assert Path(cwd) == repo / "desktop/src-tauri"
            assert command == [
                "cargo",
                "build",
                "--locked",
                "--release",
                "--bin",
                builder.ASKPASS_NAME,
                "--target",
                "aarch64-apple-darwin",
            ]
            helper = (
                Path(env["CARGO_TARGET_DIR"])
                / "aarch64-apple-darwin/release"
                / builder.ASKPASS_NAME
            )
            _write_thin_mach_o(helper)
        elif command[2] == "PyInstaller":
            env = kwargs.pop("env")
            assert not kwargs
            assert env["PYTHONPATH"].split(os.pathsep)[0] == str(pyinstaller_root)
            commands.append("PyInstaller")
            assert Path(cwd) == repo
            assert ("--clean" in command) is clean
            add_data = [
                command[index + 1]
                for index, value in enumerate(command[:-1])
                if value == "--add-data"
            ]
            assert all(
                destination not in {"openevo/wheels", "openevo/runtime-assets", "openevo/daemon"}
                for _source, destination in (value.rsplit(os.pathsep, 1) for value in add_data)
            )
            assert not any(
                command[index : index + 2] == ["--collect-data", "openevo"]
                for index in range(len(command) - 1)
            )
            metadata_source = next(
                Path(source)
                for source, destination in (value.rsplit(os.pathsep, 1) for value in add_data)
                if destination == builder.SIDECAR_BUILD_METADATA_RELATIVE_PATH.parent.as_posix()
            )
            metadata = json.loads(metadata_source.read_text(encoding="utf-8"))
            assert metadata["schema_version"] == "2"
            assert metadata["ssh_askpass_helper"]["filename"] == builder.ASKPASS_NAME
            assert any(
                Path(source) == repo / builder.RELEASE_CONTRACT_RELATIVE_PATH
                and destination == builder.RELEASE_CONTRACT_RELATIVE_PATH.parent.as_posix()
                for source, destination in (
                    value.rsplit(os.pathsep, 1) for value in add_data
                )
            )
            dist_dir = Path(command[command.index("--distpath") + 1])
            dist_dir.mkdir(parents=True, exist_ok=True)
            (dist_dir / builder.SIDECAR_NAME).write_bytes(b"packaged-sidecar")
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(builder, "_repo_root", lambda: repo)
    monkeypatch.setattr(builder, "_target_triple", lambda: "aarch64-apple-darwin")
    monkeypatch.setattr(
        builder,
        "_prepare_fd_bound_pyinstaller",
        lambda *_: pyinstaller_root,
    )
    monkeypatch.setattr(builder, "_validate_fd_bound_bootloader", lambda _: None)
    monkeypatch.setattr(
        builder,
        "_normalize_unsigned_macos_sidecar_signature",
        lambda _: None,
    )
    monkeypatch.setattr(builder, "_verify_macos_adhoc_signature", lambda _: "adhoc")
    monkeypatch.setattr(builder, "_validate_managed_runtime_archive", lambda _: None)
    monkeypatch.setattr(builder, "_validate_daemon_manifest_core", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(builder.subprocess, "run", fake_run)
    monkeypatch.setattr(
        builder,
        "_archive_member_names",
        lambda _: (
            *(
                f"desktop/packaging/web/{path.relative_to(repo / 'desktop/packaging/web').as_posix()}"
                for path in sorted((repo / "desktop/packaging/web").rglob("*"))
                if path.is_file()
            ),
            builder.RELEASE_CONTRACT_RELATIVE_PATH.as_posix(),
        ),
    )
    web_payloads = {
        f"desktop/packaging/web/{path.relative_to(repo / 'desktop/packaging/web').as_posix()}": path.read_bytes()
        for path in (repo / "desktop/packaging/web").rglob("*")
        if path.is_file()
    }
    web_payloads[builder.RELEASE_CONTRACT_RELATIVE_PATH.as_posix()] = (
        repo / builder.RELEASE_CONTRACT_RELATIVE_PATH
    ).read_bytes()
    monkeypatch.setattr(
        builder,
        "_archive_member_bytes",
        lambda _, member: web_payloads[member],
    )

    wheel_output = repo / ".openevo-remote-wheel"
    target = builder.build_sidecar(
        clean=clean,
        core_wheel_output_dir=None if release_build else wheel_output,
        core_wheel=authoritative_wheel if release_build else None,
        core_framework_lock=authoritative_lock if release_build else None,
        managed_runtime_archive=runtime_archive if release_build else None,
        daemon_bundle=daemon_bundle if release_build else None,
        daemon_manifest=daemon_manifest if release_build else None,
        release_build=release_build,
    )

    assert commands == (
        ["cargo", "product-web", "PyInstaller"]
        if release_build
        else ["build", "cargo", "product-web", "PyInstaller"]
    )
    assert target == (
        repo
        / "desktop/src-tauri/binaries"
        / "openevo-desktop-sidecar-aarch64-apple-darwin"
    )
    assert target.read_bytes() == b"packaged-sidecar"
    assert target.stat().st_mode & 0o777 == 0o755
    published_helper = (
        repo / "desktop/src-tauri/binaries/openevo-ssh-askpass-aarch64-apple-darwin"
    )
    assert published_helper.is_file()
    assert published_helper.stat().st_mode & 0o777 == 0o755
    if release_build:
        assert not wheel_output.exists()
    else:
        assert [wheel.name for wheel in wheel_output.glob("*.whl")] == [
            "openevo-0.1.0-py3-none-any.whl"
        ]
        assert next(wheel_output.glob("*.whl")).is_file()
        assert (wheel_output / "framework-lock.json").is_file()
    assert (stale_stage / "stale.whl").read_text(encoding="utf-8") == "stale"
    assert (generic_build / "user-output.txt").read_text(encoding="utf-8") == "keep"


def test_unsigned_macos_sidecar_is_resigned_without_hardened_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    executable = tmp_path / "openevo-desktop-sidecar"
    executable.write_bytes(b"sidecar")
    calls: list[list[str]] = []
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""),
            subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=b"",
                stderr=(
                    b"CodeDirectory v=20400 flags=0x2(adhoc) hashes=1+2 location=embedded\n"
                    b"Signature=adhoc\n"
                    b"TeamIdentifier=not set\n"
                ),
            ),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=b"",
                stderr=b"Executable=/private/example/openevo-desktop-sidecar\n",
            ),
        ]
    )

    def fake_run(command, *, check, capture_output):
        assert check is False
        assert capture_output is True
        calls.append(command)
        if len(calls) == 1:
            replacement = executable.with_name("codesign-replacement")
            replacement.write_bytes(b"resigned-sidecar")
            replacement.chmod(0o755)
            os.replace(replacement, executable)
        return next(responses)

    monkeypatch.setattr(builder.subprocess, "run", fake_run)

    builder._normalize_unsigned_macos_sidecar_signature(executable)

    assert calls == [
        [
            "/usr/bin/codesign",
            "--force",
            "--sign",
            "-",
            "--timestamp=none",
            str(executable),
        ],
        ["/usr/bin/codesign", "--verify", "--strict", str(executable)],
        ["/usr/bin/codesign", "-d", "--verbose=4", str(executable)],
        [
            "/usr/bin/codesign",
            "-d",
            "--entitlements",
            "-",
            str(executable),
        ],
    ]


def test_native_askpass_helper_has_closed_target_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    helper = tmp_path / builder.ASKPASS_NAME
    _write_thin_mach_o(helper)
    signatures: list[Path] = []
    monkeypatch.setattr(
        builder,
        "_verify_macos_adhoc_signature",
        lambda path: signatures.append(path) or "adhoc",
    )

    identity = builder._validate_native_askpass_helper(
        helper,
        target_triple="aarch64-apple-darwin",
    )

    assert identity == {
        "architecture": "arm64",
        "byte_size": helper.stat().st_size,
        "filename": builder.ASKPASS_NAME,
        "mode": "0755",
        "sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
        "signature": "adhoc",
        "target_triple": "aarch64-apple-darwin",
    }
    assert signatures == [helper]


def test_native_askpass_helper_rejects_wrong_architecture_mode_symlink_and_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    helper = tmp_path / builder.ASKPASS_NAME
    _write_thin_mach_o(helper, architecture="x86_64")
    monkeypatch.setattr(builder, "_verify_macos_adhoc_signature", lambda _path: "adhoc")

    with pytest.raises(RuntimeError, match="architecture"):
        builder._validate_native_askpass_helper(
            helper,
            target_triple="aarch64-apple-darwin",
        )
    _write_thin_mach_o(helper)
    helper.chmod(0o775)
    with pytest.raises(RuntimeError, match="mode"):
        builder._validate_native_askpass_helper(
            helper,
            target_triple="aarch64-apple-darwin",
        )

    helper.unlink()
    real = tmp_path / "real-helper"
    _write_thin_mach_o(real)
    helper.symlink_to(real)
    with pytest.raises(RuntimeError, match="regular file"):
        builder._validate_native_askpass_helper(
            helper,
            target_triple="aarch64-apple-darwin",
        )

    helper.unlink()
    _write_thin_mach_o(helper)

    def replace_during_signature(path: Path) -> str:
        path.unlink()
        _write_thin_mach_o(path)
        return "adhoc"

    monkeypatch.setattr(builder, "_verify_macos_adhoc_signature", replace_during_signature)
    with pytest.raises(RuntimeError, match="changed during verification"):
        builder._validate_native_askpass_helper(
            helper,
            target_triple="aarch64-apple-darwin",
        )


def test_native_askpass_helper_has_closed_linux_ci_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    helper = tmp_path / builder.ASKPASS_NAME
    _write_x86_64_elf(helper)
    monkeypatch.setattr(
        builder,
        "_verify_macos_adhoc_signature",
        lambda _path: (_ for _ in ()).throw(AssertionError("codesign must not run")),
    )

    identity = builder._validate_native_askpass_helper(
        helper,
        target_triple="x86_64-unknown-linux-gnu",
    )

    assert identity["architecture"] == "x86_64"
    assert identity["signature"] == "none"
    assert identity["target_triple"] == "x86_64-unknown-linux-gnu"


def test_native_askpass_helper_build_uses_locked_targeted_cargo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    tauri_root = tmp_path / "desktop/src-tauri"
    tauri_root.mkdir(parents=True)
    cargo_target = tmp_path / "cargo-target"
    observed: list[tuple[list[str], Path, dict[str, str]]] = []

    def fake_run(command, *, check, cwd, env):
        observed.append((command, Path(cwd), env))
        built = cargo_target / "aarch64-apple-darwin/release" / builder.ASKPASS_NAME
        _write_thin_mach_o(built)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(builder.subprocess, "run", fake_run)

    built = builder._build_native_askpass_helper(
        tauri_root,
        cargo_target=cargo_target,
        target_triple="aarch64-apple-darwin",
    )

    assert built == cargo_target / "aarch64-apple-darwin/release" / builder.ASKPASS_NAME
    assert observed[0][0] == [
        "cargo",
        "build",
        "--locked",
        "--release",
        "--bin",
        builder.ASKPASS_NAME,
        "--target",
        "aarch64-apple-darwin",
    ]
    assert observed[0][1] == tauri_root
    assert observed[0][2]["CARGO_TARGET_DIR"] == str(cargo_target)
    assert json.loads(observed[0][2]["TAURI_CONFIG"]) == {"bundle": {"externalBin": []}}


def test_sidecar_build_metadata_binds_exact_native_askpass_helper(tmp_path: Path) -> None:
    builder = _load_builder()
    path = tmp_path / "sidecar-build-metadata.json"
    identity = {
        "architecture": "arm64",
        "byte_size": 42,
        "filename": builder.ASKPASS_NAME,
        "mode": "0755",
        "sha256": "a" * 64,
        "signature": "adhoc",
        "target_triple": "aarch64-apple-darwin",
    }

    builder._write_sidecar_build_metadata(
        path,
        source_commit="b" * 40,
        askpass_helper=identity,
    )

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": "2",
        "source_commit": "b" * 40,
        "ssh_askpass_helper": identity,
    }


def test_tauri_bundles_exact_native_askpass_external_binary() -> None:
    config = json.loads(Path("desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    cargo = Path("desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    build_script = Path("desktop/src-tauri/build.rs").read_text(encoding="utf-8")
    helper = Path("desktop/src-tauri/src/askpass.rs").read_text(encoding="utf-8")

    assert config["bundle"]["externalBin"] == [
        "binaries/openevo-desktop-sidecar",
        "binaries/openevo-ssh-askpass",
    ]
    assert 'name = "openevo-ssh-askpass"' in cargo
    assert "cargo:rerun-if-changed=src/askpass.rs" in build_script
    assert "cargo:rerun-if-changed=src/bin/openevo-ssh-askpass.rs" in build_script
    assert "NSSecureTextField" in helper
    assert "osascript" not in helper
    assert 'Command::new("sh")' not in helper


def test_unsigned_macos_sidecar_rejects_hardened_runtime_after_resigning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    executable = tmp_path / "openevo-desktop-sidecar"
    executable.write_bytes(b"sidecar")
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""),
            subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=b"",
                stderr=(
                    b"CodeDirectory v=20500 flags=0x10002(adhoc,runtime) hashes=1+2\n"
                    b"Signature=adhoc\n"
                    b"TeamIdentifier=not set\n"
                    b"Runtime Version=12.1.0\n"
                ),
            ),
        ]
    )
    monkeypatch.setattr(
        builder.subprocess,
        "run",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(RuntimeError, match="hardened runtime"):
        builder._normalize_unsigned_macos_sidecar_signature(executable)


def test_imported_core_pair_cannot_be_combined_with_export_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    repo = tmp_path / "repo"
    _write_repo_skeleton(repo)
    wheel = tmp_path / "core/openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(wheel)
    framework_lock = builder._write_core_framework_lock(wheel, version="0.1.0")
    monkeypatch.setattr(builder, "_repo_root", lambda: repo)

    with pytest.raises(RuntimeError, match="cannot be combined"):
        builder.build_sidecar(
            clean=False,
            core_wheel_output_dir=tmp_path / "export",
            core_wheel=wheel,
            core_framework_lock=framework_lock,
        )


def test_fd_bound_bootloader_patch_is_exact_and_cross_platform(
    tmp_path: Path,
) -> None:
    builder = _load_builder()
    source_root = tmp_path / "pyinstaller"
    source = source_root / "bootloader/src/pyi_main.c"
    source.parent.mkdir(parents=True)
    source.write_text(
        builder._BOOTLOADER_MACOS_INCLUDE_NEEDLE
        + builder._BOOTLOADER_RESOLVER_NEEDLE
        + builder._BOOTLOADER_ARCHIVE_NEEDLE
        + builder._BOOTLOADER_RESTART_NEEDLE
        + builder._BOOTLOADER_CHILD_MAIN_NEEDLE,
        encoding="utf-8",
    )
    utils_source = source_root / "bootloader/src/pyi_utils_posix.c"
    utils_source.write_text(
        builder._BOOTLOADER_POSIX_INCLUDE_NEEDLE
        + builder._BOOTLOADER_NATIVE_HANDOFF_NEEDLE
        + builder._BOOTLOADER_CHILD_EXEC_NEEDLE,
        encoding="utf-8",
    )
    utils_header = source_root / "bootloader/src/pyi_utils.h"
    utils_header.write_text(builder._BOOTLOADER_UTILS_HEADER_NEEDLE, encoding="utf-8")
    wscript = source_root / "bootloader/wscript"
    wscript.write_text(
        builder._BOOTLOADER_DARWIN_LIB_NEEDLE + builder._BOOTLOADER_PROGRAM_LIBS_NEEDLE,
        encoding="utf-8",
    )

    builder._patch_fd_bound_bootloader(source_root)

    patched = source.read_text(encoding="utf-8")
    patched_utils = utils_source.read_text(encoding="utf-8")
    patched_header = utils_header.read_text(encoding="utf-8")
    patched_wscript = wscript.read_text(encoding="utf-8")
    assert 'getenv("OPENEVO_NATIVE_EXECUTABLE_FD")' in patched
    assert 'getenv("OPENEVO_NATIVE_LISTENER_FD")' in patched
    assert 'strcmp(openevo_native_fd, "4")' in patched
    assert 'strcmp(openevo_native_listener_fd, "3")' in patched
    assert '"/proc/self/fd/4"' in patched
    assert '"/dev/fd/4"' in patched
    assert patched.count(builder._BOOTLOADER_RESOLVER_REPLACEMENT) == 1
    assert "pyi_utils_openevo_native_handoff_prepare()" in patched
    assert "pyi_utils_openevo_native_handoff_finish()" in patched
    assert "F_DUPFD" in patched_utils
    assert "dup2(openevo_listener_guard_fd, OPENEVO_NATIVE_LISTENER_FD)" in patched_utils
    assert "dup2(openevo_archive_guard_fd, OPENEVO_NATIVE_ARCHIVE_FD)" in patched_utils
    assert "FD_CLOEXEC" in patched_utils
    assert "SO_ACCEPTCONN" in patched_utils
    assert "proc_pidfdinfo" in patched_utils
    assert "PROC_PIDFDSOCKETINFO" in patched_utils
    assert "SOCKINFO_TCP" in patched_utils
    assert "INADDR_LOOPBACK" in patched_utils
    assert "listener_info.psi.soi_options & SO_ACCEPTCONN" in patched_utils
    assert "ctx.check_cc(lib='proc', mandatory=True, uselib_store='PROC')" in patched_wscript
    assert "'PROC',  # macOS process and descriptor inspection" in patched_wscript
    assert "pyi_utils_openevo_native_handoff_restore()" in patched_utils
    assert "pyi_utils_openevo_native_handoff_prepare" in patched_header


def test_fd_bound_bootloader_rejections_emit_closed_startup_diagnostics(
    tmp_path: Path,
) -> None:
    builder = _load_builder()
    source_root = tmp_path / "pyinstaller"
    source = source_root / "bootloader/src/pyi_main.c"
    source.parent.mkdir(parents=True)
    source.write_text(
        builder._BOOTLOADER_MACOS_INCLUDE_NEEDLE
        + builder._BOOTLOADER_RESOLVER_NEEDLE
        + builder._BOOTLOADER_ARCHIVE_NEEDLE
        + builder._BOOTLOADER_RESTART_NEEDLE
        + builder._BOOTLOADER_CHILD_MAIN_NEEDLE,
        encoding="utf-8",
    )
    utils_source = source_root / "bootloader/src/pyi_utils_posix.c"
    utils_source.write_text(
        builder._BOOTLOADER_POSIX_INCLUDE_NEEDLE
        + builder._BOOTLOADER_NATIVE_HANDOFF_NEEDLE
        + builder._BOOTLOADER_CHILD_EXEC_NEEDLE,
        encoding="utf-8",
    )
    utils_header = source_root / "bootloader/src/pyi_utils.h"
    utils_header.write_text(builder._BOOTLOADER_UTILS_HEADER_NEEDLE, encoding="utf-8")
    wscript = source_root / "bootloader/wscript"
    wscript.write_text(
        builder._BOOTLOADER_DARWIN_LIB_NEEDLE + builder._BOOTLOADER_PROGRAM_LIBS_NEEDLE,
        encoding="utf-8",
    )

    builder._patch_fd_bound_bootloader(source_root)

    patched_sources = (
        source.read_text(encoding="utf-8"),
        utils_source.read_text(encoding="utf-8"),
    )
    for patched in patched_sources:
        lines = patched.splitlines()
        for index, line in enumerate(lines):
            if line.strip() not in {"return -1;", "exit(-1);"}:
                continue
            assert (
                "OPENEVO_STARTUP_FAILURE(" in lines[index - 1]
                or "_pyi_utils_openevo_validate_native_fds()" in lines[index - 1]
            ), line
        assert "OPENEVO_STARTUP_V1 stage=" in patched
        assert "%s" not in "\n".join(line for line in lines if "OPENEVO_STARTUP_V1" in line)

    combined = "\n".join(patched_sources)
    for stage in (
        "bootloader_resolver",
        "bootloader_archive",
        "bootloader_handoff",
        "bootloader_restore",
        "bootloader_exec",
        "bootloader_restart",
        "bootloader_child",
    ):
        assert f'OPENEVO_STARTUP_FAILURE("{stage}",' in combined


@pytest.mark.parametrize(
    ("platform", "platform_markers"),
    [
        ("linux", (b"/proc/self/fd/4",)),
        (
            "darwin",
            (
                b"/dev/fd/4",
                b"openevo-desktop-sidecar",
                b"listener_info_probe_failed",
                b"listener_endpoint_invalid",
            ),
        ),
    ],
)
def test_native_bootloader_validation_uses_the_compiled_platform_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    platform_markers: tuple[bytes, ...],
) -> None:
    builder = _load_builder()
    executable = tmp_path / "sidecar"
    executable.write_bytes(
        b"\0".join(
            (
                b"OPENEVO_NATIVE_LISTENER_FD",
                b"OPENEVO_NATIVE_EXECUTABLE_FD",
                b"OPENEVO_NATIVE_EXECUTABLE_PATH",
                b"OpenEvo native descriptors",
                *platform_markers,
            )
        )
    )
    monkeypatch.setattr(builder.sys, "platform", platform)

    builder._validate_fd_bound_bootloader(executable)


def test_native_bootloader_validation_rejects_an_unsupported_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    executable = tmp_path / "sidecar"
    executable.write_bytes(
        b"OPENEVO_NATIVE_LISTENER_FD\0OPENEVO_NATIVE_EXECUTABLE_FD\0"
        b"OPENEVO_NATIVE_EXECUTABLE_PATH\0OpenEvo native descriptors"
    )
    monkeypatch.setattr(builder.sys, "platform", "win32")

    with pytest.raises(RuntimeError, match="platform is unsupported"):
        builder._validate_fd_bound_bootloader(executable)


def test_pyinstaller_source_identity_comes_from_exact_uv_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    (tmp_path / "uv.lock").write_text(
        """
[[package]]
name = "pyinstaller"
version = "6.21.0"
sdist = { url = "https://files.pythonhosted.org/source.tar.gz", hash = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", size = 1234 }
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(builder, "distribution_version", lambda _: "6.21.0")

    assert builder._locked_pyinstaller_sdist(tmp_path) == (
        "6.21.0",
        "https://files.pythonhosted.org/source.tar.gz",
        "a" * 64,
        1234,
    )


def test_locked_download_retries_a_truncated_response_on_the_same_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    payload = b"locked-pyinstaller-source"
    calls = 0
    output_inodes: list[int] = []
    destination = tmp_path / "pyinstaller.tar.gz"

    class Response(BytesIO):
        def __init__(
            self,
            value: bytes,
            *,
            status: int,
            headers: dict[str, str] | None = None,
        ) -> None:
            super().__init__(value)
            self.status = status
            self.headers = headers or {}

    def urlopen(request: object, *, timeout: int) -> BytesIO:
        nonlocal calls
        calls += 1
        assert timeout == 30
        output_inodes.append(destination.stat().st_ino)
        if calls == 1:
            assert request == "https://files.pythonhosted.org/locked.tar.gz"
            return Response(payload[:-1], status=200)
        assert request.get_header("Range") == f"bytes={len(payload) - 1}-{len(payload) - 1}"
        return Response(
            payload[-1:],
            status=206,
            headers={
                "Content-Length": "1",
                "Content-Range": f"bytes {len(payload) - 1}-{len(payload) - 1}/{len(payload)}",
            },
        )

    monkeypatch.setattr(builder, "urlopen", urlopen)

    builder._download_locked_file(
        "https://files.pythonhosted.org/locked.tar.gz",
        destination,
        expected_digest=hashlib.sha256(payload).hexdigest(),
        expected_size=len(payload),
    )

    assert calls == 2
    assert len(set(output_inodes)) == 1
    assert destination.read_bytes() == payload


def test_locked_download_fails_closed_after_bounded_identity_mismatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    payload = b"locked-pyinstaller-source"
    mismatched_payload = payload[:-1] + b"!"
    calls = 0

    def urlopen(_url: str, *, timeout: int) -> BytesIO:
        nonlocal calls
        calls += 1
        assert timeout == 30
        return BytesIO(mismatched_payload)

    monkeypatch.setattr(builder, "urlopen", urlopen)
    destination = tmp_path / "pyinstaller.tar.gz"

    with pytest.raises(RuntimeError, match="does not match its locked identity"):
        builder._download_locked_file(
            "https://files.pythonhosted.org/locked.tar.gz",
            destination,
            expected_digest=hashlib.sha256(payload).hexdigest(),
            expected_size=len(payload),
        )

    assert calls == builder._LOCKED_DOWNLOAD_ATTEMPTS
    assert destination.read_bytes() == b""


def test_core_release_inputs_publish_one_complete_directory(tmp_path: Path) -> None:
    builder = _load_builder()
    wheel, lock = _write_export_inputs(builder, tmp_path)
    output = tmp_path / "release-inputs"

    builder._publish_core_release_inputs_once(output, wheel, lock)

    assert sorted(path.name for path in output.iterdir()) == sorted(
        (wheel.name, builder.CORE_FRAMEWORK_LOCK_BASENAME)
    )
    assert (output / wheel.name).read_bytes() == wheel.read_bytes()
    assert (output / builder.CORE_FRAMEWORK_LOCK_BASENAME).read_bytes() == lock.read_bytes()
    assert not list(tmp_path.glob(".release-inputs.staging-*"))

    with pytest.raises(RuntimeError, match="must not already exist"):
        builder._publish_core_release_inputs_once(output, wheel, lock)


def test_core_release_publish_failure_never_creates_authoritative_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    wheel, lock = _write_export_inputs(builder, tmp_path)
    output = tmp_path / "release-inputs"

    def reject_publish(*_args: object) -> None:
        raise OSError(errno.EIO, "injected publish failure")

    monkeypatch.setattr(builder, "_rename_noreplace", reject_publish)

    with pytest.raises(RuntimeError, match="could not be published atomically"):
        builder._publish_core_release_inputs_once(output, wheel, lock)

    assert not output.exists()
    staging = list(tmp_path.glob(".release-inputs.staging-*"))
    assert len(staging) == 1
    assert sorted(path.name for path in staging[0].iterdir()) == sorted(
        (wheel.name, builder.CORE_FRAMEWORK_LOCK_BASENAME)
    )


def test_sidecar_binary_publish_atomically_replaces_existing_target(
    tmp_path: Path,
) -> None:
    builder = _load_builder()
    built = tmp_path / "dist" / "openevo-desktop-sidecar"
    built.parent.mkdir()
    built.write_bytes(b"new-sidecar")
    built.chmod(0o755)
    target = tmp_path / "binaries" / "openevo-desktop-sidecar-test"
    target.parent.mkdir()
    target.write_bytes(b"old-sidecar")

    builder._publish_sidecar_binary(built, target)

    assert target.read_bytes() == b"new-sidecar"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert not list(target.parent.glob(f".{target.name}.staging-*"))


def test_sidecar_binary_publish_failure_preserves_existing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    built = tmp_path / "dist" / "openevo-desktop-sidecar"
    built.parent.mkdir()
    built.write_bytes(b"new-sidecar")
    built.chmod(0o755)
    target = tmp_path / "binaries" / "openevo-desktop-sidecar-test"
    target.parent.mkdir()
    target.write_bytes(b"old-sidecar")

    def reject_publish(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EIO, "injected sidecar publish failure")

    monkeypatch.setattr(builder.os, "replace", reject_publish)

    with pytest.raises(RuntimeError, match="could not be published atomically"):
        builder._publish_sidecar_binary(built, target)

    assert target.read_bytes() == b"old-sidecar"
    staging = list(target.parent.glob(f".{target.name}.staging-*"))
    assert len(staging) == 1
    assert staging[0].read_bytes() == b"new-sidecar"
    assert stat.S_IMODE(staging[0].stat().st_mode) == 0o755


def test_build_sidecar_rejects_existing_wheel_output_without_deleting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    repo = tmp_path / "repo"
    _write_repo_skeleton(repo)
    output = tmp_path / "wheel-output"
    output.mkdir()
    existing = output / "openevo-old.whl"
    existing.write_bytes(b"existing")
    monkeypatch.setattr(builder, "_repo_root", lambda: repo)

    with pytest.raises(RuntimeError, match="must not already exist"):
        builder.build_sidecar(clean=True, core_wheel_output_dir=output)

    assert existing.read_bytes() == b"existing"


def test_build_sidecar_rejects_symlink_wheel_output_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    repo = tmp_path / "repo"
    _write_repo_skeleton(repo)
    real_output = tmp_path / "real-output"
    real_output.mkdir()
    linked_output = tmp_path / "linked-output"
    linked_output.symlink_to(real_output, target_is_directory=True)
    monkeypatch.setattr(builder, "_repo_root", lambda: repo)

    with pytest.raises(RuntimeError, match="symbolic link"):
        builder.build_sidecar(clean=True, core_wheel_output_dir=linked_output)

    assert list(real_output.iterdir()) == []


def test_internal_snapshot_allows_only_explicit_darwin_system_path_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    path = Path("/var/folders/openevo")
    original_is_symlink = Path.is_symlink

    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda candidate: candidate == Path("/var") or original_is_symlink(candidate),
    )
    monkeypatch.setattr(
        builder,
        "_is_darwin_system_path_alias",
        lambda candidate: candidate == Path("/var"),
    )

    builder._reject_symlink_path(path, allow_darwin_system_aliases=True)
    with pytest.raises(RuntimeError, match="symbolic link"):
        builder._reject_symlink_path(path)


def test_darwin_system_path_alias_requires_exact_private_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    monkeypatch.setattr(builder.sys, "platform", "darwin")
    original_resolve = Path.resolve
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda candidate, strict=False: (
            Path("/private/var")
            if candidate == Path("/var")
            else original_resolve(candidate, strict=strict)
        ),
    )

    assert builder._is_darwin_system_path_alias(Path("/var")) is True
    assert builder._is_darwin_system_path_alias(Path("/usr")) is False

    monkeypatch.setattr(
        Path,
        "resolve",
        lambda candidate, strict=False: (
            Path("/attacker/var")
            if candidate == Path("/var")
            else original_resolve(candidate, strict=strict)
        ),
    )
    assert builder._is_darwin_system_path_alias(Path("/var")) is False


def test_temporary_directory_cleanup_failure_keeps_complete_published_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    repo = tmp_path / "repo"
    _write_repo_skeleton(repo)
    output = tmp_path / "output"

    class FailingTemporaryDirectory(builder.TemporaryDirectory):
        def __exit__(self, exc_type, exc_value, traceback):
            super().__exit__(exc_type, exc_value, traceback)
            raise OSError("injected TemporaryDirectory cleanup failure")

    def fake_core_wheel(_repo: Path, build_root: Path) -> Path:
        wheel = build_root / "openevo-0.1.0-py3-none-any.whl"
        wheel.parent.mkdir(parents=True)
        wheel.write_bytes(b"wheel")
        return wheel

    def fake_pyinstaller(command, **kwargs):
        del kwargs
        dist = Path(command[command.index("--distpath") + 1])
        dist.mkdir(parents=True, exist_ok=True)
        (dist / builder.SIDECAR_NAME).write_bytes(b"sidecar")
        return subprocess.CompletedProcess(command, 0)

    def fake_askpass(
        _tauri_root: Path,
        *,
        cargo_target: Path,
        target_triple: str,
    ) -> Path:
        helper = cargo_target / target_triple / "release" / builder.ASKPASS_NAME
        _write_thin_mach_o(helper)
        return helper

    monkeypatch.setattr(builder, "TemporaryDirectory", FailingTemporaryDirectory)
    monkeypatch.setattr(builder, "_repo_root", lambda: repo)
    monkeypatch.setattr(builder, "_target_triple", lambda: "aarch64-apple-darwin")
    monkeypatch.setattr(builder, "_build_core_wheel", fake_core_wheel)
    monkeypatch.setattr(builder, "_build_native_askpass_helper", fake_askpass)
    monkeypatch.setattr(builder, "_build_product_web", lambda _: "0" * 64)
    monkeypatch.setattr(builder, "_prepare_fd_bound_pyinstaller", lambda *args: Path(args[1]))
    monkeypatch.setattr(builder.subprocess, "run", fake_pyinstaller)
    monkeypatch.setattr(
        builder,
        "_normalize_unsigned_macos_sidecar_signature",
        lambda _: None,
    )
    monkeypatch.setattr(builder, "_verify_macos_adhoc_signature", lambda _: "adhoc")
    monkeypatch.setattr(builder, "_validate_fd_bound_bootloader", lambda _: None)
    monkeypatch.setattr(
        builder, "_validate_sidecar_excludes_remote_release_assets", lambda *_: None
    )
    monkeypatch.setattr(builder, "_validate_embedded_product_web", lambda *args: None)
    monkeypatch.setattr(builder, "_validate_embedded_release_contract", lambda *args: None)

    with pytest.raises(OSError, match="TemporaryDirectory cleanup failure"):
        builder.build_sidecar(clean=True, core_wheel_output_dir=output)

    assert sorted(path.name for path in output.iterdir()) == [
        "framework-lock.json",
        "openevo-0.1.0-py3-none-any.whl",
    ]


@pytest.mark.parametrize(
    "relative_output",
    [
        Path("desktop/packaging/sidecar-dist/export"),
        Path("desktop/packaging"),
    ],
)
def test_build_sidecar_rejects_output_overlapping_generated_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_output: Path,
) -> None:
    builder = _load_builder()
    repo = tmp_path / "repo"
    _write_repo_skeleton(repo)
    monkeypatch.setattr(builder, "_repo_root", lambda: repo)

    with pytest.raises(RuntimeError, match="overlaps generated paths"):
        builder.build_sidecar(
            clean=True,
            core_wheel_output_dir=repo / relative_output,
        )


@pytest.mark.parametrize("clean", [False, True])
def test_build_sidecar_keeps_clean_semantics_for_owned_build_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean: bool,
) -> None:
    builder = _load_builder()
    repo = tmp_path / "repo"
    _write_repo_skeleton(repo)
    dist_marker = repo / "desktop/packaging/sidecar-dist/marker"
    work_marker = repo / "desktop/packaging/sidecar-build/marker"
    dist_marker.parent.mkdir()
    work_marker.parent.mkdir()
    dist_marker.write_text("dist", encoding="utf-8")
    work_marker.write_text("work", encoding="utf-8")
    monkeypatch.setattr(builder, "_repo_root", lambda: repo)
    monkeypatch.setattr(builder, "_target_triple", lambda: "test-target")
    monkeypatch.setattr(
        builder,
        "_build_core_wheel",
        lambda *_: (_ for _ in ()).throw(RuntimeError("stop after cleanup")),
    )

    with pytest.raises(RuntimeError, match="stop after cleanup"):
        builder.build_sidecar(clean=clean)

    assert dist_marker.exists() is not clean
    assert work_marker.exists() is not clean


def test_sidecar_archive_rejects_missing_or_extra_core_wheels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    executable = tmp_path / "sidecar"
    executable.write_bytes(b"sidecar")
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(wheel)
    monkeypatch.setattr(
        builder,
        "_archive_member_names",
        lambda _: (
            "openevo/wheels/openevo-0.1.0-py3-none-any.whl",
            "openevo/wheels/openevo-0.0.9-py3-none-any.whl",
        ),
    )

    with pytest.raises(RuntimeError, match="exact staged Core wheel"):
        builder._validate_embedded_core_wheel(
            executable,
            wheel,
        )


def test_sidecar_archive_rejects_terminal_bench_automation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    executable = tmp_path / "sidecar"
    executable.write_bytes(b"sidecar")
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(wheel)
    monkeypatch.setattr(
        builder,
        "_archive_member_names",
        lambda _: (
            "openevo/wheels/openevo-0.1.0-py3-none-any.whl",
            "openevo_terminal_bench/cli.py",
        ),
    )

    with pytest.raises(RuntimeError, match="Terminal Bench automation"):
        builder._validate_embedded_core_wheel(executable, wheel)


def test_sidecar_archive_rejects_removed_terminal_bench_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    executable = tmp_path / "sidecar"
    executable.write_bytes(b"sidecar")
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(wheel)
    monkeypatch.setattr(
        builder,
        "_archive_member_names",
        lambda _: (
            "openevo/wheels/openevo-0.1.0-py3-none-any.whl",
            "openevo.evolution.terminal_bench_local_parametric",
        ),
    )

    with pytest.raises(RuntimeError, match="removed Terminal Bench Core modules"):
        builder._validate_embedded_core_wheel(executable, wheel)


def test_sidecar_archive_rejects_tampered_embedded_core_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    executable = tmp_path / "sidecar"
    executable.write_bytes(b"sidecar")
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(wheel)
    member = "openevo/wheels/openevo-0.1.0-py3-none-any.whl"
    tampered = BytesIO()
    with ZipFile(tampered, "w") as archive:
        archive.writestr("openevo-0.1.0.dist-info/METADATA", "")
        archive.writestr(
            "openevo/evolution/terminal_bench_task_local_parametric.py",
            b"",
        )
    monkeypatch.setattr(builder, "_archive_member_names", lambda _: (member,))
    monkeypatch.setattr(builder, "_archive_member_bytes", lambda *_: tampered.getvalue())

    with pytest.raises(RuntimeError, match="removed Terminal Bench Core modules"):
        builder._validate_embedded_core_wheel(executable, wheel)


def test_sidecar_archive_rejects_embedded_core_wheel_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    executable = tmp_path / "sidecar"
    executable.write_bytes(b"sidecar")
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(wheel)
    member = "openevo/wheels/openevo-0.1.0-py3-none-any.whl"
    tampered = BytesIO()
    with ZipFile(tampered, "w") as archive:
        archive.writestr(
            "openevo-0.1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: openevo\nVersion: 0.1.0\n",
        )
        archive.writestr("openevo/unexpected.py", b"")
    monkeypatch.setattr(builder, "_archive_member_names", lambda _: (member,))
    monkeypatch.setattr(builder, "_archive_member_bytes", lambda *_: tampered.getvalue())

    with pytest.raises(RuntimeError, match="digest does not match"):
        builder._validate_embedded_core_wheel(executable, wheel)


def test_sidecar_archive_rejects_core_snapshot_tampered_then_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    executable = tmp_path / "sidecar"
    executable.write_bytes(b"sidecar")
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(wheel)
    wheel.chmod(0o600)
    source = builder._open_core_release_input(wheel, name=wheel.name)
    original = wheel.read_bytes()
    tampered = BytesIO()
    with ZipFile(tampered, "w") as archive:
        archive.writestr(
            "openevo-0.1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: openevo\nVersion: 0.1.0\n",
        )
        archive.writestr("openevo/tampered.py", b"tampered")
    tampered_payload = tampered.getvalue()
    member = f"openevo/wheels/{wheel.name}"
    monkeypatch.setattr(builder, "_archive_member_names", lambda _: (member,))
    monkeypatch.setattr(
        builder,
        "_archive_member_bytes",
        lambda *_: tampered_payload,
    )

    try:
        wheel.write_bytes(tampered_payload)
        wheel.write_bytes(original)
        builder._verify_core_release_input(source)
        with pytest.raises(RuntimeError, match="digest does not match"):
            builder._validate_embedded_core_wheel(executable, source)
    finally:
        source.close()


def test_sidecar_archive_requires_exact_wheel_bound_framework_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    executable = tmp_path / "sidecar"
    executable.write_bytes(b"sidecar")
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(wheel)
    framework_lock = builder._write_core_framework_lock(wheel, version="0.1.0")
    wheel_member = "openevo/wheels/openevo-0.1.0-py3-none-any.whl"
    lock_member = "openevo/wheels/framework-lock.json"
    payloads = {
        wheel_member: wheel.read_bytes(),
        lock_member: framework_lock.read_bytes(),
    }
    monkeypatch.setattr(builder, "_archive_member_names", lambda _: tuple(payloads))
    monkeypatch.setattr(builder, "_archive_member_bytes", lambda _, name: payloads[name])

    builder._validate_embedded_core_framework_lock(
        executable,
        wheel,
        framework_lock,
        version="0.1.0",
    )

    payloads[lock_member] = payloads[lock_member].replace(b'"0.1.0"', b'"9.9.9"')
    with pytest.raises(RuntimeError, match="framework lock differs"):
        builder._validate_embedded_core_framework_lock(
            executable,
            wheel,
            framework_lock,
            version="0.1.0",
        )


@pytest.mark.parametrize(
    "members",
    [
        ("openevo/wheels/openevo-0.1.0-py3-none-any.whl",),
        (
            "openevo/wheels/openevo-0.1.0-py3-none-any.whl",
            "openevo/wheels/framework-lock.json",
            "openevo/wheels/stale.json",
        ),
        (
            "openevo/wheels/openevo-0.1.0-py3-none-any.whl",
            "openevo/wheels/framework-lock.json",
            "openevo/wheels/../escaped.json",
        ),
    ],
)
def test_sidecar_archive_rejects_non_closed_core_release_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    members: tuple[str, ...],
) -> None:
    builder = _load_builder()
    executable = tmp_path / "sidecar"
    executable.write_bytes(b"sidecar")
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(wheel)
    framework_lock = builder._write_core_framework_lock(wheel, version="0.1.0")
    monkeypatch.setattr(builder, "_archive_member_names", lambda _: members)

    with pytest.raises(RuntimeError, match="exact Core release inputs"):
        builder._validate_embedded_core_framework_lock(
            executable,
            wheel,
            framework_lock,
            version="0.1.0",
        )


def test_sidecar_archive_rejects_framework_lock_for_another_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    executable = tmp_path / "sidecar"
    executable.write_bytes(b"sidecar")
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    other_wheel = tmp_path / "other" / wheel.name
    _write_core_wheel(wheel)
    _write_core_wheel(other_wheel)
    with ZipFile(other_wheel, "a") as archive:
        archive.writestr("openevo/other.py", b"different")
    framework_lock = builder._write_core_framework_lock(other_wheel, version="0.1.0")
    wheel_member = "openevo/wheels/openevo-0.1.0-py3-none-any.whl"
    lock_member = "openevo/wheels/framework-lock.json"
    payloads = {
        wheel_member: wheel.read_bytes(),
        lock_member: framework_lock.read_bytes(),
    }
    monkeypatch.setattr(builder, "_archive_member_names", lambda _: tuple(payloads))
    monkeypatch.setattr(builder, "_archive_member_bytes", lambda _, name: payloads[name])

    with pytest.raises(RuntimeError, match="exact built wheel|identity is invalid"):
        builder._validate_embedded_core_framework_lock(
            executable,
            wheel,
            framework_lock,
            version="0.1.0",
        )


def test_sidecar_archive_rejects_duplicate_core_release_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    executable = tmp_path / "sidecar"
    executable.write_bytes(b"sidecar")
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(wheel)
    framework_lock = builder._write_core_framework_lock(wheel, version="0.1.0")
    wheel_member = "openevo/wheels/openevo-0.1.0-py3-none-any.whl"
    monkeypatch.setattr(
        builder,
        "_archive_member_names",
        lambda _: (
            wheel_member,
            wheel_member,
            "openevo/wheels/framework-lock.json",
        ),
    )

    with pytest.raises(RuntimeError, match="exact Core release inputs"):
        builder._validate_embedded_core_framework_lock(
            executable,
            wheel,
            framework_lock,
            version="0.1.0",
        )


def test_raw_carchive_parser_rejects_duplicate_toc_members(tmp_path: Path) -> None:
    builder = _load_builder()
    name = "openevo/wheels/framework-lock.json"
    toc_entry_format = "!IIIIBc"
    toc_entry_length = struct.calcsize(toc_entry_format)

    def entry() -> bytes:
        encoded = name.encode("utf-8") + b"\0"
        return (
            struct.pack(
                toc_entry_format,
                toc_entry_length + len(encoded),
                0,
                0,
                0,
                0,
                b"x",
            )
            + encoded
        )

    toc = entry() + entry()
    cookie_format = "!8sIIII64s"
    cookie_magic = b"MEI\x0c\x0b\n\x0b\x0e"
    cookie_length = struct.calcsize(cookie_format)
    cookie = struct.pack(
        cookie_format,
        cookie_magic,
        len(toc) + cookie_length,
        0,
        len(toc),
        311,
        b"python".ljust(64, b"\0"),
    )
    executable = tmp_path / "sidecar"
    executable.write_bytes(toc + cookie)

    class FakeArchive:
        _COOKIE_LENGTH = cookie_length
        _COOKIE_FORMAT = cookie_format
        _TOC_ENTRY_LENGTH = toc_entry_length
        _TOC_ENTRY_FORMAT = toc_entry_format
        _COOKIE_MAGIC_PATTERN = cookie_magic
        toc = {name: object()}

        @staticmethod
        def _find_magic_pattern(stream, pattern) -> int:
            del stream, pattern
            return len(toc)

    with pytest.raises(RuntimeError, match="duplicate members"):
        builder._raw_carchive_member_names(FakeArchive(), executable)
