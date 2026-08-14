from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "dev" / "live_agent_daemon.py"
SPEC = importlib.util.spec_from_file_location("live_agent_daemon", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_validate_request_accepts_the_closed_development_contract() -> None:
    assert MODULE.validate_request(
        {
            "schema_version": "1",
            "project_id": "fixture-project-1",
            "project_name": "Live project",
            "task_title": "Question",
            "instruction": "What is two plus two?",
        }
    ) == {
        "project_id": "fixture-project-1",
        "project_name": "Live project",
        "task_title": "Question",
        "instruction": "What is two plus two?",
    }


@pytest.mark.parametrize(
    "change",
    [
        {"extra": "not allowed"},
        {"schema_version": "2"},
        {"project_id": "contains spaces"},
        {"instruction": ""},
    ],
)
def test_validate_request_rejects_non_contract_input(change: dict[str, str]) -> None:
    payload = {
        "schema_version": "1",
        "project_id": "project-1",
        "project_name": "Live project",
        "task_title": "Question",
        "instruction": "Hello",
    }
    payload.update(change)
    with pytest.raises(MODULE.RequestError):
        MODULE.validate_request(payload)


def test_extract_event_logs_ignores_agent_message_content() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "secret response body"},
                }
            ),
            json.dumps({"type": "turn.completed"}),
        ]
    )
    assert MODULE.extract_event_logs(stdout) == [
        "Codex event: thread.started",
        "Codex event: turn.completed",
    ]
