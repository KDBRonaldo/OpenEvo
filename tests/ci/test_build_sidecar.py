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
import sys
import time
from types import ModuleType, SimpleNamespace
from zipfile import ZipFile

import pytest


_FD_BOUND_REMOVAL_TESTKIT_SOURCE = """
if not module._core_release_fd_removal_supported():
    def prepare_test_fd_bound_removal(object_fd, *, is_directory):
        del object_fd, is_directory
        return None

    def execute_test_fd_bound_removal(
        token,
        parent_fd,
        name,
        object_fd,
        *,
        is_directory,
    ):
        del token
        held = os.fstat(object_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
            raise RuntimeError("test conditional removal preserved a replacement")
        if is_directory:
            os.rmdir(name, dir_fd=parent_fd)
        else:
            os.unlink(name, dir_fd=parent_fd)

    module._prepare_core_release_fd_removal = prepare_test_fd_bound_removal
    module._execute_core_release_fd_removal = execute_test_fd_bound_removal
    module._core_release_fd_removal_supported = lambda: True
"""


def _install_fd_bound_removal_testkit(module: ModuleType) -> None:
    if module._core_release_fd_removal_supported():
        return

    def prepare_test_fd_bound_removal(
        object_fd: int,
        *,
        is_directory: bool,
    ) -> None:
        del object_fd, is_directory

    def execute_test_fd_bound_removal(
        token: object,
        parent_fd: int,
        name: str,
        object_fd: int,
        *,
        is_directory: bool,
    ) -> None:
        del token
        held = os.fstat(object_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
            raise RuntimeError("test conditional removal preserved a replacement")
        if is_directory:
            os.rmdir(name, dir_fd=parent_fd)
        else:
            os.unlink(name, dir_fd=parent_fd)

    module._prepare_core_release_fd_removal = prepare_test_fd_bound_removal
    module._execute_core_release_fd_removal = execute_test_fd_bound_removal
    module._core_release_fd_removal_supported = lambda: True


def _load_builder(*, install_fd_removal_testkit: bool = True) -> ModuleType:
    path = Path("desktop/packaging/build_sidecar.py").resolve()
    spec = importlib.util.spec_from_file_location("openevo_build_sidecar", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if install_fd_removal_testkit:
        _install_fd_bound_removal_testkit(module)
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


def _run_crashing_core_export(
    *,
    builder_path: Path,
    output: Path,
    wheel: Path,
    lock: Path,
    mode: str,
    cleanup_name: str = "",
    stage_window: str = "",
) -> subprocess.CompletedProcess[bytes]:
    script = f"""
import importlib.util
import os
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location("crash_build_sidecar", {str(builder_path)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
{_FD_BOUND_REMOVAL_TESTKIT_SOURCE}

mode = sys.argv[1]
cleanup_name = sys.argv[2]
stage_window = sys.argv[3]

def crash_after_publish(authority, member):
    del authority, member
    if mode == "publish":
        os._exit(73)

def crash_during_cleanup(authority, source):
    del authority
    if mode in {{"recovery", "rollback"}} and source.name == cleanup_name:
        os._exit(74)

def crash_during_stage(authority, source, window):
    del authority
    if mode == "stage" and source.name == cleanup_name and window == stage_window:
        os._exit(75)

def crash_during_marker(authority, payload, window):
    del authority
    marker = module._decode_marker(payload)
    bound_count = sum("inode" in member for member in marker.get("members", []))
    if (
        mode == "marker"
        and marker.get("phase") == "preparing"
        and bound_count == int(cleanup_name)
        and window == stage_window
    ):
        os._exit(76)

def crash_during_tombstone(authority, window):
    del authority
    if mode == "tombstone" and window == stage_window:
        os._exit(77)

module._after_core_release_member_published = crash_after_publish
module._after_core_release_member_cleaned = crash_during_cleanup
module._after_core_release_stage_window = crash_during_stage
module._after_core_release_marker_window = crash_during_marker
module._after_core_release_tombstone_window = crash_during_tombstone
with module._open_core_release_output(Path({str(output)!r})) as authority:
    module._export_core_release_inputs(
        authority,
        Path({str(wheel)!r}),
        Path({str(lock)!r}),
    )
    if mode in {{"rollback", "tombstone"}}:
        raise OSError("trigger rollback")
"""
    return subprocess.run(
        [sys.executable, "-c", script, mode, cleanup_name, stage_window],
        check=False,
    )


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
    wheel_output.mkdir()
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

    builder._patch_fd_bound_bootloader(source_root)

    patched = source.read_text(encoding="utf-8")
    patched_utils = utils_source.read_text(encoding="utf-8")
    patched_header = utils_header.read_text(encoding="utf-8")
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
    assert "pyi_utils_openevo_native_handoff_restore()" in patched_utils
    assert "pyi_utils_openevo_native_handoff_prepare" in patched_header


@pytest.mark.parametrize(
    ("platform", "platform_markers"),
    [
        ("linux", (b"/proc/self/fd/4",)),
        ("darwin", (b"/dev/fd/4", b"openevo-desktop-sidecar")),
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


def test_build_sidecar_rejects_nonempty_wheel_output_without_deleting_it(
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

    with pytest.raises(RuntimeError, match="must be empty"):
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


@pytest.mark.parametrize("mode", [0o720, 0o702, 0o777])
def test_core_release_output_rejects_group_or_world_writable_directory(
    tmp_path: Path,
    mode: int,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    output.mkdir(mode=mode)
    output.chmod(mode)

    with pytest.raises(RuntimeError, match="owner, or private permissions"):
        with builder._open_core_release_output(output):
            pass


def test_core_release_output_rejects_directory_not_owned_by_current_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    actual_euid = os.geteuid()
    monkeypatch.setattr(builder.os, "geteuid", lambda: actual_euid + 1)

    with pytest.raises(RuntimeError, match="owner, or private permissions"):
        with builder._open_core_release_output(output):
            pass


def test_core_release_output_is_created_private_and_stays_pinned(tmp_path: Path) -> None:
    builder = _load_builder()
    output = tmp_path / "output"

    with pytest.raises(RuntimeError, match="owner, or private permissions"):
        with builder._open_core_release_output(output):
            assert stat.S_IMODE(output.stat().st_mode) == 0o700
            output.chmod(0o777)

    assert list(output.iterdir()) == []


def test_concurrent_builder_cannot_recover_a_live_transaction(tmp_path: Path) -> None:
    builder_path = Path("desktop/packaging/build_sidecar.py").resolve()
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    ready = tmp_path / "builder-a-ready"
    release = tmp_path / "release-builder-a"
    script = f"""
import importlib.util
import os
from pathlib import Path
import time

spec = importlib.util.spec_from_file_location("concurrent_build_sidecar", {str(builder_path)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
{_FD_BOUND_REMOVAL_TESTKIT_SOURCE}
ready = Path({str(ready)!r})
release = Path({str(release)!r})

def pause_live_transaction(authority, member):
    del authority
    if member.source.name != {wheel.name!r} or ready.exists():
        return
    ready.write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + 20
    while not release.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("concurrency test release timed out")
        time.sleep(0.02)

module._after_core_release_member_published = pause_live_transaction
with module._open_core_release_output(Path({str(output)!r})) as authority:
    module._export_core_release_inputs(
        authority,
        Path({str(wheel)!r}),
        Path({str(lock)!r}),
    )
"""
    first = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and first.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.is_file()
        assert first.poll() is None
        transactions = list(output.glob(".openevo-core-release-*"))
        assert len(transactions) == 1

        with pytest.raises(RuntimeError, match="locked by another active sidecar builder"):
            with builder._open_core_release_output(output):
                raise AssertionError("contending builder entered the output context")

        assert first.poll() is None
        assert list(output.glob(".openevo-core-release-*")) == transactions
        release.write_text("release", encoding="utf-8")
        assert first.wait(timeout=10) == 0
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)

    assert sorted(path.name for path in output.iterdir()) == sorted((wheel.name, lock.name))
    assert not list(tmp_path.glob(".openevo-core-release-tombstone-*"))
    assert not list(tmp_path.glob(".openevo-core-release-purge-*"))


@pytest.mark.parametrize("mode", [0o720, 0o702, 0o777])
def test_core_release_output_rejects_unsafe_parent_before_creation(
    tmp_path: Path,
    mode: int,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    tmp_path.chmod(mode)

    with pytest.raises(RuntimeError, match="parent owner, or private permissions"):
        with builder._open_core_release_output(output):
            pass

    assert not output.exists()


def test_core_release_output_rejects_unowned_parent_before_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    actual_euid = os.geteuid()
    monkeypatch.setattr(builder.os, "geteuid", lambda: actual_euid + 1)

    with pytest.raises(RuntimeError, match="parent owner, or private permissions"):
        with builder._open_core_release_output(output):
            pass

    assert not output.exists()


def test_core_release_output_rejects_parent_mutating_acl_before_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    deleted: list[int] = []
    monkeypatch.setattr(builder.sys, "platform", "darwin")
    monkeypatch.setattr(
        builder,
        "_darwin_extended_acl_entries",
        lambda _: ((builder._DARWIN_ACL_EXTENDED_ALLOW, 1 << 6),),
    )
    monkeypatch.setattr(builder, "_delete_darwin_extended_acl", deleted.append)

    with pytest.raises(RuntimeError, match="parent owner, or private permissions"):
        with builder._open_core_release_output(output):
            pass

    assert not output.exists()
    assert deleted == []


def test_core_release_output_clears_inherited_acl_before_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    parent_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)
    output_reads = 0
    deleted: list[int] = []

    def acl_entries(file_fd: int) -> tuple[tuple[int, int], ...]:
        nonlocal output_reads
        descriptor = os.fstat(file_fd)
        if (descriptor.st_dev, descriptor.st_ino) == parent_identity:
            return ()
        output_reads += 1
        if output_reads == 1:
            return ((builder._DARWIN_ACL_EXTENDED_DENY, 1 << 6),)
        return ()

    monkeypatch.setattr(builder.sys, "platform", "darwin")
    monkeypatch.setattr(builder, "_darwin_extended_acl_entries", acl_entries)
    monkeypatch.setattr(builder, "_delete_darwin_extended_acl", deleted.append)

    with builder._open_core_release_output(output):
        assert stat.S_IMODE(output.stat().st_mode) == 0o700

    assert len(deleted) == 1
    assert output_reads >= 3


def test_core_release_output_rejects_acl_added_after_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    output.mkdir(mode=0o700)
    output_identity = (output.stat().st_dev, output.stat().st_ino)
    deleted: list[int] = []

    def acl_entries(file_fd: int) -> tuple[tuple[int, int], ...]:
        descriptor = os.fstat(file_fd)
        if (descriptor.st_dev, descriptor.st_ino) == output_identity:
            return ((builder._DARWIN_ACL_EXTENDED_ALLOW, 1 << 6),)
        return ()

    monkeypatch.setattr(builder.sys, "platform", "darwin")
    monkeypatch.setattr(builder, "_darwin_extended_acl_entries", acl_entries)
    monkeypatch.setattr(builder, "_delete_darwin_extended_acl", deleted.append)

    with pytest.raises(RuntimeError, match="permits mutation"):
        with builder._open_core_release_output(output):
            pass

    assert deleted == []


class _FakeDarwinAclFunction:
    def __init__(self, result: object) -> None:
        self.result = result
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *_args: object) -> object:
        return self.result


def _fake_darwin_acl_libc() -> SimpleNamespace:
    return SimpleNamespace(
        acl_get_fd_np=_FakeDarwinAclFunction(None),
        acl_get_entry=_FakeDarwinAclFunction(0),
        acl_get_tag_type=_FakeDarwinAclFunction(0),
        acl_get_permset_mask_np=_FakeDarwinAclFunction(0),
        acl_free=_FakeDarwinAclFunction(0),
    )


def test_macos_fd_acl_treats_enoent_as_no_extended_acl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    monkeypatch.setattr(builder.sys, "platform", "darwin")
    monkeypatch.setattr(builder.ctypes, "CDLL", lambda *_args, **_kwargs: _fake_darwin_acl_libc())
    monkeypatch.setattr(builder.ctypes, "get_errno", lambda: errno.ENOENT)

    assert builder._darwin_extended_acl_entries(41) == ()


@pytest.mark.parametrize("error", [0, errno.EBADF, errno.EIO])
def test_macos_fd_acl_rejects_non_enoent_lookup_failure(
    monkeypatch: pytest.MonkeyPatch,
    error: int,
) -> None:
    builder = _load_builder()
    monkeypatch.setattr(builder.sys, "platform", "darwin")
    monkeypatch.setattr(builder.ctypes, "CDLL", lambda *_args, **_kwargs: _fake_darwin_acl_libc())
    monkeypatch.setattr(builder.ctypes, "get_errno", lambda: error)

    with pytest.raises(RuntimeError, match=rf"errno {error}$"):
        builder._darwin_extended_acl_entries(41)


@pytest.mark.parametrize(
    "entries",
    [
        ((1, 1 << 6),),
        ((1, (1 << 2) | (1 << 4)),),
        ((2, (1 << 2) | (1 << 6)),),
    ],
)
def test_macos_fd_acl_is_cleared_and_reverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entries: tuple[tuple[int, int], ...],
) -> None:
    builder = _load_builder()
    path = tmp_path / "acl-member"
    path.write_bytes(b"member")
    snapshots = iter((entries, ()))
    deleted: list[int] = []
    monkeypatch.setattr(builder.sys, "platform", "darwin")
    monkeypatch.setattr(builder, "_darwin_extended_acl_entries", lambda _: next(snapshots))
    monkeypatch.setattr(builder, "_delete_darwin_extended_acl", deleted.append)
    file_fd = os.open(path, os.O_RDONLY)
    try:
        builder._clear_and_verify_fd_acl(file_fd, name=path.name)
    finally:
        os.close(file_fd)

    assert len(deleted) == 1


@pytest.mark.parametrize("entries", [((99, 0),), ((1, 1 << 63),)])
def test_macos_fd_acl_rejects_unknown_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entries: tuple[tuple[int, int], ...],
) -> None:
    builder = _load_builder()
    path = tmp_path / "acl-member"
    path.write_bytes(b"member")
    deleted: list[int] = []
    monkeypatch.setattr(builder.sys, "platform", "darwin")
    monkeypatch.setattr(builder, "_darwin_extended_acl_entries", lambda _: entries)
    monkeypatch.setattr(builder, "_delete_darwin_extended_acl", deleted.append)
    file_fd = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(RuntimeError, match="unknown"):
            builder._clear_and_verify_fd_acl(file_fd, name=path.name)
    finally:
        os.close(file_fd)

    assert deleted == []


def test_macos_fd_acl_rejects_mutation_after_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    path = tmp_path / "acl-member"
    path.write_bytes(b"member")
    monkeypatch.setattr(builder.sys, "platform", "darwin")
    monkeypatch.setattr(
        builder,
        "_darwin_extended_acl_entries",
        lambda _: ((builder._DARWIN_ACL_EXTENDED_ALLOW, 1 << 6),),
    )
    file_fd = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(RuntimeError, match="permits mutation"):
            builder._require_fd_acl_free(file_fd, name=path.name)
    finally:
        os.close(file_fd)


@pytest.mark.parametrize("kind", ["marker", "member"])
def test_runtime_acl_injection_is_rejected_without_acl_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    builder = _load_builder()
    path = tmp_path / kind
    payload = b"member"
    path.write_bytes(payload)
    path.chmod(0o600 if kind == "marker" else 0o644)
    deleted: list[int] = []
    monkeypatch.setattr(builder.sys, "platform", "darwin")
    monkeypatch.setattr(
        builder,
        "_darwin_extended_acl_entries",
        lambda _: ((builder._DARWIN_ACL_EXTENDED_ALLOW, 1 << 6),),
    )
    monkeypatch.setattr(builder, "_delete_darwin_extended_acl", deleted.append)
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="permits mutation"):
            if kind == "marker":
                builder._read_marker(directory_fd, path.name)
            else:
                descriptor = path.stat()
                source = builder._CoreReleaseSource(
                    path=path,
                    name=path.name,
                    file_fd=-1,
                    device=descriptor.st_dev,
                    inode=descriptor.st_ino,
                    byte_size=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
                builder._verify_member_path(directory_fd, source)
    finally:
        os.close(directory_fd)

    assert deleted == []


@pytest.mark.skipif(sys.platform != "darwin", reason="requires real macOS extended ACL APIs")
def test_macos_real_output_creation_clears_inherited_acl(tmp_path: Path) -> None:
    builder = _load_builder()
    parent = tmp_path / "acl-parent"
    parent.mkdir(mode=0o700)
    subprocess.run(
        [
            "chmod",
            "+a",
            "everyone allow read,file_inherit,directory_inherit",
            str(parent),
        ],
        check=True,
    )
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert builder._darwin_extended_acl_entries(parent_fd)
    finally:
        os.close(parent_fd)

    output = parent / "output"
    with builder._open_core_release_output(output) as authority:
        assert builder._darwin_extended_acl_entries(authority.directory_fd) == ()


def test_bounded_directory_scan_stops_at_limit_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    yielded = 0

    class Entry:
        name = "entry"

    class Entries:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal yielded
            yielded += 1
            if yielded > 5:
                raise AssertionError("enumeration continued past limit + 1")
            return Entry()

    monkeypatch.setattr(builder.os, "scandir", lambda _: Entries())

    with pytest.raises(RuntimeError, match="too many entries"):
        builder._bounded_directory_scan(-1, limit=4, container="test directory")

    assert yielded == 5


def test_bounded_directory_scan_rejects_large_real_directory(tmp_path: Path) -> None:
    builder = _load_builder()
    directory = tmp_path / "large"
    directory.mkdir()
    for index in range(2_000):
        (directory / f"entry-{index:04d}").touch()
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="too many entries"):
            builder._bounded_listdir(directory_fd, limit=4, container="test directory")
    finally:
        os.close(directory_fd)


def test_bounded_directory_scan_rejects_concurrent_fill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    directory = tmp_path / "concurrent"
    directory.mkdir()
    original_scan = builder._bounded_directory_scan
    calls = 0

    def scan_and_fill(directory_fd: int, *, limit: int, container: str):
        nonlocal calls
        names = original_scan(directory_fd, limit=limit, container=container)
        calls += 1
        if calls == 1:
            (directory / "late-entry").touch()
        return names

    monkeypatch.setattr(builder, "_bounded_directory_scan", scan_and_fill)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(RuntimeError, match="changed while it was enumerated"):
            builder._bounded_listdir(directory_fd, limit=4, container="test directory")
    finally:
        os.close(directory_fd)


def test_core_release_export_stays_bound_to_original_output_directory(
    tmp_path: Path,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    wheel, lock = _write_export_inputs(builder, tmp_path)

    original = tmp_path / "original-output"
    with pytest.raises(RuntimeError, match="changed during the sidecar build"):
        with builder._open_core_release_output(output) as authority:
            output.rename(original)
            output.symlink_to(redirected, target_is_directory=True)
            builder._export_core_release_inputs(authority, wheel, lock)

    assert list(original.iterdir()) == []
    assert list(redirected.iterdir()) == []


def test_core_release_export_removes_partial_file_after_copy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)

    def fail_after_partial_copy(source, destination) -> None:
        del source
        destination.write(b"partial")
        destination.flush()
        raise OSError("injected copy failure")

    monkeypatch.setattr(builder.shutil, "copyfileobj", fail_after_partial_copy)

    with pytest.raises(OSError, match="injected copy failure"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)

    assert list(output.iterdir()) == []


def test_core_release_export_rolls_back_wheel_when_lock_copy_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    original_copy = builder._copy_core_release_member

    def fail_lock(authority, source):
        if source.name == builder.CORE_FRAMEWORK_LOCK_BASENAME:
            raise OSError("injected lock copy failure")
        return original_copy(authority, source)

    monkeypatch.setattr(builder, "_copy_core_release_member", fail_lock)

    with pytest.raises(OSError, match="injected lock copy failure"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)

    assert list(output.iterdir()) == []


def test_core_release_output_rolls_back_pair_when_later_build_step_fails(
    tmp_path: Path,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)

    with pytest.raises(OSError, match="injected later build failure"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)
            raise OSError("injected later build failure")

    assert list(output.iterdir()) == []


def test_core_release_output_rolls_back_pair_when_path_changes_after_export(
    tmp_path: Path,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    wheel, lock = _write_export_inputs(builder, tmp_path)
    original = tmp_path / "original-output"

    with pytest.raises(RuntimeError, match="changed during the sidecar build"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)
            output.rename(original)
            output.symlink_to(redirected, target_is_directory=True)

    assert list(original.iterdir()) == []
    assert list(redirected.iterdir()) == []


def test_core_release_commit_verifies_exact_member_contract(tmp_path: Path) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)

    with builder._open_core_release_output(output) as authority:
        builder._export_core_release_inputs(authority, wheel, lock)

    assert sorted(path.name for path in output.iterdir()) == sorted(
        (wheel.name, builder.CORE_FRAMEWORK_LOCK_BASENAME)
    )
    output_descriptor = output.stat()
    assert output_descriptor.st_uid == os.geteuid()
    assert output_descriptor.st_mode & (stat.S_IWGRP | stat.S_IWOTH) == 0
    for source, exported in ((wheel, output / wheel.name), (lock, output / lock.name)):
        descriptor = exported.stat()
        assert descriptor.st_nlink == 1
        assert descriptor.st_uid == os.geteuid()
        assert descriptor.st_mode & 0o777 == 0o644
        assert exported.read_bytes() == source.read_bytes()
        assert (
            hashlib.sha256(exported.read_bytes()).digest()
            == hashlib.sha256(source.read_bytes()).digest()
        )


@pytest.mark.parametrize("fault", ["unlink", "rename", "extra"])
def test_core_release_member_path_faults_fail_before_commit_and_roll_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    injected = False

    def fault_after_wheel(authority, member) -> None:
        nonlocal injected
        if injected or member.source.name != wheel.name:
            return
        injected = True
        if fault == "unlink":
            os.unlink(wheel.name, dir_fd=authority.directory_fd)
        elif fault == "rename":
            os.rename(
                wheel.name,
                "renamed-wheel.whl",
                src_dir_fd=authority.directory_fd,
                dst_dir_fd=authority.directory_fd,
            )
        else:
            extra_fd = os.open(
                "unexpected.txt",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=authority.directory_fd,
            )
            os.close(extra_fd)

    monkeypatch.setattr(builder, "_after_core_release_member_published", fault_after_wheel)

    expected = "rollback could not be verified" if fault == "extra" else "inventory changed"
    with pytest.raises(RuntimeError, match=expected):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)

    if fault == "extra":
        assert (output / "unexpected.txt").read_bytes() == b""
        assert len(list(output.glob(".openevo-core-release-*"))) == 1
    else:
        assert list(output.iterdir()) == []


