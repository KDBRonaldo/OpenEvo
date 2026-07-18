from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from openevo.backend import daemon_bundle
from openevo.backend.service import CoreServiceError, CoreServiceErrorCode


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "build_openevo_daemon_bundle.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_openevo_daemon_bundle", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = _load_builder()


def _wheel(path: Path, *, version: str = "0.1.0") -> Path:
    metadata_root = f"openevo-{version}.dist-info"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("openevo/__init__.py", f'__version__ = "{version}"\n')
        archive.writestr(
            f"{metadata_root}/METADATA",
            "\n".join(
                (
                    "Metadata-Version: 2.1",
                    "Name: openevo",
                    f"Version: {version}",
                    "Requires-Python: >=3.11",
                    "",
                )
            ),
        )
        archive.writestr(
            f"{metadata_root}/entry_points.txt",
            "\n".join(
                (
                    "[console_scripts]",
                    "openevo-backend = openevo.backend.launcher:main",
                    "openevo-core-service = openevo.backend.service:main",
                    "",
                )
            ),
        )
    return path


def _lock(path: Path, wheel: Path, *, extra: dict[str, object] | None = None) -> Path:
    payload: dict[str, object] = {
        "distribution": "openevo",
        "distribution_digest": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "distribution_version": "0.1.0",
        "schema_version": "1",
        "wheel_filename": wheel.name,
    }
    if extra:
        payload.update(extra)
    path.write_bytes(builder._canonical_json(payload))
    return path


