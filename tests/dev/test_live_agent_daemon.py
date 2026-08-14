from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sqlite3
import threading
import urllib.request

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


def test_codex_readiness_accepts_login_status_written_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="",
            stderr="Logged in using ChatGPT\n",
        ),
    )
    MODULE.CodexRunner("codex", 30, None).check_ready()


def test_sqlite_store_persists_projects_sessions_and_transcripts(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = MODULE.DevelopmentStateStore(database)
    project = {
        "project_id": "development-project-1",
        "display_name": "Persistent project",
        "config": {"schema_version": "2", "task": {"title": "Question", "objective": "Hello"}},
    }
    store.create_project(project)
    request = {
        "project_id": project["project_id"],
        "project_name": project["display_name"],
        "task_title": "Question",
        "instruction": "Hello",
    }
    store.start_session("dev-session-1", request)
    store.complete_session(
        "dev-session-1",
        {
            "response": "Hello from Codex.",
            "model": "test-model",
            "duration_ms": 123,
            "logs": ["admitted", "completed"],
        },
    )

    restored = MODULE.DevelopmentStateStore(database).snapshot()
    assert restored["active_project_id"] == "development-project-1"
    assert restored["projects"][0]["display_name"] == "Persistent project"
    assert restored["projects"][0]["config"] == project["config"]
    assert restored["sessions"][0] == {
        "session_id": "dev-session-1",
        "project_id": "development-project-1",
        "task_title": "Question",
        "instruction": "Hello",
        "response": "Hello from Codex.",
        "model": "test-model",
        "state": "completed",
        "duration_ms": 123,
        "logs": ["admitted", "completed"],
        "error": None,
        "created_at": restored["sessions"][0]["created_at"],
        "updated_at": restored["sessions"][0]["updated_at"],
    }

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "development_metadata",
        "development_projects",
        "development_sessions",
    } <= tables


def test_sqlite_store_marks_interrupted_running_session_failed_on_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = MODULE.DevelopmentStateStore(database)
    store.create_project(
        {
            "project_id": "development-project-1",
            "display_name": "Persistent project",
            "config": {},
        }
    )
    store.start_session(
        "dev-session-running",
        {
            "project_id": "development-project-1",
            "project_name": "Persistent project",
            "task_title": "Interrupted",
            "instruction": "Wait",
        },
    )

    restored = MODULE.DevelopmentStateStore(database).snapshot()
    assert restored["sessions"][0]["state"] == "failed"
    assert "restarted" in restored["sessions"][0]["error"]


def test_http_api_round_trip_persists_a_real_runner_response(tmp_path: Path) -> None:
    class FakeRunner:
        def run(self, request: dict[str, str]) -> dict[str, object]:
            return {
                "schema_version": "1",
                "response": f"Answer to: {request['instruction']}",
                "model": "fake-model",
                "duration_ms": 5,
                "logs": ["admitted", "completed"],
            }

    token = "t" * 32
    server = MODULE.DevelopmentAgentServer(
        ("127.0.0.1", 0),
        token,
        FakeRunner(),
        MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        project = {
            "schema_version": "1",
            "project_id": "development-project-1",
            "display_name": "Persistent project",
            "config": {"schema_version": "2"},
        }
        _request_json(base_url, "/openevo-dev-agent/v1/projects", token, project)
        turn = _request_json(
            base_url,
            "/openevo-dev-agent/v1/sessions",
            token,
            {
                "schema_version": "1",
                "project_id": "development-project-1",
                "project_name": "Persistent project",
                "task_title": "Question",
                "instruction": "Hello",
            },
        )
        state = _request_json(base_url, "/openevo-dev-agent/v1/state", token)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert turn["response"] == "Answer to: Hello"
    assert state["projects"][0]["display_name"] == "Persistent project"
    assert state["sessions"][0]["response"] == "Answer to: Hello"
    assert state["sessions"][0]["state"] == "completed"


def _request_json(
    base_url: str,
    path: str,
    token: str,
    body: dict[str, object] | None = None,
) -> dict[str, object]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        method="GET" if body is None else "POST",
        headers={
            "Authorization": f"Bearer {token}",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))
