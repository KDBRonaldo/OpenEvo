from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote

import pytest

from openevo_terminal_bench.cli import (
    _terminal_bench_forbidden_literals_from_dataset_artifacts,
    _terminal_bench_forbidden_literals_from_events,
    _terminal_bench_task_tags,
    main,
)
from openevo.evolution.models import DatasetCreateRequest, EventIngestRequest, WorkerClaimRequest
from openevo.evolution.parametric.training_data import normalize_chat_messages
from openevo.evolution.store import EvolutionStore
from openevo_terminal_bench.bridge import (
    CodexGatewayTrainingContract,
    TerminalBenchBridgeError,
    build_terminal_bench_events,
)


def _codex_gateway_contract(task_instruction: str) -> CodexGatewayTrainingContract:
    return CodexGatewayTrainingContract.from_gateway_request(
        {
            "messages": [
                {"role": "system", "content": "Use the Codex harness tools."},
                {"role": "user", "content": task_instruction},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "exec_command",
                        "description": "Run a command in a PTY.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "cmd": {"type": "string"},
                                "max_output_tokens": {"type": "integer"},
                                "yield_time_ms": {"type": "integer"},
                            },
                            "required": ["cmd"],
                            "additionalProperties": False,
                        },
                        "strict": False,
                    },
                }
            ],
        }
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
    assert event.event_type == "openevo.session_completed"
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
            query={"event_types": ["openevo.session_completed"], "status": ["COMPLETED"]},
        )
    )

    assert ingested.ingested is True
    assert dataset.event_count == 1
    assert dataset.trace_count == 1


