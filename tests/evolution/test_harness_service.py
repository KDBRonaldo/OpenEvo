from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from openevo.evolution import cli as evolution_cli
from openevo.evolution.framework import HarnessInferenceRequest
from openevo.evolution.harness_service import (
    CODEX_PROXY_BASE_URL_ENV,
    CodexInvocationResult,
    CodexProxyHarnessService,
    CodexSubscriptionHarnessService,
    HarnessInferenceError,
)
from openevo.evolution.parametric.trainer_service import (
    SubprocessSdLoraTrainerService,
)


def _completed_events(text: str) -> bytes:
    events = (
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "id": "item-1",
                "type": "agent_message",
                "text": text,
            },
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 12,
                "cached_input_tokens": 0,
                "output_tokens": 4,
                "reasoning_output_tokens": 1,
            },
        },
    )
    return b"".join(
        json.dumps(event, sort_keys=True).encode("utf-8") + b"\n"
        for event in events
    )


class _RecordingRunner:
    def __init__(self, result: CodexInvocationResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        input_bytes: bytes,
        env: dict[str, str],
        cwd: Path,
        timeout_seconds: float,
    ) -> CodexInvocationResult:
        codex_home = Path(env["CODEX_HOME"])
        self.calls.append(
            {
                "argv": argv,
                "auth": (codex_home / "auth.json").read_bytes(),
                "codex_home": codex_home,
                "cwd": cwd,
                "env": env,
                "input_bytes": input_bytes,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.result


class _ProxyRecordingRunner:
    def __init__(self, result: CodexInvocationResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        input_bytes: bytes,
        env: dict[str, str],
        cwd: Path,
        timeout_seconds: float,
    ) -> CodexInvocationResult:
        codex_home = Path(env["CODEX_HOME"])
        self.calls.append(
            {
                "argv": argv,
                "auth_exists": (codex_home / "auth.json").exists(),
                "codex_home": codex_home,
                "cwd": cwd,
                "env": env,
                "input_bytes": input_bytes,
                "timeout_seconds": timeout_seconds,
            }
        )
        return self.result


def _service(
    tmp_path: Path,
    runner: _RecordingRunner,
) -> tuple[CodexSubscriptionHarnessService, Path, bytes]:
    auth_bytes = b'{"tokens":{"access_token":"subscription-secret"}}'
    auth_path = tmp_path / "host-codex" / "auth.json"
    auth_path.parent.mkdir(mode=0o700)
    auth_path.write_bytes(auth_bytes)
    auth_path.chmod(0o600)
    scratch = tmp_path / "harness-scratch"
    scratch.mkdir(mode=0o700)
    return (
        CodexSubscriptionHarnessService(
            codex_binary="/managed/bin/codex",
            credential_source=auth_path,
            temporary_root=scratch,
            runner=runner,
        ),
        scratch,
        auth_bytes,
    )


def test_codex_harness_service_uses_closed_subscription_cli_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _RecordingRunner(
        CodexInvocationResult(
            returncode=0,
            stdout=_completed_events("evolved memory"),
            stderr=b"",
        )
    )
    service, scratch, auth_bytes = _service(tmp_path, runner)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://must-not-leak.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "http://must-not-leak.invalid")

    response = service.infer(
        HarnessInferenceRequest(
            request_id="memevolve-analysis-1",
            harness_id="codex",
            system_instruction="Analyze reusable memory architecture failures.",
            prompt="Trajectory evidence goes here.",
            model_name="gpt-5.5",
            timeout_seconds=17,
        )
    )

    assert response.model_dump(mode="json") == {
        "request_id": "memevolve-analysis-1",
        "text": "evolved memory",
        "capture_mode": "transcript",
        "transcript_ref": None,
    }
    assert len(runner.calls) == 1
    call = runner.calls[0]
    argv = call["argv"]
    assert isinstance(argv, tuple)
    assert argv[:2] == ("/managed/bin/codex", "exec")
    assert "--json" in argv
    assert "--strict-config" in argv
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert "--ephemeral" in argv
    assert "--sandbox" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    disabled = {
        argv[index + 1]
        for index, value in enumerate(argv)
        if value == "--disable"
    }
    assert {"shell_tool", "unified_exec", "standalone_web_search"} <= disabled
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert argv[argv.index("--model") + 1] == "gpt-5.5"
    assert argv[-1] == "-"
    assert call["auth"] == auth_bytes
    assert call["input_bytes"] == (
        b"Analyze reusable memory architecture failures.\n\n"
        b"Trajectory evidence goes here."
    )
    assert call["timeout_seconds"] == 17.0
    env = call["env"]
    assert isinstance(env, dict)
    assert not {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "HTTPS_PROXY",
        "HTTP_PROXY",
    }.intersection(env)
    assert call["cwd"] != call["codex_home"]
    assert list(scratch.iterdir()) == []


def test_codex_proxy_harness_service_uses_only_verified_loopback_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _ProxyRecordingRunner(
        CodexInvocationResult(
            returncode=0,
            stdout=_completed_events("locally evolved memory"),
            stderr=b"",
        )
    )
    scratch = tmp_path / "proxy-harness-scratch"
    scratch.mkdir(mode=0o700)
    service = CodexProxyHarnessService(
        proxy_base_url="http://127.0.0.1:18345/v1",
        codex_binary="/managed/bin/codex",
        temporary_root=scratch,
        runner=runner,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key-must-not-leak")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://ambient.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient.invalid")

    response = service.infer(
        HarnessInferenceRequest(
            request_id="self-deployed-analysis-1",
            harness_id="codex",
            system_instruction="Analyze reusable memory architecture failures.",
            prompt="Trajectory evidence goes here.",
            model_name="Qwen/Qwen3-0.6B",
            timeout_seconds=19,
        )
    )

    assert response.text == "locally evolved memory"
    assert len(runner.calls) == 1
    call = runner.calls[0]
    argv = call["argv"]
    assert isinstance(argv, tuple)
    overrides = [
        argv[index + 1]
        for index, value in enumerate(argv)
        if value == "-c"
    ]
    assert 'model_provider="openevo_reflector_proxy"' in overrides
    assert (
        'model_providers.openevo_reflector_proxy.base_url="http://127.0.0.1:18345/v1"'
        in overrides
    )
    assert 'model_providers.openevo_reflector_proxy.env_key="OPENAI_API_KEY"' in overrides
    assert 'model_providers.openevo_reflector_proxy.wire_api="responses"' in overrides
    assert argv[argv.index("--model") + 1] == "Qwen/Qwen3-0.6B"
    assert call["auth_exists"] is False
    env = call["env"]
    assert isinstance(env, dict)
    assert env["OPENAI_API_KEY"] != "ambient-key-must-not-leak"
    assert "OPENAI_BASE_URL" not in env
    assert "HTTPS_PROXY" not in env
    assert CODEX_PROXY_BASE_URL_ENV not in env
    assert call["cwd"] != call["codex_home"]
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize(
    "proxy_base_url",
    [
        "https://127.0.0.1:18345/v1",
        "http://localhost:18345/v1",
        "http://127.0.0.1/v1",
        "http://127.0.0.1:18345",
        "http://127.0.0.1:18345/v1?token=secret",
        "http://user@127.0.0.1:18345/v1",
    ],
)
def test_codex_proxy_harness_service_rejects_noncanonical_gateway_url(
    proxy_base_url: str,
) -> None:
    with pytest.raises(ValueError, match="proxy base URL"):
        CodexProxyHarnessService(proxy_base_url=proxy_base_url)


def test_codex_harness_service_redacts_credential_from_failure(
    tmp_path: Path,
) -> None:
    runner = _RecordingRunner(
        CodexInvocationResult(
            returncode=7,
            stdout=b"",
            stderr=b"request failed with subscription-secret",
        )
    )
    service, scratch, _auth_bytes = _service(tmp_path, runner)

    with pytest.raises(HarnessInferenceError, match="Codex harness inference failed") as exc:
        service.infer(
            HarnessInferenceRequest(
                request_id="request-failure",
                harness_id="codex",
                prompt="Reflect.",
            )
        )

    assert "subscription-secret" not in str(exc.value)
    assert list(scratch.iterdir()) == []


def test_codex_harness_service_redacts_credential_from_response(
    tmp_path: Path,
) -> None:
    runner = _RecordingRunner(
        CodexInvocationResult(
            returncode=0,
            stdout=_completed_events("subscription-secret"),
            stderr=b"",
        )
    )
    service, scratch, _auth_bytes = _service(tmp_path, runner)

    response = service.infer(
        HarnessInferenceRequest(
            request_id="redacted-response",
            harness_id="codex",
            prompt="Reflect.",
        )
    )

    assert "subscription-secret" not in response.text
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize(
    "inference_request",
    [
        HarnessInferenceRequest(
            request_id="wrong-harness",
            harness_id="claude_code",
            prompt="Reflect.",
        ),
        HarnessInferenceRequest(
            request_id="temperature",
            harness_id="codex",
            prompt="Reflect.",
            temperature=0.5,
        ),
        HarnessInferenceRequest(
            request_id="token-limit",
            harness_id="codex",
            prompt="Reflect.",
            max_output_tokens=100,
        ),
    ],
)
def test_codex_harness_service_rejects_unsupported_request_controls(
    tmp_path: Path,
    inference_request: HarnessInferenceRequest,
) -> None:
    runner = _RecordingRunner(
        CodexInvocationResult(
            returncode=0,
            stdout=_completed_events("unused"),
            stderr=b"",
        )
    )
    service, _scratch, _auth_bytes = _service(tmp_path, runner)

    with pytest.raises(HarnessInferenceError, match="does not support"):
        service.infer(inference_request)

    assert runner.calls == []


def test_codex_harness_service_requires_completed_transcript(
    tmp_path: Path,
) -> None:
    runner = _RecordingRunner(
        CodexInvocationResult(
            returncode=0,
            stdout=b'{"type":"turn.started"}\n',
            stderr=b"",
        )
    )
    service, scratch, _auth_bytes = _service(tmp_path, runner)

    with pytest.raises(HarnessInferenceError, match="completed transcript"):
        service.infer(
            HarnessInferenceRequest(
                request_id="incomplete",
                harness_id="codex",
                prompt="Reflect.",
            )
        )

    assert list(scratch.iterdir()) == []


def test_worker_injects_core_codex_harness_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class _Client:
        def __init__(self, base_url: str, *, headers=None) -> None:
            observed["client"] = (base_url, headers)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

    def fake_run_once(client, **kwargs) -> bool:
        observed["run_once"] = (client, kwargs)
        return False

    monkeypatch.delenv("OPENEVO_INTERNAL_SERVICE_IDENTITY_FD", raising=False)
    monkeypatch.setattr(evolution_cli, "EvolutionWorkerClient", _Client)
    monkeypatch.setattr(evolution_cli, "run_once", fake_run_once)
    monkeypatch.setattr(
        evolution_cli,
        "read_internal_service_identity",
        lambda **_kwargs: None,
    )

    result = evolution_cli.main(
        [
            "worker",
            "--once",
            "--artifact-root",
            os.fspath(tmp_path / "artifacts"),
        ]
    )

    assert result == 0
    _client, kwargs = observed["run_once"]
    services = kwargs["method_services"]
    assert isinstance(services.harness, CodexSubscriptionHarnessService)
    assert isinstance(services.parametric_trainer, SubprocessSdLoraTrainerService)
    artifact_root = tmp_path / "artifacts"
    assert artifact_root.is_dir()
    assert artifact_root.stat().st_mode & 0o777 == 0o700


def test_worker_injects_core_codex_proxy_harness_for_self_deployed_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class _Client:
        def __init__(self, base_url: str, *, headers=None) -> None:
            observed["client"] = (base_url, headers)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

    def fake_run_once(client, **kwargs) -> bool:
        observed["run_once"] = (client, kwargs)
        return False

    monkeypatch.delenv("OPENEVO_INTERNAL_SERVICE_IDENTITY_FD", raising=False)
    monkeypatch.setenv(CODEX_PROXY_BASE_URL_ENV, "http://127.0.0.1:18345/v1")
    monkeypatch.setattr(evolution_cli, "EvolutionWorkerClient", _Client)
    monkeypatch.setattr(evolution_cli, "run_once", fake_run_once)
    monkeypatch.setattr(
        evolution_cli,
        "read_internal_service_identity",
        lambda **_kwargs: None,
    )

    result = evolution_cli.main(
        [
            "worker",
            "--once",
            "--artifact-root",
            os.fspath(tmp_path / "artifacts"),
        ]
    )

    assert result == 0
    _client, kwargs = observed["run_once"]
    services = kwargs["method_services"]
    assert isinstance(services.harness, CodexProxyHarnessService)
