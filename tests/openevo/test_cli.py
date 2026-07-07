from __future__ import annotations

import json
from pathlib import Path

import yaml

from openevo.cli import main


def _write_config(path: Path, payload: dict) -> Path:
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def _minimal_payload() -> dict:
    return {
        "version": 1,
        "experiment": {"name": "biology-components"},
        "agent": {"preset": "codex", "model": "gpt-5.1-codex-mini"},
        "runtime": {"image": "runtime:latest"},
        "tasks": [{"id": "task-a", "instruction": "Do A.", "workspace": "/tmp/a"}],
    }


def _minimal_science_payload() -> dict:
    return {
        "version": 1,
        "project": {"name": "protein-design"},
        "remote_profile": "science-team",
        "task": {
            "id": "folding-baseline",
            "objective": "Improve the folding baseline.",
            "source": {
                "type": "remote_path",
                "path": "/datasets/folding-baseline",
            },
        },
    }


def test_cli_dry_run_json_outputs_compiled_plan(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _write_config(tmp_path / "experiment.yaml", _minimal_payload())

    exit_code = main(["run", str(config_path), "--dry-run", "--json", "--rounds", "2"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry_run"
    assert payload["round_count"] == 2
    assert payload["tasks"][0]["rounds"][1]["evolution_jobs"][2]["method"] == (
        "agent_system_history_reflector"
    )


def test_cli_dry_run_output_file_matches_reported_plan_path(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _write_config(tmp_path / "experiment.yaml", _minimal_payload())
    output_dir = tmp_path / "out"

    exit_code = main(
        [
            "run",
            str(config_path),
            "--dry-run",
            "--json",
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    file_payload = json.loads((output_dir / "plan.json").read_text(encoding="utf-8"))
    assert file_payload["plan_path"] == payload["plan_path"]


def test_cli_invalid_config_returns_nonzero(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _write_config(
        tmp_path / "experiment.yaml",
        _minimal_payload() | {"unexpected": True},
    )

    exit_code = main(["run", str(config_path), "--dry-run"])

    assert exit_code == 1
    assert "error:" in capsys.readouterr().err


def test_cli_science_compile_outputs_experiment_config(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _write_config(tmp_path / "science.yaml", _minimal_science_payload())

    exit_code = main(["science", "compile", str(config_path), "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["experiment"]["name"] == "protein-design"
    assert payload["agent"]["auth"] == "subscription"
    assert payload["agent"]["settings"]["capture_mode"] == "transcript"
    assert payload["runtime"]["image"] == "openevo/science-runtime:0.1.0"
    assert payload["tasks"][0]["workspace"] == "/datasets/folding-baseline"
    assert "path" not in payload


def test_cli_science_compile_accepts_prepared_workspace(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _write_config(
        tmp_path / "science.yaml",
        _minimal_science_payload()
        | {
            "task": {
                "id": "local-task",
                "objective": "Run local workflow.",
                "source": {
                    "type": "local_folder",
                    "path": "workflows/local-task",
                },
            }
        },
    )

    exit_code = main(
        [
            "science",
            "compile",
            str(config_path),
            "--json",
            "--prepared-workspace",
            "local-task=/home/user/.openevo/workspaces/local-task",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"][0]["workspace"] == (
        "/home/user/.openevo/workspaces/local-task"
    )


def test_cli_science_compile_rejects_invalid_prepared_workspace(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = _write_config(tmp_path / "science.yaml", _minimal_science_payload())

    exit_code = main(
        [
            "science",
            "compile",
            str(config_path),
            "--prepared-workspace",
            "local-task",
        ]
    )

    assert exit_code == 1
    assert "--prepared-workspace must use task_id=/remote/path" in capsys.readouterr().err
