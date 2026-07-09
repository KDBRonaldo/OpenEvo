"""Offline Terminal Bench result conversion for Polar evolution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openevo.evolution.models import EventIngestRequest


DEFAULT_MAX_TRANSCRIPT_CHARS = 60000
DEFAULT_MAX_VERIFIER_STDOUT_CHARS = 12000
DEFAULT_MAX_FAILED_TESTS = 20
DEFAULT_MAX_LLM_CALLS = 64
DEFAULT_MAX_LLM_CALL_MESSAGE_CHARS = 4096


class TerminalBenchBridgeError(ValueError):
    """Raised when a Terminal Bench result cannot be converted safely."""


def build_terminal_bench_events(
    path: str | Path,
    *,
    max_transcript_chars: int = DEFAULT_MAX_TRANSCRIPT_CHARS,
    max_verifier_stdout_chars: int = DEFAULT_MAX_VERIFIER_STDOUT_CHARS,
    max_failed_tests: int = DEFAULT_MAX_FAILED_TESTS,
    include_llm_calls: bool = False,
    max_llm_calls: int = DEFAULT_MAX_LLM_CALLS,
    max_llm_call_message_chars: int = DEFAULT_MAX_LLM_CALL_MESSAGE_CHARS,
    policy_version: str | None = None,
    rollout_step: int | None = None,
) -> list[EventIngestRequest]:
    """Build Polar event ingest requests from a Terminal Bench trial or job directory."""

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

    trajectory = {
        "status": status,
        "metadata": {
            "builder": "terminal_bench_transcript_bridge",
            "capture_mode": "transcript",
            "token_level_metrics_available": False,
            "trace_count": 1,
            "task_metadata": _task_metadata(result, trial_dir, task_id),
        },
        "traces": [
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
        ],
    }
    if status == "ERROR":
        trajectory["error"] = _exception_summary(result.get("exception_info"))

    return EventIngestRequest(
        source="terminal_bench.harbor",
        event_type="openevo.session_completed",
        source_event_id=f"terminal-bench:{session_id}",
        created_at=_text_or_none(result.get("finished_at")) or _text_or_none(result.get("started_at")),
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
    trial_dirs = [
        child
        for child in root.iterdir()
        if child.is_dir() and _is_trial_dir(child)
    ]
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
        _nested_get(result, ["agent_result", "metadata", "terminal_bench_harbor_agent", "task_id"]),
        result.get("task_name"),
        _nested_get(result, ["config", "agent", "kwargs", "task_id"]),
        result.get("task_id"),
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
