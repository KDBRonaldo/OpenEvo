from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlparse
import zipfile

import pytest


from openevo.daemon import product_app as MODULE


def test_legacy_daemon_script_is_only_a_thin_compatibility_launcher() -> None:
    script = (
        Path(__file__).parents[2] / "scripts" / "dev" / "live_agent_daemon.py"
    ).read_text(encoding="utf-8")

    assert "from openevo.daemon.product_app import" in script
    assert len(script.splitlines()) < 30


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


def test_task_presentation_v2_is_paginated_and_restart_safe(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    project_id = "development-project-presentations"
    task_id = "development-task-presentation-1"
    store = MODULE.DevelopmentStateStore(database)
    store.create_project(
        {
            "project_id": project_id,
            "display_name": "Presentation project",
            "config": {},
        }
    )
    store.start_session(
        task_id,
        {
            "project_id": project_id,
            "project_name": "Presentation project",
            "task_title": "Read the answer",
            "instruction": "Reply with the durable answer.",
        },
    )
    store.complete_session(
        task_id,
        {
            "response": "The durable answer.",
            "model": "codex-test",
            "duration_ms": 12,
            "logs": ["Harness completed."],
            "workspace_changes": [],
            "runtime_activation": None,
        },
    )

    page = MODULE.DevelopmentStateStore(database).task_presentations_v2(
        project_id=project_id, limit=25
    )

    assert page.has_more is False
    assert page.next_cursor is None
    assert len(page.items) == 1
    presentation = page.items[0]
    assert presentation.task_id == task_id
    assert presentation.instruction == "Reply with the durable answer."
    assert presentation.response == "The durable answer."
    assert presentation.state == "completed"
    assert presentation.model_dump(mode="json")["schema_version"] == "2"


def test_project_and_session_deletions_are_durable_filtered_tombstones(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    store = MODULE.DevelopmentStateStore(database)
    for project_id, display_name in (
        ("project-keep", "Keep"),
        ("project-delete", "Delete"),
    ):
        store.create_project(
            {"project_id": project_id, "display_name": display_name, "config": {}}
        )
    store.start_session(
        "session-delete",
        {
            "project_id": "project-keep",
            "project_name": "Keep",
            "task_title": "Disposable Session",
            "instruction": "Complete before deletion.",
        },
    )
    store.complete_session(
        "session-delete",
        {
            "response": "Done.",
            "model": "codex-test",
            "duration_ms": 1,
            "logs": [],
            "workspace_changes": [],
            "runtime_activation": None,
        },
    )

    assert store.delete_session("session-delete", "delete-session-action") == "project-delete"
    assert store.delete_session("session-delete", "delete-session-action") == "project-delete"
    with pytest.raises(KeyError):
        store.delete_session("session-delete", "different-session-action")
    with pytest.raises(KeyError):
        store.get_session("session-delete")
    assert store.task_presentations_v2(project_id="project-keep").items == []

    active_project_id = store.delete_project("project-delete", "delete-project-action")
    assert active_project_id == "project-keep"
    restarted = MODULE.DevelopmentStateStore(database)
    snapshot = restarted.snapshot()
    assert snapshot["active_project_id"] == "project-keep"
    assert [project["project_id"] for project in snapshot["projects"]] == ["project-keep"]
    assert snapshot["sessions"] == []
    assert (tmp_path / "workspaces" / "project-delete").is_dir()
    with pytest.raises(KeyError):
        restarted.update_project(
            "project-delete", {"display_name": "Still deleted", "config": {}}
        )
    assert restarted.delete_project("project-delete", "delete-project-action") == "project-keep"
    with pytest.raises(KeyError):
        restarted.delete_project("project-delete", "different-project-action")


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


def test_workspace_snapshot_projects_bounded_pdf_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePage:
        def __init__(self, text: str) -> None:
            self._text = text

        def extract_text(self) -> str:
            return self._text

    class FakePdfReader:
        def __init__(self, path: Path, *, strict: bool) -> None:
            assert path.name == "paper.pdf"
            assert strict is False
            self.pages = [FakePage("A reusable result from the uploaded paper.")]

    monkeypatch.setitem(
        MODULE.ProjectWorkspaceStore.snapshot.__globals__, "PdfReader", FakePdfReader
    )
    store = MODULE.ProjectWorkspaceStore(tmp_path / "workspaces")
    project = store.ensure_project("project-1")
    (project / "paper.pdf").write_bytes(b"%PDF-1.7\nfixture")

    snapshot = store.snapshot("project-1")
    entry = snapshot["entries"][0]

    assert entry["path"] == "paper.pdf"
    assert entry["media_type"] == "application/pdf"
    assert entry["content"] == (
        "[Text extracted from PDF: paper.pdf]\n"
        "\n--- Page 1 ---\n"
        "A reusable result from the uploaded paper.\n"
    )


def test_workspace_snapshot_projects_docx_text_and_zip_listing(tmp_path: Path) -> None:
    store = MODULE.ProjectWorkspaceStore(tmp_path / "workspaces")
    project = store.ensure_project("project-1")
    with zipfile.ZipFile(project / "notes.docx", "w") as archive:
        archive.writestr(
            "word/document.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <w:document xmlns:w="urn:test"><w:body><w:p>
            <w:r><w:t>Uploaded Office document text.</w:t></w:r>
            </w:p></w:body></w:document>""",
        )
    with zipfile.ZipFile(project / "sources.zip", "w") as archive:
        archive.writestr("src/main.py", "print('hello')\n")

    entries = {entry["path"]: entry for entry in store.snapshot("project-1")["entries"]}

    assert "Uploaded Office document text." in entries["notes.docx"]["content"]
    assert "src/main.py" in entries["sources.zip"]["content"]


def test_codex_readiness_accepts_login_status_written_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="",
            stderr="Logged in using ChatGPT\n",
        ),
    )
    MODULE.CodexRunner("codex", 30, None).check_ready()


def test_codex_runner_materializes_core_runtime_contributions_for_the_next_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "reference.png").write_bytes(b"small image fixture")

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["prompt"] = str(kwargs["input"])
        captured["args"] = args
        captured["env"] = kwargs["env"]
        runtime_workspace = Path(args[args.index("--cd") + 1])
        captured["agents_md"] = (runtime_workspace / "AGENTS.md").read_text(encoding="utf-8")
        captured["skill_md"] = next(
            (runtime_workspace / ".agents" / "skills").glob("*/SKILL.md")
        ).read_text(encoding="utf-8")
        schema_path = Path(args[args.index("--output-schema") + 1])
        captured["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "answer": "The next answer used prior memory.",
                    "file_writes": [{"path": "answer.py", "content": "print(4)\n"}],
                    "delete_paths": [],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    result = MODULE.CodexRunner("codex", 30, "test-model").run(
        {
            "project_name": "Memory project",
            "task_title": "Second question",
            "instruction": "Answer the next question.",
            "workspace_path": workspace,
            "workspace_snapshot": {"entries": []},
            "inference": {
                "model": "OpenEvo/Fixture-0.1B",
                "base_url": "http://127.0.0.1:18432/v1",
            },
            "evolved_contexts": [
                {
                    "artifact_id": "memory-artifact-1",
                    "artifact_type": "text_memory",
                    "target_id": "text_memory",
                    "manifest": {"content_path": "memory.md"},
                    "documents": [
                        {
                            "path": "memory.md",
                            "media_type": "text/markdown",
                            "content": "# Evolved memory\n\n- Verify the answer before responding.",
                        }
                    ],
                },
                {
                    "artifact_id": "skill-artifact-1",
                    "artifact_type": "skill_bundle",
                    "target_id": "skill_bundle",
                    "manifest": {"content_path": "SKILL.md"},
                    "documents": [
                        {
                            "path": "SKILL.md",
                            "media_type": "text/markdown",
                            "content": "# Native evolved skill\n\nUse this workflow when relevant.",
                        }
                    ],
                },
                {
                    "artifact_id": "agent-system-artifact-1",
                    "artifact_type": "agent_system",
                    "target_id": "agent_system",
                    "manifest": {"content_path": "AGENTS.md", "target_path": "AGENTS.md"},
                    "documents": [
                        {
                            "path": "AGENTS.md",
                            "media_type": "text/markdown",
                            "content": "# Native evolved agent system\n\nFollow the project policy.",
                        }
                    ],
                },
            ],
        }
    )

    assert "Runtime instructions resolved by OpenEvo Core" in captured["prompt"]
    assert "Verify the answer before responding" in captured["prompt"]
    assert "Native evolved skill" not in captured["prompt"]
    assert "Native evolved agent system" not in captured["prompt"]
    assert captured["agents_md"] == "# Native evolved agent system\n\nFollow the project policy."
    assert captured["skill_md"].startswith("---\nname: skill-artifact-1\ndescription: ")
    assert "\n---\n\n# Native evolved skill\n" in captured["skill_md"]
    assert "persistent OpenEvo project workspace" in captured["prompt"]
    assert "trusted runtime model identifier is OpenEvo/Fixture-0.1B" in captured["prompt"]
    assert captured["schema"]["properties"]["answer"]["minLength"] == 1
    assert "read-only" in captured["args"]
    assert "shell_tool" in captured["args"]
    assert "--image" in captured["args"]
    assert captured["args"][captured["args"].index("--image") + 1].endswith("/reference.png")
    assert "daemon-extracted document projections" in captured["prompt"]
    assert "--ignore-rules" not in captured["args"]
    assert "--output-schema" in captured["args"]
    assert captured["args"][captured["args"].index("--model") + 1] == "OpenEvo/Fixture-0.1B"
    assert 'model_provider="openevo_self_deployed"' in captured["args"]
    assert any("http://127.0.0.1:18432/v1" in value for value in captured["args"])
    assert captured["env"]["OPENAI_API_KEY"] == "openevo-local"
    assert result["model"] == "OpenEvo/Fixture-0.1B"
    assert Path(captured["args"][captured["args"].index("--cd") + 1]) != workspace
    assert captured["env"]["OPENEVO_MEMORY_FILE"].endswith("/memory.md")
    assert captured["env"]["OPENEVO_SKILLS_DIR"].endswith("/.agents/skills")
    assert captured["env"]["OPENEVO_AGENTS_MD"].endswith("/AGENTS.md")
    assert not (workspace / "answer.py").exists()
    MODULE.ProjectWorkspaceStore(tmp_path / "workspaces").apply_mutations(
        "project-1", result["file_mutations"]
    )
    assert (tmp_path / "workspaces" / "project-1" / "answer.py").read_text(
        encoding="utf-8"
    ) == "print(4)\n"
    assert result["response"] == "The next answer used prior memory."


def test_runtime_materializer_passes_explicit_memory_and_spawn_controls_to_harness(
    tmp_path: Path,
) -> None:
    persistent_workspace = tmp_path / "persistent-workspace"
    persistent_workspace.mkdir()
    runtime_workspace = tmp_path / "runtime-workspace"

    runtime = MODULE.DevelopmentRuntimeContextMaterializer().materialize(
        persistent_workspace=persistent_workspace,
        runtime_workspace=runtime_workspace,
        contexts=[
            {
                "artifact_id": "memory-policy-1",
                "artifact_type": "text_memory",
                "target_id": "text_memory",
                "manifest": {
                    "content_path": "memory.md",
                    "runtime_control": {
                        "kind": "memory",
                        "read_timing": "on_demand",
                        "write_timing": "manual",
                    },
                },
                "documents": [
                    {
                        "path": "memory.md",
                        "media_type": "text/markdown",
                        "content": "# On-demand memory",
                    }
                ],
            },
            {
                "artifact_id": "agent-policy-1",
                "artifact_type": "agent_system",
                "target_id": "agent_system",
                "manifest": {
                    "content_path": "AGENTS.md",
                    "target_path": "AGENTS.md",
                    "runtime_control": {
                        "kind": "agent_system",
                        "spawn_plan": {
                            "agents": [
                                {
                                    "agent_id": "reviewer",
                                    "role": "Reviewer",
                                    "instructions": "Review the proposed result.",
                                }
                            ],
                        },
                    },
                },
                "documents": [
                    {
                        "path": "AGENTS.md",
                        "media_type": "text/markdown",
                        "content": "# Coordinator",
                    }
                ],
            },
        ],
    )

    assert runtime["instruction_sections"] == []
    assert {control["kind"] for control in runtime["runtime_controls"]} == {
        "agent_system",
        "memory",
    }
    assert runtime["environment"]["OPENEVO_MEMORY_RUNTIME_CONTROL"].endswith(
        "/runtime-controls/text_memory.json"
    )
    assert runtime["environment"]["OPENEVO_AGENT_SYSTEM_RUNTIME_CONTROL"].endswith(
        "/runtime-controls/agent_system.json"
    )
    assert any("structured spawn plan staged" in item for item in runtime["activations"])


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
            "runtime_activation": {
                "schema_version": "1",
                "adapter_id": "codex-development-v1",
                "fully_supported": True,
                "decisions": [],
            },
        },
    )

    restored = MODULE.DevelopmentStateStore(database).snapshot()
    assert restored["active_project_id"] == "development-project-1"
    assert restored["projects"][0]["display_name"] == "Persistent project"
    assert restored["projects"][0]["config"] == project["config"]
    assert restored["sessions"][0] == {
        "session_id": "dev-session-1",
        "project_id": "development-project-1",
        "project_head_id": "development-project-1-head-0",
        "task_title": "Question",
        "instruction": "Hello",
        "response": "Hello from Codex.",
        "model": "test-model",
        "state": "completed",
        "duration_ms": 123,
        "logs": ["Remote development daemon admitted the session.", "admitted", "completed"],
        "selected_evolution": [],
        "evolution_errors": [],
        "workspace_changes": [],
        "context_artifact_ids": [],
        "runtime_activation": {
            "schema_version": "1",
            "adapter_id": "codex-development-v1",
            "fully_supported": True,
            "decisions": [],
        },
        "evolution_evidence_ready": False,
        "error": None,
        "created_at": restored["sessions"][0]["created_at"],
        "updated_at": restored["sessions"][0]["updated_at"],
    }

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {
        "development_metadata",
        "development_state_events",
        "development_projects",
        "development_sessions",
        "development_artifacts",
        "development_document_artifacts",
        "development_evolution_artifacts_v2",
        "development_evolution_jobs",
        "development_dataset_artifacts",
    } <= tables

    workspace = restored["workspaces"][0]
    assert workspace == {
        "project_id": "development-project-1",
        "entries": [],
        "truncated": False,
    }


def test_daemon_event_journal_persists_ordered_state_changes(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = MODULE.DevelopmentStateStore(database)
    project_id = "development-project-events"
    store.create_project(
        {
            "project_id": project_id,
            "display_name": "Event project",
            "config": {},
        }
    )
    baseline = store.read_events(after_sequence=None, limit=100, wait_seconds=0)
    assert baseline["events"] == []
    assert baseline["latest_sequence"] == 1

    store.start_session(
        "dev-session-events",
        {
            "project_id": project_id,
            "project_name": "Event project",
            "task_title": "Observe events",
            "instruction": "Return one line.",
        },
    )
    store.complete_session(
        "dev-session-events",
        {
            "response": "done",
            "model": "test-model",
            "duration_ms": 1,
            "logs": ["completed"],
        },
    )
    page = store.read_events(
        after_sequence=baseline["latest_sequence"],
        limit=100,
        wait_seconds=0,
    )

    assert [event["sequence"] for event in page["events"]] == [2, 3]
    assert {event["project_id"] for event in page["events"]} == {project_id}
    assert {event["event_type"] for event in page["events"]} == {"state_changed"}
    assert all(len(event["payload_sha256"]) == 64 for event in page["events"])

    restored = MODULE.DevelopmentStateStore(database)
    restored_page = restored.read_events(after_sequence=1, limit=100, wait_seconds=0)
    assert restored_page == page


def test_daemon_event_long_poll_wakes_after_committed_change(tmp_path: Path) -> None:
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    project_id = "development-project-event-wait"
    store.create_project(
        {
            "project_id": project_id,
            "display_name": "Waiting project",
            "config": {},
        }
    )
    cursor = store.read_events(after_sequence=None, limit=100, wait_seconds=0)["latest_sequence"]
    result: dict[str, object] = {}

    def wait_for_event() -> None:
        result.update(store.read_events(after_sequence=cursor, limit=100, wait_seconds=2))

    thread = threading.Thread(target=wait_for_event)
    thread.start()
    time.sleep(0.05)
    store.update_project(
        project_id,
        {"display_name": "Updated project", "config": {}},
    )
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(result["events"]) == 1
    assert result["events"][0]["sequence"] == cursor + 1


def test_daemon_event_journal_rejects_an_evicted_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(MODULE, "MAX_DEVELOPMENT_STATE_EVENTS", 3)
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    project_id = "development-project-event-bound"
    store.create_project(
        {
            "project_id": project_id,
            "display_name": "Bounded project",
            "config": {},
        }
    )
    for index in range(4):
        store.update_project(
            project_id,
            {"display_name": f"Bounded project {index}", "config": {}},
        )

    with pytest.raises(MODULE.EventCursorExpiredError, match="outside the replay window"):
        store.read_events(after_sequence=1, limit=100, wait_seconds=0)

    page = store.read_events(after_sequence=2, limit=100, wait_seconds=0)
    assert [event["sequence"] for event in page["events"]] == [3, 4, 5]


def test_standalone_evolution_candidate_is_not_injected_until_applied(tmp_path: Path) -> None:
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    project_id = "development-project-decoupled"
    store.create_project(
        {
            "project_id": project_id,
            "display_name": "Decoupled project",
            "config": {
                "evolution": {
                    "targets": {
                        "text_memory": {
                            "enabled": True,
                            "method": "text_memory_reflector",
                            "config": {},
                        }
                    }
                }
            },
        }
    )
    for index in (1, 2):
        session_id = f"dev-session-evidence-{index}"
        store.start_session(
            session_id,
            {
                "project_id": project_id,
                "project_name": "Decoupled project",
                "task_title": f"Evidence {index}",
                "instruction": f"Collect evidence {index}",
            },
        )
        store.complete_session(
            session_id,
            {
                "response": f"Observation {index}",
                "model": "test-model",
                "duration_ms": 1,
                "logs": [],
            },
        )
        dataset = tmp_path / f"dataset-{index}.json"
        dataset.write_text("{}", encoding="utf-8")
        store.record_dataset_artifact(
            artifact_id=f"dataset-{session_id}",
            project_id=project_id,
            session_id=session_id,
            uri=dataset.resolve().as_uri(),
            name=f"Evidence {index}",
        )

    assert all(session["selected_evolution"] == [] for session in store.snapshot()["sessions"])
    assert all(session["evolution_evidence_ready"] for session in store.snapshot()["sessions"])
    request = MODULE.validate_evolution_run_request(
        {
            "schema_version": "1",
            "project_id": project_id,
            "session_ids": ["dev-session-evidence-1", "dev-session-evidence-2"],
            "selections": [
                {
                    "target_id": "text_memory",
                    "method": "text_memory_reflector",
                    "config": {},
                }
            ],
        }
    )
    run = store.start_evolution_run("evolution-run-1", request)
    attempt = store.start_evolution_job(
        job_id="job-memory-evolution-run-1",
        session_id="dev-session-evidence-2",
        run_id=run["run_id"],
        target_id="text_memory",
        method_id="text_memory_reflector",
        requested_method_id="text_memory_reflector",
        resolver_input_artifact_ids=["dataset-dev-session-evidence-1"],
        previous_artifact_id=None,
        config={},
    )
    artifact = store.record_evolution_artifact(
        artifact_id="candidate-memory-1",
        project_id=project_id,
        session_id="dev-session-evidence-2",
        run_id=run["run_id"],
        target_id="text_memory",
        artifact_type="text_memory",
        method_id="text_memory_reflector",
        renderer_kind="markdown",
        documents=[
            {
                "path": "memory.md",
                "media_type": "text/markdown",
                "content": "# Candidate memory",
            }
        ],
        manifest={"content_path": "memory.md"},
        previous_artifact_id=None,
        promoted=False,
    )
    replacement_artifact = store.record_evolution_artifact(
        artifact_id="candidate-memory-2",
        project_id=project_id,
        session_id="dev-session-evidence-2",
        run_id=run["run_id"],
        target_id="text_memory",
        artifact_type="text_memory",
        method_id="text_memory_reflector",
        renderer_kind="markdown",
        documents=[
            {
                "path": "memory.md",
                "media_type": "text/markdown",
                "content": "# Final candidate memory",
            }
        ],
        manifest={"content_path": "memory.md"},
        previous_artifact_id=artifact["artifact_id"],
        promoted=False,
    )
    store.finish_evolution_job(
        "job-memory-evolution-run-1",
        attempt_id=attempt["attempt_id"],
        artifact_ids=[artifact["artifact_id"], replacement_artifact["artifact_id"]],
    )
    candidate = store.finish_evolution_run(
        run["run_id"],
        artifact_ids=[artifact["artifact_id"], replacement_artifact["artifact_id"]],
        error=None,
    )

    assert candidate["state"] == "candidate_ready"
    assert store.latest_context_artifacts(project_id) == []
    applied = store.apply_evolution_run(run["run_id"])
    assert applied["state"] == "applied"
    assert [item["artifact_id"] for item in store.latest_context_artifacts(project_id)] == [
        "candidate-memory-2"
    ]
    store.start_session(
        "dev-session-uses-applied-context",
        {
            "project_id": project_id,
            "project_name": "Decoupled project",
            "task_title": "Use applied context",
            "instruction": "Use the current memory",
        },
    )
    started_session = next(
        session
        for session in store.snapshot()["sessions"]
        if session["session_id"] == "dev-session-uses-applied-context"
    )
    assert started_session["context_artifact_ids"] == ["candidate-memory-2"]
    second_run = store.start_evolution_run("evolution-run-2", request)
    second_attempt = store.start_evolution_job(
        job_id="job-memory-evolution-run-2",
        session_id="dev-session-evidence-2",
        run_id=second_run["run_id"],
        target_id="text_memory",
        method_id="text_memory_reflector",
        requested_method_id="text_memory_reflector",
        resolver_input_artifact_ids=["dataset-dev-session-evidence-1"],
        previous_artifact_id="candidate-memory-2",
        config={},
    )
    assert second_attempt["ordinal"] == 1


def test_store_fails_an_interrupted_evolution_run_on_restart(tmp_path: Path) -> None:
    database = tmp_path / "interrupted-evolution.sqlite3"
    project_id = "development-project-interrupted-evolution"
    session_id = "dev-session-interrupted-evolution"
    run_id = "evolution-run-interrupted"
    job_id = "job-memory-evolution-run-interrupted"
    store = MODULE.DevelopmentStateStore(database)
    store.create_project(
        {
            "project_id": project_id,
            "display_name": "Interrupted Evolution project",
            "config": {},
        }
    )
    store.start_session(
        session_id,
        {
            "project_id": project_id,
            "project_name": "Interrupted Evolution project",
            "task_title": "Collect evidence",
            "instruction": "Return durable evidence.",
        },
    )
    store.complete_session(
        session_id,
        {
            "response": "Durable evidence.",
            "model": "test-model",
            "duration_ms": 1,
            "logs": [],
        },
    )
    dataset = tmp_path / "interrupted-evolution-dataset.json"
    dataset.write_text("{}", encoding="utf-8")
    store.record_dataset_artifact(
        artifact_id=f"dataset-{session_id}",
        project_id=project_id,
        session_id=session_id,
        uri=dataset.resolve().as_uri(),
        name="Interrupted Evolution evidence",
    )
    request = MODULE.validate_evolution_run_request(
        {
            "schema_version": "1",
            "project_id": project_id,
            "session_ids": [session_id],
            "selections": [
                {
                    "target_id": "text_memory",
                    "method": "text_memory_reflector",
                    "config": {},
                }
            ],
        }
    )
    store.start_evolution_run(run_id, request)
    attempt = store.start_evolution_job(
        job_id=job_id,
        session_id=session_id,
        run_id=run_id,
        target_id="text_memory",
        method_id="text_memory_reflector",
        requested_method_id="text_memory_reflector",
        resolver_input_artifact_ids=[],
        previous_artifact_id=None,
        config={},
    )

    restored = MODULE.DevelopmentStateStore(database).snapshot()
    restored_run = next(item for item in restored["evolution_runs"] if item["run_id"] == run_id)
    restored_job = next(item for item in restored["evolution_jobs"] if item["job_id"] == job_id)

    assert restored_run["state"] == "failed"
    assert restored_run["error"] == (
        "Development daemon restarted before this Evolution Run completed."
    )
    assert restored_job["state"] == "failed"
    assert restored_job["error"] == (
        "Development daemon restarted before this evolution job completed."
    )
    restored_attempt = next(
        item for item in restored_job["attempts"] if item["attempt_id"] == attempt["attempt_id"]
    )
    assert restored_attempt["state"] == "failed"
    assert restored_attempt["error_code"] == "daemon_restarted"


def test_store_repairs_duplicate_active_head_targets_with_immutable_successor(
    tmp_path: Path,
) -> None:
    database = tmp_path / "duplicate-head.sqlite3"
    project_id = "development-project-duplicate-head"
    store = MODULE.DevelopmentStateStore(database)
    store.create_project(
        {
            "project_id": project_id,
            "display_name": "Duplicate Head",
            "config": {"evolution": {"targets": {}}},
        }
    )
    store.start_session(
        "duplicate-head-session",
        {
            "project_id": project_id,
            "project_name": "Duplicate Head",
            "task_title": "Create context",
            "instruction": "Create context",
        },
    )
    store.complete_session(
        "duplicate-head-session",
        {
            "response": "done",
            "model": "test-model",
            "duration_ms": 1,
            "logs": [],
        },
    )
    for ordinal in (1, 2):
        store.record_evolution_artifact(
            artifact_id=f"duplicate-agent-system-{ordinal}",
            project_id=project_id,
            session_id="duplicate-head-session",
            target_id="agent_system",
            artifact_type="agent_system",
            method_id="agent_system_reflector",
            renderer_kind="markdown",
            documents=[{
                "path": "agent-system.md",
                "media_type": "text/markdown",
                "content": f"# Agent system {ordinal}",
            }],
            manifest={"content_path": "agent-system.md"},
            previous_artifact_id=(
                None if ordinal == 1 else "duplicate-agent-system-1"
            ),
            promoted=True,
        )

    invalid_head_id = store.active_project_head(project_id)["project_head_id"]
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE development_project_heads SET artifact_ids_json = ? "
            "WHERE project_head_id = ?",
            (
                json.dumps([
                    "duplicate-agent-system-1",
                    "duplicate-agent-system-2",
                ]),
                invalid_head_id,
            ),
        )

    restored = MODULE.DevelopmentStateStore(database)
    repaired = restored.active_project_head(project_id)

    assert repaired["project_head_id"] != invalid_head_id
    assert repaired["predecessor_project_head_id"] == invalid_head_id
    assert repaired["artifact_ids"] == ["duplicate-agent-system-2"]
    assert restored.project_head(invalid_head_id)["artifact_ids"] == [
        "duplicate-agent-system-1",
        "duplicate-agent-system-2",
    ]
    restored.start_session(
        "session-after-head-repair",
        {
            "project_id": project_id,
            "project_name": "Duplicate Head",
            "task_title": "Use repaired context",
            "instruction": "Use repaired context",
        },
    )
    assert restored.get_session("session-after-head-repair")["context_artifact_ids"] == [
        "duplicate-agent-system-2"
    ]


def test_completed_legacy_sessions_are_backfilled_as_evolution_evidence(tmp_path: Path) -> None:
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    project_id = "development-project-legacy-evidence"
    store.create_project(
        {
            "project_id": project_id,
            "display_name": "Legacy evidence",
            "config": {"evolution": {"targets": {}}},
        }
    )
    store.start_session(
        "dev-session-legacy-evidence",
        {
            "project_id": project_id,
            "project_name": "Legacy evidence",
            "task_title": "Old completed Session",
            "instruction": "Preserve this question",
        },
    )
    store.complete_session(
        "dev-session-legacy-evidence",
        {
            "response": "Preserve this answer",
            "model": "test-model",
            "duration_ms": 1,
            "logs": [],
        },
    )
    assert store.snapshot()["sessions"][0]["evolution_evidence_ready"] is False

    runner = MODULE.DocumentEvolutionRunner(
        state_root=tmp_path,
        codex_binary="codex",
        model="test-model",
        timeout_seconds=30,
    )
    assert runner.seal_completed_session_datasets(store) == []

    restored = store.snapshot()["sessions"][0]
    assert restored["evolution_evidence_ready"] is True
    dataset = store.dataset_artifacts(project_id)[0]
    manifest_path = Path(urlparse(dataset["uri"]).path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records_path = manifest_path.parent / manifest["records_path"]
    record = json.loads(records_path.read_text(encoding="utf-8"))
    assert record["traces"][0]["prompt_messages"][0]["content"] == "Preserve this question"
    assert record["traces"][0]["response_messages"][0]["content"] == "Preserve this answer"


def test_session_project_head_reuses_its_pinned_evolution_context(tmp_path: Path) -> None:
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    project_id = "development-project-head-context"
    store.create_project(
        {
            "project_id": project_id,
            "display_name": "Head context",
            "config": {"evolution": {"targets": {}}},
        }
    )

    def request(head: int, title: str) -> dict[str, str]:
        return {
            "project_id": project_id,
            "project_head_id": f"{project_id}-head-{head}",
            "project_name": "Head context",
            "task_title": title,
            "instruction": title,
        }

    def complete(session_id: str) -> None:
        store.complete_session(
            session_id,
            {
                "response": "done",
                "model": "test-model",
                "duration_ms": 1,
                "logs": [],
            },
        )

    store.start_session("head-session-0", request(0, "Genesis"))
    complete("head-session-0")
    store.record_evolution_artifact(
        artifact_id="memory-head-1",
        project_id=project_id,
        session_id="head-session-0",
        target_id="text_memory",
        artifact_type="text_memory",
        method_id="text_memory_reflector",
        renderer_kind="markdown",
        documents=[{"path": "memory.md", "media_type": "text/markdown", "content": "old"}],
        manifest={"content_path": "memory.md"},
        previous_artifact_id=None,
        promoted=True,
    )
    store.start_session("head-session-1", request(1, "Use old memory"))
    complete("head-session-1")
    store.record_evolution_artifact(
        artifact_id="memory-head-2",
        project_id=project_id,
        session_id="head-session-1",
        target_id="text_memory",
        artifact_type="text_memory",
        method_id="text_memory_reflector",
        renderer_kind="markdown",
        documents=[{"path": "memory.md", "media_type": "text/markdown", "content": "new"}],
        manifest={"content_path": "memory.md"},
        previous_artifact_id="memory-head-1",
        promoted=True,
    )

    store.start_session("head-session-2", request(2, "Use new memory"))
    store.start_session("head-session-historical", request(1, "Reuse old memory"))
    sessions = {item["session_id"]: item for item in store.snapshot()["sessions"]}

    assert sessions["head-session-2"]["context_artifact_ids"] == ["memory-head-2"]
    assert sessions["head-session-historical"]["project_head_id"] == (
        f"{project_id}-head-1"
    )
    assert sessions["head-session-historical"]["context_artifact_ids"] == [
        "memory-head-1"
    ]


def test_store_migrates_legacy_per_session_job_uniqueness(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE development_evolution_jobs (
                job_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                method_id TEXT NOT NULL,
                requested_method_id TEXT NOT NULL,
                resolver_input_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                previous_artifact_id TEXT,
                config_json TEXT NOT NULL,
                state TEXT NOT NULL,
                artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(session_id, target_id)
            );
            CREATE TABLE development_evolution_job_attempts (
                attempt_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                state TEXT NOT NULL,
                stage TEXT NOT NULL,
                artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                error_code TEXT,
                error_message TEXT,
                logs_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(job_id, ordinal)
            );
            """
        )

    MODULE.DevelopmentStateStore(database)

    with sqlite3.connect(database) as connection:
        job_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'development_evolution_jobs'"
        ).fetchone()[0]
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(development_evolution_jobs)")
        }
    assert "run_id" in columns
    assert "UNIQUE(run_id, target_id)" in job_sql
    assert "UNIQUE(session_id, target_id)" not in job_sql


