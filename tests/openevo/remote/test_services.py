from __future__ import annotations

from openevo.deployment import build_remote_bootstrap_plan
from openevo.deployment.preflight import RemoteCommandResult
from openevo.deployment.services import (
    build_remote_services_plan,
    execute_remote_services_plan,
    inspect_remote_services,
    managed_service_step_by_id,
    read_remote_service_logs,
    restart_remote_service,
    stop_remote_service,
)
from openevo.projects.science import ScienceProjectConfig
from openevo.deployment.profile import RemoteProfileConfig
from openevo.deployment.planner import build_sidecar_science_plan


def test_service_plan_starts_subscription_runtime_services() -> None:
    bootstrap_plan = _bootstrap_plan()

    plan = build_remote_services_plan(bootstrap_plan)

    assert plan.state_root.endswith("/runs/protein-design/folding-baseline")
    assert plan.topology_path == f"{plan.state_root}/services/topology.yaml"
    assert [step.id for step in plan.steps] == [
        "write_topology",
        "openevo_backend",
        "evolution_backend",
        "rollout",
        "gateway",
        "evolution_worker",
    ]
    topology = plan.steps[0]
    assert "gateway:" in topology.command
    assert "model_served: gpt-5.1-codex-mini" in topology.command
    assert "backend_url: http://127.0.0.1:8200" in topology.command
    backend = plan.step_by_id("openevo_backend")
    assert backend.manifest["service_id"] == "openevo_backend"
    assert backend.manifest["pid_path"].endswith("/services/pids/openevo_backend.pid")
    assert backend.manifest["log_path"].endswith("/services/logs/openevo_backend.log")
    assert backend.manifest["port"] == 8765
    assert 'kill -0 "$(cat' not in backend.command
    _assert_before(backend.command, "if pid <= 0:", "os.kill(pid, 0)")
    assert "openevo-backend serve --host 127.0.0.1 --port 8765" in backend.command
    assert f"--state-root {plan.state_root}" in backend.command
    assert backend.health_command is not None
    assert "http://127.0.0.1:8765/health" in backend.health_command
    gateway = plan.step_by_id("gateway")
    assert gateway.manifest["service_id"] == "gateway"
    assert gateway.manifest["pid_path"].endswith("/services/pids/gateway.pid")
    assert gateway.manifest["log_path"].endswith("/services/logs/gateway.log")
    assert gateway.manifest["port"] == 8100
    assert 'kill -0 "$(cat' not in gateway.command
    _assert_before(gateway.command, "if pid <= 0:", "os.kill(pid, 0)")
    assert "python3 -m openevo.gateway.server" in gateway.command
    assert plan.topology_path in gateway.command
    assert gateway.health_command is not None
    assert "http://127.0.0.1:8100/health" in gateway.health_command
    assert "deadline = time.monotonic() + 30" in gateway.health_command
    assert gateway.health_timeout_seconds == 35.0
    rollout = plan.step_by_id("rollout")
    assert rollout.manifest["service_id"] == "rollout"
    assert rollout.manifest["pid_path"].endswith("/services/pids/rollout.pid")
    assert rollout.manifest["log_path"].endswith("/services/logs/rollout.log")
    assert 'kill -0 "$(cat' not in rollout.command
    _assert_before(rollout.command, "if pid <= 0:", "os.kill(pid, 0)")
    assert f"python3 -m openevo.rollout.server --config {plan.topology_path}" in rollout.command
    assert (
        f"python3 -m openevo.gateway.server --config {plan.topology_path} "
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
        "openevo_backend",
        "evolution_backend",
        "rollout",
        "gateway",
        "evolution_worker",
    ]
    vllm = plan.step_by_id("vllm")
    assert vllm.manifest["service_id"] == "vllm"
    assert vllm.manifest["pid_path"].endswith("/services/pids/vllm.pid")
    assert vllm.manifest["log_path"].endswith("/services/logs/vllm.log")
    assert vllm.manifest["model"] == "Qwen/Qwen3-Coder-30B-A3B-Instruct"
    assert "pip" in vllm.command
    assert "vllm.entrypoints.openai.api_server" in vllm.command
    assert "Qwen/Qwen3-Coder-30B-A3B-Instruct" in vllm.command
    assert vllm.health_command is not None
    assert "http://127.0.0.1:8000/v1/models" in vllm.health_command
    assert "Qwen/Qwen3-Coder-30B-A3B-Instruct" in vllm.health_command
    assert "expected_model not in models" in vllm.health_command
    assert "deadline = time.monotonic() + 900" in vllm.health_command
    assert vllm.health_timeout_seconds == 905.0
    assert 'kill -0 "$(cat' not in vllm.command
    _assert_before(vllm.command, "if pid <= 0:", "os.kill(pid, 0)")
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
    assert any("python3 -m openevo.rollout.server" in command for command, _env in transport.commands)
    assert any("openevo-backend serve" in command for command, _env in transport.commands)
    assert any(
        "http://127.0.0.1:8080/health" in command
        for command, _env in transport.commands
    )


