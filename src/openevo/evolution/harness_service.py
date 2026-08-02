"""Core-owned harness inference services for context evolution methods."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from openevo.codex_models import codex_cli_model_name, validate_codex_model_ref
from openevo.evolution.framework.execution import (
    HarnessInferenceRequest,
    HarnessInferenceResponse,
)
from openevo.gateway.session_files import (
    CredentialRedactor,
    SessionFileSecurityError,
    capture_session_root_identity,
    remove_credential_tree,
    remove_session_tree,
    stage_codex_subscription_auth,
)
from openevo.runtime.managed import MANAGED_CODEX_DEFAULT_MODEL


_MAX_CAPTURE_BYTES = 2 * 1024 * 1024
_MAX_ERROR_TEXT_BYTES = 4096
_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "standalone_web_search",
)
CORE_GATEWAY_BASE_URL_ENV = "OPENEVO_EVOLUTION_HARNESS_GATEWAY_BASE_URL"
_CODEX_PROXY_API_KEY = "openevo-internal-reflector"


class HarnessInferenceError(RuntimeError):
    """Raised when Core cannot prove one closed harness inference result."""


@dataclass(frozen=True, slots=True)
class CodexInvocationResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class CodexInvocationRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        input_bytes: bytes,
        env: dict[str, str],
        cwd: Path,
        timeout_seconds: float,
    ) -> CodexInvocationResult: ...


class _CodexHarnessServiceBase:
    def __init__(
        self,
        *,
        codex_binary: str,
        temporary_root: Path | None,
        runner: CodexInvocationRunner | None,
    ) -> None:
        if (
            not codex_binary
            or "\x00" in codex_binary
            or any(ord(char) < 0x20 for char in codex_binary)
        ):
            raise ValueError("Codex harness executable is invalid")
        if temporary_root is not None and not temporary_root.is_absolute():
            raise ValueError("Codex harness temporary root must be absolute")
        self._codex_binary = codex_binary
        self._temporary_root = temporary_root
        self._runner = runner or _BoundedCodexInvocationRunner()

    @staticmethod
    def _validate_request(request: HarnessInferenceRequest) -> None:
        if request.harness_id != "codex":
            raise HarnessInferenceError(
                f"Codex harness service does not support {request.harness_id!r}"
            )
        if request.temperature is not None:
            raise HarnessInferenceError(
                "Codex harness service does not support temperature control"
            )
        if request.max_output_tokens is not None:
            raise HarnessInferenceError(
                "Codex harness service does not support max_output_tokens control"
            )

    @staticmethod
    def _model_name(value: str | None) -> str:
        try:
            validated = validate_codex_model_ref(
                value or MANAGED_CODEX_DEFAULT_MODEL,
                field_name="Codex harness model",
            )
        except ValueError as exc:
            raise HarnessInferenceError(
                "Codex harness service does not support the requested model"
            ) from exc
        return codex_cli_model_name(validated)

    def _argv(
        self,
        *,
        model: str,
        workspace: Path,
    ) -> tuple[str, ...]:
        disabled = tuple(
            part
            for feature in _DISABLED_FEATURES
            for part in ("--disable", feature)
        )
        return (
            self._codex_binary,
            "exec",
            "--json",
            "--strict-config",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--sandbox",
            "read-only",
            *disabled,
            "--skip-git-repo-check",
            "--cd",
            os.fspath(workspace),
            "--model",
            model,
            "-",
        )


class CodexSubscriptionHarnessService(_CodexHarnessServiceBase):
    """Run model-only Codex subscription turns for verified method plugins.

    The method request controls generation text and model identity only. Core
    owns the executable, credential source, process environment, filesystem
    roots, and CLI feature profile.
    """

    def __init__(
        self,
        *,
        codex_binary: str = "codex",
        credential_source: Path | None = None,
        temporary_root: Path | None = None,
        runner: CodexInvocationRunner | None = None,
    ) -> None:
        source = credential_source or (Path.home() / ".codex" / "auth.json")
        if not source.is_absolute():
            raise ValueError("Codex harness credential source must be absolute")
        super().__init__(
            codex_binary=codex_binary,
            temporary_root=temporary_root,
            runner=runner,
        )
        self._credential_source = source

    def infer(self, request: HarnessInferenceRequest) -> HarnessInferenceResponse:
        checked = HarnessInferenceRequest.model_validate(request)
        self._validate_request(checked)
        model = self._model_name(checked.model_name)
        prompt = _render_prompt(checked)

        run_root: Path | None = None
        run_identity = None
        credential_dir: Path | None = None
        credential_identity = None
        auth_identity = None
        failure: BaseException | None = None
        response: HarnessInferenceResponse | None = None
        try:
            run_root = Path(
                mkdtemp(
                    prefix="openevo-method-codex-",
                    dir=self._temporary_root,
                )
            )
            os.chmod(run_root, 0o700)
            run_identity = capture_session_root_identity(run_root)
            credential_dir = run_root / "credentials"
            workspace = run_root / "workspace"
            home = run_root / "home"
            temporary = run_root / "tmp"
            for directory in (credential_dir, workspace, home, temporary):
                directory.mkdir(mode=0o700)
            credential_identity = capture_session_root_identity(credential_dir)
            staged = stage_codex_subscription_auth(
                source=self._credential_source,
                session_dir=credential_dir,
                session_identity=credential_identity,
                target_home_parts=(),
            )
            auth_identity = staged.auth_identity
            result = self._runner.run(
                self._argv(model=model, workspace=workspace),
                input_bytes=prompt,
                env=_controlled_codex_environment(
                    codex_home=credential_dir,
                    home=home,
                    temporary=temporary,
                ),
                cwd=workspace,
                timeout_seconds=checked.timeout_seconds,
            )
            if result.returncode != 0:
                detail = _redacted_detail(
                    result.stderr or result.stdout,
                    redactor=staged.redactor,
                )
                suffix = f": {detail}" if detail else ""
                raise HarnessInferenceError(
                    f"Codex harness inference failed{suffix}"
                )
            response_text = staged.redactor.redact(
                _completed_agent_message(result.stdout)
            )
            response = HarnessInferenceResponse(
                request_id=checked.request_id,
                text=response_text,
                capture_mode="transcript",
            )
        except HarnessInferenceError as exc:
            failure = exc
        except (OSError, ValueError, SessionFileSecurityError) as exc:
            failure = HarnessInferenceError(
                "Codex harness inference could not be executed safely"
            )
            failure.__cause__ = exc
        finally:
            cleanup_error: BaseException | None = None
            if credential_dir is not None and credential_identity is not None:
                try:
                    remove_credential_tree(
                        credential_dir,
                        credential_identity,
                        auth_identity,
                    )
                except (OSError, SessionFileSecurityError) as exc:
                    cleanup_error = exc
            if (
                cleanup_error is None
                and run_root is not None
                and run_identity is not None
            ):
                try:
                    remove_session_tree(run_root, run_identity)
                except (OSError, SessionFileSecurityError) as exc:
                    cleanup_error = exc
            if cleanup_error is not None:
                cleanup_failure = HarnessInferenceError(
                    "Codex harness private state could not be cleaned safely"
                )
                cleanup_failure.__cause__ = cleanup_error
                if failure is None:
                    failure = cleanup_failure
                else:
                    failure.add_note(str(cleanup_failure))

        if failure is not None:
            raise failure
        if response is None:
            raise HarnessInferenceError("Codex harness inference produced no response")
        return response

class CoreGatewayHarnessService:
    """Run model-only turns through one generation-bound local Gateway."""

    def __init__(
        self,
        *,
        proxy_base_url: str,
    ) -> None:
        self._proxy_base_url = _validated_core_gateway_base_url(proxy_base_url)

    def infer(self, request: HarnessInferenceRequest) -> HarnessInferenceResponse:
        checked = HarnessInferenceRequest.model_validate(request)
        _CodexHarnessServiceBase._validate_request(checked)
        model = _CodexHarnessServiceBase._model_name(checked.model_name)
        text = core_gateway_chat_completion(
            proxy_base_url=self._proxy_base_url,
            model=model,
            system_instruction=checked.system_instruction,
            prompt=checked.prompt,
            timeout_seconds=checked.timeout_seconds,
        )
        return HarnessInferenceResponse(
            request_id=checked.request_id,
            text=text,
            capture_mode="transcript",
        )


def _validated_core_gateway_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("Core Gateway base URL is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65_535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/v1"
        or parsed.query
        or parsed.fragment
        or parsed.netloc != f"127.0.0.1:{port}"
    ):
        raise ValueError("Core Gateway base URL must be a canonical loopback /v1 URL")
    return f"http://127.0.0.1:{port}/v1"


def core_gateway_base_url_from_environment() -> str | None:
    value = os.environ.get(CORE_GATEWAY_BASE_URL_ENV)
    return None if value is None else _validated_core_gateway_base_url(value)


def core_gateway_chat_completion(
    *,
    proxy_base_url: str,
    model: str,
    system_instruction: str,
    prompt: str,
    timeout_seconds: float,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    base_url = _validated_core_gateway_base_url(proxy_base_url)
    messages: list[dict[str, str]] = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": prompt})
    payload: dict[str, Any] = {"model": model, "messages": messages}
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    try:
        with httpx.Client(timeout=timeout_seconds, trust_env=False) as client:
            response = client.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {_CODEX_PROXY_API_KEY}",
                    "content-type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            content = _chat_completion_content(response.json())
    except (httpx.HTTPError, TypeError, ValueError, RecursionError) as exc:
        raise HarnessInferenceError(
            "Core Gateway harness inference failed"
        ) from exc
    if not content.strip():
        raise HarnessInferenceError("Core Gateway harness returned empty content")
    if len(content.encode("utf-8")) > 1_048_576:
        raise HarnessInferenceError("Core Gateway harness response exceeded its limit")
    return content.strip()


def _chat_completion_content(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _render_prompt(request: HarnessInferenceRequest) -> bytes:
    text = request.prompt
    if request.system_instruction:
        text = f"{request.system_instruction}\n\n{text}"
    return text.encode("utf-8")


def _controlled_codex_environment(
    *,
    codex_home: Path,
    home: Path,
    temporary: Path,
) -> dict[str, str]:
    env = {
        "CODEX_HOME": os.fspath(codex_home),
        "HOME": os.fspath(home),
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "TMPDIR": os.fspath(temporary),
    }
    for key in ("LANG", "LC_ALL"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _completed_agent_message(raw: bytes) -> str:
    if len(raw) > _MAX_CAPTURE_BYTES:
        raise HarnessInferenceError("Codex harness transcript exceeded its limit")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise HarnessInferenceError(
            "Codex harness did not return a completed transcript"
        ) from exc
    if lines and lines[0] == "Reading additional input from stdin...":
        lines = lines[1:]
    counts = {"thread.started": 0, "turn.started": 0, "turn.completed": 0}
    turn_active = False
    messages: list[str] = []
    last_event_type: str | None = None
    try:
        for line in lines:
            if not line:
                raise ValueError
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError
            event_type = event["type"]
            if event_type == "thread.started":
                if any(counts.values()) or turn_active:
                    raise ValueError
                thread_id = event.get("thread_id")
                if not isinstance(thread_id, str) or not thread_id:
                    raise ValueError
                counts[event_type] += 1
            elif event_type == "turn.started":
                if counts != {
                    "thread.started": 1,
                    "turn.started": 0,
                    "turn.completed": 0,
                } or turn_active:
                    raise ValueError
                counts[event_type] += 1
                turn_active = True
            elif event_type in {"item.started", "item.updated", "item.completed"}:
                item = event.get("item")
                if (
                    not turn_active
                    or not isinstance(item, dict)
                    or not isinstance(item.get("id"), str)
                    or not item["id"]
                    or not isinstance(item.get("type"), str)
                    or not item["type"]
                ):
                    raise ValueError
                if event_type == "item.completed" and item["type"] == "agent_message":
                    message = item.get("text")
                    if not isinstance(message, str):
                        raise ValueError
                    messages.append(message)
            elif event_type == "turn.completed":
                usage = event.get("usage")
                required_usage = {
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                }
                if (
                    not turn_active
                    or counts[event_type] != 0
                    or not isinstance(usage, dict)
                    or set(usage) != required_usage
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value < 0
                        for value in usage.values()
                    )
                ):
                    raise ValueError
                counts[event_type] += 1
                turn_active = False
            else:
                raise ValueError
            last_event_type = event_type
    except (json.JSONDecodeError, RecursionError, ValueError, TypeError) as exc:
        raise HarnessInferenceError(
            "Codex harness did not return a completed transcript"
        ) from exc
    if (
        counts
        != {
            "thread.started": 1,
            "turn.started": 1,
            "turn.completed": 1,
        }
        or turn_active
        or last_event_type != "turn.completed"
        or not messages
        or not messages[-1].strip()
    ):
        raise HarnessInferenceError(
            "Codex harness did not return a completed transcript"
        )
    message = messages[-1].strip()
    if len(message.encode("utf-8")) > 1_048_576:
        raise HarnessInferenceError("Codex harness response exceeded its limit")
    return message


def _redacted_detail(raw: bytes, *, redactor: CredentialRedactor) -> str:
    bounded = raw[:_MAX_ERROR_TEXT_BYTES]
    return redactor.redact_bytes(bounded).decode("utf-8", errors="replace").strip()


class _BoundedCodexInvocationRunner:
    def run(
        self,
        argv: tuple[str, ...],
        *,
        input_bytes: bytes,
        env: dict[str, str],
        cwd: Path,
        timeout_seconds: float,
    ) -> CodexInvocationResult:
        process: subprocess.Popen[bytes] | None = None
        selector: selectors.BaseSelector | None = None
        output = {"stdout": bytearray(), "stderr": bytearray()}
        input_offset = 0
        returncode: int | None = None
        deadline = time.monotonic() + timeout_seconds
        failure_message: bytes | None = None
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                close_fds=True,
                start_new_session=True,
            )
            if process.stdin is None or process.stdout is None or process.stderr is None:
                raise OSError("Codex harness process pipes are unavailable")
            selector = selectors.DefaultSelector()
            for name, stream in (
                ("stdin", process.stdin),
                ("stdout", process.stdout),
                ("stderr", process.stderr),
            ):
                os.set_blocking(stream.fileno(), False)
                selector.register(
                    stream,
                    selectors.EVENT_WRITE if name == "stdin" else selectors.EVENT_READ,
                    name,
                )
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure_message = b"Codex harness inference timed out"
                    _kill_process_group(process)
                    break
                for key, _events in selector.select(min(0.05, remaining)):
                    stream = key.fileobj
                    name = key.data
                    if name == "stdin":
                        try:
                            written = os.write(
                                stream.fileno(),
                                input_bytes[input_offset : input_offset + 64 * 1024],
                            )
                        except BrokenPipeError:
                            written = 0
                        input_offset += written
                        if written == 0 or input_offset == len(input_bytes):
                            selector.unregister(stream)
                            stream.close()
                        continue
                    chunk = os.read(stream.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    if (
                        sum(len(value) for value in output.values()) + len(chunk)
                        > _MAX_CAPTURE_BYTES
                    ):
                        failure_message = b"Codex harness output exceeded its limit"
                        _kill_process_group(process)
                        break
                    output[name].extend(chunk)
                if failure_message is not None:
                    break
            if failure_message is not None:
                returncode = process.wait(timeout=5.0)
            else:
                returncode = process.wait(
                    timeout=max(0.0, deadline - time.monotonic())
                )
        except subprocess.TimeoutExpired:
            if process is not None:
                _kill_process_group(process)
                process.wait(timeout=5.0)
            failure_message = b"Codex harness inference timed out"
            returncode = 124
        finally:
            if selector is not None:
                selector.close()
            if process is not None and process.poll() is None:
                _kill_process_group(process)
                process.wait(timeout=5.0)
        if failure_message is not None:
            output["stderr"] = bytearray(failure_message)
            returncode = 124 if b"timed out" in failure_message else 125
        if returncode is None:
            raise OSError("Codex harness process did not terminate")
        return CodexInvocationResult(
            returncode=returncode,
            stdout=bytes(output["stdout"]),
            stderr=bytes(output["stderr"]),
        )


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        process.kill()
