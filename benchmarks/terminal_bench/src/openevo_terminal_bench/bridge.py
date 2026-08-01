"""Offline Terminal Bench result conversion for OpenEvo evolution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openevo.evolution.framework.contracts import canonical_digest, canonical_json
from openevo.evolution.models import EventIngestRequest
from openevo.evolution.parametric.training_data import (
    normalize_chat_messages,
    normalize_tool_definitions,
)


DEFAULT_MAX_TRANSCRIPT_CHARS = 60000
DEFAULT_MAX_VERIFIER_STDOUT_CHARS = 12000
DEFAULT_MAX_FAILED_TESTS = 20
DEFAULT_MAX_LLM_CALLS = 64
DEFAULT_MAX_LLM_CALL_MESSAGE_CHARS = 4096
DEFAULT_MAX_ATIF_AGENT_TURNS = 256
DEFAULT_MAX_ATIF_MESSAGE_CHARS = 60000
_MAX_ATIF_STEPS = 4096
_MAX_ATIF_TOOL_CALLS_PER_TURN = 128
_MAX_ATIF_TRAJECTORY_BYTES = 256 * 1024 * 1024
_MAX_CODEX_GATEWAY_CONTRACT_BYTES = 8 * 1024 * 1024
_CODEX_GATEWAY_CONTRACT_SCHEMA = "openevo.terminal_bench.codex_gateway_contract.v1"


class TerminalBenchBridgeError(ValueError):
    """Raised when a Terminal Bench result cannot be converted safely."""


class CodexGatewayTrainingContract:
    """Canonical training-relevant prefix captured from a real Gateway request."""

    __slots__ = ("_canonical_payload",)

    def __init__(self, *, messages: Any, tools: Any) -> None:
        try:
            normalized_messages = normalize_chat_messages(messages)
            normalized_tools = normalize_tool_definitions(tools)
        except ValueError as exc:
            raise TerminalBenchBridgeError(
                "Codex Gateway training contract is not a supported chat request"
            ) from exc
        if not normalized_messages or not any(
            message["role"] in {"developer", "system"} for message in normalized_messages
        ):
            raise TerminalBenchBridgeError(
                "Codex Gateway training contract requires an instruction message"
            )
        if not any(message["role"] == "user" for message in normalized_messages):
            raise TerminalBenchBridgeError(
                "Codex Gateway training contract requires a user message"
            )
        if any(message["role"] in {"assistant", "tool"} for message in normalized_messages):
            raise TerminalBenchBridgeError(
                "Codex Gateway training contract must come from the first model request"
            )
        tool_names = [tool["function"]["name"] for tool in normalized_tools]
        if len(tool_names) != len(set(tool_names)):
            raise TerminalBenchBridgeError(
                "Codex Gateway training contract contains duplicate tool names"
            )
        payload = {
            "schema_version": _CODEX_GATEWAY_CONTRACT_SCHEMA,
            "messages": normalized_messages,
            "tools": normalized_tools,
        }
        canonical_payload = canonical_json(payload)
        if len(canonical_payload.encode("utf-8")) > _MAX_CODEX_GATEWAY_CONTRACT_BYTES:
            raise TerminalBenchBridgeError(
                "Codex Gateway training contract exceeds its byte budget"
            )
        self._canonical_payload = canonical_payload

    @classmethod
    def from_gateway_request(cls, request: Any) -> CodexGatewayTrainingContract:
        if not isinstance(request, dict):
            raise TerminalBenchBridgeError("Gateway completion request must be an object")
        return cls(messages=request.get("messages"), tools=request.get("tools"))

    @classmethod
    def from_payload(cls, payload: Any) -> CodexGatewayTrainingContract:
        if (
            not isinstance(payload, dict)
            or set(payload) != {"messages", "schema_version", "tools"}
            or payload.get("schema_version") != _CODEX_GATEWAY_CONTRACT_SCHEMA
        ):
            raise TerminalBenchBridgeError("Codex Gateway training contract payload is invalid")
        contract = cls(messages=payload["messages"], tools=payload["tools"])
        if contract.to_payload() != payload:
            raise TerminalBenchBridgeError(
                "Codex Gateway training contract payload is not canonical"
            )
        return contract

    def to_payload(self) -> dict[str, Any]:
        payload = json.loads(self._canonical_payload)
        if not isinstance(payload, dict):
            raise AssertionError("canonical Codex Gateway contract changed shape")
        return payload

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.to_payload()["messages"]

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self.to_payload()["tools"]

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(tool["function"]["name"] for tool in self.tools)

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_payload())

    def validate_request_extension(self, request: Any) -> None:
        if not isinstance(request, dict):
            raise TerminalBenchBridgeError("Gateway completion request must be an object")
        try:
            messages = normalize_chat_messages(request.get("messages"))
            tools = normalize_tool_definitions(request.get("tools"))
        except ValueError as exc:
            raise TerminalBenchBridgeError(
                "Gateway request changed its supported chat contract"
            ) from exc
        prefix = self.messages
        if tools != self.tools or messages[: len(prefix)] != prefix:
            raise TerminalBenchBridgeError(
                "Gateway request changed its initial messages or tool contract"
            )


def build_terminal_bench_events(
    path: str | Path,
    *,
    max_transcript_chars: int = DEFAULT_MAX_TRANSCRIPT_CHARS,
    max_verifier_stdout_chars: int = DEFAULT_MAX_VERIFIER_STDOUT_CHARS,
    max_failed_tests: int = DEFAULT_MAX_FAILED_TESTS,
    include_llm_calls: bool = False,
    max_llm_calls: int = DEFAULT_MAX_LLM_CALLS,
    max_llm_call_message_chars: int = DEFAULT_MAX_LLM_CALL_MESSAGE_CHARS,
    include_atif_traces: bool = False,
    max_atif_agent_turns: int = DEFAULT_MAX_ATIF_AGENT_TURNS,
    max_atif_message_chars: int = DEFAULT_MAX_ATIF_MESSAGE_CHARS,
    codex_gateway_contract: CodexGatewayTrainingContract | None = None,
    policy_version: str | None = None,
    rollout_step: int | None = None,
) -> list[EventIngestRequest]:
    """Build OpenEvo event ingest requests from a Terminal Bench trial or job directory."""

    root = Path(path)
    if root.is_file():
        root = root.parent
    trial_dirs = _trial_dirs(root)
    if not trial_dirs:
        raise TerminalBenchBridgeError(f"no Terminal Bench trial result found under {root}")
    return [
        _build_trial_event(
            trial_dir,
            max_transcript_chars=max_transcript_chars,
            max_verifier_stdout_chars=max_verifier_stdout_chars,
            max_failed_tests=max_failed_tests,
            include_llm_calls=include_llm_calls,
            max_llm_calls=max_llm_calls,
            max_llm_call_message_chars=max_llm_call_message_chars,
            include_atif_traces=include_atif_traces,
            max_atif_agent_turns=max_atif_agent_turns,
            max_atif_message_chars=max_atif_message_chars,
            codex_gateway_contract=codex_gateway_contract,
            policy_version=policy_version,
            rollout_step=rollout_step,
        )
        for trial_dir in trial_dirs
    ]


def _build_trial_event(
    trial_dir: Path,
    *,
    max_transcript_chars: int,
    max_verifier_stdout_chars: int,
    max_failed_tests: int,
    include_llm_calls: bool,
    max_llm_calls: int,
    max_llm_call_message_chars: int,
    include_atif_traces: bool,
    max_atif_agent_turns: int,
    max_atif_message_chars: int,
    codex_gateway_contract: CodexGatewayTrainingContract | None,
    policy_version: str | None,
    rollout_step: int | None,
) -> EventIngestRequest:
    result_path = trial_dir / "result.json"
    result = _read_json(result_path)
    task_id = _infer_task_id(result, trial_dir)
    session_id = _text_or_none(result.get("trial_name")) or _text_or_none(result.get("id"))
    if not session_id:
        raise TerminalBenchBridgeError(f"trial result has no session id: {result_path}")

    reward = _extract_reward(result, trial_dir)
    status = _infer_status(result)
    instruction = _read_text_if_exists(trial_dir / "agent" / "instruction.txt").strip()
    llm_calls: list[dict[str, Any]] = []
    if include_llm_calls:
        llm_calls = _extract_compact_llm_calls(
            trial_dir,
            max_calls=max_llm_calls,
            max_message_chars=max_llm_call_message_chars,
        )
    try:
        transcript = _extract_transcript(
            trial_dir,
            max_transcript_chars=max_transcript_chars,
        )
    except TerminalBenchBridgeError:
        if not llm_calls:
            raise
        transcript = {"text": "", "sources": []}
    verifier = _extract_verifier_feedback(
        trial_dir,
        result,
        max_stdout_chars=max_verifier_stdout_chars,
        max_failed_tests=max_failed_tests,
    )

    trace_metadata: dict[str, Any] = {
        "capture_mode": "transcript",
        "token_level_metrics_available": False,
        "transcript_sources": transcript["sources"],
        "verifier": verifier,
    }
    stderr_text = _read_text_if_exists(trial_dir / "agent" / "stderr.txt")
    if stderr_text.strip():
        trace_metadata["stderr"] = _truncate_text(stderr_text, max_verifier_stdout_chars)
    if result.get("exception_info") is not None:
        trace_metadata["exception_info"] = result.get("exception_info")
    if llm_calls:
        trace_metadata["llm_calls"] = llm_calls

    if include_atif_traces:
        traces = _extract_atif_traces(
            trial_dir,
            reward=reward,
            max_agent_turns=max_atif_agent_turns,
            max_message_chars=max_atif_message_chars,
            codex_gateway_contract=codex_gateway_contract,
        )
        builder = "terminal_bench_atif_bridge"
    else:
        traces = [
            {
                "prompt_ids": [],
                "response_ids": [],
                "loss_mask": [],
                "prompt_messages": (
                    [{"role": "user", "content": instruction}] if instruction else []
                ),
                "response_messages": (
                    [{"role": "assistant", "content": transcript["text"]}]
                    if transcript["text"]
                    else []
                ),
                "finish_reason": "transcript",
                "response_logprobs": None,
                "reward": reward,
                "metadata": trace_metadata,
            }
        ]
        builder = "terminal_bench_transcript_bridge"

    trajectory = {
        "status": status,
        "metadata": {
            "builder": builder,
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "trace_count": len(traces),
            "task_metadata": _task_metadata(result, trial_dir, task_id),
        },
        "traces": traces,
    }
    if status == "ERROR":
        trajectory["error"] = _exception_summary(result.get("exception_info"))

    return EventIngestRequest(
        source="terminal_bench.harbor",
        event_type="openevo.session_completed",
        source_event_id=f"terminal-bench:{session_id}",
        created_at=_text_or_none(result.get("finished_at"))
        or _text_or_none(result.get("started_at")),
        task_id=task_id,
        session_id=session_id,
        policy_version=policy_version,
        rollout_step=rollout_step,
        agent=_agent_metadata(result),
        reward=reward,
        status=status,
        payload={
            "session_result": {
                "session_id": session_id,
                "task_id": task_id,
                "status": status,
                "trajectory": trajectory,
                "metadata": {
                    "terminal_bench": {
                        "trial_name": _text_or_none(result.get("trial_name")),
                        "trial_dir": str(trial_dir),
                        "result_path": str(result_path),
                        "agent": _safe_harbor_agent_metadata(result),
                    }
                },
            }
        },
    )


def _trial_dirs(root: Path) -> list[Path]:
    root = root.resolve()
    if _is_trial_dir(root):
        return [root]
    trial_dirs = [child for child in root.iterdir() if child.is_dir() and _is_trial_dir(child)]
    return sorted(trial_dirs)


def _is_trial_dir(path: Path) -> bool:
    return (path / "result.json").is_file() and (
        (path / "agent").is_dir() or (path / "verifier").is_dir()
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TerminalBenchBridgeError(f"missing Terminal Bench result: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TerminalBenchBridgeError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TerminalBenchBridgeError(f"expected object JSON in {path}")
    return payload


def _read_text_if_exists(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _extract_transcript(trial_dir: Path, *, max_transcript_chars: int) -> dict[str, Any]:
    candidates = [
        trial_dir / "agent" / "stdout.txt",
        trial_dir / "agent" / "codex.txt",
        trial_dir / "agent" / "evolab_lab" / "terminal_bench_report.md",
        trial_dir / "agent" / "evolab_lab" / "final_artifacts.jsonl",
        trial_dir / "agent" / "evolab_lab" / "context_summary.json",
        trial_dir / "agent" / "stderr.txt",
    ]
    for candidate in candidates:
        text = _read_text_if_exists(candidate)
        if text.strip():
            return {
                "text": _truncate_text(text, max_transcript_chars),
                "sources": [_relative_to_trial(candidate, trial_dir)],
            }
    raise TerminalBenchBridgeError(f"no transcript text found in {trial_dir}")


def _extract_compact_llm_calls(
    trial_dir: Path,
    *,
    max_calls: int,
    max_message_chars: int,
) -> list[dict[str, Any]]:
    if max_calls <= 0 or max_message_chars <= 0:
        return []
    calls_path = (
        trial_dir
        / "agent"
        / "evolab_lab"
        / ".evolab"
        / "registries"
        / "trajectory"
        / "llm_calls.jsonl"
    )
    if not calls_path.is_file():
        return []

    compact_calls: list[dict[str, Any]] = []
    for line in calls_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if len(compact_calls) >= max_calls:
            break
        if not line.strip():
            continue
        try:
            raw_call = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw_call, dict):
            continue

        input_messages = _compact_llm_messages(
            raw_call.get("input_messages"),
            max_message_chars=max_message_chars,
        )
        if not input_messages:
            continue
        compact_call: dict[str, Any] = {"input_messages": input_messages}
        model = _text_or_none(raw_call.get("model"))
        if model:
            compact_call["model"] = model
        metadata = _compact_llm_call_metadata(raw_call.get("metadata"))
        if metadata:
            compact_call["metadata"] = metadata
        compact_calls.append(compact_call)
    return compact_calls


def _extract_atif_traces(
    trial_dir: Path,
    *,
    reward: float | None,
    max_agent_turns: int,
    max_message_chars: int,
    codex_gateway_contract: CodexGatewayTrainingContract | None,
) -> list[dict[str, Any]]:
    if max_agent_turns < 1 or max_message_chars < 1:
        raise TerminalBenchBridgeError("ATIF projection budgets must be positive")
    trajectory_path = trial_dir / "agent" / "trajectory.json"
    try:
        size = trajectory_path.stat().st_size
    except FileNotFoundError as exc:
        raise TerminalBenchBridgeError(
            f"training projection requires an ATIF trajectory: {trajectory_path}"
        ) from exc
    if size < 1 or size > _MAX_ATIF_TRAJECTORY_BYTES:
        raise TerminalBenchBridgeError("ATIF trajectory exceeds its file budget")
    trajectory = _read_json(trajectory_path)
    schema_version = trajectory.get("schema_version")
    steps = trajectory.get("steps")
    if (
        not isinstance(schema_version, str)
        or not schema_version.startswith("ATIF-v1.")
        or not isinstance(steps, list)
        or not steps
        or len(steps) > _MAX_ATIF_STEPS
    ):
        raise TerminalBenchBridgeError("ATIF trajectory has an unsupported shape")

    projected_steps = [
        _project_atif_step(
            step,
            max_message_chars,
            codex_gateway_contract=codex_gateway_contract,
        )
        for step in steps
    ]
    tool_definitions = _atif_tool_definitions(
        projected_steps,
        codex_gateway_contract=codex_gateway_contract,
    )
    if codex_gateway_contract is not None:
        _validate_atif_contract_task(projected_steps, codex_gateway_contract)
    history: list[dict[str, Any]] = (
        codex_gateway_contract.messages if codex_gateway_contract is not None else []
    )
    traces: list[dict[str, Any]] = []
    pending_agent_steps: list[dict[str, Any]] = []
    saw_agent = False
    agent = trajectory.get("agent")
    agent_model = agent.get("model_name") if isinstance(agent, dict) else None

    for step in projected_steps:
        source = step["source"]
        if source == "agent":
            saw_agent = True
            if not step["message"] and not step["tool_calls"]:
                continue
            pending_agent_steps.append(step)
            if not step["tool_calls"]:
                continue
        elif not pending_agent_steps:
            if (
                (codex_gateway_contract is None or saw_agent)
                and source in {"system", "user"}
                and step["message"]
            ):
                history.append({"role": source, "content": step["message"]})
            continue

        _append_atif_trace(
            traces=traces,
            history=history,
            agent_steps=pending_agent_steps,
            reward=reward,
            tool_definitions=tool_definitions,
            agent_model=agent_model,
            max_message_chars=max_message_chars,
            codex_gateway_contract=codex_gateway_contract,
        )
        pending_agent_steps.clear()
        if len(traces) >= max_agent_turns:
            break
        if source in {"system", "user"} and step["message"]:
            history.append({"role": source, "content": step["message"]})

    if pending_agent_steps and len(traces) < max_agent_turns:
        _append_atif_trace(
            traces=traces,
            history=history,
            agent_steps=pending_agent_steps,
            reward=reward,
            tool_definitions=tool_definitions,
            agent_model=agent_model,
            max_message_chars=max_message_chars,
            codex_gateway_contract=codex_gateway_contract,
        )

    if not traces:
        raise TerminalBenchBridgeError("ATIF trajectory contains no trainable agent turns")
    return traces


def _append_atif_trace(
    *,
    traces: list[dict[str, Any]],
    history: list[dict[str, Any]],
    agent_steps: list[dict[str, Any]],
    reward: float | None,
    tool_definitions: list[dict[str, Any]],
    agent_model: Any,
    max_message_chars: int,
    codex_gateway_contract: CodexGatewayTrainingContract | None,
) -> None:
    if not history:
        raise TerminalBenchBridgeError("ATIF agent turn has no prompt context")
    if any(step["tool_calls"] for step in agent_steps[:-1]):
        raise TerminalBenchBridgeError("ATIF response batch has an internal tool boundary")
    final_step = agent_steps[-1]
    assistant: dict[str, Any] = {
        "role": "assistant",
        "content": _truncate_text(
            "\n".join(step["message"] for step in agent_steps if step["message"]),
            max_message_chars,
        ),
    }
    if final_step["tool_calls"]:
        assistant["tool_calls"] = final_step["tool_calls"]
    if not assistant["content"] and "tool_calls" not in assistant:
        return

    trace: dict[str, Any] = {
        "prompt_ids": [],
        "response_ids": [],
        "loss_mask": [],
        "prompt_messages": list(history),
        "response_messages": [assistant],
        "finish_reason": "atif_agent_turn",
        "response_logprobs": None,
        "reward": reward,
        "metadata": {
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "trajectory_source": "agent/trajectory.json",
            "atif_step_id": final_step["step_id"],
            "atif_step_ids": [step["step_id"] for step in agent_steps],
            **(
                {"harness_contract_digest": codex_gateway_contract.digest}
                if codex_gateway_contract is not None
                else {}
            ),
            **({"model": agent_model} if isinstance(agent_model, str) else {}),
        },
    }
    if tool_definitions:
        trace["tools"] = tool_definitions
    traces.append(trace)
    if codex_gateway_contract is None or not final_step["tool_calls"]:
        history.append(assistant)
    else:
        history.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": final_step["tool_calls"],
            }
        )
    history.extend(final_step["tool_outputs"])


def _project_atif_step(
    step: Any,
    max_message_chars: int,
    *,
    codex_gateway_contract: CodexGatewayTrainingContract | None,
) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise TerminalBenchBridgeError("ATIF trajectory step must be an object")
    source = step.get("source")
    if source not in {"agent", "system", "user"}:
        return {
            "source": "unsupported",
            "step_id": step.get("step_id"),
            "message": "",
            "tool_calls": [],
            "tool_outputs": [],
        }
    message = step.get("message", "")
    if message is None:
        message = ""
    if not isinstance(message, str):
        raise TerminalBenchBridgeError("ATIF trajectory message must be text")

    raw_calls = step.get("tool_calls") or []
    if not isinstance(raw_calls, list) or len(raw_calls) > _MAX_ATIF_TOOL_CALLS_PER_TURN:
        raise TerminalBenchBridgeError("ATIF agent turn has invalid tool calls")
    if codex_gateway_contract is not None and _is_codex_tool_display_message(message, raw_calls):
        message = ""
    tool_calls = [
        _project_atif_tool_call(
            call,
            encode_arguments=codex_gateway_contract is not None,
        )
        for call in raw_calls
    ]
    call_ids = {call["id"] for call in tool_calls}
    tool_outputs = _project_atif_observation(
        step.get("observation"),
        call_ids=call_ids,
        max_message_chars=max_message_chars,
    )
    return {
        "source": source,
        "step_id": step.get("step_id"),
        "message": _truncate_text(message, max_message_chars),
        "tool_calls": tool_calls,
        "tool_outputs": tool_outputs,
    }


def _project_atif_tool_call(
    call: Any,
    *,
    encode_arguments: bool,
) -> dict[str, Any]:
    if not isinstance(call, dict):
        raise TerminalBenchBridgeError("ATIF tool call must be an object")
    call_id = _text_or_none(call.get("tool_call_id"))
    function_name = _text_or_none(call.get("function_name"))
    arguments = call.get("arguments")
    if not call_id or not function_name or not isinstance(arguments, dict):
        raise TerminalBenchBridgeError("ATIF tool call identity or arguments are invalid")
    projected_arguments: dict[str, Any] | str = arguments
    if encode_arguments:
        try:
            projected_arguments = canonical_json(arguments)
        except (TypeError, ValueError) as exc:
            raise TerminalBenchBridgeError("ATIF tool arguments are not canonical JSON") from exc
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": function_name,
            "arguments": projected_arguments,
        },
    }


def _is_codex_tool_display_message(message: str, raw_calls: list[Any]) -> bool:
    if len(raw_calls) != 1 or not isinstance(raw_calls[0], dict):
        return False
    function_name = _text_or_none(raw_calls[0].get("function_name"))
    call_id = _text_or_none(raw_calls[0].get("tool_call_id"))
    return bool(function_name and call_id and message == f"Executed {function_name} {call_id}")


def _validate_atif_contract_task(
    steps: list[dict[str, Any]],
    contract: CodexGatewayTrainingContract,
) -> None:
    atif_user_messages = [
        step["message"] for step in steps if step["source"] == "user" and step["message"]
    ]
    contract_user_messages = [
        message["content"]
        for message in contract.messages
        if message["role"] == "user" and message["content"]
    ]
    if not atif_user_messages or not contract_user_messages:
        raise TerminalBenchBridgeError(
            "ATIF trajectory and Gateway contract require a task instruction"
        )
    expected = contract_user_messages[-1].strip()
    observed = atif_user_messages[-1].strip()
    if not expected or not observed.startswith(expected):
        raise TerminalBenchBridgeError(
            "ATIF training trajectory does not match the evaluated task instruction"
        )


def _project_atif_observation(
    observation: Any,
    *,
    call_ids: set[str],
    max_message_chars: int,
) -> list[dict[str, Any]]:
    if observation is None:
        return []
    if not isinstance(observation, dict):
        raise TerminalBenchBridgeError("ATIF observation must be an object")
    results = observation.get("results") or []
    if not isinstance(results, list) or len(results) > _MAX_ATIF_TOOL_CALLS_PER_TURN:
        raise TerminalBenchBridgeError("ATIF observation results are invalid")
    projected: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            raise TerminalBenchBridgeError("ATIF observation result must be an object")
        source_call_id = _text_or_none(result.get("source_call_id"))
        if source_call_id is None and len(call_ids) == 1:
            source_call_id = next(iter(call_ids))
        if source_call_id not in call_ids:
            raise TerminalBenchBridgeError("ATIF observation does not match its tool call")
        content = result.get("content", "")
        if not isinstance(content, str):
            try:
                content = json.dumps(content, ensure_ascii=False, sort_keys=True)
            except TypeError as exc:
                raise TerminalBenchBridgeError(
                    "ATIF observation content is not JSON serializable"
                ) from exc
        projected.append(
            {
                "role": "tool",
                "content": _truncate_text(content, max_message_chars),
                "tool_call_id": source_call_id,
            }
        )
    return projected


def _atif_tool_definitions(
    steps: list[dict[str, Any]],
    *,
    codex_gateway_contract: CodexGatewayTrainingContract | None,
) -> list[dict[str, Any]]:
    observed_names = {call["function"]["name"] for step in steps for call in step["tool_calls"]}
    if codex_gateway_contract is not None:
        unavailable = sorted(observed_names.difference(codex_gateway_contract.tool_names))
        if unavailable:
            raise TerminalBenchBridgeError(
                "ATIF trajectory uses unavailable Codex tool(s): " + ", ".join(unavailable)
            )
        return codex_gateway_contract.tools

    by_name: dict[str, dict[str, Any]] = {}
    for step in steps:
        for call in step["tool_calls"]:
            function = call["function"]
            name = function["name"]
            arguments = function["arguments"]
            properties = {
                key: _json_schema_for_example(value)
                for key, value in sorted(arguments.items())
                if isinstance(key, str) and key
            }
            candidate = {
                "type": "function",
                "function": {
                    "name": name,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "additionalProperties": True,
                    },
                },
            }
            existing = by_name.get(name)
            if existing is None or len(properties) > len(
                existing["function"]["parameters"]["properties"]
            ):
                by_name[name] = candidate
    return [by_name[name] for name in sorted(by_name)]


def _json_schema_for_example(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        item_schema = _json_schema_for_example(value[0]) if value else {}
        return {"type": "array", "items": item_schema}
    if isinstance(value, dict):
        return {"type": "object"}
    if value is None:
        return {"type": "null"}
    raise TerminalBenchBridgeError("ATIF tool arguments contain a non-JSON value")


def _compact_llm_messages(value: Any, *, max_message_chars: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    messages: list[dict[str, Any]] = []
    for message in value:
        if not isinstance(message, dict):
            continue
        role = _text_or_none(message.get("role"))
        if not role:
            continue
        content = message.get("content")
        if content is None:
            content = ""
        compact: dict[str, Any] = {
            "role": role,
            "content": _truncate_text(str(content), max_message_chars),
        }
        name = _text_or_none(message.get("name"))
        if name:
            compact["name"] = name
        tool_call_id = _text_or_none(message.get("tool_call_id"))
        if tool_call_id:
            compact["tool_call_id"] = tool_call_id
        messages.append(compact)
    return messages


def _compact_llm_call_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    metadata: dict[str, Any] = {}
    for key in ("step_index", "runtime_stage", "role", "task_id"):
        item = value.get(key)
        if item is not None:
            metadata[key] = item
    tool_specs = _compact_tool_specs(value.get("tool_specs"))
    if tool_specs:
        metadata["tool_specs"] = tool_specs
    return metadata


def _compact_tool_specs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    specs: list[dict[str, Any]] = []
    for spec in value:
        if not isinstance(spec, dict):
            continue
        name = _text_or_none(spec.get("name"))
        if not name:
            continue
        compact: dict[str, Any] = {"name": name}
        description = _text_or_none(spec.get("description"))
        if description:
            compact["description"] = description
        parameters_schema = spec.get("parameters_schema")
        if isinstance(parameters_schema, dict):
            compact["parameters_schema"] = parameters_schema
        specs.append(compact)
    return specs


def _extract_reward(result: dict[str, Any], trial_dir: Path) -> float | None:
    reward = _nested_get(result, ["verifier_result", "rewards", "reward"])
    if isinstance(reward, int | float):
        return float(reward)
    reward_text = _read_text_if_exists(trial_dir / "verifier" / "reward.txt").strip()
    if not reward_text:
        return None
    try:
        return float(reward_text)
    except ValueError as exc:
        raise TerminalBenchBridgeError(
            f"invalid verifier reward in {trial_dir / 'verifier' / 'reward.txt'}"
        ) from exc


def _infer_status(result: dict[str, Any]) -> str:
    status = _text_or_none(result.get("status"))
    if status in {"COMPLETED", "TIMEOUT", "ERROR"}:
        return status
    if result.get("exception_info") is not None:
        return "ERROR"
    return "COMPLETED"


def _infer_task_id(result: dict[str, Any], trial_dir: Path) -> str:
    candidates: list[Any] = [
        _nested_get(
            result, ["agent_result", "metadata", "terminal_bench_harbor_agent", "task_id"]
        ),
        _nested_get(result, ["config", "agent", "kwargs", "task_id"]),
        result.get("task_id"),
        result.get("task_name"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if isinstance(candidate, dict):
            path = candidate.get("path")
            if isinstance(path, str) and path.strip():
                return Path(path).name
    if "__" in trial_dir.name:
        return trial_dir.name.split("__", 1)[0]
    return trial_dir.name


def _agent_metadata(result: dict[str, Any]) -> dict[str, Any]:
    metadata = {"harness": "terminal-bench-harbor"}
    model_name = _nested_get(result, ["config", "agent", "model_name"])
    if isinstance(model_name, str) and model_name.strip():
        metadata["model_name"] = model_name.strip()
    return metadata


def _safe_harbor_agent_metadata(result: dict[str, Any]) -> dict[str, Any]:
    metadata = _nested_get(result, ["agent_result", "metadata", "terminal_bench_harbor_agent"])
    if not isinstance(metadata, dict):
        return {}
    safe_keys = {"agent", "mode", "return_code", "task_id", "timeout_sec"}
    return {key: metadata[key] for key in safe_keys if key in metadata}


def _task_metadata(result: dict[str, Any], trial_dir: Path, task_id: str) -> dict[str, Any]:
    task_path = _nested_get(result, ["task_id", "path"])
    metadata: dict[str, Any] = {
        "task_id": task_id,
        "task_name": _text_or_none(result.get("task_name")) or task_id,
        "trial_name": _text_or_none(result.get("trial_name")),
        "trial_dir": str(trial_dir),
    }
    if isinstance(task_path, str) and task_path:
        metadata["task_path"] = task_path
    return metadata


def _extract_verifier_feedback(
    trial_dir: Path,
    result: dict[str, Any],
    *,
    max_stdout_chars: int,
    max_failed_tests: int,
) -> dict[str, Any]:
    feedback: dict[str, Any] = {"reward": _extract_reward(result, trial_dir)}
    ctrf_path = trial_dir / "verifier" / "ctrf.json"
    if ctrf_path.is_file():
        ctrf = _read_json(ctrf_path)
        summary = _nested_get(ctrf, ["results", "summary"])
        if isinstance(summary, dict):
            feedback["summary"] = summary
        tests = _nested_get(ctrf, ["results", "tests"])
        if isinstance(tests, list):
            failed_tests = [
                _summarize_test(test)
                for test in tests
                if isinstance(test, dict) and str(test.get("status", "")).lower() not in {"passed"}
            ]
            feedback["failed_tests"] = failed_tests[:max_failed_tests]
            if len(failed_tests) > max_failed_tests:
                feedback["failed_tests_truncated"] = len(failed_tests) - max_failed_tests
    stdout = _read_text_if_exists(trial_dir / "verifier" / "test-stdout.txt")
    if stdout.strip():
        feedback["stdout"] = _truncate_text(stdout, max_stdout_chars, keep_tail=True)
    return feedback


def _summarize_test(test: dict[str, Any]) -> dict[str, Any]:
    keys = ("name", "status", "message", "file_path")
    return {key: test[key] for key in keys if key in test and test[key] is not None}


def _exception_summary(exception_info: Any) -> str:
    if exception_info is None:
        return ""
    if isinstance(exception_info, str):
        return exception_info
    try:
        return json.dumps(exception_info, sort_keys=True)
    except TypeError:
        return str(exception_info)


def _truncate_text(text: str, max_chars: int, *, keep_tail: bool = False) -> str:
    if max_chars < 1:
        return ""
    if len(text) <= max_chars:
        return text
    marker = "\n[truncated]\n"
    if max_chars <= len(marker):
        return text[-max_chars:] if keep_tail else text[:max_chars]
    remaining = max_chars - len(marker)
    if keep_tail:
        return marker + text[-remaining:]
    return text[:remaining] + marker


def _nested_get(payload: dict[str, Any], keys: list[str]) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _text_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _relative_to_trial(path: Path, trial_dir: Path) -> str:
    try:
        return str(path.relative_to(trial_dir))
    except ValueError:
        return str(path)
