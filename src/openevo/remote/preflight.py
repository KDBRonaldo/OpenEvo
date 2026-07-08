from __future__ import annotations

from typing import Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class RemoteCommandResult(_StrictFrozenModel):
    command: str
    return_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.return_code == 0


class RemoteProbe(Protocol):
    def run(
        self, command: str, *, timeout_seconds: float = 30.0
    ) -> RemoteCommandResult: ...


class RemotePreflightSettings(_StrictFrozenModel):
    require_codex_subscription: bool = False
    min_home_available_kb: int = Field(default=20_000_000, ge=0)


class PreflightCheck(_StrictFrozenModel):
    name: str
    status: Literal["pass", "warn", "fail"]
    message: str
    command: str | None = None
    remediation_kind: Literal[
        "none", "openevo_retry", "openevo_install", "user_action"
    ] = "none"
    stdout: str = ""
    stderr: str = ""


class PreflightReport(_StrictFrozenModel):
    checks: tuple[PreflightCheck, ...]

    @model_validator(mode="before")
    @classmethod
    def _ignore_dumped_ready(cls, value):
        if isinstance(value, dict) and "ready" in value:
            return {key: item for key, item in value.items() if key != "ready"}
        return value

    @field_validator("checks", mode="before")
    @classmethod
    def _coerce_checks_tuple(cls, value):
        if isinstance(value, list):
            return tuple(value)
        return value

    @computed_field
    @property
    def ready(self) -> bool:
        return all(check.status != "fail" for check in self.checks)

    def by_name(self, name: str) -> PreflightCheck:
        for check in self.checks:
            if check.name == name:
                return check
        raise KeyError(name)


def run_preflight(
    probe: RemoteProbe, settings: RemotePreflightSettings | None = None
) -> PreflightReport:
    resolved_settings = settings or RemotePreflightSettings()
    ssh_check = _check_ssh(probe)
    if ssh_check.status == "fail":
        return PreflightReport(checks=[ssh_check])

    checks = [
        ssh_check,
        _check_docker(probe),
        _check_docker_compose(probe),
        _check_gpu(probe),
        _check_disk(probe, resolved_settings),
    ]
    if resolved_settings.require_codex_subscription:
        checks.extend(
            [
                _check_codex_cli(probe),
                _check_codex_subscription(probe),
            ]
        )
    return PreflightReport(checks=checks)


def _check_ssh(probe: RemoteProbe) -> PreflightCheck:
    result = probe.run("true")
    if result.ok:
        return _pass("ssh", "Remote command execution is available.", result)
    return _fail(
        "ssh",
        "Remote command execution failed.",
        result,
        remediation_kind="user_action",
    )


def _check_docker(probe: RemoteProbe) -> PreflightCheck:
    result = probe.run("docker info")
    if result.ok:
        return _pass("docker", "Docker is available.", result)

    output = f"{result.stdout}\n{result.stderr}".lower()
    if "permission denied" in output:
        return _fail(
            "docker",
            "Docker permission denied. Allow the remote user to access Docker.",
            result,
            remediation_kind="user_action",
        )
    if (
        ("cannot connect" in output and "docker daemon" in output)
        or "docker.sock" in output
    ):
        return _fail(
            "docker",
            "Docker daemon is unavailable. Start Docker or check socket access.",
            result,
            remediation_kind="user_action",
        )
    if (
        "not found" in output
        or "no such file or directory" in output
        or result.return_code == 127
    ):
        return _fail(
            "docker",
            "Docker is not available.",
            result,
            remediation_kind="openevo_install",
        )
    return _fail(
        "docker",
        "Docker is not available.",
        result,
        remediation_kind="openevo_install",
    )


def _check_docker_compose(probe: RemoteProbe) -> PreflightCheck:
    result = probe.run("docker compose version")
    if result.ok:
        return _pass("docker_compose", "Docker Compose is available.", result)
    return _warn(
        "docker_compose",
        "Docker Compose is not available.",
        result,
        remediation_kind="openevo_install",
    )


def _check_gpu(probe: RemoteProbe) -> PreflightCheck:
    result = probe.run("nvidia-smi -L")
    if result.ok:
        return _pass("gpu", "NVIDIA GPU is visible.", result)
    return _fail(
        "gpu",
        "NVIDIA GPU was not detected.",
        result,
        remediation_kind="user_action",
    )


def _check_disk(
    probe: RemoteProbe, settings: RemotePreflightSettings
) -> PreflightCheck:
    result = probe.run('df -Pk "$HOME"')
    if not result.ok:
        return _fail(
            "disk",
            "Could not inspect available disk space under $HOME.",
            result,
            remediation_kind="user_action",
        )

    available_kb = _parse_home_available_kb(result.stdout)
    if available_kb is None:
        return _fail(
            "disk",
            "Could not parse available disk space under $HOME.",
            result,
            remediation_kind="user_action",
        )
    if available_kb < settings.min_home_available_kb:
        return _fail(
            "disk",
            (
                "Insufficient available disk under $HOME: "
                f"{available_kb} KiB available, "
                f"{settings.min_home_available_kb} KiB required."
            ),
            result,
            remediation_kind="user_action",
        )

    return _pass(
        "disk",
        f"$HOME has {available_kb} KiB available.",
        result,
    )


def _check_codex_cli(probe: RemoteProbe) -> PreflightCheck:
    result = probe.run("codex --version")
    if result.ok:
        return _pass("codex_cli", "Codex CLI is available.", result)
    return _fail(
        "codex_cli",
        "Codex CLI is not available.",
        result,
        remediation_kind="user_action",
    )


def _check_codex_subscription(probe: RemoteProbe) -> PreflightCheck:
    result = probe.run("test -f ~/.codex/auth.json")
    if result.ok:
        return _pass(
            "codex_subscription",
            "Codex subscription login is available.",
            result,
        )
    return _fail(
        "codex_subscription",
        "Codex subscription login was not found.",
        result,
        remediation_kind="user_action",
    )


def _parse_home_available_kb(stdout: str) -> int | None:
    lines = stdout.splitlines()
    if len(lines) < 2:
        return None
    fields = lines[1].split()
    if len(fields) < 4:
        return None
    try:
        return int(fields[3])
    except ValueError:
        return None


def _pass(name: str, message: str, result: RemoteCommandResult) -> PreflightCheck:
    return PreflightCheck(
        name=name,
        status="pass",
        message=message,
        command=result.command,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _fail(
    name: str,
    message: str,
    result: RemoteCommandResult,
    *,
    remediation_kind: Literal["openevo_retry", "openevo_install", "user_action"],
) -> PreflightCheck:
    return PreflightCheck(
        name=name,
        status="fail",
        message=message,
        command=result.command,
        remediation_kind=remediation_kind,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _warn(
    name: str,
    message: str,
    result: RemoteCommandResult,
    *,
    remediation_kind: Literal["openevo_retry", "openevo_install", "user_action"],
) -> PreflightCheck:
    return PreflightCheck(
        name=name,
        status="warn",
        message=message,
        command=result.command,
        remediation_kind=remediation_kind,
        stdout=result.stdout,
        stderr=result.stderr,
    )