def test_build_terminal_bench_event_can_include_compact_llm_calls(tmp_path):
    trial_dir = _write_trial(
        tmp_path,
        stdout="Recovered the lost git commit and merged it into master.\n",
        stderr="",
        llm_calls=[
            {
                "schema_version": "v1",
                "model": "Qwen/Qwen3.6-35B-A3B",
                "input_messages": [
                    {
                        "schema_version": "v1",
                        "role": "system",
                        "content": "Solve exactly one task_id.",
                    },
                    {
                        "schema_version": "v1",
                        "role": "tool",
                        "tool_call_id": "call-1",
                        "content": "x" * 80,
                    },
                ],
                "output_messages": [{"role": "assistant", "content": ""}],
                "metadata": {
                    "step_index": 12,
                    "raw_response": {"secret": "do-not-preserve"},
                    "tool_specs": [
                        {
                            "name": "tb_exec",
                            "description": "Run a command.",
                            "parameters_schema": {
                                "type": "object",
                                "properties": {"command": {"type": "string"}},
                                "required": ["command"],
                            },
                        }
                    ],
                },
            }
        ],
    )

    [event] = build_terminal_bench_events(
        trial_dir,
        include_llm_calls=True,
        max_llm_calls=1,
        max_llm_call_message_chars=24,
    )

    trace = event.payload["session_result"]["trajectory"]["traces"][0]
    llm_calls = trace["metadata"]["llm_calls"]
    assert len(llm_calls) == 1
    assert llm_calls[0]["model"] == "Qwen/Qwen3.6-35B-A3B"
    assert llm_calls[0]["metadata"] == {
        "step_index": 12,
        "tool_specs": [
            {
                "name": "tb_exec",
                "description": "Run a command.",
                "parameters_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            }
        ],
    }
    assert llm_calls[0]["input_messages"][1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "xxxxxxxxxxx\n[truncated]\n",
    }
    assert "do-not-preserve" not in event.model_dump_json()


def test_build_terminal_bench_event_allows_llm_calls_without_transcript_when_opted_in(
    tmp_path,
):
    trial_dir = _write_trial(
        tmp_path,
        stdout="",
        stderr="",
        report=None,
        llm_calls=[
            {
                "model": "Qwen/Qwen3.6-35B-A3B",
                "input_messages": [
                    {"role": "system", "content": "Use tb_read_task first."},
                    {"role": "tool", "content": '{"stdout": "PASSWORD=8XDP..."}'},
                ],
                "metadata": {"step_index": 12},
            }
        ],
    )

    [event] = build_terminal_bench_events(trial_dir, include_llm_calls=True)

    trace = event.payload["session_result"]["trajectory"]["traces"][0]
    assert trace["response_messages"] == []
    assert trace["metadata"]["transcript_sources"] == []
    assert trace["metadata"]["llm_calls"][0]["metadata"]["step_index"] == 12


def test_build_terminal_bench_event_projects_atif_agent_turns_as_training_traces(
    tmp_path,
):
    trial_dir = _write_trial(
        tmp_path,
        stdout="Solved through Codex.\n",
        stderr="",
        atif_trajectory={
            "schema_version": "ATIF-v1.5",
            "session_id": "codex-session",
            "agent": {
                "name": "codex",
                "version": "0.144.1",
                "model_name": "gpt-5.5",
            },
            "steps": [
                {
                    "step_id": 1,
                    "source": "user",
                    "message": "Repair the certificate task.",
                },
                {
                    "step_id": 2,
                    "source": "agent",
                    "message": "I will inspect the workspace.",
                },
                {
                    "step_id": 3,
                    "source": "agent",
                    "message": "Executed exec_command call-1",
                    "tool_calls": [
                        {
                            "tool_call_id": "call-1",
                            "function_name": "exec_command",
                            "arguments": {
                                "cmd": "pwd && ls -la",
                                "yield_time_ms": 10000,
                                "max_output_tokens": 2000,
                            },
                        }
                    ],
                    "observation": {
                        "results": [
                            {
                                "source_call_id": "call-1",
                                "content": "/app\n",
                            }
                        ]
                    },
                },
                {
                    "step_id": 4,
                    "source": "agent",
                    "message": "The requested files are now verified.",
                },
            ],
        },
    )

    contract = _codex_gateway_contract("Repair the certificate task.")
    [event] = build_terminal_bench_events(
        trial_dir,
        include_atif_traces=True,
        codex_gateway_contract=contract,
    )

    trajectory = event.payload["session_result"]["trajectory"]
    assert trajectory["metadata"]["builder"] == "terminal_bench_atif_bridge"
    assert trajectory["metadata"]["trace_count"] == 2
    first, second = trajectory["traces"]
    assert first["prompt_messages"] == [
        {"role": "system", "content": "Use the Codex harness tools."},
        {"role": "user", "content": "Repair the certificate task."},
    ]
    assert first["response_messages"] == [
        {
            "role": "assistant",
            "content": "I will inspect the workspace.",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "exec_command",
                        "arguments": (
                            '{"cmd":"pwd && ls -la","max_output_tokens":2000,'
                            '"yield_time_ms":10000}'
                        ),
                    },
                }
            ],
        }
    ]
    assert first["tools"] == contract.tools
    assert first["metadata"]["harness_contract_digest"] == contract.digest
    assert first["metadata"]["atif_step_ids"] == [2, 3]
    assert second["prompt_messages"][-2:] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": first["response_messages"][0]["tool_calls"],
        },
        {"role": "tool", "content": "/app\n", "tool_call_id": "call-1"},
    ]
    assert second["prompt_messages"] == normalize_chat_messages(
        [
            *contract.messages,
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "I will inspect the workspace.",
                "tool_calls": first["response_messages"][0]["tool_calls"],
            },
            {"role": "tool", "content": "/app\n", "tool_call_id": "call-1"},
        ]
    )
    assert second["response_messages"] == [
        {
            "role": "assistant",
            "content": "The requested files are now verified.",
        }
    ]
    assert all(trace["reward"] == 0.5 for trace in trajectory["traces"])


def test_build_terminal_bench_event_prefers_structured_task_identity(tmp_path):
    trial_dir = _write_trial(
        tmp_path,
        stdout="done\n",
        stderr="",
    )
    result_path = trial_dir / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["task_name"] = "terminal-bench/fix-git"
    result_path.write_text(json.dumps(result), encoding="utf-8")

    [event] = build_terminal_bench_events(trial_dir)

    assert event.task_id == "fix-git"
    task_metadata = event.payload["session_result"]["trajectory"]["metadata"]["task_metadata"]
    assert task_metadata["task_name"] == "terminal-bench/fix-git"


