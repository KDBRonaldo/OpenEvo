from __future__ import annotations

from openevo.remote import build_remote_bootstrap_plan
from openevo.remote.preflight import RemoteCommandResult
from openevo.remote.services import (
    build_remote_services_plan,
    execute_remote_services_plan,
)
from openevo.science import ScienceProjectConfig
from openevo.sidecar.models import RemoteProfileConfig
from openevo.sidecar.planner import build_sidecar_science_plan


def test_service_plan_starts_subscription_runtime_services() -> None:
    bootstrap_plan = _bootstrap_plan()

    plan = build_remote_services_plan(bootstrap_plan)

    assert plan.state_root.endswith("/runs/protein-design/folding-baseline")
    assert plan.topology_path == f"{plan.state_root}/services/topology.yaml"
    assert [step.id for step in plan.steps] == [
        "write_topology",
        "evolution_backend",
        "rollout",
        "gateway",
        "evolution_worker",
    ]
    topology = plan.steps[0]
    assert "gateway:" in topology.command
    assert "model_served: gpt-5.1-codex-mini" in topology.command
    assert "backend_url: http://127.0.0.1:8200" in topology.command
    gateway = plan.step_by_id("gateway")
    assert "polar serve_gateway" in gateway.command
    assert plan.topology_path in gateway.command
    assert gateway.health_command is not None
    assert "http://127.0.0.1:8100/health" in gateway.health_command
    assert "deadline = time.monotonic() + 30" in gateway.health_command
    assert gateway.health_timeout_seconds == 35.0
    rollout = plan.step_by_id("rollout")
    assert f"polar serve_rollout --config {plan.topology_path}" in rollout.command
    assert (
        f"polar serve_gateway --config {plan.topology_path} "
        "--node-id desktop-node"
    ) in gateway.command


def test_service_plan_starts_vllm_for_managed_local_inference() -> None:
    bootstrap_plan = _bootstrap_plan(
        execution={
            "mode": "codex_managed_local_inference",
            "hf_model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        }
    )

    plan = build_remote_services_plan(bootstrap_plan)

    assert [step.id for step in plan.steps] == [
        "write_topology",
        "vllm",
        "evolution_backend",
        "rollout",
        "gateway",
        "evolution_worker",
    ]
    vllm = plan.step_by_id("vllm")
    assert "pip" in vllm.command
    assert "vllm.entrypoints.openai.api_server" in vllm.command
    assert "Qwen/Qwen3-Coder-30B-A3B-Instruct" in vllm.command
    assert vllm.health_command is not None
    assert "http://127.0.0.1:8000/v1/models" in vllm.health_command
    assert "Qwen/Qwen3-Coder-30B-A3B-Instruct" in vllm.health_command
    assert "expected_model not in models" in vllm.health_command
    assert "deadline = time.monotonic() + 900" in vllm.health_command
    assert vllm.health_timeout_seconds == 905.0
    topology = plan.step_by_id("write_topology")
    assert "engine: vllm" in topology.command
    assert "model_served: Qwen/Qwen3-Coder-30B-A3B-Instruct" in topology.command


def test_execute_remote_services_plan_runs_start_and_health_checks() -> None:
    plan = build_remote_services_plan(_bootstrap_plan())
    transport = _RecordingTransport()

    report = execute_remote_services_plan(plan, transport)

    assert report.ready is True
    assert [step.status for step in report.steps] == ["pass"] * len(plan.steps)
    assert transport.commands[0][0] == plan.steps[0].command
    assert any("polar serve_rollout" in command for command, _env in transport.commands)
    assert any(
        "http://127.0.0.1:8080/health" in command
        for command, _env in transport.commands
    )


def test_execute_remote_services_plan_stops_on_required_failure() -> None:
    plan = build_remote_services_plan(_bootstrap_plan())
    transport = _RecordingTransport(fail_contains="polar serve_rollout")

    report = execute_remote_services_plan(plan, transport)

    assert report.ready is False
    rollout = report.step_by_id("rollout")
    assert rollout.status == "fail"
    assert rollout.stderr == "boom"
    assert [step.id for step in report.steps] == [
        "write_topology",
        "evolution_backend",
        "rollout",
    ]
    assert report.next_actions == ("Fix remote service failure and restart services.",)


def test_execute_remote_services_plan_redacts_proxy_secrets() -> None:
    secret_proxy = "http://proxy-user:proxy-secret@127.0.0.1:7890"
    plan = build_remote_services_plan(
        _bootstrap_plan(proxy={"https_proxy": secret_proxy})
    )
    transport = _RecordingTransport(
        fail_contains="polar serve_rollout",
        failure_stderr=(
            "Proxy http://proxy-user:proxy-secret@127.0.0.1:7890 "
            "failed for https://download-user:download-secret@example.test/pkg"
        ),
    )

    report = execute_remote_services_plan(plan, transport)

    rollout = report.step_by_id("rollout")
    assert "proxy-secret" not in rollout.stderr
    assert "download-secret" not in rollout.stderr
    assert "[REDACTED]" in rollout.stderr


def _bootstrap_plan(
    *,
    execution: dict | None = None,
    proxy: dict | None = None,
):
    project = ScienceProjectConfig.model_validate(
        {
            "version": 1,
            "project": {"name": "Protein Design"},
            "remote_profile": "science-team",
            "task": {
                "id": "folding-baseline",
                "objective": "Improve the folding baseline.",
                "source": {"type": "remote_path", "path": "/datasets/folding"},
            },
            "execution": execution
            or {
                "mode": "codex_subscription_transcript",
                "codex_model": "gpt-5.1-codex-mini",
            },
            "evolution": {
                "text_memory": True,
                "skill_bundle": True,
                "agent_system": True,
                "parametric_memory": False,
            },
        }
    )
    profile = RemoteProfileConfig.model_validate(
        {
            "version": 1,
            "id": "science-team",
            "host": "gpu.example.edu",
            "user": "alice",
            "workspace_root": "/home/alice/.openevo/workspaces",
            "proxy": proxy or {"https_proxy": "http://127.0.0.1:7890"},
        }
    )
    return build_remote_bootstrap_plan(build_sidecar_science_plan(project, profile))


class _RecordingTransport:
    def __init__(
        self,
        *,
        fail_contains: str | None = None,
        failure_stderr: str = "boom",
    ) -> None:
        self.fail_contains = fail_contains
        self.failure_stderr = failure_stderr
        self.commands: list[tuple[str, dict[str, str] | None]] = []

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        self.commands.append((command, env))
        if self.fail_contains is not None and self.fail_contains in command:
            return RemoteCommandResult(
                command=command,
                return_code=1,
                stderr=self.failure_stderr,
            )
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")

    def upload_dir(self, local_path: str, remote_path: str) -> None:
        raise AssertionError("service supervisor must not upload directories")
