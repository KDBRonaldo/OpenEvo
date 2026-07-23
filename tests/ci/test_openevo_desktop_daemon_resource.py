from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from types import ModuleType
from zipfile import ZipFile

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts/ci/openevo_desktop_daemon_resource.py"
SOURCE_COMMIT = "8e45af371eef49a86530a849041f7dcf047620ec"
REGISTRY_DIGEST = "a" * 64


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("openevo_desktop_daemon_resource", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_thin_mach_o(path: Path, *, architecture: str = "arm64") -> None:
    cpu_type = {"arm64": 0x0100_000C, "x86_64": 0x0100_0007}[architecture]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        struct.pack("<IiiIIIII", 0xFEED_FACF, cpu_type, 0, 2, 0, 0, 0, 0)
        + b"native askpass helper"
    )
    path.chmod(0o755)


def _release_inputs(
    module: ModuleType, root: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    wheel = root / "openevo-0.1.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(
            "openevo-0.1.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: openevo\nVersion: 0.1.0\nRequires-Python: >=3.11\n\n",
        )
        archive.writestr(
            "openevo-0.1.0.dist-info/entry_points.txt",
            "[console_scripts]\nopenevo-backend = openevo.backend.launcher:main\nopenevo-core-service = openevo.backend.service:main\n",
        )
        archive.writestr("openevo/__init__.py", "__version__ = '0.1.0'\n")
    wheel.chmod(0o600)
    lock = root / "framework-lock.json"
    _write_json(
        lock,
        {
            "distribution": "openevo",
            "distribution_digest": _sha256(wheel),
            "distribution_version": "0.1.0",
            "schema_version": "1",
            "wheel_filename": wheel.name,
        },
    )
    lock.chmod(0o600)
    bundle = root / "openevo-daemon-linux-x86_64"
    bundle.write_bytes(b"linux daemon bundle")
    bundle.chmod(0o755)
    manifest = root / "openevo-daemon-bundle.json"
    _write_json(
        manifest,
        {
            "artifact": {
                "filename": bundle.name,
                "sha256": _sha256(bundle),
                "size": bundle.stat().st_size,
            },
            "build_environment_distributions": [
                {"name": "openevo", "version": "0.1.0"},
                {"name": "pyinstaller", "version": "6.0.0"},
            ],
            "core": {
                "framework_lock": {"filename": lock.name, "sha256": _sha256(lock)},
                "registry_digest": REGISTRY_DIGEST,
                "wheel": {
                    "filename": wheel.name,
                    "sha256": _sha256(wheel),
                    "size": wheel.stat().st_size,
                    "version": "0.1.0",
                },
            },
            "dependency_lock": {"filename": "uv.lock", "sha256": _sha256(REPO_ROOT / "uv.lock")},
            "platform": {"architecture": "x86_64", "system": "linux"},
            "release": {"identity": "b" * 64, "source_commit": SOURCE_COMMIT},
            "runtime": {
                "format": "pyinstaller-onefile",
                "python": {"implementation": "CPython", "version": "3.11.13"},
                "system_python_required": False,
                "target_pypi_required": False,
            },
            "schema_version": 1,
            "smoke": {
                "backend_readiness": "passed",
                "controlled_exit": "passed",
                "identity": "passed",
            },
        },
    )
    manifest.chmod(0o600)
    runtime = root / module.MANAGED_RUNTIME_ARCHIVE_RELEASE.filename
    runtime.write_bytes(b"managed subscription runtime")
    runtime.chmod(0o600)
    release = dataclasses.replace(
        module.MANAGED_RUNTIME_ARCHIVE_RELEASE,
        byte_size=runtime.stat().st_size,
        sha256=_sha256(runtime),
        asset_api_digest=f"sha256:{_sha256(runtime)}",
    )
    monkeypatch.setattr(module, "MANAGED_RUNTIME_ARCHIVE_RELEASE", release)
    monkeypatch.setattr(module, "verify_managed_runtime_archive", lambda *_args, **_kwargs: None)
    return {
        "bundle": bundle,
        "manifest": manifest,
        "wheel": wheel,
        "framework_lock": lock,
        "runtime": runtime,
    }


