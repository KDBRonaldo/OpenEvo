from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
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
        json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _release_inputs(root: Path) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    wheel = root / "openevo-0.1.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(
            "openevo-0.1.0.dist-info/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: openevo\n"
            "Version: 0.1.0\n"
            "Requires-Python: >=3.11\n\n",
        )
        archive.writestr(
            "openevo-0.1.0.dist-info/entry_points.txt",
            "[console_scripts]\n"
            "openevo-backend = openevo.backend.launcher:main\n"
            "openevo-core-service = openevo.backend.service:main\n",
        )
        archive.writestr("openevo/__init__.py", "__version__ = '0.1.0'\n")
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
                "framework_lock": {
                    "filename": lock.name,
                    "sha256": _sha256(lock),
                },
                "registry_digest": REGISTRY_DIGEST,
                "wheel": {
                    "filename": wheel.name,
                    "sha256": _sha256(wheel),
                    "size": wheel.stat().st_size,
                    "version": "0.1.0",
                },
            },
            "dependency_lock": {
                "filename": "uv.lock",
                "sha256": _sha256(REPO_ROOT / "uv.lock"),
            },
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
    return {
        "bundle": bundle,
        "manifest": manifest,
        "wheel": wheel,
        "framework_lock": lock,
    }


def _stage(module: ModuleType, inputs: dict[str, Path], output: Path) -> None:
    module.stage_daemon_resource(
        bundle=inputs["bundle"],
        manifest=inputs["manifest"],
        wheel=inputs["wheel"],
        framework_lock=inputs["framework_lock"],
        source_commit=SOURCE_COMMIT,
        registry_digest=REGISTRY_DIGEST,
        output_dir=output,
    )


def test_stage_daemon_resource_requires_exact_candidate_identity(tmp_path: Path) -> None:
    module = _load_module()
    inputs = _release_inputs(tmp_path)
    output = tmp_path / "resource"

    _stage(module, inputs, output)

    assert (output / module.DAEMON_BUNDLE_NAME).read_bytes() == inputs["bundle"].read_bytes()
    assert (output / module.DAEMON_MANIFEST_NAME).read_bytes() == inputs["manifest"].read_bytes()
    assert (output / module.DAEMON_BUNDLE_NAME).stat().st_mode & 0o777 == 0o755
    with pytest.raises(module.ResourceCompositionError, match="must not already exist"):
        _stage(module, inputs, output)


def test_stage_daemon_resource_rejects_mismatched_or_symbolic_inputs(tmp_path: Path) -> None:
    module = _load_module()
    inputs = _release_inputs(tmp_path)
    inputs["wheel"].write_bytes(inputs["wheel"].read_bytes() + b"tampered")
    with pytest.raises(module.ResourceCompositionError, match="exact Core wheel"):
        _stage(module, inputs, tmp_path / "mismatch")

    inputs = _release_inputs(tmp_path / "second")
    symlink = tmp_path / "daemon-link"
    symlink.symlink_to(inputs["bundle"])
    inputs["bundle"] = symlink
    with pytest.raises(module.ResourceCompositionError, match="canonical regular release binary"):
        _stage(module, inputs, tmp_path / "symlink-output")


def test_verify_app_resource_binds_fixed_dmg_resource_paths(tmp_path: Path) -> None:
    module = _load_module()
    inputs_root = tmp_path / "inputs"
    inputs_root.mkdir()
    inputs = _release_inputs(inputs_root)
    app = tmp_path / "OpenEvo Desktop.app"
    resource = app / module.MACOS_RESOURCE_ROOT
    resource.mkdir(parents=True)
    packaged_bundle = resource / module.DAEMON_BUNDLE_NAME
    packaged_bundle.write_bytes(inputs["bundle"].read_bytes())
    packaged_bundle.chmod(0o755)
    (resource / module.DAEMON_MANIFEST_NAME).write_bytes(inputs["manifest"].read_bytes())
    dmg = tmp_path / "OpenEvo-Desktop-0.1.0-aarch64.dmg"
    dmg.write_bytes(b"candidate dmg")
    evidence = tmp_path / "daemon-mounted-resource.json"

    module.verify_app_resource(
        app=app,
        bundle=inputs["bundle"],
        manifest=inputs["manifest"],
        source_dmg=dmg,
        launch_origin="mounted_dmg",
        evidence_out=evidence,
    )

    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["source_dmg"] == {"filename": dmg.name, "sha256": _sha256(dmg)}
    assert payload["daemon_bundle"]["relative_path"] == (
        "Contents/Resources/openevo-daemon/openevo-daemon-linux-x86_64"
    )
    packaged_bundle.write_bytes(b"mutable fallback")
    with pytest.raises(module.ResourceCompositionError, match="differ from verified inputs"):
        module.verify_app_resource(
            app=app,
            bundle=inputs["bundle"],
            manifest=inputs["manifest"],
            source_dmg=dmg,
            launch_origin="detached_copy",
            evidence_out=tmp_path / "daemon-copy-resource.json",
        )


def test_release_workflow_uses_verified_linux_artifact_without_fallback() -> None:
    workflow = (REPO_ROOT / ".github/workflows/openevo-desktop-candidate.yml").read_text(
        encoding="utf-8"
    )
    release_config = json.loads(
        (REPO_ROOT / "desktop/src-tauri/tauri.release.conf.json").read_text(encoding="utf-8")
    )
    linux_job, macos_and_later = workflow.split("  macos-candidate:\n", maxsplit=1)
    macos_job = macos_and_later.split("  linux-core-candidate:\n", maxsplit=1)[0]
    artifact_name = (
        "openevo-desktop-daemon-${{ github.sha }}-"
        "${{ github.run_id }}-${{ github.run_attempt }}"
    )

    assert "linux-daemon-bundle:" in linux_job
    assert "runs-on: ubuntu-latest" in linux_job
    assert "test \"$(uname -m)\" = \"x86_64\"" in linux_job
    assert "openevo_desktop_daemon_resource.py build" in linux_job
    assert linux_job.count(artifact_name) == 1
    assert "needs: linux-daemon-bundle" in macos_job
    assert macos_job.count(artifact_name) == 1
    assert "openevo_desktop_daemon_resource.py stage" in macos_job
    assert (
        '--daemon-bundle "$OPENEVO_DAEMON_INPUTS/daemon/'
        'openevo-daemon-linux-x86_64"'
    ) in macos_job
    assert (
        '--daemon-manifest "$OPENEVO_DAEMON_INPUTS/daemon/'
        'openevo-daemon-bundle.json"'
    ) in macos_job
    assert macos_job.index("openevo_desktop_daemon_resource.py stage") < macos_job.index(
        "npm run tauri:build"
    )
    assert "--config src-tauri/tauri.release.conf.json" in macos_job
    assert macos_job.count("openevo_desktop_daemon_resource.py verify-app") == 2
    assert macos_job.index("openevo_desktop_daemon_resource.py verify-app") < macos_job.index(
        "openevo_release_candidate.py create"
    )
    assert "curl " not in linux_job
    assert "latest" not in artifact_name
    assert release_config["bundle"]["resources"] == {
        "release-resources/openevo-daemon/openevo-daemon-bundle.json": (
            "openevo-daemon/openevo-daemon-bundle.json"
        ),
        "release-resources/openevo-daemon/openevo-daemon-linux-x86_64": (
            "openevo-daemon/openevo-daemon-linux-x86_64"
        ),
    }
