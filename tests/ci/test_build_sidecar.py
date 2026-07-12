from __future__ import annotations

import importlib.util
import os
from pathlib import Path
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


def _write_repo_skeleton(repo: Path) -> None:
    (repo / "desktop/packaging/web").mkdir(parents=True)
    (repo / "desktop/packaging/sidecar_entry.py").write_text("", encoding="utf-8")
    (repo / "README.md").write_text("# OpenEvo\n", encoding="utf-8")
    (repo / "LICENSE").write_text("test license\n", encoding="utf-8")
    (repo / "src/openevo").mkdir(parents=True)
    (repo / "src/openevo/__init__.py").write_text("", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "openevo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )


def test_validate_core_wheel_rejects_project_identity_mismatch(tmp_path: Path) -> None:
    builder = _load_builder()
    wheel = tmp_path / "openevo-9.9.9-py3-none-any.whl"
    _write_core_wheel(wheel, version="9.9.9")

    with pytest.raises(RuntimeError, match="does not match pyproject"):
        builder._validate_core_wheel(wheel, name="openevo", version="0.1.0")


def test_validate_core_wheel_rejects_nested_wheel(tmp_path: Path) -> None:
    builder = _load_builder()
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    _write_core_wheel(wheel)
    with ZipFile(wheel, "a") as archive:
        archive.writestr("openevo/wheels/stale.whl", b"stale")

    with pytest.raises(RuntimeError, match="must not contain nested wheels"):
        builder._validate_core_wheel(wheel, name="openevo", version="0.1.0")


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
    (repo / "src/openevo/openevo.egg-info/PKG-INFO").write_text(
        "stale", encoding="utf-8"
    )
    commands: list[str] = []
    embedded_wheel: Path | None = None
    embedded_bytes: bytes | None = None

    def fake_run(command, *, check, cwd, **kwargs):
        nonlocal embedded_bytes, embedded_wheel
        del kwargs
        assert check is True
        if command[2] == "build":
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
            commands.append("PyInstaller")
            assert Path(cwd) == repo
            assert ("--clean" in command) is clean
            add_data = command[command.index("--add-data") + 1]
            source_value, destination = add_data.rsplit(os.pathsep, 1)
            embedded_wheel = Path(source_value)
            assert embedded_wheel.name == "openevo-0.1.0-py3-none-any.whl"
            assert embedded_wheel.is_file()
            embedded_bytes = embedded_wheel.read_bytes()
            assert destination == "openevo/wheels"
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
    monkeypatch.setattr(builder.subprocess, "run", fake_run)
    monkeypatch.setattr(
        builder,
        "_archive_member_names",
        lambda _: ("openevo/wheels/openevo-0.1.0-py3-none-any.whl",),
    )
    monkeypatch.setattr(
        builder,
        "_archive_member_bytes",
        lambda *_: embedded_bytes,
    )

    wheel_output = repo / ".openevo-remote-wheel"
    wheel_output.mkdir()
    target = builder.build_sidecar(
        clean=clean,
        core_wheel_output_dir=wheel_output,
    )

    assert commands == ["build", "PyInstaller"]
    assert target == (
        repo
        / "desktop/src-tauri/binaries"
        / "openevo-desktop-sidecar-test-target"
    )
    assert target.read_bytes() == b"packaged-sidecar"
    assert [wheel.name for wheel in wheel_output.glob("*.whl")] == [
        "openevo-0.1.0-py3-none-any.whl"
    ]
    assert next(wheel_output.glob("*.whl")).read_bytes() == embedded_bytes
    assert (stale_stage / "stale.whl").read_text(encoding="utf-8") == "stale"
    assert (generic_build / "user-output.txt").read_text(encoding="utf-8") == "keep"
    assert embedded_wheel is not None
    assert not embedded_wheel.exists()


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
    monkeypatch.setattr(builder, "_archive_member_names", lambda _: (member,))
    monkeypatch.setattr(builder, "_archive_member_bytes", lambda *_: b"tampered")

    with pytest.raises(RuntimeError, match="digest does not match"):
        builder._validate_embedded_core_wheel(executable, wheel)