def test_project_workspace_files_persist_on_the_server_and_are_bounded(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = MODULE.DevelopmentStateStore(database)
    store.create_project(
        {
            "project_id": "development-project-files",
            "display_name": "Coding project",
            "config": {},
        }
    )
    store.apply_workspace_mutations(
        "development-project-files",
        {
            "file_writes": [{"path": "src/main.py", "content": "print('hello')\n"}],
            "delete_paths": [],
        },
    )
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
    store.create_project(
        {
            "project_id": "development-project-safe",
            "display_name": "Safe project",
            "config": {},
        }
    )

    with pytest.raises(MODULE.AgentRunError, match="unsafe workspace path"):
        store.apply_workspace_mutations(
            "development-project-safe",
            {
                "file_writes": [{"path": "../escaped.txt", "content": "no"}],
                "delete_paths": [],
            },
        )

    assert not (tmp_path / "escaped.txt").exists()


def test_project_workspace_upload_and_download_preserve_binary_bytes(tmp_path: Path) -> None:
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    store.create_project(
        {
            "project_id": "development-project-transfer",
            "display_name": "Transfer project",
            "config": {},
        }
    )
    payload = b"\x00\x01OpenEvo\xff"

    entry = store.upload_workspace_file(
        "development-project-transfer",
        "inputs/sample.bin",
        payload,
        overwrite=False,
    )

    assert entry["path"] == "inputs/sample.bin"
    assert entry["byte_size"] == len(payload)
    assert entry["content"] is None
    downloaded, media_type, file_name = store.download_workspace_file(
        "development-project-transfer",
        "inputs/sample.bin",
    )
    assert downloaded == payload
    assert media_type == "application/octet-stream"
    assert file_name == "sample.bin"
    with pytest.raises(MODULE.StateConflictError, match="already exists"):
        store.upload_workspace_file(
            "development-project-transfer",
            "inputs/sample.bin",
            b"replacement",
            overwrite=False,
        )
    with pytest.raises(MODULE.RequestError, match="unsafe"):
        store.upload_workspace_file(
            "development-project-transfer",
            "../escaped.bin",
            b"no",
            overwrite=False,
        )


def test_http_workspace_file_upload_and_download_round_trip(tmp_path: Path) -> None:
    token = "t" * 32
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    store.create_project(
        {
            "project_id": "development-project-transfer",
            "display_name": "Transfer project",
            "config": {},
        }
    )
    server = MODULE.DevelopmentAgentServer(
        ("127.0.0.1", 0),
        token,
        object(),
        store,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    query = urlencode({"path": "data/实验.csv", "overwrite": "false"})
    payload = "name,value\n样本,42\n".encode()
    try:
        upload = urllib.request.Request(
            f"{base_url}/openevo-dev-agent/v1/projects/development-project-transfer/workspace/files?{query}",
            data=payload,
            method="PUT",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "text/csv",
            },
        )
        with urllib.request.urlopen(upload, timeout=5) as response:
            created = json.loads(response.read())
        download_query = urlencode({"path": "data/实验.csv"})
        download = urllib.request.Request(
            f"{base_url}/openevo-dev-agent/v1/projects/development-project-transfer/workspace/files?{download_query}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(download, timeout=5) as response:
            downloaded = response.read()
            content_type = response.headers["Content-Type"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert created["entry"]["path"] == "data/实验.csv"
    assert downloaded == payload
    assert content_type == "text/csv"


def test_daemon_v2_model_routes_keep_download_authority_server_side(
    tmp_path: Path,
) -> None:
    token = "m" * 32
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    now = "2026-09-02T00:00:00Z"

    class Models:
        def __init__(self) -> None:
            self.record = {
                "model_resource_id": "model-fixture",
                "repository_id": "OpenEvo/Fixture-0.1B",
                "requested_revision": "main",
                "resolved_revision": None,
                "manifest_sha256": None,
                "state": "downloading",
                "downloaded_bytes": 32,
                "total_bytes": 128,
                "error": None,
                "created_at": now,
                "updated_at": now,
            }
            self.calls: list[tuple[object, ...]] = []

        def list(self) -> list[dict[str, object]]:
            return [self.record]

        def get(self, model_resource_id: str) -> dict[str, object]:
            if model_resource_id != "model-fixture":
                raise KeyError(model_resource_id)
            return self.record

        def register(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(("register", kwargs))
            return self.record

        def retry(self, model_resource_id: str, *, action_id: str) -> dict[str, object]:
            self.calls.append(("retry", model_resource_id, action_id))
            return self.record

    models = Models()
    server = MODULE.DevelopmentAgentServer(
        ("127.0.0.1", 0), token, object(), store, None, models
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/v2/development/models"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        register = urllib.request.Request(
            base,
            data=json.dumps(
                {
                    "schema_version": "2",
                    "action_id": "register-model",
                    "repository_id": "OpenEvo/Fixture-0.1B",
                    "revision": "main",
                }
            ).encode(),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(register, timeout=5) as response:
            assert response.status == 202
            registered = json.loads(response.read())
        with urllib.request.urlopen(
            urllib.request.Request(base, headers=headers), timeout=5
        ) as response:
            inventory = json.loads(response.read())
        with urllib.request.urlopen(
            urllib.request.Request(f"{base}/model-fixture", headers=headers), timeout=5
        ) as response:
            detail = json.loads(response.read())
        retry = urllib.request.Request(
            f"{base}/model-fixture/retry",
            data=json.dumps(
                {"schema_version": "2", "action_id": "retry-model"}
            ).encode(),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(retry, timeout=5) as response:
            assert response.status == 202
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert registered["model_resource_id"] == "model-fixture"
    assert registered["downloaded_bytes"] == 32
    assert inventory["items"] == [detail]
    assert models.calls == [
        (
            "register",
            {
                "action_id": "register-model",
                "repository_id": "OpenEvo/Fixture-0.1B",
                "revision": "main",
            },
        ),
        ("retry", "model-fixture", "retry-model"),
    ]


def test_daemon_v2_capabilities_follow_the_persisted_project_execution_mode(
    tmp_path: Path,
) -> None:
    token = "c" * 32
    project_id = "development-project-self-deployed"
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    store.create_project(
        {
            "project_id": project_id,
            "display_name": "Self-deployed project",
            "config": {"execution": {"mode": "self-deployed"}},
        }
    )

    class Evolution:
        def __init__(self) -> None:
            self.execution_modes: list[str] = []

        def capabilities(
            self, execution_mode: str = "codex_subscription_transcript"
        ) -> dict[str, object]:
            self.execution_modes.append(execution_mode)
            return {
                "schema_version": "1",
                "authority": "development_catalog_unverified",
                "capabilities": {
                    "evaluated_profile": {
                        "execution_mode": (
                            "subscription"
                            if execution_mode == "codex_subscription_transcript"
                            else "self_deployed"
                        )
                    }
                },
            }

    evolution = Evolution()
    server = MODULE.DevelopmentAgentServer(
        ("127.0.0.1", 0), token, object(), store, evolution
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/v2/development/capabilities"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        request = urllib.request.Request(
            f"{base}?{urlencode({'project_id': project_id})}", headers=headers
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert evolution.execution_modes == ["self-deployed"]
    assert payload["capabilities"]["evaluated_profile"]["execution_mode"] == "self_deployed"


def test_daemon_v2_project_and_task_delete_routes_return_idempotent_receipts(
    tmp_path: Path,
) -> None:
    token = "d" * 32
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    for project_id in ("project-keep", "project-delete"):
        store.create_project(
            {"project_id": project_id, "display_name": project_id, "config": {}}
        )
    store.start_session(
        "task-delete",
        {
            "project_id": "project-keep",
            "project_name": "project-keep",
            "task_title": "Delete me",
            "instruction": "Finish first.",
        },
    )
    store.complete_session(
        "task-delete",
        {
            "response": "Done.",
            "model": "codex-test",
            "duration_ms": 1,
            "logs": [],
            "workspace_changes": [],
            "runtime_activation": None,
        },
    )
    server = MODULE.DevelopmentAgentServer(("127.0.0.1", 0), token, object(), store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    def delete(path: str, action_id: str) -> dict[str, object]:
        request = urllib.request.Request(
            f"{base_url}{path}",
            data=json.dumps({"schema_version": "2", "action_id": action_id}).encode(),
            method="DELETE",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    try:
        task_receipt = delete("/v2/development/tasks/task-delete", "delete-task")
        assert task_receipt == {
            "schema_version": "2",
            "action_id": "delete-task",
            "resource_kind": "task",
            "resource_id": "task-delete",
            "active_project_id": "project-delete",
        }
        assert delete("/v2/development/tasks/task-delete", "delete-task") == task_receipt
        project_receipt = delete(
            "/v2/development/projects/project-delete", "delete-project"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert project_receipt["resource_kind"] == "project"
    assert project_receipt["active_project_id"] == "project-keep"
    assert [project["project_id"] for project in store.snapshot()["projects"]] == [
        "project-keep"
    ]


def test_daemon_v2_workspace_is_digest_verified_paginated_and_restart_safe(
    tmp_path: Path,
) -> None:
    token = "v" * 32
    database = tmp_path / "state.sqlite3"
    store = MODULE.DevelopmentStateStore(database)
    store.create_project(
        {
            "project_id": "development-project-v2-files",
            "display_name": "V2 files",
            "config": {},
        }
    )
    server = MODULE.DevelopmentAgentServer(("127.0.0.1", 0), token, object(), store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/v2/projects/development-project-v2-files/workspace"
    payloads = {
        "notes/a.txt": b"alpha\n",
        "notes/b.txt": b"beta\n",
    }
    try:
        for path, payload in payloads.items():
            request = urllib.request.Request(
                f"{base}/files?{urlencode({'path': path, 'overwrite': 'false'})}",
                data=payload,
                method="PUT",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "text/plain",
                    "X-OpenEvo-Content-SHA256": hashlib.sha256(payload).hexdigest(),
                },
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                receipt = json.loads(response.read())
            assert receipt["schema_version"] == "2"
            assert receipt["entry"]["path"] == path
            assert receipt["entry"]["content_sha256"] == hashlib.sha256(payload).hexdigest()

        first_request = urllib.request.Request(
            f"{base}?limit=1", headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(first_request, timeout=5) as response:
            first = json.loads(response.read())
        assert first["has_more"] is True
        assert first["next_cursor"] == first["items"][0]["path"]
        second_query = urlencode(
            {
                "limit": "100",
                "after": first["next_cursor"],
                "manifest_sha256": first["manifest_sha256"],
            }
        )
        second_request = urllib.request.Request(
            f"{base}?{second_query}", headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(second_request, timeout=5) as response:
            second = json.loads(response.read())
        assert second["manifest_sha256"] == first["manifest_sha256"]
        assert {entry["path"] for entry in first["items"] + second["items"]} == {
            "notes",
            "notes/a.txt",
            "notes/b.txt",
        }

        download = urllib.request.Request(
            f"{base}/files?{urlencode({'path': 'notes/a.txt'})}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(download, timeout=5) as response:
            assert response.read() == payloads["notes/a.txt"]
            assert (
                response.headers["X-OpenEvo-Content-SHA256"]
                == hashlib.sha256(payloads["notes/a.txt"]).hexdigest()
            )

        bad_upload = urllib.request.Request(
            f"{base}/files?{urlencode({'path': 'bad.txt', 'overwrite': 'false'})}",
            data=b"bad",
            method="PUT",
            headers={
                "Authorization": f"Bearer {token}",
                "X-OpenEvo-Content-SHA256": "0" * 64,
            },
        )
        with pytest.raises(urllib.error.HTTPError) as bad_error:
            urllib.request.urlopen(bad_upload, timeout=5)
        assert bad_error.value.code == 400
        assert not (store.workspace_path("development-project-v2-files") / "bad.txt").exists()

        unsafe = urllib.request.Request(
            f"{base}/files?{urlencode({'path': '../escape.txt'})}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with pytest.raises(urllib.error.HTTPError) as unsafe_error:
            urllib.request.urlopen(unsafe, timeout=5)
        assert unsafe_error.value.code == 400

        delete = urllib.request.Request(
            f"{base}/files?{urlencode({'path': 'notes/a.txt'})}",
            method="DELETE",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(delete, timeout=5) as response:
            deleted = json.loads(response.read())
        assert deleted["deleted_path"] == "notes/a.txt"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    restored = MODULE.DevelopmentStateStore(database)
    page = restored.workspace_page_v2(
        "development-project-v2-files",
        after_path=None,
        expected_manifest_sha256=None,
        limit=100,
    )
    assert [entry.path for entry in page.items] == ["notes", "notes/b.txt"]
    assert page.items[1].content_sha256 == hashlib.sha256(payloads["notes/b.txt"]).hexdigest()


def test_daemon_v2_artifacts_are_paginated_bounded_and_restart_safe(tmp_path: Path) -> None:
    token = "a" * 32
    database = tmp_path / "state.sqlite3"
    project_id = "development-project-v2-artifacts"
    task_id = "development-task-v2-artifacts"
    store = MODULE.DevelopmentStateStore(database)
    store.create_project(
        {
            "project_id": project_id,
            "display_name": "V2 artifacts",
            "config": {},
        }
    )
    store.start_session(
        task_id,
        {
            "project_id": project_id,
            "project_name": "V2 artifacts",
            "task_title": "Produce artifacts",
            "instruction": "Produce two bounded artifacts",
        },
    )
    for index, artifact_type in enumerate(("skill_bundle", "report"), start=1):
        content = f"# Artifact {index}\n"
        store.record_evolution_artifact(
            artifact_id=f"artifact-v2-{index}",
            project_id=project_id,
            session_id=task_id,
            target_id="skill_bundle" if artifact_type == "skill_bundle" else "agent_system",
            artifact_type=artifact_type,
            method_id="artifact_reflector",
            renderer_kind="file_bundle"
            if artifact_type == "skill_bundle"
            else "structured_summary",
            documents=[
                {
                    "path": "SKILL.md" if artifact_type == "skill_bundle" else "report.md",
                    "media_type": "text/markdown",
                    "content": content,
                }
            ],
            manifest={"ordinal": index},
            previous_artifact_id=None,
            promoted=False,
        )

    server = MODULE.DevelopmentAgentServer(("127.0.0.1", 0), token, object(), store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_address[1]}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        first_request = urllib.request.Request(
            f"{root}/v2/tasks/{task_id}/artifacts?limit=1", headers=headers
        )
        with urllib.request.urlopen(first_request, timeout=5) as response:
            first = json.loads(response.read())
        assert first["schema_version"] == "2"
        assert first["has_more"] is True
        assert first["next_cursor"] == first["items"][0]["artifact_id"]

        second_request = urllib.request.Request(
            f"{root}/v2/tasks/{task_id}/artifacts?{urlencode({'limit': 100, 'after': first['next_cursor']})}",
            headers=headers,
        )
        with urllib.request.urlopen(second_request, timeout=5) as response:
            second = json.loads(response.read())
        assert second["has_more"] is False
        assert {item["artifact_type"] for item in first["items"] + second["items"]} == {
            "skill_bundle",
            "diagnostic",
        }

        inventory_request = urllib.request.Request(
            f"{root}/v2/development/artifacts?{urlencode({'project_id': project_id, 'limit': 5})}",
            headers=headers,
        )
        with urllib.request.urlopen(inventory_request, timeout=5) as response:
            inventory = json.loads(response.read())
        assert len(inventory["items"]) == 2
        assert inventory["items"][0]["documents"][0]["content"].startswith("# Artifact")

        artifact_id = inventory["items"][0]["artifact_id"]
        detail_request = urllib.request.Request(
            f"{root}/v2/development/artifacts/{artifact_id}", headers=headers
        )
        with urllib.request.urlopen(detail_request, timeout=5) as response:
            detail = json.loads(response.read())
        assert detail["content"] == detail["documents"][0]["content"]

        metadata_request = urllib.request.Request(
            f"{root}/v2/artifacts/{artifact_id}", headers=headers
        )
        with urllib.request.urlopen(metadata_request, timeout=5) as response:
            metadata = json.loads(response.read())
        content_request = urllib.request.Request(
            f"{root}/v2/artifacts/{artifact_id}/content", headers=headers
        )
        with urllib.request.urlopen(content_request, timeout=5) as response:
            content_metadata = json.loads(response.read())
        assert content_metadata["artifact"] == metadata
        assert content_metadata["media_type"] == "text/markdown"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    restored = MODULE.DevelopmentStateStore(database)
    restored_page = restored.artifact_page_v2(
        project_id=project_id,
        task_id=None,
        after_artifact_id=None,
        limit=5,
        development_detail=True,
    )
    assert [item.artifact_id for item in restored_page.items] == [
        "artifact-v2-1",
        "artifact-v2-2",
    ]
    assert restored_page.items[0].documents[0].path == "SKILL.md"


def test_daemon_v2_evolution_run_is_idempotent_applied_and_used_by_next_session(
    tmp_path: Path,
) -> None:
    token = "e" * 32
    database = tmp_path / "state.sqlite3"
    project_id = "development-project-v2-evolution"
    task_id = "development-task-v2-evolution"
    action_id = "development-action-v2-evolution"
    store = MODULE.DevelopmentStateStore(database)
    store.create_project(
        {
            "project_id": project_id,
            "display_name": "V2 Evolution",
            "config": {},
        }
    )
    store.start_session(
        task_id,
        {
            "project_id": project_id,
            "project_name": "V2 Evolution",
            "task_title": "Evolution evidence",
            "instruction": "Create reusable evidence",
        },
    )
    store.complete_session(
        task_id,
        {
            "response": "Reusable observation",
            "model": "test-model",
            "duration_ms": 1,
            "logs": [],
        },
    )
    dataset = tmp_path / "dataset.json"
    dataset.write_text("{}", encoding="utf-8")
    store.record_dataset_artifact(
        artifact_id="dataset-v2-evolution",
        project_id=project_id,
        session_id=task_id,
        uri=dataset.resolve().as_uri(),
        name="Evolution evidence",
    )
    evolution_started = threading.Event()
    release_evolution = threading.Event()

    class FakeEvolutionRunner:
        def evolve_run(self, *, run: dict[str, object], store: object) -> dict[str, object]:
            evolution_started.set()
            if not release_evolution.wait(5):
                raise RuntimeError("test did not release the Evolution runner")
            content = "# Applied memory\n"
            artifact = store.record_evolution_artifact(
                artifact_id=f"artifact-{run['run_id']}",
                project_id=project_id,
                session_id=task_id,
                run_id=run["run_id"],
                target_id="text_memory",
                artifact_type="text_memory",
                method_id="text_memory_reflector",
                renderer_kind="markdown",
                documents=[
                    {
                        "path": "memory.md",
                        "media_type": "text/markdown",
                        "content": content,
                    }
                ],
                manifest={"content_path": "memory.md"},
                previous_artifact_id=None,
                promoted=False,
            )
            return {"artifacts": [artifact], "errors": []}

    server = MODULE.DevelopmentAgentServer(
        ("127.0.0.1", 0),
        token,
        object(),
        store,
        evolution_runner=FakeEvolutionRunner(),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_address[1]}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    base_head = store.active_project_head(project_id)
    request_body = {
        "schema_version": "2",
        "action_id": action_id,
        "project_id": project_id,
        "base_project_head_id": base_head["project_head_id"],
        "base_project_head_manifest_sha256": base_head["manifest_sha256"],
        "source_task_ids": [task_id],
        "selections": [
            {
                "schema_version": "2",
                "target_id": "text_memory",
                "method": "text_memory_reflector",
                "config": {},
            }
        ],
    }
    try:
        create = urllib.request.Request(
            f"{root}/v2/development/evolution-runs",
            data=json.dumps(request_body).encode(),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(create, timeout=5) as response:
            created = json.loads(response.read())
        assert created["action_id"] == action_id
        assert created["base_project_head_id"] == base_head["project_head_id"]
        run_id = created["run_id"]
        assert evolution_started.wait(5)

        duplicate = urllib.request.Request(
            f"{root}/v2/development/evolution-runs",
            data=json.dumps(request_body).encode(),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(duplicate, timeout=5) as response:
            duplicate_running = json.loads(response.read())
        assert duplicate_running["run_id"] == run_id
        assert duplicate_running["state"] == "running"
        release_evolution.set()

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            detail_request = urllib.request.Request(
                f"{root}/v2/development/evolution-runs/{run_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(detail_request, timeout=5) as response:
                detail = json.loads(response.read())
            if detail["state"] == "candidate_ready":
                break
            time.sleep(0.01)
        assert detail["state"] == "candidate_ready"
        assert len(detail["artifact_ids"]) == 1

        duplicate = urllib.request.Request(
            f"{root}/v2/development/evolution-runs",
            data=json.dumps(request_body).encode(),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(duplicate, timeout=5) as response:
            duplicate_result = json.loads(response.read())
        assert duplicate_result["run_id"] == run_id
        assert duplicate_result["state"] == "candidate_ready"

        inventory = urllib.request.Request(
            f"{root}/v2/development/evolution-runs?{urlencode({'project_id': project_id, 'limit': 25})}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(inventory, timeout=5) as response:
            page = json.loads(response.read())
        assert [item["run_id"] for item in page["items"]] == [run_id]

        apply = urllib.request.Request(
            f"{root}/v2/development/evolution-runs/{run_id}/apply",
            data=b'{"schema_version":"2"}',
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(apply, timeout=5) as response:
            applied = json.loads(response.read())
        assert applied["state"] == "applied"
        assert applied["applied_project_head_id"] == f"{project_id}-head-1"
    finally:
        release_evolution.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    restored = MODULE.DevelopmentStateStore(database)
    restored_run = restored.evolution_run_v2(run_id)
    assert restored_run.state == "applied"
    assert restored_run.base_project_head_id == base_head["project_head_id"]
    assert restored_run.applied_project_head_id == f"{project_id}-head-1"
    active_head = restored.active_project_head(project_id)
    assert active_head["project_head_id"] == restored_run.applied_project_head_id
    assert active_head["predecessor_project_head_id"] == base_head["project_head_id"]
    assert active_head["artifact_ids"] == list(restored_run.artifact_ids)
    restored.start_session(
        "development-task-after-v2-evolution",
        {
            "project_id": project_id,
            "project_name": "V2 Evolution",
            "task_title": "Use applied context",
            "instruction": "Use the newly applied memory",
        },
    )
    next_session = next(
        session
        for session in restored.snapshot()["sessions"]
        if session["session_id"] == "development-task-after-v2-evolution"
    )
    assert next_session["context_artifact_ids"] == list(restored_run.artifact_ids)
    assert next_session["project_head_id"] == restored_run.applied_project_head_id


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
                json.dumps([{"target_id": "text_memory", "method": "text_memory_reflector"}]),
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

    persistent_workspace = tmp_path / "persistent-workspace"
    persistent_workspace.mkdir()
    runtime_workspace = tmp_path / "runtime-workspace"
    runtime = MODULE.DevelopmentRuntimeContextMaterializer().materialize(
        persistent_workspace=persistent_workspace,
        runtime_workspace=runtime_workspace,
        contexts=store.latest_context_artifacts(project["project_id"]),
    )
    assert (runtime_workspace / "AGENTS.md").is_file()
    skill_paths = list((runtime_workspace / ".agents" / "skills").glob("*/SKILL.md"))
    assert skill_paths
    assert skill_paths[0].read_text(encoding="utf-8").startswith("---\nname: ")
    assert (runtime_workspace / ".openevo" / "evolution" / "memory.md").is_file()
    assert runtime["instruction_sections"]
    assert runtime["environment"]["OPENEVO_SKILLS_DIR"].endswith("/.agents/skills")
    assert runtime["runtime_controls"] == []


def test_explicit_skill_evolution_combines_every_selected_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openevo.evolution import methods

    prompts: list[str] = []

    def reflect(prompt: str, *_args: object, **_kwargs: object) -> str:
        prompts.append(prompt)
        return "# Combined skill\n\n- Reuse lessons from every selected Session.\n"

    monkeypatch.setattr(methods, "_generate_reflector_markdown", reflect)
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    project = {
        "project_id": "development-project-selected-sessions",
        "display_name": "Selected Session evidence",
        "config": {},
    }
    store.create_project(project)
    runner = MODULE.DocumentEvolutionRunner(
        state_root=tmp_path,
        codex_binary="codex",
        model="test-model",
        timeout_seconds=30,
    )
    session_ids = ["dev-session-selected-1", "dev-session-selected-2"]
    for ordinal, session_id in enumerate(session_ids, start=1):
        request = {
            "project_id": project["project_id"],
            "project_name": project["display_name"],
            "task_title": f"Selected question {ordinal}",
            "instruction": f"Remember selected instruction {ordinal}.",
        }
        result = {
            "response": f"Selected answer {ordinal}.",
            "model": "test-model",
            "duration_ms": 1,
            "logs": [],
        }
        store.start_session(session_id, request)
        store.complete_session(session_id, result)
        runner.capture_session_dataset(
            session_id=session_id,
            request=request,
            result=result,
            store=store,
        )

    run = store.start_evolution_run(
        "evolution-run-selected-sessions",
        {
            "project_id": project["project_id"],
            "session_ids": session_ids,
            "selections": [
                {
                    "target_id": "skill_bundle",
                    "method": "skill_bundle_reflector",
                    "config": {},
                }
            ],
        },
    )
    batch = runner.evolve_run(run=run, store=store)

    assert batch["errors"] == []
    assert len(prompts) == 1
    assert "record_count: 2" in prompts[0]
    assert "Remember selected instruction 1." in prompts[0]
    assert "Remember selected instruction 2." in prompts[0]
    assert "Selected answer 1." in prompts[0]
    assert "Selected answer 2." in prompts[0]
    manifest = batch["artifacts"][0]["manifest"]
    assert manifest["record_count"] == 2
    assert manifest["reflected_record_count"] == 2
    assert manifest["source_dataset_artifact_ids"] == [
        "dataset-dev-session-selected-1",
        "dataset-dev-session-selected-2",
    ]
    assert manifest["source_dataset_count"] == 2
    assert manifest["aggregate_dataset_artifact_id"].startswith("dataset-selection-")


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
    self_deployed = runner.capabilities("self-deployed")

    assert payload["authority"] == "development_catalog_unverified"
    assert payload["capabilities"]["evaluated_profile"]["execution_mode"] == "subscription"
    assert (
        self_deployed["capabilities"]["evaluated_profile"]["execution_mode"]
        == "self_deployed"
    )
    assert (
        self_deployed["capabilities"]["registry_digest"]
        == payload["capabilities"]["registry_digest"]
    )
    targets = payload["capabilities"]["targets"]
    assert {target["target_id"] for target in targets} >= {
        "text_memory",
        "skill_bundle",
        "agent_system",
    }
    assert all(
        target["renderer_kind"] in {"markdown", "file_bundle", "structured_summary", "adapter"}
        for target in targets
    )
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
    assert [item["artifact_id"] for item in store.dataset_artifacts(project["project_id"])] == [
        "dataset-dev-session-none"
    ]


def test_failed_evolution_method_can_retry_with_fixed_inputs_without_rerunning_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openevo.evolution import methods

    invocations = 0

    def generate(*_args: object, **_kwargs: object) -> str:
        nonlocal invocations
        invocations += 1
        if invocations == 1:
            raise RuntimeError("temporary reflector failure")
        return "# Memory\n\n- Reuse the recovered Evolution result.\n"

    monkeypatch.setattr(methods, "_generate_reflector_markdown", generate)
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    project = {
        "project_id": "development-project-retry",
        "display_name": "Retry evolution",
        "config": {
            "schema_version": "2",
            "evolution": {
                "targets": {
                    "text_memory": {
                        "enabled": True,
                        "method": "text_memory_reflector",
                        "config": {},
                    },
                },
            },
        },
    }
    store.create_project(project)
    request = {
        "project_id": project["project_id"],
        "project_name": project["display_name"],
        "task_title": "Retain the transcript",
        "instruction": "Learn this reusable lesson.",
    }
    result = {
        "response": "Agent ran exactly once.",
        "model": "test",
        "duration_ms": 1,
        "logs": [],
    }
    session_id = "dev-session-retry"
    store.start_session(session_id, request)
    store.complete_session(session_id, result)
    runner = MODULE.DocumentEvolutionRunner(
        state_root=tmp_path,
        codex_binary="codex",
        model="test-model",
        timeout_seconds=30,
    )

    first = runner.evolve(
        session_id=session_id,
        request=request,
        result=result,
        store=store,
    )
    assert first["artifacts"] == []
    assert first["errors"][0]["message"] == "temporary reflector failure"
    failed_job = store.snapshot()["evolution_jobs"][0]
    assert failed_job["state"] == "failed"
    assert failed_job["attempts"][0]["stage"] == "method_execution"
    assert failed_job["attempts"][0]["error_code"] == "method_execution_failed"

    retry_action_id = "retry-text-memory-after-temporary-failure"
    token = "evolution-job-v2-token"
    server = MODULE.DevelopmentAgentServer(
        ("127.0.0.1", 0),
        token,
        object(),
        store,
        evolution_runner=runner,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_address[1]}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    retry_body = json.dumps(
        {
            "schema_version": "2",
            "action_id": retry_action_id,
        }
    ).encode()
    try:
        retry_request = urllib.request.Request(
            f"{root}/v2/development/evolution-jobs/{failed_job['job_id']}/retry",
            data=retry_body,
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(retry_request, timeout=5) as response:
            retried = json.loads(response.read())
        assert retried["job_id"] == failed_job["job_id"]
        assert retried["attempts"][-1]["action_id"] == retry_action_id

        duplicate_request = urllib.request.Request(
            f"{root}/v2/development/evolution-jobs/{failed_job['job_id']}/retry",
            data=retry_body,
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(duplicate_request, timeout=5) as response:
            duplicate = json.loads(response.read())
        assert duplicate["attempts"][-1]["attempt_id"] == retried["attempts"][-1]["attempt_id"]

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            detail_request = urllib.request.Request(
                f"{root}/v2/development/evolution-jobs/{failed_job['job_id']}",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(detail_request, timeout=5) as response:
                completed_v2_payload = json.loads(response.read())
            if completed_v2_payload["state"] == "completed":
                break
            time.sleep(0.01)
        assert completed_v2_payload["state"] == "completed"

        inventory_request = urllib.request.Request(
            f"{root}/v2/development/evolution-jobs?{urlencode({'project_id': project['project_id'], 'limit': 25})}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(inventory_request, timeout=5) as response:
            page_v2_payload = json.loads(response.read())
        assert [item["job_id"] for item in page_v2_payload["items"]] == [failed_job["job_id"]]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert invocations == 2
    completed_job = store.get_evolution_job(failed_job["job_id"])
    assert completed_job["state"] == "completed"
    assert [attempt["state"] for attempt in completed_job["attempts"]] == [
        "failed",
        "completed",
    ]
    assert completed_job["attempts"][1]["ordinal"] == 2
    assert completed_job["resolver_input_artifact_ids"] == []
    assert len(store.dataset_artifacts(project["project_id"])) == 1
    completed_v2 = store.evolution_job_v2(failed_job["job_id"])
    assert completed_v2.state == "completed"
    assert [attempt.state for attempt in completed_v2.attempts] == [
        "failed",
        "completed",
    ]


def test_development_runner_resolves_auto_and_supplies_ordered_project_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openevo.evolution import methods

    monkeypatch.setattr(
        methods,
        "_generate_audited_agent_system_reflection",
        lambda *_args, **_kwargs: ("# Agent system\n\n- Preserve useful history.\n", {}),
    )
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    project = {
        "project_id": "development-project-auto-history",
        "display_name": "Automatic history",
        "config": {
            "schema_version": "2",
            "evolution": {
                "targets": {
                    "agent_system": {
                        "enabled": True,
                        "method": "auto",
                        "config": {},
                    },
                },
            },
        },
    }
    store.create_project(project)
    runner = MODULE.DocumentEvolutionRunner(
        state_root=tmp_path,
        codex_binary="codex",
        model="test-model",
        timeout_seconds=30,
    )

    for ordinal in (1, 2):
        session_id = f"dev-session-auto-{ordinal}"
        request = {
            "project_id": project["project_id"],
            "project_name": project["display_name"],
            "task_title": f"Round {ordinal}",
            "instruction": f"Learn from round {ordinal}.",
        }
        result = {
            "response": f"Completed round {ordinal}.",
            "model": "test",
            "duration_ms": 1,
            "logs": [],
        }
        store.start_session(session_id, request)
        store.complete_session(session_id, result)
        batch = runner.evolve(
            session_id=session_id,
            request=request,
            result=result,
            store=store,
        )
        assert batch["errors"] == []

    jobs = store.snapshot()["evolution_jobs"]
    assert [job["method_id"] for job in jobs] == [
        "agent_system_reflector",
        "agent_system_history_reflector",
    ]
    with sqlite3.connect(store.path) as connection:
        resolution_audit = connection.execute(
            "SELECT requested_method_id, resolver_input_artifact_ids_json "
            "FROM development_evolution_jobs ORDER BY created_at, job_id"
        ).fetchall()
    assert resolution_audit == [
        ("auto", "[]"),
        ("auto", '["dataset-dev-session-auto-1"]'),
    ]
    artifacts = store.snapshot()["artifacts"]
    assert artifacts[-1]["manifest"]["source_dataset_artifact_ids"] == [
        "dataset-dev-session-auto-1",
        "dataset-dev-session-auto-2",
    ]


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
    runner_started = threading.Event()
    release_runner = threading.Event()

    class FakeRunner:
        def run(self, request: dict[str, object], **_: object) -> dict[str, object]:
            runner_started.set()
            assert release_runner.wait(timeout=5)
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
                    "file_writes": [
                        {
                            "path": "hello.py",
                            "content": "print('hello')\n",
                        }
                    ],
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
        event_cursor = _request_json(
            base_url,
            "/openevo-dev-agent/v1/events?limit=100",
            token,
        )["latest_sequence"]
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
        assert runner_started.wait(timeout=1)
        event_page = _request_json(
            base_url,
            f"/openevo-dev-agent/v1/events?after={event_cursor}&limit=100&wait_ms=1000",
            token,
        )
        assert event_page["events"]
        assert event_page["events"][0]["sequence"] == event_cursor + 1
        assert event_page["events"][0]["project_id"] == "development-project-1"
        running = _request_json(base_url, "/openevo-dev-agent/v1/state", token)
        assert running["sessions"][0]["state"] == "running"
        release_runner.set()
        deadline = time.monotonic() + 5
        while True:
            state = _request_json(base_url, "/openevo-dev-agent/v1/state", token)
            if state["sessions"][0]["state"] != "running":
                break
            if time.monotonic() >= deadline:
                raise AssertionError("asynchronous session did not finish")
            time.sleep(0.01)
        task_page_v2 = _request_json(base_url, "/v2/tasks", token)
        task_v2 = _request_json(base_url, f"/v2/tasks/{turn['session_id']}", token)
        logs_v2 = _request_json(
            base_url,
            f"/v2/tasks/{turn['session_id']}/logs?limit=100",
            token,
        )
        timeline_v2 = _request_json(
            base_url,
            f"/v2/tasks/{turn['session_id']}/timeline?limit=100",
            token,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert turn["state"] == "running"
    assert turn["session_id"].startswith("dev-session-")
    assert state["projects"][0]["display_name"] == "Persistent project"
    assert state["sessions"][0]["response"] == "Answer to: Hello"
    assert state["sessions"][0]["state"] == "completed"
    assert state["sessions"][0]["workspace_changes"][0]["path"] == "hello.py"
    assert state["workspaces"][0]["entries"][0]["content"] == "print('hello')\n"
    assert task_page_v2["items"][0]["task_id"] == turn["session_id"]
    assert task_v2["state"] == "closed"
    assert logs_v2["items"][-1]["stream"] == "transcript"
    assert logs_v2["items"][-1]["message"] == "Answer to: Hello"
    assert [item["event_type"] for item in timeline_v2["items"]] == [
        "task_admitted",
        "attempt_appended",
    ]


def test_task_v2_logs_and_timeline_are_stable_across_pages_and_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    store = MODULE.DevelopmentStateStore(database)
    store.create_project(
        {
            "project_id": "development-project-1",
            "display_name": "Persistent project",
            "config": {},
        }
    )
    request = {
        "project_id": "development-project-1",
        "project_name": "Persistent project",
        "task_title": "Question",
        "instruction": "Hello",
    }
    store.start_session("development-session-1", request)
    store.append_session_log("development-session-1", "Harness started.")
    store.complete_session(
        "development-session-1",
        {
            "response": "Persistent answer.",
            "model": "codex",
            "duration_ms": 10,
            "logs": ["Harness started.", "Harness completed."],
            "workspace_changes": [],
            "runtime_activation": None,
        },
    )
    store.record_dataset_artifact(
        artifact_id="dataset-development-session-1",
        project_id="development-project-1",
        session_id="development-session-1",
        uri="file:///tmp/manifest.json",
        name="Question transcript",
        manifest_sha256="1" * 64,
    )

    first_logs = store.task_logs_v2("development-session-1", after_sequence=0, limit=2)
    assert first_logs.has_more is True
    assert first_logs.next_cursor == "2"
    second_logs = store.task_logs_v2(
        "development-session-1",
        after_sequence=int(first_logs.next_cursor),
        limit=100,
    )
    all_logs = [*first_logs.items, *second_logs.items]
    assert [item.sequence for item in all_logs] == list(range(1, len(all_logs) + 1))
    assert all_logs[-1].stream == "transcript"
    assert all_logs[-1].message == "Persistent answer."

    first_timeline = store.task_timeline_v2("development-session-1", after_sequence=0, limit=1)
    assert first_timeline.has_more is True
    assert first_timeline.next_cursor == "1"
    later_timeline = store.task_timeline_v2("development-session-1", after_sequence=1, limit=100)
    timeline = [*first_timeline.items, *later_timeline.items]
    assert [item.event_type for item in timeline] == [
        "task_admitted",
        "attempt_appended",
        "dataset_sealed",
    ]

    restored = MODULE.DevelopmentStateStore(database)
    restored_logs = restored.task_logs_v2("development-session-1", after_sequence=0, limit=100)
    restored_timeline = restored.task_timeline_v2(
        "development-session-1", after_sequence=0, limit=100
    )
    assert restored_logs.model_dump(mode="json") == MODULE.core_v2.LogPageV2(
        items=all_logs, next_cursor=None, has_more=False
    ).model_dump(mode="json")
    assert restored_timeline.items == timeline

    with pytest.raises(MODULE.RequestError, match="beyond"):
        restored.task_logs_v2("development-session-1", after_sequence=999, limit=100)
    with pytest.raises(MODULE.RequestError, match="beyond"):
        restored.task_timeline_v2("development-session-1", after_sequence=999, limit=100)


def test_session_coordinator_cancels_a_running_harness(tmp_path: Path) -> None:
    class BlockingRunner:
        def run(
            self,
            request: dict[str, object],
            *,
            cancellation: MODULE.HarnessCancellation,
            **_: object,
        ) -> dict[str, object]:
            while not cancellation.requested:
                time.sleep(0.01)
            raise MODULE.HarnessRunCancelled("Session cancelled by user")

    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    store.create_project(
        {
            "project_id": "development-project-1",
            "display_name": "Persistent project",
            "config": {},
        }
    )
    coordinator = MODULE.DevelopmentSessionCoordinator(
        runner=BlockingRunner(),
        store=store,
        evolution_runner=None,
    )
    session_id = coordinator.submit(
        {
            "project_id": "development-project-1",
            "project_name": "Persistent project",
            "task_title": "Cancel me",
            "instruction": "Wait",
        }
    )
    requested = coordinator.cancel(session_id)
    assert requested["state"] == "cancelling"
    deadline = time.monotonic() + 5
    while store.get_session(session_id)["state"] != "cancelled":
        if time.monotonic() >= deadline:
            raise AssertionError("cancelled session did not become terminal")
        time.sleep(0.01)
    assert store.get_session(session_id)["error"] is None


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