def test_core_release_same_name_replacement_is_preserved_and_blocks_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    replacement = b"unowned replacement"
    injected = False

    def replace_after_wheel(authority, member) -> None:
        nonlocal injected
        if injected or member.source.name != wheel.name:
            return
        injected = True
        os.unlink(wheel.name, dir_fd=authority.directory_fd)
        replacement_fd = os.open(
            wheel.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
            dir_fd=authority.directory_fd,
        )
        try:
            os.write(replacement_fd, replacement)
            os.fsync(replacement_fd)
        finally:
            os.close(replacement_fd)

    monkeypatch.setattr(builder, "_after_core_release_member_published", replace_after_wheel)

    with pytest.raises(RuntimeError, match="rollback could not be verified"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)

    assert (output / wheel.name).read_bytes() == replacement
    assert len(list(output.glob(".openevo-core-release-*"))) == 1

    with pytest.raises(RuntimeError, match="identity or permissions changed|content changed"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)

    assert (output / wheel.name).read_bytes() == replacement
    assert len(list(output.glob(".openevo-core-release-*"))) == 1


def test_cleanup_name_race_quarantines_replacement_without_deleting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    replacement = b"replacement created after cleanup verification"
    injected = False

    def replace_verified_name(directory_fd: int, name: str) -> None:
        nonlocal injected
        if injected or name != wheel.name:
            return
        injected = True
        os.rename(
            name,
            "preserved-authorized-wheel",
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        replacement_fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
            dir_fd=directory_fd,
        )
        try:
            os.write(replacement_fd, replacement)
            os.fsync(replacement_fd)
        finally:
            os.close(replacement_fd)

    monkeypatch.setattr(
        builder,
        "_after_core_release_cleanup_identity_verified",
        replace_verified_name,
    )

    with pytest.raises(
        RuntimeError, match="replacement.*preserved|rollback could not be verified"
    ):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)
            raise OSError("trigger inode-bound rollback")

    assert (output / "preserved-authorized-wheel").read_bytes() == wheel.read_bytes()
    preserved_payloads = [
        path.read_bytes()
        for transaction in output.glob(".openevo-core-release-*")
        for path in transaction.iterdir()
        if path.is_file()
    ]
    assert replacement in preserved_payloads