def test_exact_framework_lock_requires_canonical_colocated_wheel(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")
    lock = _lock(tmp_path / "framework-lock.json", wheel)

    version, _size = builder._validate_wheel(wheel)
    assert builder._validate_exact_lock(lock, wheel, version=version) == json.loads(
        lock.read_text(encoding="utf-8")
    )

    lock.write_text(lock.read_text(encoding="utf-8").replace("\n", " \n"), encoding="utf-8")
    with pytest.raises(builder.BundleBuildError, match="not canonical"):
        builder._validate_exact_lock(lock, wheel, version=version)


def test_framework_lock_rejects_unknown_fields_and_wrong_wheel(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")
    lock = _lock(tmp_path / "framework-lock.json", wheel, extra={"unexpected": True})
    with pytest.raises(builder.BundleBuildError, match="closed release schema"):
        builder._validate_exact_lock(lock, wheel, version="0.1.0")

    _lock(lock, wheel)
    wheel.write_bytes(wheel.read_bytes() + b"changed")
    with pytest.raises(builder.BundleBuildError, match="does not bind"):
        builder._validate_exact_lock(lock, wheel, version="0.1.0")


def test_build_environment_removes_python_source_fallbacks() -> None:
    environment = builder._isolated_environment(
        {
            "HOME": "/tmp/home",
            "PYTHONHOME": "/source/python",
            "PYTHONPATH": "/source/repository",
            "VIRTUAL_ENV": "/source/venv",
        }
    )
    assert environment == {"HOME": "/tmp/home", "PYTHONNOUSERSITE": "1"}


def test_wheel_top_level_inventory_includes_all_core_packages(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path / "openevo-0.1.0-py3-none-any.whl")
    with ZipFile(wheel, "a", compression=ZIP_DEFLATED) as archive:
        archive.writestr("slime_bridge/__init__.py", "")
    assert builder._wheel_top_level_packages(wheel) == ("openevo", "slime_bridge")


def test_installed_distribution_rejects_editable_direct_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_root = tmp_path / "site-packages"
    package = install_root / "openevo"
    metadata = install_root / "openevo-0.1.0.dist-info"
    package.mkdir(parents=True)
    metadata.mkdir()
    (metadata / "direct_url.json").write_text(
        '{"dir_info":{"editable":true},"url":"file:///source"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        builder,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=f"{install_root}\n{metadata}\n"),
    )
    with pytest.raises(builder.BundleBuildError, match="Editable Core installs are forbidden"):
        builder._installed_distribution_paths(
            Path("/venv/bin/python"),
            top_levels=("openevo",),
            cwd=tmp_path,
            env={},
        )


def test_build_metadata_schema_is_canonical_and_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel = tmp_path / "openevo-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    assets = tmp_path / "frozen" / "openevo_daemon_bundle"
    assets.mkdir(parents=True)
    metadata = {
        "bundle_format": "pyinstaller-onefile",
        "core": {
            "distribution": "openevo",
            "version": "0.1.0",
            "wheel_filename": wheel.name,
            "wheel_sha256": hashlib.sha256(b"wheel").hexdigest(),
            "wheel_size": 5,
        },
        "dependency_lock": {
            "filename": "uv.lock",
            "sha256": "a" * 64,
        },
        "platform": {"architecture": "x86_64", "system": "linux"},
        "python": {"implementation": "CPython", "version": "3.11.15"},
        "schema_version": 1,
        "source_commit": "b" * 40,
    }
    (assets / "build-metadata.json").write_bytes(daemon_bundle._canonical_json(metadata))
    monkeypatch.setattr(daemon_bundle.sys, "_MEIPASS", str(tmp_path / "frozen"), raising=False)
    assert daemon_bundle._load_build_metadata() == metadata

    metadata["unexpected"] = True
    (assets / "build-metadata.json").write_bytes(daemon_bundle._canonical_json(metadata))
    with pytest.raises(daemon_bundle.DaemonBundleError, match="schema is not closed"):
        daemon_bundle._load_build_metadata()


def test_source_execution_is_not_a_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(daemon_bundle.sys, "_MEIPASS", raising=False)
    with pytest.raises(daemon_bundle.DaemonBundleError, match="not running from a frozen bundle"):
        daemon_bundle._bundle_root()


def test_internal_launcher_dispatch_rebinds_ephemeral_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []
    monkeypatch.setattr(
        daemon_bundle,
        "_load_build_metadata",
        lambda: {
            "source_commit": "c" * 40,
            "core": {"wheel_filename": "openevo-0.1.0-py3-none-any.whl"},
        },
    )
    monkeypatch.setattr(
        daemon_bundle,
        "_asset_path",
        lambda name: Path("/current-extraction") / name,
    )
    import openevo.backend.launcher

    monkeypatch.setattr(
        openevo.backend.launcher,
        "main",
        lambda values: captured.extend(values) or 0,
    )
    result = daemon_bundle._internal_module_dispatch(
        [
            "-m",
            "openevo.backend.launcher",
            "serve",
            "--framework-lock",
            "/parent-extraction/framework-lock.json",
            "--source-commit",
            "d" * 40,
        ]
    )
    assert result == 0
    assert captured == [
        "serve",
        "--framework-lock",
        "/current-extraction/framework-lock.json",
        "--source-commit",
        "c" * 40,
    ]


def test_internal_script_dispatch_only_allows_managed_runtime_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openevo.deployment import managed_runtime_assets

    script = 'import sys\nprint("managed:" + sys.argv[1])\n'
    monkeypatch.setattr(
        managed_runtime_assets,
        "_REMOTE_MANAGED_RUNTIME_SCRIPT",
        script,
    )
    original_argv = sys.argv

    assert daemon_bundle._internal_module_dispatch(["-I", "-c", script, "probe"]) == 0
    assert sys.argv is original_argv
    with pytest.raises(daemon_bundle.DaemonBundleError, match="not allowlisted"):
        daemon_bundle._internal_module_dispatch(["-I", "-c", "print('arbitrary')"])


def test_managed_runtime_cli_dispatches_only_named_actions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from openevo.deployment import managed_runtime_assets

    script = 'import json,sys\nprint(json.dumps({"action":sys.argv[1]}))\n'
    monkeypatch.setattr(
        managed_runtime_assets,
        "_REMOTE_MANAGED_RUNTIME_SCRIPT",
        script,
    )

    assert daemon_bundle.main(["managed-runtime", "probe", "identity"]) == 0
    assert json.loads(capsys.readouterr().out) == {"action": "probe"}
    with pytest.raises(SystemExit):
        daemon_bundle.main(["managed-runtime", "arbitrary"])


def test_manifest_identity_validation_is_closed(tmp_path: Path) -> None:
    bundle = tmp_path / builder.BUNDLE_NAME
    bundle.write_bytes(b"binary")
    lock = {
        "distribution": "openevo",
        "distribution_digest": "a" * 64,
        "distribution_version": "0.1.0",
    }
    installed = {"registry_digest": "b" * 64, "release_identity": "c" * 64}
    identity = {
        "bundle": {
            "format": "pyinstaller-onefile",
            "sha256": hashlib.sha256(b"binary").hexdigest(),
            "size": 6,
        },
        "core": {
            "distribution": "openevo",
            "version": "0.1.0",
            "wheel_sha256": "a" * 64,
        },
        "dependencies": {"lock_sha256": "f" * 64},
        "framework": {"lock_sha256": "d" * 64, "registry_digest": "b" * 64},
        "platform": {"architecture": "x86_64", "system": "linux"},
        "release": {"identity": "c" * 64, "source_commit": "e" * 40},
        "schema_version": 1,
    }
    assert (
        builder._validate_identity(
            identity,
            bundle=bundle,
            lock=lock,
            installed_identity=installed,
            source_commit="e" * 40,
            uv_lock_sha256="f" * 64,
        )
        == identity
    )
    identity["unknown"] = True
    with pytest.raises(builder.BundleBuildError, match="closed release schema"):
        builder._validate_identity(
            identity,
            bundle=bundle,
            lock=lock,
            installed_identity=installed,
            source_commit="e" * 40,
            uv_lock_sha256="f" * 64,
        )


def test_bundle_command_failure_preserves_closed_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        builder.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr='{"error":{"code":"daemon_bundle_invalid"}}\n',
        ),
    )
    with pytest.raises(builder.BundleBuildError, match="daemon_bundle_invalid"):
        builder._run_bundle_json(Path("/bundle"), ["identity"], cwd=tmp_path)


