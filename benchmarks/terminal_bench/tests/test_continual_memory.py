from __future__ import annotations

import json
from pathlib import Path

import pytest

from openevo.config import TopologyConfig
from openevo.evolution.models import EventIngestRequest
from openevo.evolution.parametric.sd_lora import _training_examples
from openevo.evolution.parametric.contracts import SdLoraMethodConfig
from openevo.evolution.store import EvolutionStore
import openevo_terminal_bench.continual_memory as continual_memory_module
from openevo_terminal_bench.cli import build_parser, main
from openevo_terminal_bench.continual_memory import (
    AdapterServingSpec,
    ContinualTask,
    build_core_codex_harbor_command,
    build_vllm_command,
    continual_learning_metrics,
    parse_continual_tasks,
    run_continual_memory_eval_dry_run,
    _prepare_training_dataset,
    resolve_gateway_advertise_host,
    _source_pythonpath,
    _write_gateway_topology,
)
from openevo_terminal_bench.core_codex import build_core_codex_run
from openevo_terminal_bench.core_codex_payload import (
    build_codex_install_command,
    resolve_codex_payload,
)


_MODEL_REVISION = "cdbee75f17c01a7cc42f958dc650907174af0554"


def test_core_codex_run_uses_gateway_and_isolates_docker_bypass() -> None:
    run = build_core_codex_run(
        instruction="Repair the task.",
        model="Qwen/Qwen3-4B-Instruct-2507",
        gateway_url="http://127.0.0.1:8100/v1",
    )

    assert len(run.steps) == 2
    assert run.steps[0].env["OPENAI_BASE_URL"] == "http://127.0.0.1:8100/v1"
    assert "dangerously-bypass" not in run.steps[0].command
    assert "--dangerously-bypass-approvals-and-sandbox" in run.steps[1].command
    assert 'model_provider="harness_proxy"' in run.steps[1].command
    assert "direct_solver" not in run.steps[1].command

    core_steps = run.harness.run_steps("Repair the task.")
    assert all("dangerously-bypass" not in step.command for step in core_steps)

    remote_run = build_core_codex_run(
        instruction="Repair the task.",
        model="Qwen/Qwen3-4B-Instruct-2507",
        gateway_url="http://172.17.0.8:8100/v1",
    )
    assert "172.17.0.8" in remote_run.steps[1].env["NO_PROXY"]


def test_codex_payload_install_is_pinned_and_offline(tmp_path: Path) -> None:
    codex = tmp_path / "codex"
    codex.write_bytes(b"fixed-codex-binary")
    codex.chmod(0o755)
    rg = tmp_path / "rg"
    rg.write_bytes(b"fixed-rg-binary")
    rg.chmod(0o755)

    payload = resolve_codex_payload(
        codex_binary_path=str(codex),
        rg_binary_path=str(rg),
    )
    command = build_codex_install_command(payload, version="0.144.1")

    assert payload.codex_sha256 in command
    assert payload.rg_sha256 in command
    assert "codex-cli 0.144.1" in command
    assert "apt-get" not in command
    assert "npm" not in command
    assert "curl" not in command


def test_harbor_command_selects_only_core_codex_agent_and_gateway(tmp_path: Path) -> None:
    command = build_core_codex_harbor_command(
        job_name="continual-task",
        task_root=tmp_path / "tasks",
        task_id="password-recovery",
        jobs_dir=tmp_path / "jobs",
        model="ordinary-lora-g0",
        gateway_url="http://127.0.0.1:8100/v1",
        codex_version="0.118.0",
        terminal_bench_package_root=tmp_path / "tb-package",
    )

    assert command[command.index("--agent-import-path") + 1] == (
        "openevo_terminal_bench.core_codex_agent:OpenEvoCoreCodexAgent"
    )
    assert "gateway_url=http://127.0.0.1:8100/v1" in command
    assert "version=0.118.0" in command
    assert "--no-delete" in command
    assert not any("trainer" in argument for argument in command)
    assert not any("direct_solver" in argument for argument in command)


def test_vllm_command_serves_one_user_adapter() -> None:
    command = build_vllm_command(
        model="Qwen/Qwen3-4B-Instruct-2507",
        model_revision=_MODEL_REVISION,
        port=8000,
        maximum_model_length=16384,
        vllm_executable="/opt/vllm/bin/vllm",
        adapter=AdapterServingSpec(
            adapter_id="sd-lora-g1",
            adapter_path=Path("/srv/adapters/sd-lora-g1"),
            maximum_rank=12,
        ),
    )

    assert command[:3] == (
        "/opt/vllm/bin/vllm",
        "serve",
        "Qwen/Qwen3-4B-Instruct-2507",
    )
    assert command[command.index("--revision") + 1] == _MODEL_REVISION
    assert command[command.index("--max-lora-rank") + 1] == "16"
    assert command[command.index("--lora-modules") + 1] == (
        "sd-lora-g1=/srv/adapters/sd-lora-g1"
    )


def test_gateway_topology_is_accepted_by_core_config(tmp_path: Path) -> None:
    path = _write_gateway_topology(
        tmp_path,
        gateway_port=8100,
        gateway_advertise_host="172.17.0.8",
        vllm_url="http://127.0.0.1:8000",
        served_model="sd-lora-g1",
    )

    topology = TopologyConfig.load(path)
    node = topology.select_gateway_node("tb21-local")
    assert node.model_served == "sd-lora-g1"
    assert node.inference_base_url == "http://127.0.0.1:8000"
    assert node.host == "0.0.0.0"
    assert node.public_url == "http://172.17.0.8:8100"


def test_gateway_advertise_host_is_closed_ipv4() -> None:
    assert resolve_gateway_advertise_host("172.17.0.8") == "172.17.0.8"
    with pytest.raises(ValueError, match="IPv4"):
        resolve_gateway_advertise_host("0.0.0.0")