def test_transaction_name_race_preserves_replacement_tombstone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    injected = False

    def replace_verified_transaction(directory_fd: int, name: str) -> None:
        nonlocal injected
        if injected or builder._CORE_RELEASE_TRANSACTION_PATTERN.fullmatch(name) is None:
            return
        injected = True
        os.rename(
            name,
            ".preserved-authorized-transaction",
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.mkdir(name, 0o700, dir_fd=directory_fd)

    monkeypatch.setattr(
        builder,
        "_after_core_release_cleanup_identity_verified",
        replace_verified_transaction,
    )

    with pytest.raises(RuntimeError, match="rollback could not be verified"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)

    assert (output / ".preserved-authorized-transaction").is_dir()
    tombstones = list(tmp_path.glob(".openevo-core-release-tombstone-*"))
    assert len(tombstones) == 1
    assert tombstones[0].is_dir()
    assert list(tombstones[0].iterdir()) == []


def test_marker_name_race_preserves_checked_and_replacement_inodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    replacement = b"replacement marker after identity check"
    original_payload: bytes | None = None
    injected = False

    def replace_verified_marker(authority, payload: bytes, identity: tuple[int, int]) -> None:
        nonlocal injected, original_payload
        del payload, identity
        if injected:
            return
        injected = True
        marker_name = builder.CORE_RELEASE_TRANSACTION_MARKER
        marker_fd = os.open(marker_name, os.O_RDONLY, dir_fd=authority.transaction_fd)
        try:
            original_payload = os.read(marker_fd, builder._MAX_CORE_RELEASE_MARKER_BYTES)
        finally:
            os.close(marker_fd)
        os.rename(
            marker_name,
            "preserved-checked-marker",
            src_dir_fd=authority.transaction_fd,
            dst_dir_fd=authority.transaction_fd,
        )
        replacement_fd = os.open(
            marker_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=authority.transaction_fd,
        )
        try:
            os.write(replacement_fd, replacement)
            os.fsync(replacement_fd)
        finally:
            os.close(replacement_fd)

    monkeypatch.setattr(
        builder,
        "_after_core_release_marker_identity_verified",
        replace_verified_marker,
    )

    with pytest.raises(RuntimeError, match="rollback could not be verified"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)

    transactions = list(output.glob(".openevo-core-release-*"))
    assert len(transactions) == 1
    assert (transactions[0] / "preserved-checked-marker").read_bytes() == original_payload
    assert replacement in [
        path.read_bytes() for path in transactions[0].iterdir() if path.is_file()
    ]


def test_tombstone_entry_replacement_is_preserved_without_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    replacement = b"replacement cleanup entry"
    injected = False

    def replace_quarantined_entry(authority, window: str) -> None:
        nonlocal injected
        if injected or window != "entry-quarantined":
            return
        injected = True
        purge_names = [
            name
            for name in os.listdir(authority.transaction_fd)
            if builder._core_release_entry_purge_identity(name) is not None
        ]
        assert len(purge_names) == 1
        purge_name = purge_names[0]
        os.rename(
            purge_name,
            "preserved-cleanup-entry",
            src_dir_fd=authority.transaction_fd,
            dst_dir_fd=authority.transaction_fd,
        )
        replacement_fd = os.open(
            purge_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=authority.transaction_fd,
        )
        try:
            os.write(replacement_fd, replacement)
            os.fsync(replacement_fd)
        finally:
            os.close(replacement_fd)

    monkeypatch.setattr(
        builder,
        "_after_core_release_tombstone_window",
        replace_quarantined_entry,
    )

    with pytest.raises(RuntimeError, match="rollback could not be verified"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)
            raise OSError("trigger cleanup")

    tombstones = list(tmp_path.glob(".openevo-core-release-tombstone-*"))
    assert len(tombstones) == 1
    assert (tombstones[0] / "preserved-cleanup-entry").is_file()
    assert replacement in [path.read_bytes() for path in tombstones[0].iterdir() if path.is_file()]


def test_tombstone_directory_replacement_is_preserved_without_rmdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    injected = False

    def replace_quarantined_directory(authority, window: str) -> None:
        nonlocal injected
        if injected or window != "directory-quarantined":
            return
        injected = True
        purge_name = builder._core_release_directory_purge_name(authority)
        os.rename(
            purge_name,
            ".preserved-cleanup-directory",
            src_dir_fd=authority.parent_fd,
            dst_dir_fd=authority.parent_fd,
        )
        os.mkdir(purge_name, 0o700, dir_fd=authority.parent_fd)
        replacement_directory_fd = os.open(
            purge_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=authority.parent_fd,
        )
        try:
            replacement_fd = os.open(
                "replacement",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=replacement_directory_fd,
            )
            os.close(replacement_fd)
        finally:
            os.close(replacement_directory_fd)

    monkeypatch.setattr(
        builder,
        "_after_core_release_tombstone_window",
        replace_quarantined_directory,
    )

    with pytest.raises(RuntimeError, match="rollback could not be verified"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)
            raise OSError("trigger cleanup")

    assert (tmp_path / ".preserved-cleanup-directory").is_dir()
    purge_directories = list(tmp_path.glob(".openevo-core-release-purge-*"))
    assert len(purge_directories) == 1
    assert (purge_directories[0] / "replacement").is_file()


def test_tombstone_entry_syscall_boundary_preserves_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    replacement = b"entry replacement at the removal syscall"
    preserved_name = "preserved-syscall-entry"
    injected = False

    def replace_at_removal_boundary(
        parent_fd: int,
        name: str,
        object_fd: int,
        *,
        is_directory: bool,
    ) -> None:
        nonlocal injected
        del object_fd
        if injected or builder._core_release_entry_purge_identity(name) is None:
            return
        assert not is_directory
        injected = True
        os.rename(name, preserved_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        replacement_fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            os.write(replacement_fd, replacement)
            os.fsync(replacement_fd)
        finally:
            os.close(replacement_fd)

    monkeypatch.setattr(
        builder,
        "_before_core_release_fd_removal",
        replace_at_removal_boundary,
    )

    with pytest.raises(RuntimeError, match="preserved|rollback could not be verified"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)

    assert injected
    tombstone = next(tmp_path.glob(".openevo-core-release-tombstone-*"))
    replacement_names = [
        name
        for name in os.listdir(tombstone)
        if builder._core_release_entry_purge_identity(name) is not None
    ]
    assert len(replacement_names) == 1
    assert (tombstone / replacement_names[0]).read_bytes() == replacement


def test_tombstone_directory_syscall_boundary_preserves_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    preserved_name = ".preserved-syscall-directory"
    injected = False

    def replace_at_removal_boundary(
        parent_fd: int,
        name: str,
        object_fd: int,
        *,
        is_directory: bool,
    ) -> None:
        nonlocal injected
        del object_fd
        if injected or not name.startswith(".openevo-core-release-purge-"):
            return
        assert is_directory
        injected = True
        os.rename(name, preserved_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.mkdir(name, 0o700, dir_fd=parent_fd)

    monkeypatch.setattr(
        builder,
        "_before_core_release_fd_removal",
        replace_at_removal_boundary,
    )

    with pytest.raises(RuntimeError, match="preserved|rollback could not be verified"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)

    assert injected
    replacements = list(tmp_path.glob(".openevo-core-release-purge-*"))
    assert len(replacements) == 1
    assert replacements[0].is_dir()
    assert list(replacements[0].iterdir()) == []


def test_cleanup_receipt_syscall_boundary_preserves_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    replacement = b"receipt replacement at the removal syscall"
    preserved_name = ".preserved-syscall-receipt"
    injected = False

    def replace_at_removal_boundary(
        parent_fd: int,
        name: str,
        object_fd: int,
        *,
        is_directory: bool,
    ) -> None:
        nonlocal injected
        del object_fd
        if (
            injected
            or builder._CORE_RELEASE_CLEANUP_AUTHORITY_PURGE_PATTERN.fullmatch(name) is None
        ):
            return
        assert not is_directory
        injected = True
        os.rename(name, preserved_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        replacement_fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            os.write(replacement_fd, replacement)
            os.fsync(replacement_fd)
        finally:
            os.close(replacement_fd)

    monkeypatch.setattr(
        builder,
        "_before_core_release_fd_removal",
        replace_at_removal_boundary,
    )

    with pytest.raises(RuntimeError, match="preserved|rollback could not be verified"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)

    assert injected
    replacement_names = [
        name
        for name in os.listdir(output)
        if builder._CORE_RELEASE_CLEANUP_AUTHORITY_PURGE_PATTERN.fullmatch(name) is not None
    ]
    assert len(replacement_names) == 1
    assert (output / replacement_names[0]).read_bytes() == replacement


def test_native_fd_removal_is_fail_closed_when_platform_has_no_safe_primitive(
    tmp_path: Path,
) -> None:
    builder = _load_builder(install_fd_removal_testkit=False)
    if builder._core_release_fd_removal_supported():
        pytest.skip("platform provides the native identity-bound remover")
    target = tmp_path / "preserved"
    target.write_bytes(b"preserve on unsupported platform")
    target.chmod(0o600)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    target_fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    descriptor = os.fstat(target_fd)
    try:
        with pytest.raises(RuntimeError, match="cannot safely remove an inode"):
            builder._remove_core_release_fd_bound_entry(
                parent_fd,
                target.name,
                target_fd,
                identity=(descriptor.st_dev, descriptor.st_ino),
                is_directory=False,
                subject="test cleanup object",
            )
    finally:
        os.close(target_fd)
        os.close(parent_fd)

    assert target.read_bytes() == b"preserve on unsupported platform"


def test_core_wheel_export_rejects_unsupported_platform_before_output_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder = _load_builder(install_fd_removal_testkit=False)
    output = tmp_path / "output"
    monkeypatch.setattr(builder, "_core_release_fd_removal_supported", lambda: False)

    with pytest.raises(RuntimeError, match="platform is unsupported"):
        builder.build_sidecar(clean=True, core_wheel_output_dir=output)

    assert not output.exists()


def test_successful_transaction_removes_tombstone_state(tmp_path: Path) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)

    for _ in range(2):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)

    assert sorted(path.name for path in output.iterdir()) == sorted((wheel.name, lock.name))
    assert not list(tmp_path.glob(".openevo-core-release-tombstone-*"))
    assert not list(tmp_path.glob(".openevo-core-release-purge-*"))


def test_twenty_successful_retries_do_not_accumulate_release_state(tmp_path: Path) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    expected_names = sorted((wheel.name, lock.name))
    expected_bytes = wheel.stat().st_size + lock.stat().st_size

    for _ in range(20):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)
        exported = list(output.iterdir())
        assert sorted(path.name for path in exported) == expected_names
        assert sum(path.stat().st_size for path in exported) == expected_bytes
        assert not list(tmp_path.glob(".openevo-core-release-tombstone-*"))
        assert not list(tmp_path.glob(".openevo-core-release-purge-*"))


@pytest.mark.parametrize(
    "tombstone_window",
    [
        "transaction-retired",
        "entry-quarantined",
        "entry-cleared",
        "entry-removed",
        "tombstone-empty",
        "directory-quarantined",
    ],
)
def test_repeated_tombstone_crashes_recover_without_growth(
    tmp_path: Path,
    tombstone_window: str,
) -> None:
    builder_path = Path("desktop/packaging/build_sidecar.py").resolve()
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)

    for _ in range(3):
        crashed = _run_crashing_core_export(
            builder_path=builder_path,
            output=output,
            wheel=wheel,
            lock=lock,
            mode="tombstone",
            stage_window=tombstone_window,
        )
        assert crashed.returncode == 77
        sibling_state = [
            *tmp_path.glob(".openevo-core-release-tombstone-*"),
            *tmp_path.glob(".openevo-core-release-purge-*"),
        ]
        assert len(sibling_state) == 1

    with builder._open_core_release_output(output) as authority:
        builder._export_core_release_inputs(authority, wheel, lock)

    assert sorted(path.name for path in output.iterdir()) == sorted((wheel.name, lock.name))
    assert not list(tmp_path.glob(".openevo-core-release-tombstone-*"))
    assert not list(tmp_path.glob(".openevo-core-release-purge-*"))