def test_build_terminal_bench_event_requires_atif_when_training_projection_is_enabled(
    tmp_path,
):
    trial_dir = _write_trial(tmp_path, stdout="Only a summary.\n", stderr="")

    with pytest.raises(TerminalBenchBridgeError, match="ATIF trajectory"):
        build_terminal_bench_events(trial_dir, include_atif_traces=True)


def test_codex_gateway_projection_rejects_unavailable_custom_tools(tmp_path):
    trial_dir = _write_trial(
        tmp_path,
        stdout="Codex attempted a custom edit.\n",
        stderr="",
        atif_trajectory={
            "schema_version": "ATIF-v1.5",
            "session_id": "codex-session",
            "agent": {"name": "codex", "version": "0.144.1"},
            "steps": [
                {"step_id": 1, "source": "user", "message": "Create a file."},
                {
                    "step_id": 2,
                    "source": "agent",
                    "message": "I will patch it.",
                    "tool_calls": [
                        {
                            "tool_call_id": "call-patch",
                            "function_name": "apply_patch",
                            "arguments": {"input": "*** Begin Patch"},
                        }
                    ],
                },
            ],
        },
    )

    with pytest.raises(TerminalBenchBridgeError, match="unavailable Codex tool"):
        build_terminal_bench_events(
            trial_dir,
            include_atif_traces=True,
            codex_gateway_contract=_codex_gateway_contract("Create a file."),
        )


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
    assert trace["metadata"]["transcript_sources"] == ["agent/evolab_lab/terminal_bench_report.md"]


def test_build_terminal_bench_event_uses_codex_log_when_stdout_is_empty(tmp_path):
    trial_dir = _write_trial(
        tmp_path,
        stdout="",
        stderr="",
        report=None,
        codex='{"type":"agent_message","text":"Created /app/filter.py"}\n',
    )

    [event] = build_terminal_bench_events(trial_dir)

    trace = event.payload["session_result"]["trajectory"]["traces"][0]
    assert trace["response_messages"] == [
        {
            "role": "assistant",
            "content": '{"type":"agent_message","text":"Created /app/filter.py"}\n',
        }
    ]
    assert trace["metadata"]["transcript_sources"] == ["agent/codex.txt"]


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
    assert event_payload["event_type"] == "openevo.session_completed"
    assert (
        event_payload["payload"]["session_result"]["trajectory"]["metadata"]["capture_mode"]
        == "transcript"
    )


def test_terminal_bench_dataset_cli_ingests_events_and_creates_dataset(tmp_path):
    trial_dir = _write_trial(
        tmp_path / "job",
        stdout="Recovered the lost git commit.\n",
        stderr="",
    )
    output_path = tmp_path / "dataset.json"
    db_path = tmp_path / "openevo.db"
    artifact_root = tmp_path / "openevo_artifacts"

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


