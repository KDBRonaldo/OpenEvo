from __future__ import annotations

import json
from pathlib import Path

import pytest

from polar_evolution.cli import main
from polar_evolution.models import DatasetCreateRequest
from polar_evolution.store import EvolutionStore
from polar_evolution.terminal_bench_bridge import (
    TerminalBenchBridgeError,
    build_terminal_bench_events,
)


def test_build_terminal_bench_event_preserves_transcript_metadata_without_secrets(tmp_path):
    trial_dir = _write_trial(
        tmp_path,
        stdout="Recovered the lost git commit and merged it into master.\n",
        stderr="",
        ctrf={
            "results": {
                "summary": {"tests": 2, "passed": 1, "failed": 1, "skipped": 0},
                "tests": [
                    {"name": "test_outputs.py::test_about_file", "status": "passed"},
                    {
                        "name": "test_outputs.py::test_layout_file",
                        "status": "failed",
                        "message": "layout file missing",
                        "file_path": "test_outputs.py",
                    },
                ],
            }
        },
        verifier_stdout="pytest output with a failing layout assertion\n",
    )

    [event] = build_terminal_bench_events(trial_dir)

    assert event.source == "terminal_bench.harbor"
    assert event.event_type == "polar.session_completed"
    assert event.source_event_id == "terminal-bench:fix-git__abc123"
    assert event.task_id == "fix-git"
    assert event.session_id == "fix-git__abc123"
    assert event.status == "COMPLETED"
    assert event.reward == 0.5
    assert event.agent == {
        "harness": "terminal-bench-harbor",
        "model_name": "gpt-5-mini",
    }
    assert event.payload["session_result"]["status"] == "COMPLETED"
    trajectory = event.payload["session_result"]["trajectory"]
    assert trajectory["status"] == "COMPLETED"
    assert trajectory["metadata"]["builder"] == "terminal_bench_transcript_bridge"
    assert trajectory["metadata"]["capture_mode"] == "transcript"
    assert trajectory["metadata"]["token_level_metrics_available"] is False
    assert trajectory["metadata"]["task_metadata"]["task_name"] == "fix-git"
    trace = trajectory["traces"][0]
    assert trace["prompt_messages"] == [
        {"role": "user", "content": "Find the missing git changes."}
    ]
    assert trace["response_messages"] == [
        {
            "role": "assistant",
            "content": "Recovered the lost git commit and merged it into master.\n",
        }
    ]
    assert trace["response_ids"] == []
    assert trace["loss_mask"] == []
    assert trace["response_logprobs"] is None
    assert trace["reward"] == 0.5
    assert trace["metadata"]["verifier"]["summary"] == {
        "tests": 2,
        "passed": 1,
        "failed": 1,
        "skipped": 0,
    }
    assert trace["metadata"]["verifier"]["failed_tests"] == [
        {
            "name": "test_outputs.py::test_layout_file",
            "status": "failed",
            "message": "layout file missing",
            "file_path": "test_outputs.py",
        }
    ]
    serialized = event.model_dump_json()
    assert "secret-token" not in serialized
    assert "AIGOCODE_GPT_API_KEY" not in serialized

    store = EvolutionStore(db_path=tmp_path / "evolution.db", artifact_root=tmp_path / "artifacts")
    store.initialize()
    ingested = store.ingest_event(event)
    dataset = store.create_dataset(
        DatasetCreateRequest(
            name="tb21_fix_git",
            purpose="agent_system_reflection",
            query={"event_types": ["polar.session_completed"], "status": ["COMPLETED"]},
        )
    )

    assert ingested.ingested is True
    assert dataset.event_count == 1
    assert dataset.trace_count == 1


def test_build_terminal_bench_event_uses_report_when_stdout_is_empty(tmp_path):
    trial_dir = _write_trial(
        tmp_path,
        stdout="",
        stderr="",
        report="# Terminal Bench Report\n\nRecovered commit c499730.\n",
    )

    [event] = build_terminal_bench_events(trial_dir)

    trace = event.payload["session_result"]["trajectory"]["traces"][0]
    assert trace["response_messages"] == [
        {
            "role": "assistant",
            "content": "# Terminal Bench Report\n\nRecovered commit c499730.\n",
        }
    ]
    assert trace["metadata"]["transcript_sources"] == [
        "agent/evolab_lab/terminal_bench_report.md"
    ]


def test_build_terminal_bench_event_rejects_missing_transcript_text(tmp_path):
    trial_dir = _write_trial(tmp_path, stdout="", stderr="", report=None)

    with pytest.raises(TerminalBenchBridgeError, match="no transcript text"):
        build_terminal_bench_events(trial_dir)


