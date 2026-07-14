from __future__ import annotations

from dataclasses import asdict
import json
import sys

import pytest
from pydantic import SecretStr

from openevo.deployment import core_control
from openevo.backend.service import CoreServiceError, CoreServiceErrorCode
from openevo.deployment.core_control import (
    CoreControlBootstrapError,
    CoreControlBootstrapErrorCode,
    build_core_control_bootstrap_plan,
    execute_core_control_bootstrap,
    open_core_control_tunnel,
    parse_core_control_attachment,
)
from openevo.deployment.preflight import RemoteCommandResult


def _attachment_json(**updates: object) -> str:
    payload: dict[str, object] = {
        "schema_version": 1,
        "host": "127.0.0.1",
        "port": 8765,
        "release_identity": "a" * 64,
        "registry_digest": "b" * 64,
        "source_commit": "1" * 40,
        "generation": "c" * 32,
        "status_proof": "d" * 64,
        "attached": True,
        "bearer_token": "E" * 64,
        "execution_mode": "subscription",
        "capture_mode": "transcript",
    }
    payload.update(updates)
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


class FakeTunnel:
    base_url = "http://openevo-core.local"

    def __init__(self) -> None:
        self.closed = False
        self.authority_checks = 0

    def verify_authority(self) -> None:
        self.authority_checks += 1

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, stdout: str, *, return_codes: list[int] | None = None) -> None:
        self.stdout = stdout
        self.return_codes = list(return_codes or [])
        self.commands: list[str] = []
        self.timeouts: list[float] = []
        self.tunnel_kwargs: dict[str, object] = {}
        self.secret_commands: list[str] = []
        self.tunnel = FakeTunnel()

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        del cwd, env
        self.commands.append(command)
        self.timeouts.append(timeout_seconds)
        return RemoteCommandResult(
            command=command,
            return_code=self.return_codes.pop(0) if self.return_codes else 0,
            stdout='{"bootstrapped":true,"schema_version":1}',
        )

    def open_core_tunnel(self, **kwargs: object) -> object:
        self.tunnel_kwargs = kwargs
        return self.tunnel

    def run_secret(self, command: str, *, timeout_seconds: float = 30.0) -> SecretStr:
        self.secret_commands.append(command)
        self.timeouts.append(timeout_seconds)
        return SecretStr(self.stdout)

    def upload_dir(self, local_path: str, remote_path: str) -> None:
        del local_path, remote_path


def test_core_bootstrap_uses_one_host_locked_command_and_secret_channel() -> None:
    plan = build_core_control_bootstrap_plan(
        wheel_path="/home/user/upload/openevo.whl",
        framework_lock="/home/user/upload/framework-lock.json",
        service_root="/home/user/.openevo/core",
        source_commit="1" * 40,
    )
    transport = FakeTransport(_attachment_json())

    attachment = execute_core_control_bootstrap(plan, transport)

    assert plan.port == 0
    assert attachment.remote_host == "127.0.0.1"
    assert attachment.remote_port == 8765
    assert attachment.execution_mode == "subscription"
    assert attachment.capture_mode == "transcript"
    assert attachment.bearer_token == "E" * 64
    assert "bearer_token" not in repr(attachment)
    assert "E" * 64 not in repr(asdict(attachment))
    assert len(transport.commands) == 1
    assert "openevo.backend.service bootstrap" in transport.commands[0]
    assert "--wheel-path" in transport.commands[0]
    assert "--port 0" in transport.commands[0]
    assert len(transport.secret_commands) == 1
    assert "consume-attachment" in transport.secret_commands[0]
    combined = " ".join(transport.commands)
    assert "gateway" not in combined
    assert "worker" not in combined
    assert "vllm" not in combined.lower()
    assert all(0 < timeout <= plan.deadline_seconds for timeout in transport.timeouts)


def test_bootstrap_does_not_expose_bearer_in_normal_command_result() -> None:
    plan = build_core_control_bootstrap_plan(
        wheel_path="/home/user/upload/openevo.whl",
        framework_lock="/home/user/upload/framework-lock.json",
        service_root="/home/user/.openevo/core",
        source_commit="1" * 40,
    )
    transport = FakeTransport(_attachment_json())

    execute_core_control_bootstrap(plan, transport)

    assert len(transport.commands) == 1
    assert transport.stdout not in repr(transport.run(transport.commands[0]))
    assert "E" * 64 not in " ".join(transport.commands)


def test_core_bootstrap_parser_rejects_duplicate_oversized_and_bad_bearer() -> None:
    duplicate = _attachment_json()[:-1] + ',"port":9999}'
    for payload in (duplicate, "x" * 5000, _attachment_json(bearer_token="short")):
        with pytest.raises(CoreControlBootstrapError) as exc_info:
            parse_core_control_attachment(SecretStr(payload))
        assert exc_info.value.code is CoreControlBootstrapErrorCode.RESPONSE_INVALID
        assert "short" not in str(exc_info.value)