def test_terminal_bench_agent_system_job_cli_creates_audited_reflector_job(tmp_path):
    trial_dir = _write_trial(
        tmp_path / "job",
        stdout="Recovered the lost git commit.\n",
        stderr="",
    )
    output_path = tmp_path / "job.json"
    db_path = tmp_path / "openevo.db"
    artifact_root = tmp_path / "openevo_artifacts"

    exit_code = main(
        [
            "terminal-bench-agent-system-job",
            "--input",
            str(trial_dir.parent),
            "--db",
            str(db_path),
            "--artifact-root",
            str(artifact_root),
            "--dataset-name",
            "tb21_round0",
            "--policy-version",
            "tb21-round0",
            "--reflector-provider",
            "codex_cli",
            "--reflector-model",
            "gpt-5.4",
            "--codex-home",
            "/tmp/codex-home",
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    dataset_artifact_id = payload["dataset"]["artifact_id"]
    assert payload["job"]["method"] == "agent_system_reflector"
    assert payload["job"]["input_artifact_ids"] == [dataset_artifact_id]
    assert payload["job"]["config"]["reflector_llm"] == {
        "provider": "codex_cli",
        "model": "gpt-5.4",
        "codex_home": "/tmp/codex-home",
    }
    assert payload["job"]["config"]["compatibility"] == {
        "agent_harness": ["terminal-bench-harbor"],
        "task_tags": ["terminal-bench", "terminal-bench:fix-git"],
    }
    assert payload["job"]["config"]["target_path"] == "AGENTS.md"
    audit = payload["job"]["config"]["agent_system_audit"]
    assert audit["max_repair_attempts"] == 2
    assert audit["forbidden_literals"] == {}

    store = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    claimed = store.claim_job(
        WorkerClaimRequest(
            worker_id="test-worker",
            capabilities=["agent_system_reflector"],
        )
    )
    assert claimed.job is not None
    assert claimed.job.method == "agent_system_reflector"
    assert claimed.job.input_artifacts[0].artifact_id == dataset_artifact_id
    assert claimed.job.config == payload["job"]["config"]


def test_terminal_bench_agent_system_job_cli_requires_policy_version_with_input(tmp_path):
    trial_dir = _write_trial(
        tmp_path / "job",
        stdout="Recovered the lost git commit.\n",
        stderr="",
    )

    with pytest.raises(ValueError, match="requires --policy-version with --input"):
        main(
            [
                "terminal-bench-agent-system-job",
                "--input",
                str(trial_dir.parent),
                "--db",
                str(tmp_path / "openevo.db"),
                "--artifact-root",
                str(tmp_path / "openevo_artifacts"),
                "--dataset-name",
                "tb21_round0",
                "--reflector-model",
                "gpt-5.4",
            ]
        )


def test_terminal_bench_agent_system_job_cli_uses_history_method_for_multiple_datasets(
    tmp_path,
):
    db_path = tmp_path / "openevo.db"
    artifact_root = tmp_path / "openevo_artifacts"
    store = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    store.initialize()

    first_trial = _write_trial(
        tmp_path / "round1",
        stdout="Left generated file in the wrong directory.\n",
        stderr="",
    )
    second_trial = _write_trial(
        tmp_path / "round2",
        stdout="Fixed output path but missed hidden verifier checks.\n",
        stderr="",
    )
    first_artifact_id = _ingest_terminal_bench_dataset(
        store,
        first_trial,
        name="tb21_round1",
        policy_version="tb21-round1",
    )
    second_artifact_id = _ingest_terminal_bench_dataset(
        store,
        second_trial,
        name="tb21_round2",
        policy_version="tb21-round2",
    )
    output_path = tmp_path / "history_job.json"

    exit_code = main(
        [
            "terminal-bench-agent-system-job",
            "--db",
            str(db_path),
            "--artifact-root",
            str(artifact_root),
            "--dataset-artifact-id",
            first_artifact_id,
            "--dataset-artifact-id",
            second_artifact_id,
            "--reflector-model",
            "gpt-5.4",
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["dataset"] is None
    assert payload["ingested_events"] == []
    assert payload["job"]["method"] == "agent_system_history_reflector"
    assert payload["job"]["input_artifact_ids"] == [first_artifact_id, second_artifact_id]
    assert payload["job"]["config"]["reflector_llm"] == {
        "provider": "openai_chat",
        "model": "gpt-5.4",
    }
    assert payload["job"]["config"]["compatibility"]["task_tags"] == [
        "terminal-bench",
        "terminal-bench:fix-git",
    ]
    forbidden_literals = payload["job"]["config"]["agent_system_audit"]["forbidden_literals"]
    assert forbidden_literals == {}


def test_terminal_bench_text_memory_job_cli_places_new_dataset_before_history(
    tmp_path,
):
    db_path = tmp_path / "openevo.db"
    artifact_root = tmp_path / "openevo_artifacts"
    store = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    store.initialize()

    prior_trial = _write_trial(
        tmp_path / "prior",
        stdout="Edited files without focused validation.\n",
        stderr="",
    )
    prior_artifact_id = _ingest_terminal_bench_dataset(
        store,
        prior_trial,
        name="tb21_round0",
        policy_version="tb21-round0",
    )
    new_trial = _write_trial(
        tmp_path / "new",
        stdout="Ran the focused verifier before final response.\n",
        stderr="",
    )
    output_path = tmp_path / "text_memory_job.json"

    exit_code = main(
        [
            "terminal-bench-text-memory-job",
            "--input",
            str(new_trial.parent),
            "--db",
            str(db_path),
            "--artifact-root",
            str(artifact_root),
            "--dataset-artifact-id",
            prior_artifact_id,
            "--dataset-name",
            "tb21_round1",
            "--policy-version",
            "tb21-round1",
            "--reflector-provider",
            "codex_cli",
            "--reflector-model",
            "gpt-5.5",
            "--method",
            "text_memory_expel_reflector",
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    new_artifact_id = payload["dataset"]["artifact_id"]
    assert payload["job"]["method"] == "text_memory_expel_reflector"
    assert payload["job"]["input_artifact_ids"] == [new_artifact_id, prior_artifact_id]
    assert payload["job"]["config"]["reflector_llm"] == {
        "provider": "codex_cli",
        "model": "gpt-5.5",
    }
    assert payload["job"]["config"]["compatibility"] == {
        "agent_harness": ["terminal-bench-harbor"],
        "task_tags": ["terminal-bench", "terminal-bench:fix-git"],
    }
    assert payload["job"]["config"]["promoted"] is False
    assert payload["job"]["config"]["scores"] == {"quality": 0.0}

    store = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    claimed = store.claim_job(
        WorkerClaimRequest(
            worker_id="test-worker",
            capabilities=["text_memory_expel_reflector"],
        )
    )
    assert claimed.job is not None
    assert claimed.job.input_artifacts[0].artifact_id == new_artifact_id
    assert claimed.job.input_artifacts[1].artifact_id == prior_artifact_id


def test_terminal_bench_text_memory_job_cli_includes_error_trials_by_default(
    tmp_path,
):
    db_path = tmp_path / "openevo.db"
    artifact_root = tmp_path / "openevo_artifacts"
    trial_dir = _write_trial(
        tmp_path / "job",
        stdout="",
        stderr="",
        codex='{"type":"agent_message","text":"I tried recovery but the shell timed out."}\n',
        verifier_stdout="agent errored before verifier could pass\n",
    )
    result_path = trial_dir / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["exception_info"] = {"exception_message": "agent execution failed"}
    result_path.write_text(json.dumps(result), encoding="utf-8")
    output_path = tmp_path / "text_memory_error_job.json"

    exit_code = main(
        [
            "terminal-bench-text-memory-job",
            "--input",
            str(trial_dir.parent),
            "--db",
            str(db_path),
            "--artifact-root",
            str(artifact_root),
            "--dataset-name",
            "tb21_error_round",
            "--policy-version",
            "tb21-error-round",
            "--reflector-provider",
            "codex_cli",
            "--reflector-model",
            "gpt-5.5",
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["dataset"]["event_count"] == 1
    assert payload["dataset"]["trace_count"] == 1


def test_terminal_bench_task_tags_preserve_short_task_ids_from_events(tmp_path):
    store = EvolutionStore(db_path=tmp_path / "openevo.db", artifact_root=tmp_path / "artifacts")
    tags = _terminal_bench_task_tags(
        store,
        [],
        [
            EventIngestRequest(
                source="terminal_bench.harbor",
                event_type="openevo.session_completed",
                source_event_id="event-1",
                task_id="git",
            )
        ],
    )

    assert tags == ["terminal-bench", "terminal-bench:git"]


def test_terminal_bench_auto_forbidden_literals_ignore_public_task_context(tmp_path):
    trial_dir = _write_trial(
        tmp_path / "job",
        stdout="Recovered the lost git commit.\n",
        stderr="",
        ctrf={
            "results": {
                "summary": {"tests": 1, "passed": 0, "failed": 1, "skipped": 0},
                "tests": [
                    {
                        "name": "test_outputs.py::test_layout_file",
                        "status": "failed",
                        "message": "layout file missing",
                    }
                ],
            }
        },
    )
    [event] = build_terminal_bench_events(trial_dir)

    forbidden_literals = _terminal_bench_forbidden_literals_from_events([event])

    serialized = json.dumps(forbidden_literals, sort_keys=True)
    assert "fix-git" not in serialized
    assert "fix-git__abc123" not in serialized
    assert "Find the missing git changes." not in serialized
    assert "layout file missing" not in serialized


def test_terminal_bench_dataset_artifact_structured_forbidden_literals_are_decoded(
    tmp_path,
):
    db_path = tmp_path / "openevo.db"
    artifact_root = tmp_path / "openevo artifacts"
    store = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    store.initialize()

    trial_dir = _write_trial(
        tmp_path / "job",
        stdout="Recovered the lost git commit.\n",
        stderr="",
    )
    artifact_id = _ingest_terminal_bench_dataset(
        store,
        trial_dir,
        name="tb21_round0",
        policy_version="tb21-round0",
    )
    with store.connect() as conn:
        artifact_row = conn.execute(
            "SELECT uri FROM artifacts WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
    assert artifact_row is not None
    manifest_path = Path(unquote(artifact_row["uri"].removeprefix("file://")))
    records_path = manifest_path.parent / "records.jsonl"
    [record] = [
        json.loads(line)
        for line in records_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    record["payload"]["session_result"]["metadata"]["leakage_basis"] = {
        "source_files": ["heldout answer sheet.xlsx"],
        "article_title": "Secret Heldout Paper",
        "source_rows": [12],
    }
    records_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    forbidden_literals = _terminal_bench_forbidden_literals_from_dataset_artifacts(
        store,
        [artifact_id],
    )

    assert forbidden_literals["source_files"] == ["heldout answer sheet.xlsx"]
    assert forbidden_literals["article_titles"] == ["Secret Heldout Paper"]
    assert forbidden_literals["source_rows"] == ["12"]
    serialized = json.dumps(forbidden_literals, sort_keys=True)
    assert "fix-git" not in serialized
    assert "fix-git__abc123" not in serialized


def _ingest_terminal_bench_dataset(
    store: EvolutionStore,
    trial_dir: Path,
    *,
    name: str,
    policy_version: str,
) -> str:
    for event in build_terminal_bench_events(trial_dir, policy_version=policy_version):
        store.ingest_event(event)
    dataset = store.create_dataset(
        DatasetCreateRequest(
            name=name,
            purpose="agent_system_reflection",
            query={
                "event_types": ["openevo.session_completed"],
                "status": ["COMPLETED"],
                "policy_version": policy_version,
            },
        )
    )
    return dataset.artifact_id


def _write_trial(
    root: Path,
    *,
    stdout: str,
    stderr: str,
    report: str | None = "# Terminal Bench Report\n",
    codex: str = "",
    ctrf: dict | None = None,
    verifier_stdout: str = "",
    llm_calls: list[dict] | None = None,
    atif_trajectory: dict | None = None,
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
    (trial_dir / "agent" / "codex.txt").write_text(codex, encoding="utf-8")
    if report is not None:
        (trial_dir / "agent" / "evolab_lab" / "terminal_bench_report.md").write_text(
            report,
            encoding="utf-8",
        )
    if llm_calls is not None:
        trajectory_dir = (
            trial_dir / "agent" / "evolab_lab" / ".evolab" / "registries" / "trajectory"
        )
        trajectory_dir.mkdir(parents=True)
        (trajectory_dir / "llm_calls.jsonl").write_text(
            "".join(json.dumps(call) + "\n" for call in llm_calls),
            encoding="utf-8",
        )
    if atif_trajectory is not None:
        (trial_dir / "agent" / "trajectory.json").write_text(
            json.dumps(atif_trajectory),
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