@pytest.mark.parametrize(
    "tombstone_window",
    ["cleanup-authority-candidate", "cleanup-authority-published"],
)
def test_repeated_pre_retirement_authority_crashes_recover_without_growth(
    tmp_path: Path,
    tombstone_window: str,
) -> None:
    builder_path = Path("desktop/packaging/build_sidecar.py").resolve()
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)

    for _ in range(3):
        crashed = _run_crashing_core_export(
            builder_path=builder_path,
            output=output,
            wheel=wheel,
            lock=lock,
            mode="tombstone",
            stage_window=tombstone_window,
        )
        assert crashed.returncode == 77
        assert len(list(output.glob(".openevo-core-release-*"))) == 2
        assert len(list(output.glob(".openevo-core-release-cleanup*"))) == 1
        assert (
            len(
                [
                    path
                    for path in output.iterdir()
                    if builder._CORE_RELEASE_TRANSACTION_PATTERN.fullmatch(path.name)
                ]
            )
            == 1
        )
        assert not list(tmp_path.glob(".openevo-core-release-tombstone-*"))
        assert not list(tmp_path.glob(".openevo-core-release-purge-*"))

    with builder._open_core_release_output(output) as authority:
        builder._export_core_release_inputs(authority, wheel, lock)

    assert sorted(path.name for path in output.iterdir()) == sorted((wheel.name, lock.name))
    assert not list(output.glob(".openevo-core-release-cleanup*"))