def test_terminal_bench_events_cli_writes_jsonl(tmp_path):
    trial_dir = _write_trial(
        tmp_path / "job",
        stdout="Recovered the lost git commit.\n",
        stderr="",
    )
    output_path = tmp_path / "events.jsonl"

    exit_code = main(
        [
            "terminal-bench-events",
            "--input",
            str(trial_dir.parent),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event_payload = json.loads(lines[0])
    assert event_payload["source"] == "terminal_bench.harbor"
    assert event_payload["event_type"] == "polar.session_completed"
    assert event_payload["payload"]["session_result"]["trajectory"]["metadata"][
        "capture_mode"
    ] == "transcript"


def test_terminal_bench_dataset_cli_ingests_events_and_creates_dataset(tmp_path):
    trial_dir = _write_trial(
        tmp_path / "job",
        stdout="Recovered the lost git commit.\n",
        stderr="",
    )
    output_path = tmp_path / "dataset.json"
    db_path = tmp_path / "polar.db"
    artifact_root = tmp_path / "polar_artifacts"

    exit_code = main(
        [
            "terminal-bench-dataset",
            "--input",
            str(trial_dir.parent),
            "--db",
            str(db_path),
            "--artifact-root",
            str(artifact_root),
            "--name",
            "tb21_fix_git_round0",
            "--purpose",
            "agent_system_reflection",
            "--policy-version",
            "tb21-round0",
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["dataset"]["name"] == "tb21_fix_git_round0"
    assert payload["dataset"]["event_count"] == 1
    assert payload["dataset"]["trace_count"] == 1
    assert payload["ingested_events"] == [
        {
            "duplicate": False,
            "event_id": payload["ingested_events"][0]["event_id"],
            "ingested": True,
            "session_id": "fix-git__abc123",
            "task_id": "fix-git",
        }
    ]

    manifest_path = Path(payload["dataset"]["manifest_uri"].removeprefix("file://"))
    records_path = manifest_path.parent / "records.jsonl"
    records = [
        json.loads(line)
        for line in records_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records[0]["policy_version"] == "tb21-round0"
    assert records[0]["traces"][0]["metadata"]["capture_mode"] == "transcript"
    assert records[0]["traces"][0]["metadata"]["token_level_metrics_available"] is False


def _write_trial(
    root: Path,
    *,
    stdout: str,
    stderr: str,
    report: str | None = "# Terminal Bench Report\n",
    ctrf: dict | None = None,
    verifier_stdout: str = "",
) -> Path:
    trial_dir = root / "fix-git__abc123"
    (trial_dir / "agent").mkdir(parents=True)
    (trial_dir / "verifier").mkdir()
    (trial_dir / "agent" / "evolab_lab").mkdir()
    (trial_dir / "agent" / "instruction.txt").write_text(
        "Find the missing git changes.",
        encoding="utf-8",
    )
    (trial_dir / "agent" / "stdout.txt").write_text(stdout, encoding="utf-8")
    (trial_dir / "agent" / "stderr.txt").write_text(stderr, encoding="utf-8")
    if report is not None:
        (trial_dir / "agent" / "evolab_lab" / "terminal_bench_report.md").write_text(
            report,
            encoding="utf-8",
        )
    (trial_dir / "verifier" / "reward.txt").write_text("0.5", encoding="utf-8")
    (trial_dir / "verifier" / "test-stdout.txt").write_text(
        verifier_stdout,
        encoding="utf-8",
    )
    if ctrf is not None:
        (trial_dir / "verifier" / "ctrf.json").write_text(
            json.dumps(ctrf),
            encoding="utf-8",
        )
    (trial_dir / "result.json").write_text(
        json.dumps(
            {
                "id": "trial-uuid",
                "task_name": "fix-git",
                "trial_name": "fix-git__abc123",
                "task_id": {"path": "/root/datasets/terminal-bench-2-1/tasks/fix-git"},
                "agent_info": {"name": "evolab", "version": "0.1.0"},
                "config": {
                    "agent": {
                        "import_path": "task_packages.terminal_bench_v1.harbor_agent:EvoLabHarborAgent",
                        "model_name": "gpt-5-mini",
                        "env": {"AIGOCODE_GPT_API_KEY": "secret-token"},
                        "kwargs": {"task_id": "fix-git"},
                    }
                },
                "agent_result": {
                    "metadata": {
                        "terminal_bench_harbor_agent": {
                            "agent": "evolab",
                            "command": "harbor run --agent-env AIGOCODE_GPT_API_KEY=secret-token",
                            "mode": "evolab",
                            "return_code": 0,
                            "task_id": "fix-git",
                        }
                    }
                },
                "verifier_result": {"rewards": {"reward": 0.5}},
                "exception_info": None,
                "started_at": "2026-06-18T15:51:08Z",
                "finished_at": "2026-06-18T15:59:18Z",
            }
        ),
        encoding="utf-8",
    )
    return trial_dir
