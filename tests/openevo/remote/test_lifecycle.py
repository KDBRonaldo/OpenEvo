from __future__ import annotations

import pytest

from openevo.deployment.lifecycle import (
    RemoteDaemonLaunchSpec,
    RemoteLifecycleEvent,
    RemoteLifecycleStatus,
    RemoteManagedServiceStatus,
    RemoteServiceLog,
    RemoteServiceOperationResult,
    RemoteServiceState,
    RemoteServiceStatus,
    RemoteServicesStatus,
    RemoteStatusReport,
)


def test_daemon_launch_spec_round_trips() -> None:
    spec = RemoteDaemonLaunchSpec(
        service_id="openevo-backend",
        kind="openevo_backend",
        command="openevo-backend run remote.yaml",
        cwd="/home/alice/.openevo/runs/protein/folding",
        env={"OPENEVO_MODE": "science"},
        ports={"api": 18080},
        pid_file="/tmp/openevo.pid",
        log_path="/tmp/openevo.log",
        health_check="curl -fsS http://127.0.0.1:18080/health",
        depends_on=["vllm"],
    )

    restored = RemoteDaemonLaunchSpec.model_validate(spec.model_dump(mode="json"))

    assert restored == spec


def test_status_report_ready_semantics_and_tuple_backing() -> None:
    report = RemoteStatusReport(
        remote_profile_id="lab-gpu",
        project_name="protein",
        task_id="folding",
        bootstrap_ready=True,
        workspace_ready=True,
        services=[
            RemoteServiceStatus(
                service_id="openevo-backend",
                status=RemoteLifecycleStatus.RUNNING,
                message="healthy",
            )
        ],
        events=[
            RemoteLifecycleEvent(
                level="info",
                message="bootstrap completed",
                source="bootstrap",
            )
        ],
    )

    dumped = report.model_dump(mode="json")
    restored = RemoteStatusReport.model_validate(dumped)

    assert dumped["ready"] is True
    assert isinstance(report.services, tuple)
    assert isinstance(report.events, tuple)
    assert restored == report
    with pytest.raises(AttributeError):
        report.services.append(report.services[0])


def test_status_report_not_ready_when_service_failed() -> None:
    report = RemoteStatusReport(
        remote_profile_id="lab-gpu",
        project_name="protein",
        task_id="folding",
        bootstrap_ready=True,
        workspace_ready=True,
        services=[
            RemoteServiceStatus(
                service_id="openevo-backend",
                status=RemoteLifecycleStatus.FAILED,
                message="crashed",
            )
        ],
        actionable_errors=["Restart openevo-backend after reviewing logs."],
    )

    assert report.ready is False


def test_status_report_ready_accepts_ready_service_status() -> None:
    report = RemoteStatusReport(
        remote_profile_id="lab-gpu",
        project_name="protein",
        task_id="folding",
        bootstrap_ready=True,
        workspace_ready=True,
        services=[
            RemoteServiceStatus(
                service_id="openevo-backend",
                status=RemoteLifecycleStatus.READY,
                message="ready",
            )
        ],
    )

    assert report.ready is True


def test_remote_service_lifecycle_models_round_trip() -> None:
    status = RemoteServicesStatus(
        services=[
            RemoteManagedServiceStatus(
                service_id="gateway",
                state=RemoteServiceState.READY,
                message="healthy",
                required=True,
                pid=123,
                log_path="/state/services/logs/gateway.log",
                health_check="curl -fsS http://127.0.0.1:8100/health",
            ),
            RemoteManagedServiceStatus(
                service_id="evolution_worker",
                state="running",
                message="pid alive",
                required=True,
            ),
        ]
    )
    log = RemoteServiceLog(
        service_id="gateway",
        content="Gateway ready\n",
        line_count=1,
    )
    operation = RemoteServiceOperationResult(
        service_id="gateway",
        state=RemoteServiceState.STOPPED,
        message="Gateway stopped.",
        stdout="stopped",
        stderr="",
    )

    assert status.ready is True
    assert RemoteServicesStatus.model_validate(status.model_dump(mode="json")) == status
    assert RemoteServiceLog.model_validate(log.model_dump(mode="json")) == log
    assert (
        RemoteServiceOperationResult.model_validate(
            operation.model_dump(mode="json")
        )
        == operation
    )
