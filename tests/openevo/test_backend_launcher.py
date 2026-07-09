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
    ) -> dict[str, object]:
        calls["loaded_config"] = loaded_config
        calls["task_ids"] = task_ids
        calls["rounds_override"] = rounds_override
        calls["output_dir"] = output_dir
        return {"status": "completed", "summary_path": str(output_dir / "summary.json")}

    monkeypatch.setattr(
        experiments,
        "load_experiment_config",
        fake_load_experiment_config,
    )
    monkeypatch.setattr(experiments, "run_experiment", fake_run_experiment)

    exit_code = launcher.main(
        [
            "run",
            str(config_path),
            "--output-dir",
            str(output_dir),
            "--rounds",
            "3",
            "--task-id",
            "task-a",
            "--task-id",
            "task-b",
            "--json",
        ]
    )

    assert exit_code == 0
    assert calls == {
        "config_path": config_path,
        "loaded_config": config,
        "task_ids": ["task-a", "task-b"],
        "rounds_override": 3,
        "output_dir": output_dir,
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
    ) -> dict[str, object]:
        calls["loaded_config"] = loaded_config
        calls["task_ids"] = task_ids
        calls["rounds_override"] = rounds_override
        return {"mode": "dry_run"}

    monkeypatch.setattr(experiments, "dry_run_experiment", fake_dry_run_experiment)

    assert launcher.main(["run", str(config_path), "--dry-run", "--json"]) == 0

    assert calls == {
        "loaded_config": config,
        "task_ids": None,
        "rounds_override": None,
    }
    assert json.loads(capsys.readouterr().out) == {"mode": "dry_run"}


def test_backend_launcher_serve_remains_reserved() -> None:
    with pytest.raises(SystemExit) as exc_info:
        launcher.main(["serve"])

    assert "openevo-backend serve is introduced in the backend API phase" in str(
        exc_info.value
    )
