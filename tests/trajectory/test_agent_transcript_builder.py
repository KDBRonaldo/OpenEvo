from __future__ import annotations

import asyncio
import json

from openevo.trajectory.builder.agent_transcript import AgentTranscriptBuilder
from openevo.trajectory.models import CompletionSession


def test_agent_transcript_builder_builds_trajectory_from_step_stdout_log(tmp_path) -> None:
    log_dir = tmp_path / "logs" / "agent"
    log_dir.mkdir(parents=True)
    (log_dir / "step.00.stdout.log").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "message",
                        "message": {"role": "assistant", "content": "Implemented it."},
                    }
                ),
                "plain transcript line",
            ]
        ),
        encoding="utf-8",
    )
    session = CompletionSession(
        session_id="session_1",
        metadata={
            "agent_instruction": "Do work.",
            "agent_result": {"metadata": {"log_dir": str(log_dir), "last_step": 0}},
            "policy_version": "policy_1",
        },
    )

    trajectory = asyncio.run(AgentTranscriptBuilder().build(session))

    assert trajectory.status == "COMPLETED"
    assert trajectory.error is None
    assert trajectory.metadata["builder"] == "agent_transcript"
    assert trajectory.metadata["capture_mode"] == "transcript"
    assert trajectory.metadata["token_level_metrics_available"] is False
    assert trajectory.metadata["policy_version"] == "policy_1"
    trace = trajectory.traces[0]
    assert trace.prompt_ids == []
    assert trace.response_ids == []
    assert trace.loss_mask == []
    assert trace.response_logprobs is None
    assert trace.prompt_messages == [{"role": "user", "content": "Do work."}]
    assert trace.response_messages == [{"role": "assistant", "content": "Implemented it."}]
    assert trace.metadata["transcript_path"].endswith("step.00.stdout.log")
    assert trace.metadata["capture_mode"] == "transcript"
    assert trace.metadata["token_level_metrics_available"] is False
    assert "plain transcript line" in trace.metadata["transcript"]


def test_agent_transcript_builder_rejects_empty_stdout_log(tmp_path) -> None:
    log_dir = tmp_path / "logs" / "agent"
    log_dir.mkdir(parents=True)
    (log_dir / "step.00.stdout.log").write_text("", encoding="utf-8")
    session = CompletionSession(
        session_id="session_1",
        metadata={"agent_result": {"metadata": {"log_dir": str(log_dir), "last_step": 0}}},
    )

    trajectory = asyncio.run(AgentTranscriptBuilder().build(session))

    assert trajectory.status == "ERROR"
    assert trajectory.error == "empty transcript"
    assert trajectory.traces == []


def test_agent_transcript_builder_ignores_non_assistant_json_events(tmp_path) -> None:
    log_dir = tmp_path / "logs" / "agent"
    log_dir.mkdir(parents=True)
    (log_dir / "step.00.stdout.log").write_text(
        "\n".join(
            [
                json.dumps({"type": "status", "message": "working"}),
                json.dumps({"message": {"role": "user", "content": "Do work."}}),
                json.dumps({"message": {"role": "tool", "content": "secret tool output"}}),
                json.dumps(
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Final answer."}],
                        },
                    }
                ),
                json.dumps({"type": "response.output_text.delta", "delta": "Extra line."}),
            ]
        ),
        encoding="utf-8",
    )
    session = CompletionSession(
        session_id="session_1",
        metadata={"agent_result": {"metadata": {"log_dir": str(log_dir), "last_step": 0}}},
    )

    trajectory = asyncio.run(AgentTranscriptBuilder().build(session))

    assert trajectory.status == "COMPLETED"
    assert trajectory.traces[0].response_messages == [
        {"role": "assistant", "content": "Final answer.\nExtra line."}
    ]


def test_agent_transcript_builder_extracts_codex_agent_message_items(tmp_path) -> None:
    log_dir = tmp_path / "logs" / "agent"
    log_dir.mkdir(parents=True)
    (log_dir / "step.00.stdout.log").write_text(
        "\n".join(
            [
                json.dumps({"type": "status", "message": "working"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "tool_call", "text": "secret tool output"},
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "Real Codex answer."},
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    session = CompletionSession(
        session_id="session_1",
        metadata={"agent_result": {"metadata": {"log_dir": str(log_dir), "last_step": 0}}},
    )

    trajectory = asyncio.run(AgentTranscriptBuilder().build(session))

    assert trajectory.status == "COMPLETED"
    assert trajectory.traces[0].response_messages == [
        {"role": "assistant", "content": "Real Codex answer."}
    ]


def test_agent_transcript_builder_concatenates_streaming_output_deltas(tmp_path) -> None:
    log_dir = tmp_path / "logs" / "agent"
    log_dir.mkdir(parents=True)
    (log_dir / "step.00.stdout.log").write_text(
        "\n".join(
            [
                json.dumps({"type": "response.output_text.delta", "delta": "Hel"}),
                json.dumps({"type": "response.output_text.delta", "delta": "lo"}),
            ]
        ),
        encoding="utf-8",
    )
    session = CompletionSession(
        session_id="session_1",
        metadata={"agent_result": {"metadata": {"log_dir": str(log_dir), "last_step": 0}}},
    )

    trajectory = asyncio.run(AgentTranscriptBuilder().build(session))

    assert trajectory.status == "COMPLETED"
    assert trajectory.traces[0].response_messages == [{"role": "assistant", "content": "Hello"}]


def test_agent_transcript_builder_uses_final_output_text_instead_of_duplicate_deltas(
    tmp_path,
) -> None:
    log_dir = tmp_path / "logs" / "agent"
    log_dir.mkdir(parents=True)
    (log_dir / "step.00.stdout.log").write_text(
        "\n".join(
            [
                json.dumps({"type": "response.output_text.delta", "delta": "Hel"}),
                json.dumps({"type": "response.output_text.delta", "delta": "lo"}),
                json.dumps({"type": "response.output_text.done", "text": "Hello"}),
            ]
        ),
        encoding="utf-8",
    )
    session = CompletionSession(
        session_id="session_1",
        metadata={"agent_result": {"metadata": {"log_dir": str(log_dir), "last_step": 0}}},
    )

    trajectory = asyncio.run(AgentTranscriptBuilder().build(session))

    assert trajectory.status == "COMPLETED"
    assert trajectory.traces[0].response_messages == [{"role": "assistant", "content": "Hello"}]


def test_agent_transcript_builder_rejects_json_log_without_assistant_content(tmp_path) -> None:
    log_dir = tmp_path / "logs" / "agent"
    log_dir.mkdir(parents=True)
    (log_dir / "step.00.stdout.log").write_text(
        "\n".join(
            [
                json.dumps({"type": "status", "message": "working"}),
                json.dumps({"message": {"role": "tool", "content": "secret tool output"}}),
            ]
        ),
        encoding="utf-8",
    )
    session = CompletionSession(
        session_id="session_1",
        metadata={"agent_result": {"metadata": {"log_dir": str(log_dir), "last_step": 0}}},
    )

    trajectory = asyncio.run(AgentTranscriptBuilder().build(session))

    assert trajectory.status == "ERROR"
    assert trajectory.error == "no assistant transcript"
    assert trajectory.traces == []
