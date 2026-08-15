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


def test_codex_runner_injects_evolved_memory_into_the_next_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["prompt"] = str(kwargs["input"])
        captured["args"] = args
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps({
                "answer": "The next answer used prior memory.",
                "file_writes": [{"path": "answer.py", "content": "print(4)\n"}],
                "delete_paths": [],
            }),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(MODULE.subprocess, "run", run)
    result = MODULE.CodexRunner("codex", 30, "test-model").run({
        "project_name": "Memory project",
        "task_title": "Second question",
        "instruction": "Answer the next question.",
        "workspace_path": workspace,
        "workspace_snapshot": {"entries": []},
        "evolved_contexts": [{
            "target_id": "text_memory",
            "documents": [{
                "path": "memory.md",
                "content": "# Evolved memory\n\n- Verify the answer before responding.",
            }],
        }],
    })

    assert "Evolved text_memory from earlier sessions" in captured["prompt"]
    assert "Verify the answer before responding" in captured["prompt"]
    assert "persistent OpenEvo project workspace" in captured["prompt"]
    assert "read-only" in captured["args"]
    assert "shell_tool" in captured["args"]
    assert "--output-schema" in captured["args"]
    assert Path(captured["args"][captured["args"].index("--cd") + 1]) == workspace
    assert not (workspace / "answer.py").exists()
    MODULE.ProjectWorkspaceStore(tmp_path / "workspaces").apply_mutations(
        "project-1", result["file_mutations"]
    )
    assert (tmp_path / "workspaces" / "project-1" / "answer.py").read_text(
        encoding="utf-8"
    ) == "print(4)\n"
    assert result["response"] == "The next answer used prior memory."


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
        "selected_evolution": [],
        "evolution_errors": [],
        "workspace_changes": [],
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
        "development_artifacts",
        "development_document_artifacts",
        "development_evolution_artifacts_v2",
        "development_evolution_jobs",
    } <= tables

    workspace = restored["workspaces"][0]
    assert workspace == {
        "project_id": "development-project-1",
        "entries": [],
        "truncated": False,
    }


