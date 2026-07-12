from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from openevo.gateway.node import GatewayNodeManager
from openevo.gateway.session import SessionRegistry
from openevo.gateway.storage import SessionStore
from openevo.harness.models import AgentSpec
from openevo.rollout.models import SessionDispatchRequest, SessionStatus
from openevo.runtime.base import BaseRuntime
from openevo.runtime.models import ExecInput, ExecResult, PrepareAction, RuntimeSpec
from openevo.trajectory.models import StrategySpec
from openevo.trajectory.registry import (
    default_builder_registry,
    default_evaluator_registry,
)


class RecordingRuntime(BaseRuntime):
    def __init__(
        self,
        spec: RuntimeSpec,
        session_id: str,
        session_dir: Path,
        events: list[str],
    ) -> None:
        super().__init__(spec, session_id, session_dir)
        self.events = events

    @property
    def runtime_id(self) -> str:
        return f"recording-{self.session_id}"

    async def start(self) -> None:
        self.events.append("start")

    async def stop(self) -> None:
        self.events.append("stop")

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> ExecResult:
        self.events.append(f"exec:{command}")
        if command == "run-agent":
            stdout = json.dumps(
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": "lifecycle transcript captured",
                    },
                }
            )
            return ExecResult(stdout=stdout, return_code=0)
        return ExecResult(stdout="prepared", return_code=0)

    async def upload_file(self, local_path: str, remote_path: str) -> None:
        raise AssertionError("upload_file was not requested")

    async def upload_dir(self, local_path: str, remote_path: str) -> None:
        raise AssertionError("upload_dir was not requested")

    async def download_file(self, remote_path: str, local_path: str) -> None:
        raise AssertionError("download_file was not requested")

    async def download_dir(self, remote_path: str, local_path: str) -> None:
        raise AssertionError("download_dir was not requested")


@pytest.mark.asyncio
async def test_dispatch_runs_runtime_lifecycle_and_builds_stdout_trajectory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    for name in (
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
    ):
        monkeypatch.delenv(name, raising=False)

    def recording_runtime_factory(
        spec: RuntimeSpec,
        session_id: str,
        session_dir: Path,
    ) -> RecordingRuntime:
        events.append("factory")
        return RecordingRuntime(spec, session_id, session_dir, events)

    monkeypatch.setattr(
        "openevo.gateway.node.create_runtime",
        recording_runtime_factory,
    )
    registry = SessionRegistry()
    manager = GatewayNodeManager(
        node_id="gateway-probe",
        gateway_url="http://gateway.test",
        max_init_workers=1,
        max_run_workers=1,
        max_postrun_workers=1,
        storage=SessionStore(),
        session_registry=registry,
        builders=default_builder_registry(),
        evaluators=default_evaluator_registry(),
        session_base_dir=str(tmp_path),
    )
    request = SessionDispatchRequest(
        session_id="lifecycle-probe",
        task_id="task-probe",
        instruction="Exercise the gateway lifecycle.",
        remaining_timeout_seconds=10,
        runtime=RuntimeSpec(
            image="probe-image",
            prepare=[PrepareAction(type="exec", command="prepare-runtime")],
        ),
        agent=AgentSpec(
            harness="shell",
            settings={"capture_mode": "transcript"},
            custom_shell=ExecInput(command="run-agent"),
        ),
        builder=StrategySpec(strategy="agent_transcript"),
    )

    await manager.start()
    try:
        await manager.dispatch(request)
        async with asyncio.timeout(5):
            while True:
                session = registry.get(request.session_id)
                if session is not None and session.status in SessionStatus.terminal():
                    break
                await asyncio.sleep(0.01)
        async with asyncio.timeout(5):
            while await manager.active_sessions() != 0:
                await asyncio.sleep(0.01)

        assert session is not None
        assert session.result is not None
        assert session.result.status == SessionStatus.COMPLETED
        trajectory = session.result.trajectory
        assert trajectory.status == "COMPLETED"
        assert trajectory.metadata["builder"] == "agent_transcript"
        assert trajectory.metadata["capture_mode"] == "transcript"
        assert trajectory.metadata["token_level_metrics_available"] is False
        assert trajectory.traces[0].prompt_messages == [
            {"role": "user", "content": request.instruction}
        ]
        assert trajectory.traces[0].response_messages == [
            {
                "role": "assistant",
                "content": "lifecycle transcript captured",
            }
        ]
        assert manager.storage.get_session_metadata(request.session_id) is None
        assert list(tmp_path.iterdir()) == []
    finally:
        await manager.close()

    assert events == [
        "factory",
        "start",
        "exec:prepare-runtime",
        "exec:run-agent",
        "stop",
    ]