@pytest.mark.parametrize(
    "tombstone_window",
    ["directory-removed", "cleanup-authority-quarantined"],
)
def test_repeated_cleanup_authority_crashes_recover_without_growth(
    tmp_path: Path,
    tombstone_window: str,
) -> None:
    builder_path = Path("desktop/packaging/build_sidecar.py").resolve()
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)

    for _ in range(3):
        crashed = _run_crashing_core_export(
            builder_path=builder_path,
            output=output,
            wheel=wheel,
            lock=lock,
            mode="tombstone",
            stage_window=tombstone_window,
        )
        assert crashed.returncode == 77
        assert not list(tmp_path.glob(".openevo-core-release-tombstone-*"))
        assert not list(tmp_path.glob(".openevo-core-release-purge-*"))
        assert len(list(output.glob(".openevo-core-release-cleanup*"))) == 1

    with builder._open_core_release_output(output) as authority:
        builder._export_core_release_inputs(authority, wheel, lock)

    assert sorted(path.name for path in output.iterdir()) == sorted((wheel.name, lock.name))
    assert not list(output.glob(".openevo-core-release-cleanup*"))


@pytest.mark.parametrize(
    ("tombstone_window", "sibling_pattern"),
    [
        ("tombstone-empty", ".openevo-core-release-tombstone-*"),
        ("directory-quarantined", ".openevo-core-release-purge-*"),
    ],
)
def test_restart_preserves_cleanup_directory_replacement_and_original(
    tmp_path: Path,
    tombstone_window: str,
    sibling_pattern: str,
) -> None:
    builder_path = Path("desktop/packaging/build_sidecar.py").resolve()
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)

    crashed = _run_crashing_core_export(
        builder_path=builder_path,
        output=output,
        wheel=wheel,
        lock=lock,
        mode="tombstone",
        stage_window=tombstone_window,
    )
    assert crashed.returncode == 77
    cleanup_paths = list(tmp_path.glob(sibling_pattern))
    assert len(cleanup_paths) == 1
    cleanup_path = cleanup_paths[0]
    original_identity = (cleanup_path.stat().st_dev, cleanup_path.stat().st_ino)
    preserved = tmp_path / f".preserved-{tombstone_window}"
    cleanup_path.rename(preserved)
    cleanup_path.mkdir(mode=0o700)
    replacement_identity = (cleanup_path.stat().st_dev, cleanup_path.stat().st_ino)
    assert replacement_identity != original_identity

    with pytest.raises(RuntimeError, match="cleanup.*replacement.*preserved"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)

    assert cleanup_path.is_dir()
    assert (cleanup_path.stat().st_dev, cleanup_path.stat().st_ino) == replacement_identity
    assert preserved.is_dir()
    assert (preserved.stat().st_dev, preserved.stat().st_ino) == original_identity


