"""Trajectory builder for agent transcript captures without token metrics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openevo.trajectory.builder.base import BaseTrajectoryBuilder
from openevo.trajectory.models import CompletionSession, Trace, Trajectory


class AgentTranscriptBuilder(BaseTrajectoryBuilder):
    """Convert an agent stdout transcript into a tokenless trajectory."""

    async def build(self, session: CompletionSession) -> Trajectory:
        transcript_path = _transcript_path(session.metadata)
        if transcript_path is None or not transcript_path.exists():
            return _error_trajectory(session, "no transcript")

        transcript_text = transcript_path.read_text(encoding="utf-8")
        if not transcript_text.strip():
            return _error_trajectory(session, "empty transcript", transcript_path)
        response_text = _response_text_from_transcript(transcript_text)
        if not response_text:
            return _error_trajectory(session, "no assistant transcript", transcript_path)
        trace = Trace(
            prompt_messages=_prompt_messages(session.metadata),
            response_messages=(
                [{"role": "assistant", "content": response_text}] if response_text else []
            ),
            finish_reason="transcript",
            metadata={
                "capture_mode": "transcript",
                "token_level_metrics_available": False,
                "transcript_path": str(transcript_path),
                "transcript": transcript_text,
            },
        )
        return Trajectory(
            status="COMPLETED",
            metadata={
                "builder": "agent_transcript",
                "session_id": session.session_id,
                "task_id": session.task_id,
                "record_count": 0,
                "trace_count": 1,
                "capture_mode": "transcript",
                "token_level_metrics_available": False,
                "task_metadata": dict(session.metadata),
                **_top_level_scheduler_metadata(session.metadata),
            },
            traces=[trace],
        )


def _error_trajectory(
    session: CompletionSession,
    error: str,
    transcript_path: Path | None = None,
) -> Trajectory:
    metadata = {
        "builder": "agent_transcript",
        "session_id": session.session_id,
        "record_count": 0,
        "capture_mode": "transcript",
        "token_level_metrics_available": False,
        "task_metadata": dict(session.metadata),
        **_top_level_scheduler_metadata(session.metadata),
    }
    if transcript_path is not None:
        metadata["transcript_path"] = str(transcript_path)
    return Trajectory(
        status="ERROR",
        metadata=metadata,
        traces=[],
        error=error,
    )


def _transcript_path(metadata: dict[str, Any]) -> Path | None:
    agent_metadata = _agent_result_metadata(metadata)
    log_dir = agent_metadata.get("log_dir")
    if not log_dir:
        return None
    last_step = agent_metadata.get("last_step", 0)
    try:
        step_index = int(last_step)
    except (TypeError, ValueError):
        return None
    if step_index < 0:
        return None
    return Path(str(log_dir)) / f"step.{step_index:02d}.stdout.log"


def _agent_result_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    agent_result = metadata.get("agent_result")
    if isinstance(agent_result, dict):
        nested_metadata = agent_result.get("metadata")
        if isinstance(nested_metadata, dict):
            return nested_metadata
    nested_metadata = metadata.get("agent_result_metadata")
    if isinstance(nested_metadata, dict):
        return nested_metadata
    return {}


def _prompt_messages(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    instruction = metadata.get("agent_instruction")
    if isinstance(instruction, str) and instruction:
        return [{"role": "user", "content": instruction}]
    return []


def _response_text_from_transcript(transcript: str) -> str:
    segments: list[str] = []
    delta_parts: list[str] = []

    def flush_deltas() -> None:
        if delta_parts:
            segments.append("".join(delta_parts))
            delta_parts.clear()

    for raw_line in transcript.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        delta_text = _extract_output_text_delta(event)
        if delta_text:
            delta_parts.append(delta_text)
            continue
        text = _extract_assistant_text(event)
        if text:
            if delta_parts:
                delta_parts.clear()
            segments.append(text)
    flush_deltas()
    return "\n".join(segments)


def _extract_output_text_delta(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    if value.get("type") != "response.output_text.delta":
        return ""
    return _extract_text_content(value)


def _extract_assistant_text(value: Any) -> str:
    if not isinstance(value, dict):
        return ""

    direct_role = value.get("role")
    if direct_role == "assistant":
        return _extract_text_content(value)
    if direct_role in {"user", "system", "tool"}:
        return ""

    message = value.get("message")
    if isinstance(message, dict):
        role = message.get("role")
        if role == "assistant":
            return _extract_text_content(message)
        if role in {"user", "system", "tool"}:
            return ""

    item = value.get("item")
    if isinstance(item, dict):
        role = item.get("role")
        if role == "assistant":
            return _extract_text_content(item)
        if role in {"user", "system", "tool"}:
            return ""
        if item.get("type") == "agent_message":
            return _extract_text_content(item)

    event_type = str(value.get("type") or "")
    if event_type in {"agent_message", "assistant_message"}:
        return _extract_text_content(value)
    if event_type.startswith("response.output_text."):
        return _extract_text_content(value)
    return ""


def _extract_text_content(value: dict[str, Any]) -> str:
    for key in ("content", "text", "output_text", "final_answer", "summary", "delta"):
        if key in value:
            text = _content_to_text(value[key])
            if text:
                return text
    message = value.get("message")
    if isinstance(message, str):
        return message
    return ""


def _content_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _content_to_text(item)))
    if not isinstance(value, dict):
        return ""
    block_type = value.get("type")
    if block_type is not None and str(block_type) not in {"text", "output_text"}:
        return ""
    for key in ("text", "content"):
        text = value.get(key)
        if isinstance(text, str):
            return text
    return ""


def _top_level_scheduler_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    keys = {"group_id", "policy_version", "rollout_step"}
    return {key: metadata[key] for key in keys if key in metadata}