def _stage(module: ModuleType, inputs: dict[str, Path], output: Path) -> None:
    module.stage_release_assets(
        bundle=inputs["bundle"],
        manifest=inputs["manifest"],
        wheel=inputs["wheel"],
        framework_lock=inputs["framework_lock"],
        managed_runtime_archive=inputs["runtime"],
        source_commit=SOURCE_COMMIT,
        registry_digest=REGISTRY_DIGEST,
        output_dir=output,
    )


def test_stage_release_assets_writes_closed_canonical_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    inputs = _release_inputs(module, tmp_path / "inputs", monkeypatch)
    output = tmp_path / module.RELEASE_ASSETS_DIRECTORY

    _stage(module, inputs, output)

    assert sorted(
        path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()
    ) == [
        "core/framework-lock.json",
        "core/openevo-0.1.0-py3-none-any.whl",
        "daemon/openevo-daemon-bundle.json",
        "daemon/openevo-daemon-linux-x86_64",
        "release-assets.json",
        f"runtime/{inputs['runtime'].name}",
    ]
    manifest = json.loads(
        (output / module.RELEASE_ASSETS_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert manifest["schema_version"] == 1
    assert manifest["source_commit"] == SOURCE_COMMIT
    assert manifest["files"] == sorted(manifest["files"], key=lambda entry: entry["relative_path"])
    assert all(
        set(entry) == {"relative_path", "sha256", "byte_size"} for entry in manifest["files"]
    )
    assert not any("/" in str(value) for value in manifest.values() if isinstance(value, str))
    assert (output / "daemon" / module.DAEMON_BUNDLE_NAME).stat().st_mode & 0o777 == 0o755
    with pytest.raises(module.ResourceCompositionError, match="must not already exist"):
        _stage(module, inputs, output)


def test_stage_release_assets_rejects_symbolic_or_changed_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    inputs = _release_inputs(module, tmp_path / "inputs", monkeypatch)
    linked_runtime = tmp_path / module.MANAGED_RUNTIME_ARCHIVE_RELEASE.filename
    linked_runtime.symlink_to(inputs["runtime"])
    inputs["runtime"] = linked_runtime
    with pytest.raises(module.ResourceCompositionError, match="symlink|identity"):
        _stage(module, inputs, tmp_path / module.RELEASE_ASSETS_DIRECTORY)


def test_verify_app_resource_binds_all_release_assets_for_both_origins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    inputs = _release_inputs(module, tmp_path / "inputs", monkeypatch)
    staged = tmp_path / module.RELEASE_ASSETS_DIRECTORY
    _stage(module, inputs, staged)
    app = tmp_path / "OpenEvo Desktop.app"
    resource_parent = app / module.MACOS_RESOURCE_ROOT.parent
    resource_parent.mkdir(parents=True)
    shutil.copytree(staged, resource_parent / module.RELEASE_ASSETS_DIRECTORY)
    helper = app / module.MACOS_ASKPASS_HELPER_PATH
    _write_thin_mach_o(helper)
    dmg = tmp_path / "OpenEvo-Desktop-0.1.0-aarch64.dmg"
    dmg.write_bytes(b"candidate dmg")
    dmg.chmod(0o600)
    runtime_loads: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        module,
        "_validate_packaged_runtime_loader",
        lambda root, *, source_commit: runtime_loads.append((root, source_commit)),
    )
    monkeypatch.setattr(module, "_verify_macos_adhoc_signature", lambda _path: "adhoc")

    for origin, evidence in (
        ("mounted_dmg", tmp_path / "mounted.json"),
        ("detached_copy", tmp_path / "copy.json"),
    ):
        module.verify_app_resource(
            app=app,
            bundle=inputs["bundle"],
            manifest=inputs["manifest"],
            wheel=inputs["wheel"],
            framework_lock=inputs["framework_lock"],
            managed_runtime_archive=inputs["runtime"],
            source_commit=SOURCE_COMMIT,
            source_dmg=dmg,
            launch_origin=origin,
            evidence_out=evidence,
        )
        payload = json.loads(evidence.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 3
        assert payload["launch_origin"] == origin
        assert payload["ssh_askpass_helper"] == {
            "architecture": "arm64",
            "byte_size": helper.stat().st_size,
            "mode": "0755",
            "relative_path": module.MACOS_ASKPASS_HELPER_PATH.as_posix(),
            "sha256": _sha256(helper),
            "signature": "adhoc",
        }
        assert payload["release_assets"]["manifest"]["relative_path"] == (
            "Contents/Resources/openevo-release-assets/release-assets.json"
        )
        assert [entry["relative_path"] for entry in payload["release_assets"]["files"]] == [
            f"Contents/Resources/openevo-release-assets/{entry['relative_path']}"
            for entry in json.loads((staged / "release-assets.json").read_text())["files"]
        ]

    assert runtime_loads == [
        (app / module.MACOS_RESOURCE_ROOT, SOURCE_COMMIT),
        (app / module.MACOS_RESOURCE_ROOT, SOURCE_COMMIT),
    ]

    (app / module.MACOS_RESOURCE_ROOT / "runtime" / inputs["runtime"].name).write_bytes(
        b"tampered"
    )
    with pytest.raises(module.ResourceCompositionError, match="differs from verified input"):
        module.verify_app_resource(
            app=app,
            bundle=inputs["bundle"],
            manifest=inputs["manifest"],
            wheel=inputs["wheel"],
            framework_lock=inputs["framework_lock"],
            managed_runtime_archive=inputs["runtime"],
            source_commit=SOURCE_COMMIT,
            source_dmg=dmg,
            launch_origin="mounted_dmg",
            evidence_out=tmp_path / "tampered.json",
        )


def test_verify_app_resource_rejects_askpass_symlink_wrong_mode_and_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    inputs = _release_inputs(module, tmp_path / "inputs", monkeypatch)
    staged = tmp_path / module.RELEASE_ASSETS_DIRECTORY
    _stage(module, inputs, staged)
    app = tmp_path / "OpenEvo Desktop.app"
    resources = app / module.MACOS_RESOURCE_ROOT.parent
    resources.mkdir(parents=True)
    shutil.copytree(staged, resources / module.RELEASE_ASSETS_DIRECTORY)
    helper = app / module.MACOS_ASKPASS_HELPER_PATH
    real = tmp_path / "real-helper"
    _write_thin_mach_o(real)
    helper.parent.mkdir(parents=True)
    helper.symlink_to(real)
    dmg = tmp_path / "OpenEvo-Desktop.dmg"
    dmg.write_bytes(b"candidate")
    dmg.chmod(0o600)
    monkeypatch.setattr(module, "_validate_packaged_runtime_loader", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_verify_macos_adhoc_signature", lambda _path: "adhoc")

    arguments = {
        "app": app,
        "bundle": inputs["bundle"],
        "manifest": inputs["manifest"],
        "wheel": inputs["wheel"],
        "framework_lock": inputs["framework_lock"],
        "managed_runtime_archive": inputs["runtime"],
        "source_commit": SOURCE_COMMIT,
        "source_dmg": dmg,
        "launch_origin": "mounted_dmg",
        "evidence_out": tmp_path / "evidence.json",
    }
    with pytest.raises(module.ResourceCompositionError, match="symlink|trusted"):
        module.verify_app_resource(**arguments)

    helper.unlink()
    _write_thin_mach_o(helper)
    helper.chmod(0o700)
    with pytest.raises(module.ResourceCompositionError, match="mode"):
        module.verify_app_resource(**arguments)

    helper.chmod(0o755)

    def replace_during_signature(path: Path) -> str:
        path.unlink()
        _write_thin_mach_o(path)
        return "adhoc"

    monkeypatch.setattr(module, "_verify_macos_adhoc_signature", replace_during_signature)
    with pytest.raises(module.ResourceCompositionError, match="changed during verification"):
        module.verify_app_resource(**arguments)


def test_packaged_runtime_loader_adds_repo_root_for_direct_script_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_module()
    repo_root = str(module.REPO_ROOT)
    original_import = module.importlib.import_module
    import_path_observations: list[bool] = []

    def recording_import(name: str, package: str | None = None):
        if name == "desktop.sidecar.release_runtime":
            import_path_observations.append(repo_root in sys.path)
        return original_import(name, package)

    monkeypatch.setattr(sys, "path", [entry for entry in sys.path if entry != repo_root])
    monkeypatch.setattr(module.importlib, "import_module", recording_import)

    loader = module._load_packaged_runtime_loader()

    assert loader.__name__ == "load_core_bootstrap_config"
    assert import_path_observations
    assert all(import_path_observations)
    assert repo_root not in sys.path


def test_packaged_runtime_loader_works_in_fresh_direct_script_process(tmp_path: Path) -> None:
    code = f"""
import runpy
import sys
from pathlib import Path

repo = Path({str(REPO_ROOT)!r})
sys.path = [entry for entry in sys.path if Path(entry or '.').resolve() != repo]
for name in tuple(sys.modules):
    if name == 'desktop' or name.startswith('desktop.'):
        del sys.modules[name]
namespace = runpy.run_path(str(repo / 'scripts/ci/openevo_desktop_daemon_resource.py'))
loader = namespace['_load_packaged_runtime_loader']()
assert loader.__module__ == 'desktop.sidecar.release_runtime'
assert str(repo) not in sys.path
"""
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=environment,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )


def test_packaged_runtime_loader_rejects_foreign_or_incomplete_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    foreign = ModuleType("desktop.sidecar.release_runtime")
    foreign.__file__ = str(tmp_path / "release_runtime.py")
    monkeypatch.setattr(module.importlib, "import_module", lambda _name: foreign)
    with pytest.raises(module.ResourceCompositionError, match="candidate source checkout"):
        module._load_packaged_runtime_loader()

    incomplete = ModuleType("desktop.sidecar.release_runtime")
    incomplete.__file__ = str(REPO_ROOT / "desktop/sidecar/release_runtime.py")
    monkeypatch.setattr(module.importlib, "import_module", lambda _name: incomplete)
    with pytest.raises(module.ResourceCompositionError, match="loader is unavailable"):
        module._load_packaged_runtime_loader()


def test_release_workflow_stages_one_release_asset_tree_without_sidecar_embedding() -> None:
    workflow = (REPO_ROOT / ".github/workflows/openevo-desktop-candidate.yml").read_text(
        encoding="utf-8"
    )
    release_config = json.loads(
        (REPO_ROOT / "desktop/src-tauri/tauri.release.conf.json").read_text(encoding="utf-8")
    )
    linux_job, macos_and_later = workflow.split("  macos-candidate:\n", maxsplit=1)
    macos_job = macos_and_later.split("  linux-core-candidate:\n", maxsplit=1)[0]

    assert "linux-daemon-bundle:" in linux_job
    assert "openevo_desktop_daemon_resource.py build" in linux_job
    assert "needs: linux-daemon-bundle" in macos_job
    assert "openevo_desktop_daemon_resource.py stage" in macos_job
    assert '--managed-runtime-archive "$OPENEVO_MANAGED_RUNTIME_ARCHIVE"' in macos_job
    assert "--output-dir desktop/src-tauri/release-resources/openevo-release-assets" in macos_job
    assert macos_job.index("openevo_desktop_daemon_resource.py stage") < macos_job.index(
        "npm run tauri:build"
    )
    assert macos_job.count("openevo_desktop_daemon_resource.py verify-app") == 2
    assert "openevo-ssh-askpass-${OPENEVO_RUST_TARGET}" in macos_job
    assert 'test -x "$askpass"' in macos_job
    assert "_validate_embedded_managed_runtime_archive" not in macos_job
    assert release_config["bundle"]["resources"] == {
        "release-resources/openevo-release-assets": "openevo-release-assets"
    }