@pytest.mark.parametrize(
    ("tombstone_window", "sibling_pattern"),
    [
        ("tombstone-empty", ".openevo-core-release-tombstone-*"),
        ("directory-quarantined", ".openevo-core-release-purge-*"),
    ],
)
def test_restart_detects_renamed_cleanup_directory_without_replacement(
    tmp_path: Path,
    tombstone_window: str,
    sibling_pattern: str,
) -> None:
    builder_path = Path("desktop/packaging/build_sidecar.py").resolve()
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    crashed = _run_crashing_core_export(
        builder_path=builder_path,
        output=output,
        wheel=wheel,
        lock=lock,
        mode="tombstone",
        stage_window=tombstone_window,
    )
    assert crashed.returncode == 77
    cleanup_path = next(tmp_path.glob(sibling_pattern))
    original_identity = (cleanup_path.stat().st_dev, cleanup_path.stat().st_ino)
    preserved = tmp_path / f".preserved-only-{tombstone_window}"
    cleanup_path.rename(preserved)

    with pytest.raises(RuntimeError, match="cleanup directory was renamed and preserved"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)

    assert preserved.is_dir()
    assert (preserved.stat().st_dev, preserved.stat().st_ino) == original_identity


def test_restart_rejects_cleanup_authority_same_name_replacement(tmp_path: Path) -> None:
    builder_path = Path("desktop/packaging/build_sidecar.py").resolve()
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    crashed = _run_crashing_core_export(
        builder_path=builder_path,
        output=output,
        wheel=wheel,
        lock=lock,
        mode="tombstone",
        stage_window="tombstone-empty",
    )
    assert crashed.returncode == 77
    authority_path = next(output.glob(".openevo-core-release-cleanup-*"))
    authority_payload = authority_path.read_bytes()
    original_identity = (authority_path.stat().st_dev, authority_path.stat().st_ino)
    preserved = tmp_path / ".preserved-cleanup-authority"
    authority_path.rename(preserved)
    authority_path.write_bytes(authority_payload)
    authority_path.chmod(0o600)
    replacement_identity = (authority_path.stat().st_dev, authority_path.stat().st_ino)
    assert replacement_identity != original_identity

    with pytest.raises(RuntimeError, match="authority filename identity changed"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)

    assert authority_path.read_bytes() == authority_payload
    assert (authority_path.stat().st_dev, authority_path.stat().st_ino) == replacement_identity
    assert preserved.read_bytes() == authority_payload
    assert (preserved.stat().st_dev, preserved.stat().st_ino) == original_identity