def test_subprocess_pythonpath_uses_absolute_core_roots() -> None:
    entries = _source_pythonpath("src:benchmarks/terminal_bench/src").split(":")

    assert all(Path(entry).is_absolute() for entry in entries)
    assert any((Path(entry) / "openevo").is_dir() for entry in entries)
    assert any((Path(entry) / "openevo_terminal_bench").is_dir() for entry in entries)


def test_continual_metrics_report_transfer_and_forgetting() -> None:
    metrics = continual_learning_metrics(
        [[1.0, 0.5], [0.25, 1.0]],
        [0.0, 0.0],
    )

    assert metrics == pytest.approx(
        {
            "baseline_average": 0.0,
            "final_average": 0.625,
            "anytime_average": 0.75,
            "forward_transfer": 0.5,
            "backward_transfer": -0.75,
            "forgetting": 0.75,
        }
    )


def test_task_stream_requires_exact_ordered_training_trials(tmp_path: Path) -> None:
    tasks = parse_continual_tasks(
        ["task-b", "task-a"],
        [f"task-a={tmp_path / 'a'}", f"task-b={tmp_path / 'b'}"],
    )

    assert [task.task_id for task in tasks] == ["task-b", "task-a"]
    with pytest.raises(ValueError, match="exactly match"):
        parse_continual_tasks(["task-a", "task-b"], [f"task-a={tmp_path / 'a'}"])


def test_training_dataset_normalizes_only_bound_legacy_task_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = EventIngestRequest(
        source="terminal_bench.harbor",
        event_type="openevo.session_completed",
        source_event_id="terminal-bench:task-a__trial",
        task_id="terminal-bench-task",
        session_id="task-a__trial",
        status="COMPLETED",
        reward=1.0,
        payload={
            "session_result": {
                "trajectory": {
                    "traces": [
                        {
                            "prompt_messages": [
                                {"role": "user", "content": "Repair task A."}
                            ],
                            "response_messages": [
                                {"role": "assistant", "content": "Completed task A."}
                            ],
                            "reward": 1.0,
                        }
                    ]
                }
            }
        },
    )
    monkeypatch.setattr(
        continual_memory_module,
        "build_terminal_bench_events",
        lambda _: [event],
    )
    store = EvolutionStore(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
    )
    store.initialize()

    claimed = _prepare_training_dataset(
        store,
        ContinualTask("task-a", tmp_path / "task-a__trial"),
    )
    examples = _training_examples((claimed,), maximum=8, minimum_reward=1.0)

    assert len(examples) == 1
    assert examples[0]["metadata"]["task_id"] == "task-a"

    other_store = EvolutionStore(
        db_path=tmp_path / "other.db",
        artifact_root=tmp_path / "other-artifacts",
    )
    other_store.initialize()
    with pytest.raises(ValueError, match="identity does not match"):
        _prepare_training_dataset(
            other_store,
            ContinualTask("task-b", tmp_path / "task-a__trial"),
        )


def test_dry_run_records_controlled_three_condition_schedule(tmp_path: Path) -> None:
    tasks = [
        ContinualTask("task-a", tmp_path / "train-a"),
        ContinualTask("task-b", tmp_path / "train-b"),
    ]
    payload = run_continual_memory_eval_dry_run(
        tasks=tasks,
        task_root=tmp_path / "tasks",
        run_root=tmp_path / "run",
        model="Qwen/Qwen3-4B-Instruct-2507",
        model_revision=_MODEL_REVISION,
        gpu="3",
        codex_version="0.118.0",
        config=SdLoraMethodConfig(
            base_model="Qwen/Qwen3-4B-Instruct-2507",
            model_revision=_MODEL_REVISION,
            rank=4,
            max_steps=2,
        ),
    )

    assert payload["conditions"] == [
        "base",
        "ordinary_sequential_lora",
        "sd_lora",
    ]
    assert payload["enabled_evolution_targets"] == ["parametric_memory"]
    assert payload["disabled_evolution_targets"] == [
        "text_memory",
        "skill_bundle",
        "agent_system",
    ]
    assert payload["training_config"]["rank"] == 4
    assert payload["training_config"]["max_steps"] == 2
    assert payload["maximum_model_length"] == 16384
    assert payload["evaluation_schedule"] == {
        "base_rows": 1,
        "post_training_rows_per_method": 2,
        "tasks_per_row": 2,
        "attempts_per_task": 1,
    }


def test_continual_cli_dry_run_writes_closed_plan(tmp_path: Path) -> None:
    output = tmp_path / "plan.json"
    assert (
        main(
            [
                "terminal-bench-continual-memory-eval",
                "--task-root",
                str(tmp_path / "tasks"),
                "--task-id",
                "task-a",
                "--training-trial",
                f"task-a={tmp_path / 'train-a'}",
                "--run-root",
                str(tmp_path / "run"),
                "--model-revision",
                _MODEL_REVISION,
                "--codex-version",
                "0.118.0",
                "--dry-run",
                "--output",
                str(output),
            ]
        )
        == 0
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["task_order"] == ["task-a"]
    assert payload["inference_path"].startswith("OpenEvo Core CodexHarness")


def test_legacy_parametric_commands_are_not_parseable() -> None:
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if action.dest == "command"  # noqa: SLF001
    )

    assert "terminal-bench-continual-memory-eval" in subparsers.choices
    assert {
        "terminal-bench-parametric-memory-job",
        "terminal-bench-task-local-parametric-memory-job",
        "terminal-bench-local-success-replay-parametric-memory-job",
        "terminal-bench-local-parametric-memory-eval",
    }.isdisjoint(subparsers.choices)