def test_service_failure_is_rendered_as_closed_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        daemon_bundle,
        "smoke_daemon",
        lambda **_kwargs: (_ for _ in ()).throw(
            CoreServiceError(
                CoreServiceErrorCode.IDENTITY_MISMATCH,
                "A different verified Core release is already running.",
                retryable=False,
            )
        ),
    )
    assert daemon_bundle.main(["smoke"]) == 1
    value = json.loads(capsys.readouterr().err)
    assert value == {
        "error": {
            "code": "core_service_identity_mismatch",
            "message": "A different verified Core release is already running.",
            "retryable": False,
        },
        "schema_version": 1,
    }


def test_service_ensure_returns_authenticated_subscription_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = SimpleNamespace(
        attached=False,
        bearer_token="B" * 64,
        generation="5" * 32,
        port=43117,
        registry_digest="3" * 64,
        release_identity="2" * 64,
        source_commit="1" * 40,
        status_proof="4" * 64,
    )
    captured: dict[str, object] = {}

    def ensure(**kwargs: object) -> object:
        captured.update(kwargs)
        return attachment

    monkeypatch.setattr(
        daemon_bundle,
        "_load_build_metadata",
        lambda: {"source_commit": "1" * 40},
    )
    monkeypatch.setattr(
        daemon_bundle,
        "_asset_path",
        lambda name: Path("/embedded") / name,
    )
    monkeypatch.setattr(daemon_bundle, "ensure_core_service", ensure)

    result = daemon_bundle._run_service_command(
        SimpleNamespace(
            service_command="ensure",
            port=43117,
            deadline_seconds=30.0,
        )
    )

    assert result == {
        "attached": False,
        "bearer_token": "B" * 64,
        "capture_mode": "transcript",
        "execution_mode": "subscription",
        "generation": "5" * 32,
        "host": "127.0.0.1",
        "port": 43117,
        "registry_digest": "3" * 64,
        "release_identity": "2" * 64,
        "schema_version": 1,
        "source_commit": "1" * 40,
        "status_proof": "4" * 64,
    }
    assert captured["replace_mismatched"] is True