def test_cleanup_recovery_parent_identity_scan_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_path = Path("desktop/packaging/build_sidecar.py").resolve()
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    crashed = _run_crashing_core_export(
        builder_path=builder_path,
        output=output,
        wheel=wheel,
        lock=lock,
        mode="tombstone",
        stage_window="tombstone-empty",
    )
    assert crashed.returncode == 77
    cleanup_path = next(tmp_path.glob(".openevo-core-release-tombstone-*"))
    preserved = tmp_path / ".preserved-bounded-cleanup"
    cleanup_path.rename(preserved)
    monkeypatch.setattr(builder, "_MAX_CORE_RELEASE_PARENT_RECOVERY_MEMBERS", 1)

    with pytest.raises(RuntimeError, match="output parent contains too many entries"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)

    assert preserved.is_dir()


def test_core_release_crash_between_names_is_reconciled_on_retry(tmp_path: Path) -> None:
    builder_path = Path("desktop/packaging/build_sidecar.py").resolve()
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    script = f"""
import importlib.util
import os
from pathlib import Path

spec = importlib.util.spec_from_file_location("crash_build_sidecar", {str(builder_path)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def crash_after_first_member(authority, member):
    del authority, member
    os._exit(73)

module._after_core_release_member_published = crash_after_first_member
with module._open_core_release_output(Path({str(output)!r})) as authority:
    module._export_core_release_inputs(
        authority,
        Path({str(wheel)!r}),
        Path({str(lock)!r}),
    )
"""

    crashed = subprocess.run([sys.executable, "-c", script], check=False)

    assert crashed.returncode == 73
    assert (output / wheel.name).read_bytes() == wheel.read_bytes()
    assert not (output / lock.name).exists()
    assert len(list(output.glob(".openevo-core-release-*"))) == 1

    with builder._open_core_release_output(output) as authority:
        builder._export_core_release_inputs(authority, wheel, lock)

    assert sorted(path.name for path in output.iterdir()) == sorted((wheel.name, lock.name))
    assert (output / wheel.name).read_bytes() == wheel.read_bytes()
    assert (output / lock.name).read_bytes() == lock.read_bytes()


@pytest.mark.parametrize(
    ("cleanup_member", "cleanup_index"),
    (("wheel", 1), ("lock", 2)),
)
def test_core_release_recovery_resumes_after_second_cleanup_crash(
    tmp_path: Path,
    cleanup_member: str,
    cleanup_index: int,
) -> None:
    builder_path = Path("desktop/packaging/build_sidecar.py").resolve()
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    cleanup_name = wheel.name if cleanup_member == "wheel" else lock.name

    first = _run_crashing_core_export(
        builder_path=builder_path,
        output=output,
        wheel=wheel,
        lock=lock,
        mode="publish",
    )
    assert first.returncode == 73

    second = _run_crashing_core_export(
        builder_path=builder_path,
        output=output,
        wheel=wheel,
        lock=lock,
        mode="recovery",
        cleanup_name=cleanup_name,
    )
    assert second.returncode == 74
    transactions = list(output.glob(".openevo-core-release-*"))
    assert len(transactions) == 1
    marker = json.loads(
        (transactions[0] / builder.CORE_RELEASE_TRANSACTION_MARKER).read_text(encoding="utf-8")
    )
    staging_names = {member["name"]: member["staging_name"] for member in marker["members"]}
    assert marker["phase"] == "cleaning"
    assert marker["cleanup_index"] == cleanup_index
    assert not (output / wheel.name).exists()
    assert not (transactions[0] / staging_names[wheel.name]).exists()
    if cleanup_member == "wheel":
        assert (transactions[0] / staging_names[lock.name]).read_bytes() == lock.read_bytes()
    else:
        assert not (output / lock.name).exists()
        assert (transactions[0] / staging_names[lock.name]).read_bytes() == lock.read_bytes()

    with builder._open_core_release_output(output) as authority:
        builder._export_core_release_inputs(authority, wheel, lock)

    assert sorted(path.name for path in output.iterdir()) == sorted((wheel.name, lock.name))
    assert (output / wheel.name).read_bytes() == wheel.read_bytes()
    assert (output / lock.name).read_bytes() == lock.read_bytes()


