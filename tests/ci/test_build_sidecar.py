from __future__ import annotations

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
                    "stdout",
                    "stderr",
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
    (repo / "README.md").write_text("# OpenEvo\n", encoding="utf-8")
    (repo / "LICENSE").write_text("test license\n", encoding="utf-8")
    (repo / "src/openevo").mkdir(parents=True)
    (repo / "src/openevo/__init__.py").write_text("", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "openevo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )


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


def test_core_wheel_and_lock_build_are_reproducible(tmp_path: Path) -> None:
    builder = _load_builder()
    repo = Path.cwd()

    first = builder._build_core_wheel(repo, tmp_path / "first")
    second = builder._build_core_wheel(repo, tmp_path / "second")
    first_lock = builder._core_framework_lock_bytes(first, version="0.1.0")
    second_lock = builder._core_framework_lock_bytes(second, version="0.1.0")

    assert first.read_bytes() == second.read_bytes()
    assert first_lock == second_lock


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
        "stdout",
        "stderr",
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

    assert {"contract_simulator", "scaffold", "dry_run"}.issubset(
        policy["forbidden_text"]
    )
    schemas = Path("desktop/src/api/v1/schemas.ts").read_text(encoding="utf-8")
    assert '["dry", "run"].join("_")' not in schemas


def test_packaged_product_graph_excludes_non_release_provider_code() -> None:
    release_schema = Path(
        "desktop/src/api/v1/providerKinds.release.ts"
    ).read_text(encoding="utf-8")
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


@pytest.mark.parametrize("clean", [False, True])
def test_build_sidecar_uses_isolated_source_and_preserves_repository_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean: bool,
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
    commands: list[str] = []
    embedded_wheel: Path | None = None
    embedded_bytes: bytes | None = None
    embedded_lock: Path | None = None
    embedded_lock_bytes: bytes | None = None

    pyinstaller_root = repo / "fd-bound-pyinstaller"

    def fake_run(command, *, check, cwd, **kwargs):
        nonlocal embedded_bytes, embedded_lock, embedded_lock_bytes, embedded_wheel
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
            core_data = {
                Path(source_value).name: (Path(source_value), destination)
                for source_value, destination in (
                    value.rsplit(os.pathsep, 1) for value in add_data
                )
                if destination == "openevo/wheels"
            }
            embedded_wheel, wheel_destination = core_data["openevo-0.1.0-py3-none-any.whl"]
            embedded_lock, lock_destination = core_data["framework-lock.json"]
            assert embedded_wheel.name == "openevo-0.1.0-py3-none-any.whl"
            assert embedded_wheel.is_file()
            embedded_bytes = embedded_wheel.read_bytes()
            assert embedded_lock.is_file()
            embedded_lock_bytes = embedded_lock.read_bytes()
            assert wheel_destination == lock_destination == "openevo/wheels"
            assert not any(
                command[index : index + 2] == ["--collect-data", "openevo"]
                for index in range(len(command) - 1)
            )
            dist_dir = Path(command[command.index("--distpath") + 1])
            dist_dir.mkdir(parents=True, exist_ok=True)
            (dist_dir / builder.SIDECAR_NAME).write_bytes(b"packaged-sidecar")
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(builder, "_repo_root", lambda: repo)
    monkeypatch.setattr(builder, "_target_triple", lambda: "test-target")
    monkeypatch.setattr(
        builder,
        "_prepare_fd_bound_pyinstaller",
        lambda *_: pyinstaller_root,
    )
    monkeypatch.setattr(builder, "_validate_fd_bound_bootloader", lambda _: None)
    monkeypatch.setattr(builder.subprocess, "run", fake_run)
    monkeypatch.setattr(
        builder,
        "_archive_member_names",
        lambda _: (
            "openevo/wheels/openevo-0.1.0-py3-none-any.whl",
            "openevo/wheels/framework-lock.json",
            *(
                f"desktop/packaging/web/{path.relative_to(repo / 'desktop/packaging/web').as_posix()}"
                for path in sorted((repo / "desktop/packaging/web").rglob("*"))
                if path.is_file()
            ),
        ),
    )
    web_payloads = {
        f"desktop/packaging/web/{path.relative_to(repo / 'desktop/packaging/web').as_posix()}": path.read_bytes()
        for path in (repo / "desktop/packaging/web").rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(
        builder,
        "_archive_member_bytes",
        lambda _, member: (
            embedded_bytes
            if member.endswith(".whl")
            else embedded_lock_bytes
            if member == "openevo/wheels/framework-lock.json"
            else web_payloads[member]
        ),
    )

    wheel_output = repo / ".openevo-remote-wheel"
    target = builder.build_sidecar(
        clean=clean,
        core_wheel_output_dir=wheel_output,
    )

    assert commands == ["build", "product-web", "PyInstaller"]
    assert target == (repo / "desktop/src-tauri/binaries" / "openevo-desktop-sidecar-test-target")
    assert target.read_bytes() == b"packaged-sidecar"
    assert target.stat().st_mode & 0o777 == 0o755
    assert [wheel.name for wheel in wheel_output.glob("*.whl")] == [
        "openevo-0.1.0-py3-none-any.whl"
    ]
    assert next(wheel_output.glob("*.whl")).read_bytes() == embedded_bytes
    assert (wheel_output / "framework-lock.json").read_bytes() == embedded_lock_bytes
    assert (stale_stage / "stale.whl").read_text(encoding="utf-8") == "stale"
    assert (generic_build / "user-output.txt").read_text(encoding="utf-8") == "keep"
    assert embedded_wheel is not None
    assert not embedded_wheel.exists()
    assert embedded_lock is not None
    assert not embedded_lock.exists()


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
        builder._BOOTLOADER_DARWIN_LIB_NEEDLE
        + builder._BOOTLOADER_PROGRAM_LIBS_NEEDLE,
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
    assert (
        "dup2(openevo_listener_guard_fd, OPENEVO_NATIVE_LISTENER_FD)"
        in patched_utils
    )
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
        builder._BOOTLOADER_DARWIN_LIB_NEEDLE
        + builder._BOOTLOADER_PROGRAM_LIBS_NEEDLE,
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

    monkeypatch.setattr(builder, "TemporaryDirectory", FailingTemporaryDirectory)
    monkeypatch.setattr(builder, "_repo_root", lambda: repo)
    monkeypatch.setattr(builder, "_target_triple", lambda: "test-target")
    monkeypatch.setattr(builder, "_build_core_wheel", fake_core_wheel)
    monkeypatch.setattr(builder, "_build_product_web", lambda _: "0" * 64)
    monkeypatch.setattr(builder, "_prepare_fd_bound_pyinstaller", lambda *args: Path(args[1]))
    monkeypatch.setattr(builder.subprocess, "run", fake_pyinstaller)
    monkeypatch.setattr(builder, "_validate_fd_bound_bootloader", lambda _: None)
    monkeypatch.setattr(builder, "_validate_embedded_core_wheel", lambda *_: None)
    monkeypatch.setattr(
        builder, "_validate_embedded_core_framework_lock", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(builder, "_validate_embedded_product_web", lambda *args: None)

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
