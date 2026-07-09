from __future__ import annotations

from dataclasses import dataclass

import pytest

from openevo.deployment import (
    PreflightCheck,
    PreflightReport,
    RemoteCommandResult,
    RemotePreflightSettings,
    run_preflight,
)


@dataclass
class FakeProbe:
    results: dict[str, RemoteCommandResult]

    def run(
        self, command: str, *, timeout_seconds: float = 30.0
    ) -> RemoteCommandResult:
        return self.results[command]


def _ok(command: str, stdout: str = "") -> RemoteCommandResult:
    return RemoteCommandResult(command=command, return_code=0, stdout=stdout)


def _fail(command: str, stderr: str = "failed") -> RemoteCommandResult:
    return RemoteCommandResult(command=command, return_code=1, stderr=stderr)


def _ready_probe() -> FakeProbe:
    return FakeProbe(
        {
            "true": _ok("true"),
            "docker info": _ok("docker info", "Server Version: 27.0.0"),
            "docker compose version": _ok(
                "docker compose version", "Docker Compose version v2.27.0"
            ),
            "nvidia-smi -L": _ok("nvidia-smi -L", "GPU 0: NVIDIA A100"),
            'df -Pk "$HOME"': _ok(
                'df -Pk "$HOME"',
                "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                "/dev/sda1 100000000 1000 50000000 1% /home/user\n",
            ),
            "codex --version": _ok("codex --version", "codex 1.0.0"),
            "test -f ~/.codex/auth.json": _ok("test -f ~/.codex/auth.json"),
        }
    )


def test_ready_server_with_subscription_required_passes_all_checks() -> None:
    report = run_preflight(
        _ready_probe(),
        RemotePreflightSettings(require_codex_subscription=True),
    )

    assert report.ready is True
    assert [check.name for check in report.checks] == [
        "ssh",
        "docker",
        "docker_compose",
        "gpu",
        "disk",
        "codex_cli",
        "codex_subscription",
    ]
    assert {check.name: check.status for check in report.checks} == {
        "ssh": "pass",
        "docker": "pass",
        "docker_compose": "pass",
        "gpu": "pass",
        "disk": "pass",
        "codex_cli": "pass",
        "codex_subscription": "pass",
    }
    assert report.by_name("docker").command == "docker info"
    assert report.by_name("disk").command == 'df -Pk "$HOME"'
    assert "docker info" in _ready_probe().results
    assert 'df -Pk "$HOME"' in _ready_probe().results


def test_docker_permission_denied_is_user_action_failure() -> None:
    probe = _ready_probe()
    probe.results["docker info"] = _fail(
        "docker info", "permission denied while connecting to Docker daemon"
    )

    report = run_preflight(probe)
    check = report.by_name("docker")

    assert report.ready is False
    assert check.status == "fail"
    assert check.remediation_kind == "user_action"
    assert "Docker permission denied" in check.message


def test_docker_daemon_unavailable_is_user_action_failure() -> None:
    probe = _ready_probe()
    probe.results["docker info"] = _fail(
        "docker info",
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
    )

    report = run_preflight(probe)
    check = report.by_name("docker")

    assert report.ready is False
    assert check.status == "fail"
    assert check.remediation_kind == "user_action"
    assert "Docker daemon" in check.message


def test_docker_missing_is_openevo_install_failure() -> None:
    probe = _ready_probe()
    probe.results["docker info"] = _fail("docker info", "docker: command not found")

    report = run_preflight(probe)
    check = report.by_name("docker")

    assert report.ready is False
    assert check.status == "fail"
    assert check.remediation_kind == "openevo_install"
    assert "Docker is not available" in check.message


def test_missing_docker_compose_is_non_blocking_warning() -> None:
    probe = _ready_probe()
    probe.results["docker compose version"] = _fail(
        "docker compose version", "docker: 'compose' is not a docker command"
    )

    report = run_preflight(probe)
    check = report.by_name("docker_compose")

    assert report.ready is True
    assert check.status == "warn"
    assert check.remediation_kind == "openevo_install"
    assert "Docker Compose is not available" in check.message


def test_ssh_failure_short_circuits_remaining_checks() -> None:
    probe = FakeProbe({"true": _fail("true", "ssh failed")})

    report = run_preflight(probe)

    assert report.ready is False
    assert [check.name for check in report.checks] == ["ssh"]
    assert report.checks[0].status == "fail"
    assert report.checks[0].remediation_kind == "user_action"


def test_missing_codex_auth_is_user_action_failure_when_subscription_required() -> None:
    probe = _ready_probe()
    probe.results["test -f ~/.codex/auth.json"] = _fail(
        "test -f ~/.codex/auth.json", "missing"
    )

    report = run_preflight(
        probe,
        RemotePreflightSettings(require_codex_subscription=True),
    )
    check = report.by_name("codex_subscription")

    assert report.ready is False
    assert check.status == "fail"
    assert check.remediation_kind == "user_action"
    assert "Codex subscription login was not found" in check.message


def test_low_disk_available_fails_with_user_action() -> None:
    probe = _ready_probe()
    probe.results['df -Pk "$HOME"'] = _ok(
        'df -Pk "$HOME"',
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "/dev/sda1 100000000 99900000 100000 99% /home/user\n",
    )

    report = run_preflight(probe)
    check = report.by_name("disk")

    assert report.ready is False
    assert check.status == "fail"
    assert check.remediation_kind == "user_action"
    assert "available disk" in check.message


def test_codex_checks_omitted_when_subscription_not_required() -> None:
    report = run_preflight(_ready_probe())

    names = [check.name for check in report.checks]

    assert "codex_cli" not in names
    assert "codex_subscription" not in names


def test_report_by_name_raises_key_error_for_missing_check() -> None:
    report = run_preflight(_ready_probe())

    with pytest.raises(KeyError):
        report.by_name("codex_subscription")


def test_preflight_report_checks_are_tuple_backed_and_dump_as_json_array() -> None:
    report = PreflightReport(
        checks=[
            PreflightCheck(
                name="ssh",
                status="pass",
                message="Remote command execution is available.",
            )
        ]
    )

    assert isinstance(report.checks, tuple)
    dumped = report.model_dump(mode="json")
    assert dumped["ready"] is True
    assert dumped["checks"] == [
        {
            "name": "ssh",
            "status": "pass",
            "message": "Remote command execution is available.",
            "command": None,
            "remediation_kind": "none",
            "stdout": "",
            "stderr": "",
        }
    ]
    assert PreflightReport.model_validate(dumped).ready is True
    with pytest.raises(AttributeError):
        report.checks.append(
            PreflightCheck(
                name="docker",
                status="pass",
                message="Docker is available.",
            )
        )