def test_core_release_recovery_adopts_fsynced_cleanup_marker_candidate(
    tmp_path: Path,
) -> None:
    builder_path = Path("desktop/packaging/build_sidecar.py").resolve()
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)

    first = _run_crashing_core_export(
        builder_path=builder_path,
        output=output,
        wheel=wheel,
        lock=lock,
        mode="publish",
    )
    assert first.returncode == 73
    transactions = list(output.glob(".openevo-core-release-*"))
    assert len(transactions) == 1
    transaction = transactions[0]
    marker = json.loads(
        (transaction / builder.CORE_RELEASE_TRANSACTION_MARKER).read_text(encoding="utf-8")
    )
    marker["phase"] = "cleaning"
    marker["cleanup_index"] = 1
    candidate_payload = (json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n").encode()
    candidate_fd = os.open(
        transaction / builder.CORE_RELEASE_TRANSACTION_READY,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        assert os.write(candidate_fd, candidate_payload) == len(candidate_payload)
        os.fsync(candidate_fd)
    finally:
        os.close(candidate_fd)
    transaction_fd = os.open(transaction, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(transaction_fd)
    finally:
        os.close(transaction_fd)

    with builder._open_core_release_output(output) as authority:
        builder._export_core_release_inputs(authority, wheel, lock)

    assert sorted(path.name for path in output.iterdir()) == sorted((wheel.name, lock.name))
    assert (output / wheel.name).read_bytes() == wheel.read_bytes()
    assert (output / lock.name).read_bytes() == lock.read_bytes()


def test_core_release_cleaning_recovery_rejects_unbound_hardlink(tmp_path: Path) -> None:
    builder_path = Path("desktop/packaging/build_sidecar.py").resolve()
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)

    first = _run_crashing_core_export(
        builder_path=builder_path,
        output=output,
        wheel=wheel,
        lock=lock,
        mode="publish",
    )
    assert first.returncode == 73
    second = _run_crashing_core_export(
        builder_path=builder_path,
        output=output,
        wheel=wheel,
        lock=lock,
        mode="recovery",
        cleanup_name=wheel.name,
    )
    assert second.returncode == 74
    transactions = list(output.glob(".openevo-core-release-*"))
    assert len(transactions) == 1
    marker = json.loads(
        (transactions[0] / builder.CORE_RELEASE_TRANSACTION_MARKER).read_text(encoding="utf-8")
    )
    lock_staging_name = next(
        member["staging_name"] for member in marker["members"] if member["name"] == lock.name
    )
    hidden_lock = tmp_path / "hidden-framework-lock.json"
    os.link(transactions[0] / lock_staging_name, hidden_lock)

    with pytest.raises(RuntimeError, match="identity or permissions changed"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)

    assert hidden_lock.read_bytes() == lock.read_bytes()
    assert (transactions[0] / lock_staging_name).read_bytes() == lock.read_bytes()
    marker = json.loads(
        (transactions[0] / builder.CORE_RELEASE_TRANSACTION_MARKER).read_text(encoding="utf-8")
    )
    assert marker["phase"] == "cleaning"
    assert marker["cleanup_index"] == 1


@pytest.mark.parametrize(
    ("cleanup_member", "cleanup_index"),
    (("wheel", 1), ("lock", 2)),
)
def test_core_release_rollback_resumes_after_cleanup_crash(
    tmp_path: Path,
    cleanup_member: str,
    cleanup_index: int,
) -> None:
    builder_path = Path("desktop/packaging/build_sidecar.py").resolve()
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    cleanup_name = wheel.name if cleanup_member == "wheel" else lock.name

    crashed = _run_crashing_core_export(
        builder_path=builder_path,
        output=output,
        wheel=wheel,
        lock=lock,
        mode="rollback",
        cleanup_name=cleanup_name,
    )

    assert crashed.returncode == 74
    transactions = list(output.glob(".openevo-core-release-*"))
    assert len(transactions) == 1
    marker = json.loads(
        (transactions[0] / builder.CORE_RELEASE_TRANSACTION_MARKER).read_text(encoding="utf-8")
    )
    assert marker["phase"] == "cleaning"
    assert marker["cleanup_index"] == cleanup_index

    with builder._open_core_release_output(output) as authority:
        builder._export_core_release_inputs(authority, wheel, lock)

    assert sorted(path.name for path in output.iterdir()) == sorted((wheel.name, lock.name))
    assert (output / wheel.name).read_bytes() == wheel.read_bytes()
    assert (output / lock.name).read_bytes() == lock.read_bytes()


@pytest.mark.parametrize("marker_payload", [None, b'{"schema_version":'])
def test_core_release_bootstrap_crash_is_reconciled_before_publication(
    tmp_path: Path,
    marker_payload: bytes | None,
) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    output.mkdir()
    transaction = output / f".openevo-core-release-{'a' * 32}"
    transaction.mkdir(mode=0o700)
    if marker_payload is not None:
        marker = transaction / builder.CORE_RELEASE_TRANSACTION_MARKER
        marker.write_bytes(marker_payload)
        marker.chmod(0o600)
    wheel, lock = _write_export_inputs(builder, tmp_path)

    with builder._open_core_release_output(output) as authority:
        builder._export_core_release_inputs(authority, wheel, lock)

    assert sorted(path.name for path in output.iterdir()) == sorted((wheel.name, lock.name))


@pytest.mark.parametrize("member", ["wheel", "lock"])
@pytest.mark.parametrize(
    "stage_window",
    [
        "intent-durable",
        "file-created",
        "inode-bound",
        "bytes-fsynced",
        "mode-fsynced",
    ],
)
def test_preparing_member_crash_windows_recover_repeatedly(
    tmp_path: Path,
    member: str,
    stage_window: str,
) -> None:
    builder_path = Path("desktop/packaging/build_sidecar.py").resolve()
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    member_name = wheel.name if member == "wheel" else lock.name

    for _ in range(2):
        crashed = _run_crashing_core_export(
            builder_path=builder_path,
            output=output,
            wheel=wheel,
            lock=lock,
            mode="stage",
            cleanup_name=member_name,
            stage_window=stage_window,
        )
        assert crashed.returncode == 75
        assert len(list(output.glob(".openevo-core-release-*"))) == 1

    with builder._open_core_release_output(output) as authority:
        builder._export_core_release_inputs(authority, wheel, lock)

    assert sorted(path.name for path in output.iterdir()) == sorted((wheel.name, lock.name))


@pytest.mark.parametrize("bound_count", [1, 2])
@pytest.mark.parametrize(
    "marker_window",
    ["candidate-durable", "marker-quarantined", "marker-replaced", "marker-durable"],
)
def test_preparing_marker_crash_windows_recover_repeatedly(
    tmp_path: Path,
    bound_count: int,
    marker_window: str,
) -> None:
    builder_path = Path("desktop/packaging/build_sidecar.py").resolve()
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)

    for _ in range(2):
        crashed = _run_crashing_core_export(
            builder_path=builder_path,
            output=output,
            wheel=wheel,
            lock=lock,
            mode="marker",
            cleanup_name=str(bound_count),
            stage_window=marker_window,
        )
        assert crashed.returncode == 76
        assert len(list(output.glob(".openevo-core-release-*"))) == 1

    with builder._open_core_release_output(output) as authority:
        builder._export_core_release_inputs(authority, wheel, lock)

    assert sorted(path.name for path in output.iterdir()) == sorted((wheel.name, lock.name))


def test_preparing_recovery_preserves_path_outside_durable_member_intents(tmp_path: Path) -> None:
    builder = _load_builder()
    output = tmp_path / "output"
    output.mkdir()
    transaction = output / f".openevo-core-release-{'b' * 32}"
    transaction.mkdir(mode=0o700)
    wheel, lock = _write_export_inputs(builder, tmp_path)
    staging_names = {
        wheel.name: f".member-{'c' * 32}",
        lock.name: f".member-{'d' * 32}",
    }
    marker_payload = {
        "schema_version": "2",
        "phase": "preparing",
        "output_device": output.stat().st_dev,
        "output_inode": output.stat().st_ino,
        "transaction_device": transaction.stat().st_dev,
        "transaction_inode": transaction.stat().st_ino,
        "members": [
            {
                "name": source.name,
                "staging_name": staging_names[source.name],
                "byte_size": source.stat().st_size,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
            for source in (wheel, lock)
        ],
    }
    marker = transaction / builder.CORE_RELEASE_TRANSACTION_MARKER
    marker.write_bytes(
        (json.dumps(marker_payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    marker.chmod(0o600)
    staged = transaction / wheel.name
    staged.write_bytes(b"partial staged bytes")
    staged.chmod(0o600)

    with pytest.raises(RuntimeError, match="unknown transaction member"):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)

    assert staged.read_bytes() == b"partial staged bytes"
    assert marker.exists()


@pytest.mark.parametrize("replacement", ["symlink", "hardlink"])
def test_preparing_recovery_preserves_replaced_unbound_intent(
    tmp_path: Path,
    replacement: str,
) -> None:
    builder_path = Path("desktop/packaging/build_sidecar.py").resolve()
    builder = _load_builder()
    output = tmp_path / "output"
    wheel, lock = _write_export_inputs(builder, tmp_path)
    crashed = _run_crashing_core_export(
        builder_path=builder_path,
        output=output,
        wheel=wheel,
        lock=lock,
        mode="stage",
        cleanup_name=wheel.name,
        stage_window="file-created",
    )
    assert crashed.returncode == 75
    transaction = next(output.glob(".openevo-core-release-*"))
    marker = json.loads(
        (transaction / builder.CORE_RELEASE_TRANSACTION_MARKER).read_text(encoding="utf-8")
    )
    staging_name = next(
        member["staging_name"] for member in marker["members"] if member["name"] == wheel.name
    )
    staged = transaction / staging_name
    staged.unlink()
    attacker_path = tmp_path / "attacker-owned"
    attacker_path.write_bytes(b"do not remove")
    attacker_path.chmod(0o600)
    if replacement == "symlink":
        staged.symlink_to(attacker_path)
    else:
        os.link(attacker_path, staged)

    with pytest.raises((OSError, RuntimeError)):
        with builder._open_core_release_output(output) as authority:
            builder._export_core_release_inputs(authority, wheel, lock)

    assert attacker_path.read_bytes() == b"do not remove"
    assert staged.exists()


def test_temporary_directory_cleanup_failure_rolls_back_release_pair(
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

    assert list(output.iterdir()) == []


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