def test_project_workspace_files_persist_on_the_server_and_are_bounded(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = MODULE.DevelopmentStateStore(database)
    store.create_project({
        "project_id": "development-project-files",
        "display_name": "Coding project",
        "config": {},
    })
    store.apply_workspace_mutations("development-project-files", {
        "file_writes": [{"path": "src/main.py", "content": "print('hello')\n"}],
        "delete_paths": [],
    })
    workspace = store.workspace_path("development-project-files")
    (workspace / "binary.bin").write_bytes(b"\x00\x01")

    restored = MODULE.DevelopmentStateStore(database).snapshot()["workspaces"][0]
    entries = {entry["path"]: entry for entry in restored["entries"]}

    assert entries["src"]["kind"] == "directory"
    assert entries["src/main.py"]["content"] == "print('hello')\n"
    assert len(entries["src/main.py"]["content_sha256"]) == 64
    assert entries["binary.bin"]["content"] is None
    assert all("/root/" not in entry["path"] for entry in restored["entries"])


def test_project_workspace_broker_rejects_paths_outside_the_project(tmp_path: Path) -> None:
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    store.create_project({
        "project_id": "development-project-safe",
        "display_name": "Safe project",
        "config": {},
    })

    with pytest.raises(MODULE.AgentRunError, match="unsafe workspace path"):
        store.apply_workspace_mutations("development-project-safe", {
            "file_writes": [{"path": "../escaped.txt", "content": "no"}],
            "delete_paths": [],
        })

    assert not (tmp_path / "escaped.txt").exists()


def test_sqlite_store_upgrades_legacy_session_evolution_selections(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = MODULE.DevelopmentStateStore(database)
    store.create_project(
        {
            "project_id": "development-project-legacy",
            "display_name": "Legacy project",
            "config": {},
        }
    )
    store.start_session(
        "dev-session-legacy",
        {
            "project_id": "development-project-legacy",
            "project_name": "Legacy project",
            "task_title": "Old selection",
            "instruction": "Restore this session.",
        },
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE development_sessions SET selected_evolution_json = ? WHERE session_id = ?",
            (
                json.dumps([
                    {"target_id": "text_memory", "method": "text_memory_reflector"}
                ]),
                "dev-session-legacy",
            ),
        )

    restored = MODULE.DevelopmentStateStore(database).snapshot()

    assert restored["sessions"][0]["selected_evolution"] == [
        {
            "target_id": "text_memory",
            "method": "text_memory_reflector",
            "config": {},
        }
    ]
    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            "SELECT selected_evolution_json FROM development_sessions WHERE session_id = ?",
            ("dev-session-legacy",),
        ).fetchone()[0]
    assert json.loads(stored)[0]["config"] == {}


def test_real_text_memory_reflector_persists_and_consumes_prior_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openevo.evolution import methods

    prompts: list[str] = []

    def reflect(prompt: str, _config: dict[str, object], **_kwargs: object) -> str:
        prompts.append(prompt)
        return (
            "# Evolved memory\n\n"
            f"- Reflection round {len(prompts)}: verify the answer before responding.\n"
        )

    monkeypatch.setattr(methods, "_generate_reflector_markdown", reflect)
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    project = {
        "project_id": "development-project-1",
        "display_name": "Evolving project",
        "config": {
            "schema_version": "2",
            "evolution": {
                "targets": {
                    "text_memory": {
                        "enabled": True,
                        "method": "text_memory_reflector",
                        "config": {},
                    }
                }
            },
        },
    }
    store.create_project(project)
    evolver = MODULE.TextMemoryEvolutionRunner(
        state_root=tmp_path,
        codex_binary="codex",
        model="test-model",
        timeout_seconds=30,
    )

    artifacts = []
    for ordinal in (1, 2):
        session_id = f"dev-session-{ordinal}"
        request = {
            "project_id": project["project_id"],
            "project_name": project["display_name"],
            "task_title": f"Question {ordinal}",
            "instruction": f"Answer question {ordinal}",
        }
        result = {
            "response": f"Answer {ordinal}",
            "model": "test-model",
            "duration_ms": 1,
            "logs": ["completed"],
        }
        store.start_session(session_id, request)
        store.complete_session(session_id, result)
        batch = evolver.evolve(
            session_id=session_id,
            request=request,
            result=result,
            store=store,
        )
        assert batch["errors"] == []
        artifacts.append(batch["artifacts"][0])

    assert artifacts[0]["method"] == "text_memory_reflector"
    assert artifacts[1]["previous_artifact_id"] == artifacts[0]["artifact_id"]
    assert "Reflection round 1" in prompts[1]
    assert store.latest_memory(project["project_id"])["content"].startswith("# Evolved memory")


def test_document_evolution_runner_can_publish_all_selected_document_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openevo.evolution import methods

    monkeypatch.setattr(
        methods,
        "_generate_reflector_markdown",
        lambda prompt, _config, **_kwargs: (
            "# Skill\n\n- Reuse the workflow.\n"
            if "SKILL.md" in prompt
            else "# Memory\n\n- Remember the lesson.\n"
        ),
    )
    monkeypatch.setattr(
        methods,
        "_generate_audited_agent_system_reflection",
        lambda *_args, **_kwargs: ("# Agent system\n\n- Verify before answering.\n", {}),
    )
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    project = {
        "project_id": "development-project-all",
        "display_name": "All document methods",
        "config": {
            "schema_version": "2",
            "evolution": {
                "targets": {
                    target_id: {"enabled": True, "method": method, "config": {}}
                    for target_id, method in {
                        "text_memory": "text_memory_reflector",
                        "skill_bundle": "skill_bundle_reflector",
                        "agent_system": "agent_system_reflector",
                    }.items()
                }
            },
        },
    }
    store.create_project(project)
    request = {
        "project_id": project["project_id"],
        "project_name": project["display_name"],
        "task_title": "Learn",
        "instruction": "Learn from this exchange.",
    }
    result = {"response": "Done.", "model": "test", "duration_ms": 1, "logs": []}
    store.start_session("dev-session-all", request)
    store.complete_session("dev-session-all", result)

    batch = MODULE.DocumentEvolutionRunner(
        state_root=tmp_path,
        codex_binary="codex",
        model="test-model",
        timeout_seconds=30,
    ).evolve(
        session_id="dev-session-all",
        request=request,
        result=result,
        store=store,
    )

    assert batch["errors"] == []
    assert {artifact["artifact_type"] for artifact in batch["artifacts"]} == {
        "text_memory",
        "skill_bundle",
        "agent_system",
    }
    assert {artifact["content_path"] for artifact in batch["artifacts"]} == {
        "memory.md",
        "SKILL.md",
        "AGENTS.md",
    }
    jobs = store.snapshot()["evolution_jobs"]
    assert {job["target_id"] for job in jobs} == {
        "text_memory",
        "skill_bundle",
        "agent_system",
    }
    assert all(job["state"] == "completed" and job["artifact_ids"] for job in jobs)
    assert all(job["config"] == {} for job in jobs)


def test_desktop_default_reflectors_receive_the_current_transcript_dataset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openevo.evolution import methods

    monkeypatch.setattr(
        methods,
        "_generate_reflector_markdown",
        lambda *_args, **_kwargs: "# Memory\n\n- Reuse the successful action.\n",
    )
    monkeypatch.setattr(
        methods,
        "_generate_audited_agent_system_reflection",
        lambda *_args, **_kwargs: ("# Agent system\n\n- Verify the result.\n", {}),
    )
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    project = {
        "project_id": "development-project-default-reflectors",
        "display_name": "Default reflectors",
        "config": {
            "schema_version": "2",
            "evolution": {
                "targets": {
                    "agent_system": {
                        "enabled": True,
                        "method": "agent_system_gepa_reflector",
                        "config": {},
                    },
                    "text_memory": {
                        "enabled": True,
                        "method": "text_memory_expel_reflector",
                        "config": {},
                    },
                }
            },
        },
    }
    store.create_project(project)
    request = {
        "project_id": project["project_id"],
        "project_name": project["display_name"],
        "task_title": "Learn from file creation",
        "instruction": "Create a file and remember the reusable lesson.",
    }
    result = {"response": "Created it.", "model": "test", "duration_ms": 1, "logs": []}
    store.start_session("dev-session-default-reflectors", request)
    store.complete_session("dev-session-default-reflectors", result)

    batch = MODULE.DocumentEvolutionRunner(
        state_root=tmp_path,
        codex_binary="codex",
        model="test-model",
        timeout_seconds=30,
    ).evolve(
        session_id="dev-session-default-reflectors",
        request=request,
        result=result,
        store=store,
    )

    assert batch["errors"] == []
    assert {artifact["artifact_type"] for artifact in batch["artifacts"]} >= {
        "agent_system",
        "text_memory",
    }
    assert all(job["state"] == "completed" for job in store.snapshot()["evolution_jobs"])


def test_development_capabilities_are_projected_from_the_core_catalog(tmp_path: Path) -> None:
    runner = MODULE.DocumentEvolutionRunner(
        state_root=tmp_path,
        codex_binary="codex",
        model="test-model",
        timeout_seconds=30,
    )
    runner.check_ready()
    payload = runner.capabilities()

    assert payload["authority"] == "development_catalog_unverified"
    targets = payload["capabilities"]["targets"]
    assert {target["target_id"] for target in targets} >= {
        "text_memory",
        "skill_bundle",
        "agent_system",
    }
    assert all(target["renderer_kind"] in {
        "markdown", "file_bundle", "structured_summary", "adapter"
    } for target in targets)
    assert all(
        method["method_id"] != "text_memory_memevolve"
        for target in targets
        for method in target["methods"]
    )


def test_document_evolution_runner_allows_a_session_with_no_selected_method(
    tmp_path: Path,
) -> None:
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    project = {
        "project_id": "development-project-none",
        "display_name": "No evolution",
        "config": {
            "evolution": {
                "targets": {
                    target_id: {"enabled": False, "method": method, "config": {}}
                    for target_id, method in {
                        "text_memory": "text_memory_reflector",
                        "skill_bundle": "skill_bundle_reflector",
                        "agent_system": "agent_system_reflector",
                    }.items()
                }
            }
        },
    }
    store.create_project(project)
    request = {
        "project_id": project["project_id"],
        "project_name": project["display_name"],
        "task_title": "Answer only",
        "instruction": "Do not evolve documents.",
    }
    result = {"response": "Done.", "model": "test", "duration_ms": 1, "logs": []}
    store.start_session("dev-session-none", request)
    store.complete_session("dev-session-none", result)

    batch = MODULE.DocumentEvolutionRunner(
        state_root=tmp_path,
        codex_binary="codex",
        model="test-model",
        timeout_seconds=30,
    ).evolve(
        session_id="dev-session-none",
        request=request,
        result=result,
        store=store,
    )

    assert batch == {"artifacts": [], "errors": []}
    assert store.snapshot()["sessions"][0]["selected_evolution"] == []


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
        def run(self, request: dict[str, object]) -> dict[str, object]:
            workspace = request["workspace_path"]
            assert isinstance(workspace, Path)
            assert request["workspace_snapshot"] == {
                "project_id": "development-project-1",
                "entries": [],
                "truncated": False,
            }
            return {
                "schema_version": "1",
                "response": f"Answer to: {request['instruction']}",
                "file_mutations": {
                    "file_writes": [{
                        "path": "hello.py",
                        "content": "print('hello')\n",
                    }],
                    "delete_paths": [],
                },
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
    assert turn["workspace_changes"][0]["path"] == "hello.py"
    assert turn["workspace_changes"][0]["change_type"] == "created"
    assert state["projects"][0]["display_name"] == "Persistent project"
    assert state["sessions"][0]["response"] == "Answer to: Hello"
    assert state["sessions"][0]["state"] == "completed"
    assert state["sessions"][0]["workspace_changes"][0]["path"] == "hello.py"
    assert state["workspaces"][0]["entries"][0]["content"] == "print('hello')\n"


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