def test_execute_remote_services_plan_stops_on_required_failure() -> None:
    plan = build_remote_services_plan(_bootstrap_plan())
    transport = _RecordingTransport(fail_contains="python3 -m openevo.rollout.server")

    report = execute_remote_services_plan(plan, transport)

    assert report.ready is False
    rollout = report.step_by_id("rollout")
    assert rollout.status == "fail"
    assert rollout.stderr == "boom"
    assert [step.id for step in report.steps] == [
        "write_topology",
        "openevo_backend",
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
        fail_contains="python3 -m openevo.rollout.server",
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


def test_pid_health_command_validates_positive_pid_before_kill() -> None:
    plan = build_remote_services_plan(_bootstrap_plan())
    worker = plan.step_by_id("evolution_worker")

    assert worker.health_command is not None
    _assert_before(worker.health_command, "if pid <= 0:", "os.kill(pid, 0)")
    assert "invalid pid file" in worker.health_command


def test_managed_service_lookup_excludes_topology_step() -> None:
    plan = build_remote_services_plan(_bootstrap_plan())

    assert managed_service_step_by_id(plan, "gateway").id == "gateway"
    try:
        managed_service_step_by_id(plan, "write_topology")
    except ValueError as exc:
        assert "Unknown remote service id: write_topology" == str(exc)
    else:
        raise AssertionError("write_topology must not be a managed service")


def test_inspect_remote_services_reports_ready_running_and_failed_states() -> None:
    plan = build_remote_services_plan(_bootstrap_plan())
    transport = _LifecycleTransport(
        pid_states={
            "openevo_backend": {"pid": 127, "alive": True},
            "gateway": {"pid": 123, "alive": True},
            "rollout": {"pid": 124, "alive": True},
            "evolution_backend": {"pid": 125, "alive": True},
            "evolution_worker": {"pid": 126, "alive": False},
        },
        health_failures={"rollout": "connection refused"},
    )

    report = inspect_remote_services(transport, plan)

    services = {service.service_id: service for service in report.services}
    assert services["gateway"].state == "ready"
    assert services["gateway"].pid == 123
    assert services["rollout"].state == "degraded"
    assert services["rollout"].message == "connection refused"
    assert services["evolution_worker"].state == "stopped"
    assert report.ready is False


def test_inspect_remote_services_reports_unknown_for_malformed_inspect_output() -> None:
    plan = build_remote_services_plan(_bootstrap_plan())
    transport = _LifecycleTransport(malformed_inspect={"gateway"})

    report = inspect_remote_services(transport, plan)

    services = {service.service_id: service for service in report.services}
    assert services["gateway"].state == "unknown"
    assert services["gateway"].message == "gateway inspect returned invalid status."


def test_inspect_remote_services_reports_unknown_for_invalid_pid_without_kill() -> None:
    plan = build_remote_services_plan(_bootstrap_plan())
    transport = _LifecycleTransport(pid_states={"gateway": {"pid": 0, "alive": True}})

    report = inspect_remote_services(transport, plan)

    services = {service.service_id: service for service in report.services}
    assert services["gateway"].state == "unknown"
    assert services["gateway"].message == "invalid pid file for gateway: 0"
    gateway_inspect = next(
        command
        for command, _env in transport.commands
        if "/gateway.pid" in command and "json.dumps" in command
    )
    _assert_before(gateway_inspect, "if pid <= 0:", "os.kill(pid, 0)")


def test_read_remote_service_logs_tails_and_redacts_sensitive_headers() -> None:
    secret_proxy = "http://proxy-user:proxy-secret@127.0.0.1:7890"
    plan = build_remote_services_plan(
        _bootstrap_plan(proxy={"https_proxy": secret_proxy})
    )
    transport = _LifecycleTransport(
        log_content=(
            "Proxy http://proxy-user:proxy-secret@127.0.0.1:7890\n"
            "Authorization: Bearer secret-token\n"
            "Gateway ready\n"
        )
    )

    log = read_remote_service_logs(transport, plan, "gateway", lines=50)

    assert log.service_id == "gateway"
    assert log.line_count == 3
    assert "Gateway ready" in log.content
    assert "proxy-secret" not in log.content
    assert "Authorization: Bearer secret-token" not in log.content
    assert "Authorization: [REDACTED]" in log.content


def test_read_remote_service_logs_returns_structured_log_on_transport_exception() -> None:
    secret_proxy = "http://proxy-user:proxy-secret@127.0.0.1:7890"
    plan = build_remote_services_plan(
        _bootstrap_plan(proxy={"https_proxy": secret_proxy})
    )
    transport = _LifecycleTransport(
        log_exception=RuntimeError(
            "tail failed via http://proxy-user:proxy-secret@127.0.0.1:7890\n"
            "Authorization: Bearer secret-token"
        )
    )

    log = read_remote_service_logs(transport, plan, "gateway", lines=50)

    assert log.service_id == "gateway"
    assert log.line_count == 2
    assert "tail failed" in log.content
    assert "proxy-secret" not in log.content
    assert "secret-token" not in log.content
    assert "Authorization: [REDACTED]" in log.content


def test_stop_remote_service_kills_pid_and_missing_pid_returns_stopped() -> None:
    plan = build_remote_services_plan(_bootstrap_plan())
    transport = _LifecycleTransport(pid_states={"gateway": {"pid": 123, "alive": True}})

    stopped = stop_remote_service(transport, plan, "gateway")
    already_stopped = stop_remote_service(transport, plan, "rollout")

    assert stopped.state == "stopped"
    assert stopped.message == "gateway stopped."
    assert already_stopped.state == "stopped"
    assert already_stopped.message == "rollout is already stopped."
    assert transport.stopped_services == ["gateway"]


def test_stop_remote_service_fails_when_process_survives_grace_period() -> None:
    plan = build_remote_services_plan(_bootstrap_plan())
    transport = _LifecycleTransport(
        pid_states={"gateway": {"pid": 123, "alive": True}},
        stop_still_running={"gateway"},
    )

    result = stop_remote_service(transport, plan, "gateway")

    assert result.state == "failed"
    assert result.message == "gateway did not stop after SIGTERM."
    assert result.stderr == "gateway did not stop after SIGTERM."
    assert transport.stopped_services == []
    assert "gateway" in transport.pid_states
    stop_command = transport.commands[-1][0]
    assert "did not stop after SIGTERM" in stop_command


def test_stop_remote_service_fails_for_invalid_pid_without_signaling() -> None:
    plan = build_remote_services_plan(_bootstrap_plan())
    transport = _LifecycleTransport(pid_states={"gateway": {"pid": 0, "alive": True}})

    result = stop_remote_service(transport, plan, "gateway")

    assert result.state == "failed"
    assert result.message == "invalid pid file for gateway: 0"
    assert result.stderr == "invalid pid file for gateway: 0"
    assert transport.stopped_services == []
    assert "gateway" in transport.pid_states
    stop_command = transport.commands[-1][0]
    _assert_before(stop_command, "if pid <= 0:", "os.kill(pid, signal.SIGTERM)")


def test_restart_remote_service_stops_starts_and_checks_selected_service_only() -> None:
    plan = build_remote_services_plan(_bootstrap_plan())
    transport = _LifecycleTransport(pid_states={"gateway": {"pid": 123, "alive": True}})

    result = restart_remote_service(transport, plan, "gateway")

    assert result.state == "ready"
    assert result.message == "gateway restarted."
    assert transport.stopped_services == ["gateway"]
    assert any("python3 -m openevo.gateway.server" in command for command, _env in transport.commands)
    assert not any(
        "python3 -m openevo.rollout.server" in command for command, _env in transport.commands
    )
    assert any(
        "http://127.0.0.1:8100/health" in command
        for command, _env in transport.commands
    )


def test_restart_remote_service_does_not_start_when_stop_fails() -> None:
    plan = build_remote_services_plan(_bootstrap_plan())
    transport = _LifecycleTransport(
        pid_states={"gateway": {"pid": 123, "alive": True}},
        stop_still_running={"gateway"},
    )

    result = restart_remote_service(transport, plan, "gateway")

    assert result.state == "failed"
    assert result.message == "gateway did not stop after SIGTERM."
    assert not any(
        "python3 -m openevo.gateway.server" in command for command, _env in transport.commands
    )


def test_restart_remote_service_does_not_start_when_pid_file_is_invalid() -> None:
    plan = build_remote_services_plan(_bootstrap_plan())
    transport = _LifecycleTransport(pid_states={"gateway": {"pid": -1, "alive": True}})

    result = restart_remote_service(transport, plan, "gateway")

    assert result.state == "failed"
    assert result.message == "invalid pid file for gateway: -1"
    assert not any(
        "python3 -m openevo.gateway.server" in command for command, _env in transport.commands
    )


def test_service_lifecycle_unknown_service_id_raises_value_error() -> None:
    plan = build_remote_services_plan(_bootstrap_plan())
    transport = _LifecycleTransport()

    for operation in (
        lambda: read_remote_service_logs(transport, plan, "write_topology"),
        lambda: stop_remote_service(transport, plan, "missing"),
        lambda: restart_remote_service(transport, plan, "missing"),
    ):
        try:
            operation()
        except ValueError as exc:
            assert str(exc).startswith("Unknown remote service id:")
        else:
            raise AssertionError("unknown service id should raise ValueError")


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
                "targets": {
                    "text_memory": {
                        "enabled": True,
                        "method": "text_memory_reflector",
                        "config": {},
                    },
                    "skill_bundle": {
                        "enabled": True,
                        "method": "skill_bundle_reflector",
                        "config": {},
                    },
                    "agent_system": {
                        "enabled": True,
                        "method": "auto",
                        "config": {"target_path": "AGENTS.md"},
                    },
                    "parametric_memory": {
                        "enabled": False,
                        "method": "parametric_memory_register",
                        "config": {},
                    },
                }
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


class _LifecycleTransport(_RecordingTransport):
    def __init__(
        self,
        *,
        pid_states: dict[str, dict[str, object]] | None = None,
        health_failures: dict[str, str] | None = None,
        log_content: str = "",
        log_exception: Exception | None = None,
        malformed_inspect: set[str] | None = None,
        stop_still_running: set[str] | None = None,
    ) -> None:
        super().__init__()
        self.pid_states = pid_states or {}
        self.health_failures = health_failures or {}
        self.log_content = log_content
        self.log_exception = log_exception
        self.malformed_inspect = malformed_inspect or set()
        self.stop_still_running = stop_still_running or set()
        self.stopped_services: list[str] = []

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        self.commands.append((command, env))
        service_id = _service_id_from_command(command)
        if "json.dumps" in command and "pid_path =" in command:
            if service_id in self.malformed_inspect:
                return RemoteCommandResult(
                    command=command,
                    return_code=0,
                    stdout="not-json",
                )
            state = self.pid_states.get(service_id or "", {})
            pid = state.get("pid")
            if isinstance(pid, int) and not isinstance(pid, bool) and pid <= 0:
                return RemoteCommandResult(
                    command=command,
                    return_code=0,
                    stdout=__import__("json").dumps(
                        {
                            "pid_exists": True,
                            "pid": pid,
                            "alive": False,
                            "invalid_pid": True,
                            "message": f"invalid pid file for {service_id}: {pid}",
                        }
                    ),
                )
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=__import__("json").dumps(
                    {
                        "pid_exists": service_id in self.pid_states,
                        "pid": state.get("pid"),
                        "alive": bool(state.get("alive")),
                    }
                ),
            )
        if command.startswith("if [ -f ") and "tail -n" in command:
            if self.log_exception is not None:
                raise self.log_exception
            return RemoteCommandResult(command=command, return_code=0, stdout=self.log_content)
        if "os.kill(pid, signal.SIGTERM)" in command:
            if service_id not in self.pid_states:
                return RemoteCommandResult(
                    command=command,
                    return_code=0,
                    stdout=f"{service_id} is already stopped.",
                )
            pid = self.pid_states[service_id].get("pid")
            if isinstance(pid, int) and not isinstance(pid, bool) and pid <= 0:
                return RemoteCommandResult(
                    command=command,
                    return_code=1,
                    stderr=f"invalid pid file for {service_id}: {pid}",
                )
            if service_id in self.stop_still_running:
                return RemoteCommandResult(
                    command=command,
                    return_code=1,
                    stderr=f"{service_id} did not stop after SIGTERM.",
                )
            self.stopped_services.append(service_id or "")
            self.pid_states.pop(service_id or "", None)
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=f"{service_id} stopped.",
            )
        if service_id in self.health_failures and (
            "/health" in command or "pid_path =" in command
        ):
            return RemoteCommandResult(
                command=command,
                return_code=1,
                stderr=self.health_failures[service_id],
            )
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")


def _assert_before(command: str, earlier: str, later: str) -> None:
    assert earlier in command
    assert later in command
    assert command.index(earlier) < command.index(later)


def _service_id_from_command(command: str) -> str | None:
    url_services = {
        "127.0.0.1:8765": "openevo_backend",
        "127.0.0.1:8200": "evolution_backend",
        "127.0.0.1:8080": "rollout",
        "127.0.0.1:8100": "gateway",
        "127.0.0.1:8000": "vllm",
    }
    for url_fragment, service_id in url_services.items():
        if url_fragment in command:
            return service_id
    for service_id in (
        "openevo_backend",
        "evolution_backend",
        "evolution_worker",
        "gateway",
        "rollout",
        "vllm",
    ):
        if f"/{service_id}.pid" in command or f"/{service_id}.log" in command:
            return service_id
        if f" {service_id} " in command:
            return service_id
    return None
