from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sqlite3
import threading
import time
import urllib.request
from urllib.parse import urlencode

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

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["prompt"] = str(kwargs["input"])
        captured["args"] = args
        captured["env"] = kwargs["env"]
        runtime_workspace = Path(args[args.index("--cd") + 1])
        captured["agents_md"] = (runtime_workspace / "AGENTS.md").read_text(
            encoding="utf-8"
        )
        captured["skill_md"] = next(
            (runtime_workspace / ".agents" / "skills").glob("*/SKILL.md")
        ).read_text(encoding="utf-8")
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

    monkeypatch.setattr(subprocess, "run", run)
    result = MODULE.CodexRunner("codex", 30, "test-model").run({
        "project_name": "Memory project",
        "task_title": "Second question",
        "instruction": "Answer the next question.",
        "workspace_path": workspace,
        "workspace_snapshot": {"entries": []},
        "evolved_contexts": [
            {
                "artifact_id": "memory-artifact-1",
                "artifact_type": "text_memory",
                "target_id": "text_memory",
                "manifest": {"content_path": "memory.md"},
                "documents": [{
                    "path": "memory.md",
                    "media_type": "text/markdown",
                    "content": "# Evolved memory\n\n- Verify the answer before responding.",
                }],
            },
            {
                "artifact_id": "skill-artifact-1",
                "artifact_type": "skill_bundle",
                "target_id": "skill_bundle",
                "manifest": {"content_path": "SKILL.md"},
                "documents": [{
                    "path": "SKILL.md",
                    "media_type": "text/markdown",
                    "content": "# Native evolved skill\n\nUse this workflow when relevant.",
                }],
            },
            {
                "artifact_id": "agent-system-artifact-1",
                "artifact_type": "agent_system",
                "target_id": "agent_system",
                "manifest": {"content_path": "AGENTS.md", "target_path": "AGENTS.md"},
                "documents": [{
                    "path": "AGENTS.md",
                    "media_type": "text/markdown",
                    "content": "# Native evolved agent system\n\nFollow the project policy.",
                }],
            },
        ],
    })

    assert "Runtime instructions resolved by OpenEvo Core" in captured["prompt"]
    assert "Verify the answer before responding" in captured["prompt"]
    assert "Native evolved skill" not in captured["prompt"]
    assert "Native evolved agent system" not in captured["prompt"]
    assert captured["agents_md"] == "# Native evolved agent system\n\nFollow the project policy."
    assert captured["skill_md"].startswith(
        "---\nname: skill-artifact-1\ndescription: "
    )
    assert "\n---\n\n# Native evolved skill\n" in captured["skill_md"]
    assert "persistent OpenEvo project workspace" in captured["prompt"]
    assert "read-only" in captured["args"]
    assert "shell_tool" in captured["args"]
    assert "--ignore-rules" not in captured["args"]
    assert "--output-schema" in captured["args"]
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
                "documents": [{
                    "path": "memory.md",
                    "media_type": "text/markdown",
                    "content": "# On-demand memory",
                }],
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
                            "agents": [{
                                "agent_id": "reviewer",
                                "role": "Reviewer",
                                "instructions": "Review the proposed result.",
                            }],
                        },
                    },
                },
                "documents": [{
                    "path": "AGENTS.md",
                    "media_type": "text/markdown",
                    "content": "# Coordinator",
                }],
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
        "runtime_activation": {
            "schema_version": "1",
            "adapter_id": "codex-development-v1",
            "fully_supported": True,
            "decisions": [],
        },
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
        "development_dataset_artifacts",
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


def test_project_workspace_upload_and_download_preserve_binary_bytes(tmp_path: Path) -> None:
    store = MODULE.DevelopmentStateStore(tmp_path / "state.sqlite3")
    store.create_project({
        "project_id": "development-project-transfer",
        "display_name": "Transfer project",
        "config": {},
    })
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
    store.create_project({
        "project_id": "development-project-transfer",
        "display_name": "Transfer project",
        "config": {},
    })
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

    persistent_workspace = tmp_path / "persistent-workspace"
    persistent_workspace.mkdir()
    runtime_workspace = tmp_path / "runtime-workspace"
    runtime = MODULE.DevelopmentRuntimeContextMaterializer().materialize(
        persistent_workspace=persistent_workspace,
        runtime_workspace=runtime_workspace,
        contexts=store.latest_context_artifacts(project["project_id"]),
    )
    assert (runtime_workspace / "AGENTS.md").is_file()
    skill_paths = list(
        (runtime_workspace / ".agents" / "skills").glob("*/SKILL.md")
    )
    assert skill_paths
    assert skill_paths[0].read_text(encoding="utf-8").startswith("---\nname: ")
    assert (runtime_workspace / ".openevo" / "evolution" / "memory.md").is_file()
    assert runtime["instruction_sections"]
    assert runtime["environment"]["OPENEVO_SKILLS_DIR"].endswith("/.agents/skills")
    assert runtime["runtime_controls"] == []


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

    retry_job, retry_attempt = store.start_evolution_retry(failed_job["job_id"])
    retried_artifacts = runner.retry(job=retry_job, attempt=retry_attempt, store=store)

    assert invocations == 2
    assert len(retried_artifacts) == 1
    completed_job = store.get_evolution_job(failed_job["job_id"])
    assert completed_job["state"] == "completed"
    assert [attempt["state"] for attempt in completed_job["attempts"]] == [
        "failed",
        "completed",
    ]
    assert completed_job["attempts"][1]["ordinal"] == 2
    assert completed_job["resolver_input_artifact_ids"] == []
    assert len(store.dataset_artifacts(project["project_id"])) == 1


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
        assert runner_started.wait(timeout=1)
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
    store.create_project({
        "project_id": "development-project-1",
        "display_name": "Persistent project",
        "config": {},
    })
    coordinator = MODULE.DevelopmentSessionCoordinator(
        runner=BlockingRunner(),
        store=store,
        evolution_runner=None,
    )
    session_id = coordinator.submit({
        "project_id": "development-project-1",
        "project_name": "Persistent project",
        "task_title": "Cancel me",
        "instruction": "Wait",
    })
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