def test_core_bootstrap_failure_does_not_expose_command_or_paths() -> None:
    plan = build_core_control_bootstrap_plan(
        wheel_path="/secret/upload/openevo.whl",
        framework_lock="/secret/upload/framework-lock.json",
        service_root="/secret/home/.openevo/core",
        source_commit="1" * 40,
    )

    class FailingTransport(FakeTransport):
        def run(self, command: str, **kwargs: object) -> RemoteCommandResult:
            del kwargs
            return RemoteCommandResult(
                command=command,
                return_code=1,
                stderr="Authorization: Bearer super-secret /secret/home",
            )

    with pytest.raises(CoreControlBootstrapError) as exc_info:
        execute_core_control_bootstrap(plan, FailingTransport(""))
    rendered = str(exc_info.value)
    assert "super-secret" not in rendered
    assert "/secret" not in rendered
    assert "pip" not in rendered


def test_tunnel_authenticates_generation_and_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = parse_core_control_attachment(SecretStr(_attachment_json()))
    transport = FakeTransport("")
    calls: list[dict[str, object]] = []

    def authenticate(**kwargs: object) -> str:
        calls.append(kwargs)
        return attachment.status_proof

    monkeypatch.setattr(core_control, "authenticate_core_service_endpoint", authenticate)
    tunnel = open_core_control_tunnel(attachment, transport)
    assert tunnel.base_url == transport.tunnel.base_url
    assert tunnel.generation == attachment.generation
    assert tunnel.bearer_token == attachment.bearer_token
    assert "E" * 64 not in repr(tunnel)
    assert calls[0]["generation"] == attachment.generation
    assert calls[0]["release_identity"] == attachment.release_identity
    assert calls[0]["registry_digest"] == attachment.registry_digest
    assert calls[0]["endpoint"] is transport.tunnel
    assert transport.tunnel.authority_checks == 1
    assert transport.tunnel_kwargs == {
        "remote_port": 8765,
        "remote_host": "127.0.0.1",
        "wait_for_ready": True,
        "timeout_seconds": 10.0,
    }


def test_tunnel_rejects_restarted_or_wrong_core_and_closes_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = parse_core_control_attachment(SecretStr(_attachment_json()))
    transport = FakeTransport("")
    monkeypatch.setattr(
        core_control,
        "authenticate_core_service_endpoint",
        lambda **_kwargs: "0" * 64,
    )

    with pytest.raises(CoreControlBootstrapError) as exc_info:
        open_core_control_tunnel(attachment, transport)

    assert exc_info.value.code is CoreControlBootstrapErrorCode.RESPONSE_INVALID
    assert transport.tunnel.closed is True
    assert "E" * 64 not in repr(exc_info.value)


def test_tunnel_rejects_base_url_not_bound_to_verified_local_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = parse_core_control_attachment(SecretStr(_attachment_json()))
    transport = FakeTransport("")
    transport.tunnel.base_url = "http://127.0.0.1:43124"
    monkeypatch.setattr(
        core_control,
        "authenticate_core_service_endpoint",
        lambda **_kwargs: attachment.status_proof,
    )

    with pytest.raises(CoreControlBootstrapError) as exc_info:
        open_core_control_tunnel(attachment, transport)

    assert exc_info.value.code is CoreControlBootstrapErrorCode.RESPONSE_INVALID
    assert transport.tunnel.closed is True


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (
            CoreServiceError(
                CoreServiceErrorCode.DEADLINE_EXCEEDED,
                "deadline",
                retryable=True,
            ),
            CoreControlBootstrapErrorCode.DEADLINE_EXCEEDED,
        ),
        (RuntimeError("ssh daemon exited"), CoreControlBootstrapErrorCode.SERVICE_FAILED),
    ],
)
def test_tunnel_authentication_preserves_retryable_failures_and_closes(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected_code: CoreControlBootstrapErrorCode,
) -> None:
    attachment = parse_core_control_attachment(SecretStr(_attachment_json()))
    transport = FakeTransport("")

    def fail_authenticate(**_kwargs: object) -> str:
        raise failure

    monkeypatch.setattr(core_control, "authenticate_core_service_endpoint", fail_authenticate)

    with pytest.raises(CoreControlBootstrapError) as exc_info:
        open_core_control_tunnel(attachment, transport)

    assert exc_info.value.code is expected_code
    assert exc_info.value.retryable is True
    assert transport.tunnel.closed is True


def test_tunnel_authentication_base_exception_closes_before_propagation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = parse_core_control_attachment(SecretStr(_attachment_json()))
    transport = FakeTransport("")

    class Cancelled(BaseException):
        pass

    def cancel(**_kwargs: object) -> str:
        raise Cancelled

    monkeypatch.setattr(core_control, "authenticate_core_service_endpoint", cancel)

    with pytest.raises(Cancelled):
        open_core_control_tunnel(attachment, transport)

    assert transport.tunnel.closed is True
    assert sys.exc_info() == (None, None, None)
