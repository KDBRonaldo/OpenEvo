from __future__ import annotations

import json
from pathlib import Path

import pytest

from openevo import experiments
from openevo.backend import launcher


def test_backend_launcher_run_invokes_experiment_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "experiment.yaml"
    output_dir = tmp_path / "run"
    config = object()
    framework_lock = tmp_path / "framework-lock.json"
    registry = object()
    profile = object()
    calls: dict[str, object] = {}

    def fake_load_experiment_config(path: Path) -> object:
        calls["config_path"] = path
        return config

    def fake_run_experiment(
        loaded_config: object,
        *,
        task_ids: list[str] | None,
        rounds_override: int | None,
        output_dir: Path | None,
        artifact_root: Path | None,
        executable_registry: object,
        execution_profile: object,
    ) -> dict[str, object]:
        calls["loaded_config"] = loaded_config
        calls["task_ids"] = task_ids
        calls["rounds_override"] = rounds_override
        calls["output_dir"] = output_dir
        calls["artifact_root"] = artifact_root
        calls["executable_registry"] = executable_registry
        calls["execution_profile"] = execution_profile
        return {"status": "completed", "summary_path": str(output_dir / "summary.json")}

    monkeypatch.setattr(
        experiments,
        "load_experiment_config",
        fake_load_experiment_config,
    )
    monkeypatch.setattr(experiments, "run_experiment", fake_run_experiment)
    monkeypatch.setattr(launcher, "load_verified_framework_registry", lambda path: registry)
    monkeypatch.setattr(launcher, "_execution_profile_for_config", lambda value: profile)

    exit_code = launcher.main(
        [
            "run",
            str(config_path),
            "--output-dir",
            str(output_dir),
            "--artifact-root",
            str(tmp_path / "state" / "evolution" / "artifacts"),
            "--rounds",
            "3",
            "--task-id",
            "task-a",
            "--task-id",
            "task-b",
            "--json",
            "--framework-lock",
            str(framework_lock),
        ]
    )

    assert exit_code == 0
    assert calls == {
        "config_path": config_path,
        "loaded_config": config,
        "task_ids": ["task-a", "task-b"],
        "rounds_override": 3,
        "output_dir": output_dir,
        "artifact_root": tmp_path / "state" / "evolution" / "artifacts",
        "executable_registry": registry,
        "execution_profile": profile,
    }
    assert json.loads(capsys.readouterr().out) == {
        "status": "completed",
        "summary_path": str(output_dir / "summary.json"),
    }


def test_backend_launcher_run_dry_run_uses_dry_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "experiment.yaml"
    config = object()
    framework_lock = tmp_path / "framework-lock.json"
    registry = type("Registry", (), {"snapshot": object()})()
    profile = object()
    calls: dict[str, object] = {}

    monkeypatch.setattr(
        experiments,
        "load_experiment_config",
        lambda path: config,
    )

    def fake_dry_run_experiment(
        loaded_config: object,
        *,
        task_ids: list[str] | None,
        rounds_override: int | None,
        registry_snapshot: object,
        execution_profile: object,
    ) -> dict[str, object]:
        calls["loaded_config"] = loaded_config
        calls["task_ids"] = task_ids
        calls["rounds_override"] = rounds_override
        calls["registry_snapshot"] = registry_snapshot
        calls["execution_profile"] = execution_profile
        return {"mode": "dry_run"}

    monkeypatch.setattr(experiments, "dry_run_experiment", fake_dry_run_experiment)
    monkeypatch.setattr(launcher, "load_verified_framework_registry", lambda path: registry)
    monkeypatch.setattr(launcher, "_execution_profile_for_config", lambda value: profile)

    assert launcher.main(
        [
            "run",
            str(config_path),
            "--dry-run",
            "--json",
            "--framework-lock",
            str(framework_lock),
        ]
    ) == 0

    assert calls == {
        "loaded_config": config,
        "task_ids": None,
        "rounds_override": None,
        "registry_snapshot": registry.snapshot,
        "execution_profile": profile,
    }
    assert json.loads(capsys.readouterr().out) == {"mode": "dry_run"}


def test_backend_launcher_serve_starts_backend_api(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    app = object()

    registry = object()

    def fake_create_backend_app(
        *,
        state_root: Path | None = None,
        evolution_registry: object,
    ) -> object:
        calls["state_root"] = state_root
        calls["evolution_registry"] = evolution_registry
        return app

    def fake_uvicorn_run(app: object, **kwargs: object) -> None:
        calls["app"] = app
        calls.update(kwargs)

    monkeypatch.setattr(launcher, "create_backend_app", fake_create_backend_app)
    monkeypatch.setattr(launcher, "load_verified_framework_registry", lambda path: registry)
    monkeypatch.setattr("uvicorn.run", fake_uvicorn_run)

    assert launcher.main(
        [
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            "9876",
            "--state-root",
            "/srv/openevo/state",
            "--framework-lock",
            "/srv/openevo/framework-lock.json",
        ]
    ) == 0
    assert calls == {
        "state_root": Path("/srv/openevo/state"),
        "evolution_registry": registry,
        "app": app,
        "host": "0.0.0.0",
        "port": 9876,
    }
