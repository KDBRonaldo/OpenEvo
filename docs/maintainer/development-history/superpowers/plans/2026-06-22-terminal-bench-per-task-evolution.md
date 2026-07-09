# Terminal Bench Per-Task Evolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-task Terminal Bench evolution path that can inject evolved agent-system artifacts now and can later materialize skill and memory artifacts through the same runner boundary.

**Architecture:** Keep Polar responsible for transcript ingestion and evolution jobs, and keep Harbor responsible for executing official Terminal Bench trials. Add a thin materialization layer between them: evolved artifacts become explicit Harbor wrapper kwargs, starting with `agent_system_path`. A local per-task runner coordinates round directories, dataset creation, worker execution, artifact discovery, Harbor reruns, and summary output.

**Tech Stack:** Python 3.11+, argparse, pathlib/json/hashlib/subprocess, pytest, Polar Evolution store/CLI, Harbor custom agent wrapper, Codex subscription mode.

---

## File Structure

- Modify `/root/EvoLabCore-terminal-bench-task-package/task_packages/terminal_bench_v1/harbor_agent.py`
  - Add `agent_system_path` support in `EvoLabHarborAgent`.
  - Compose injected instruction for `codex_subscription` mode.
  - Record artifact metadata in `result.json`.

- Modify `/root/EvoLabCore-terminal-bench-task-package/tests/test_terminal_bench_task_package.py`
  - Add unit tests for `agent_system_path` composition, metadata, and missing file validation.

- Modify `/root/EvoLabCore-terminal-bench-task-package/task_packages/terminal_bench_v1/README.md`
  - Document the new `--ak agent_system_path=...` option.

- Modify `/home/ziyi/ProRL-Agent-Server/src/polar_evolution/cli.py`
  - Add optional task-scoped tags to Terminal Bench agent-system jobs.
  - Add a `terminal-bench-per-task-evolution` CLI entrypoint that calls the runner.

- Create `/home/ziyi/ProRL-Agent-Server/src/polar_evolution/terminal_bench_per_task.py`
  - Implement artifact materialization, Harbor command construction, worker invocation, artifact discovery, and per-task summaries.
  - Keep skill and memory artifacts as explicit skipped materializers for now.

- Create `/home/ziyi/ProRL-Agent-Server/tests/evolution/test_terminal_bench_per_task.py`
  - Test runner planning, artifact materialization, command construction, and unsupported skill/memory behavior without running Harbor.

- Modify `/home/ziyi/ProRL-Agent-Server/tests/evolution/test_terminal_bench_bridge.py`
  - Update expected Terminal Bench compatibility tags for task-scoped jobs.

- Modify `/home/ziyi/ProRL-Agent-Server/src/polar_evolution/README.md`
  - Document per-task Terminal Bench evolution commands and expected output layout.

---

### Task 1: Add Agent-System Injection To The Harbor Wrapper

**Files:**
- Modify: `/root/EvoLabCore-terminal-bench-task-package/task_packages/terminal_bench_v1/harbor_agent.py`
- Modify: `/root/EvoLabCore-terminal-bench-task-package/tests/test_terminal_bench_task_package.py`

- [ ] **Step 1: Write the failing test for instruction injection**

Append this test near `test_evolab_harbor_agent_can_delegate_to_codex_subscription`:

```python
def test_evolab_harbor_agent_injects_agent_system_for_codex_subscription(tmp_path: Path, monkeypatch):
    class FakeBaseAgent:
        def __init__(self, logs_dir: Path, model_name: str | None = None, **_kwargs):
            self.logs_dir = logs_dir
            self.model_name = model_name

    class FakeAgentContext:
        def __init__(self):
            self.metadata = {}

    class FakeEnvironment:
        pass

    class FakeCodex:
        instances = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.run_calls = []
            FakeCodex.instances.append(self)

        async def setup(self, environment):
            pass

        async def run(self, instruction, environment, context):
            self.run_calls.append((instruction, environment, context))

        def populate_context_post_run(self, context):
            context.metadata["codex_populated"] = True

    harbor_agents_base = types.ModuleType("harbor.agents.base")
    harbor_agents_base.BaseAgent = FakeBaseAgent
    harbor_environments_base = types.ModuleType("harbor.environments.base")
    harbor_environments_base.BaseEnvironment = object
    harbor_models_agent_context = types.ModuleType("harbor.models.agent.context")
    harbor_models_agent_context.AgentContext = FakeAgentContext
    harbor_installed_codex = types.ModuleType("harbor.agents.installed.codex")
    harbor_installed_codex.Codex = FakeCodex
    for module_name, module in {
        "harbor": types.ModuleType("harbor"),
        "harbor.agents": types.ModuleType("harbor.agents"),
        "harbor.agents.base": harbor_agents_base,
        "harbor.agents.installed": types.ModuleType("harbor.agents.installed"),
        "harbor.agents.installed.codex": harbor_installed_codex,
        "harbor.environments": types.ModuleType("harbor.environments"),
        "harbor.environments.base": harbor_environments_base,
        "harbor.models": types.ModuleType("harbor.models"),
        "harbor.models.agent": types.ModuleType("harbor.models.agent"),
        "harbor.models.agent.context": harbor_models_agent_context,
    }.items():
        monkeypatch.setitem(sys.modules, module_name, module)

    agent_system = tmp_path / "AGENTS.md"
    agent_system.write_text("Always inspect existing files before editing.\n", encoding="utf-8")
    agent_module = _load_package_module("harbor_agent.py", "terminal_bench_harbor_agent_codex_agents")
    context = FakeAgentContext()
    environment = FakeEnvironment()
    agent = agent_module.EvoLabHarborAgent(
        logs_dir=tmp_path / "logs",
        model_name="gpt-5.5",
        mode="codex_subscription",
        agent_system_path=str(agent_system),
    )

    asyncio.run(agent.setup(environment))
    asyncio.run(agent.run("Solve the task.", environment, context))

    [codex] = FakeCodex.instances
    [(composed_instruction, _, _)] = codex.run_calls
    assert "Additional operating rules:" in composed_instruction
    assert "Always inspect existing files before editing." in composed_instruction
    assert composed_instruction.endswith("Task:\nSolve the task.")
    assert (tmp_path / "logs" / "instruction.txt").read_text(encoding="utf-8") == "Solve the task."
    assert (tmp_path / "logs" / "composed_instruction.txt").read_text(encoding="utf-8") == composed_instruction
    metadata = json.loads((tmp_path / "logs" / "result.json").read_text(encoding="utf-8"))
    assert metadata["agent_system_path"] == str(agent_system)
    assert len(metadata["agent_system_sha256"]) == 64
```

- [ ] **Step 2: Write the failing test for missing artifact validation**

Append this test after the previous one:

```python
def test_evolab_harbor_agent_rejects_missing_agent_system_path(tmp_path: Path, monkeypatch):
    class FakeBaseAgent:
        def __init__(self, logs_dir: Path, model_name: str | None = None, **_kwargs):
            self.logs_dir = logs_dir
            self.model_name = model_name

    harbor_agents_base = types.ModuleType("harbor.agents.base")
    harbor_agents_base.BaseAgent = FakeBaseAgent
    harbor_environments_base = types.ModuleType("harbor.environments.base")
    harbor_environments_base.BaseEnvironment = object
    harbor_models_agent_context = types.ModuleType("harbor.models.agent.context")
    harbor_models_agent_context.AgentContext = object
    for module_name, module in {
        "harbor": types.ModuleType("harbor"),
        "harbor.agents": types.ModuleType("harbor.agents"),
        "harbor.agents.base": harbor_agents_base,
        "harbor.environments": types.ModuleType("harbor.environments"),
        "harbor.environments.base": harbor_environments_base,
        "harbor.models": types.ModuleType("harbor.models"),
        "harbor.models.agent": types.ModuleType("harbor.models.agent"),
        "harbor.models.agent.context": harbor_models_agent_context,
    }.items():
        monkeypatch.setitem(sys.modules, module_name, module)

    agent_module = _load_package_module("harbor_agent.py", "terminal_bench_harbor_agent_missing_agents")
    agent = agent_module.EvoLabHarborAgent(
        logs_dir=tmp_path / "logs",
        mode="codex_subscription",
        agent_system_path=str(tmp_path / "missing.md"),
    )

    with pytest.raises(FileNotFoundError, match="agent_system_path"):
        asyncio.run(agent.run("Solve.", object(), object()))
```

- [ ] **Step 3: Run the failing wrapper tests**

Run:

```bash
cd /root/EvoLabCore-terminal-bench-task-package
uv run --frozen pytest tests/test_terminal_bench_task_package.py::test_evolab_harbor_agent_injects_agent_system_for_codex_subscription tests/test_terminal_bench_task_package.py::test_evolab_harbor_agent_rejects_missing_agent_system_path -q
```

Expected: both tests fail because `agent_system_path` is not accepted or used yet.

- [ ] **Step 4: Implement wrapper injection**

Edit `harbor_agent.py`:

```python
import hashlib
```

Add the constructor parameter and field:

```python
        agent_system_path: str | None = None,
```

```python
        self.agent_system_path = agent_system_path
```

Add these helpers inside `EvoLabHarborAgent`:

```python
    def _instruction_for_run(self, instruction: str) -> tuple[str, dict[str, Any]]:
        if not self.agent_system_path:
            return instruction, {}
        path = Path(self.agent_system_path)
        if not path.is_file():
            raise FileNotFoundError(f"agent_system_path does not exist: {path}")
        text = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        composed = (
            "Additional operating rules:\n"
            f"{text.rstrip()}\n\n"
            "Task:\n"
            f"{instruction}"
        )
        (self.logs_dir / "composed_instruction.txt").write_text(composed, encoding="utf-8")
        return composed, {
            "agent_system_path": str(path),
            "agent_system_sha256": digest,
        }
```

Update `run()` before mode-specific execution:

```python
        run_instruction, artifact_metadata = self._instruction_for_run(instruction)
```

Use `run_instruction` only for delegated Codex:

```python
            await self._get_or_create_codex_agent().run(run_instruction, environment, context)
```

Merge `artifact_metadata` into result metadata:

```python
                    **artifact_metadata,
```

For non-Codex modes, continue passing the original `instruction` to preserve current EvoLab and command behavior.

- [ ] **Step 5: Run wrapper tests**

Run:

```bash
cd /root/EvoLabCore-terminal-bench-task-package
uv run --frozen pytest tests/test_terminal_bench_task_package.py tests/test_terminal_bench_direct_solver.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit wrapper injection**

Run:

```bash
cd /root/EvoLabCore-terminal-bench-task-package
git add task_packages/terminal_bench_v1/harbor_agent.py tests/test_terminal_bench_task_package.py
git commit -m "feat: inject evolved agent system into terminal bench codex runs"
```

---

### Task 2: Add Task-Scoped Compatibility Metadata To Polar Jobs

**Files:**
- Modify: `/home/ziyi/ProRL-Agent-Server/src/polar_evolution/cli.py`
- Modify: `/home/ziyi/ProRL-Agent-Server/tests/evolution/test_terminal_bench_bridge.py`

- [ ] **Step 1: Write the failing CLI expectation**

Change the compatibility assertion in `test_terminal_bench_agent_system_job_cli_creates_audited_reflector_job` to:

```python
    assert payload["job"]["config"]["compatibility"] == {
        "agent_harness": ["terminal-bench-harbor"],
        "task_tags": ["terminal-bench", "terminal-bench:fix-git"],
    }
```

In `test_terminal_bench_agent_system_job_cli_uses_history_method_for_multiple_datasets`, add:

```python
    assert payload["job"]["config"]["compatibility"]["task_tags"] == [
        "terminal-bench",
        "terminal-bench:fix-git",
    ]
```

- [ ] **Step 2: Run the failing Polar bridge tests**

Run:

```bash
cd /home/ziyi/ProRL-Agent-Server
uv run pytest tests/evolution/test_terminal_bench_bridge.py::test_terminal_bench_agent_system_job_cli_creates_audited_reflector_job tests/evolution/test_terminal_bench_bridge.py::test_terminal_bench_agent_system_job_cli_uses_history_method_for_multiple_datasets -q
```

Expected: assertions fail because task tags currently only contain `terminal-bench`.

- [ ] **Step 3: Implement task tag derivation**

In `cli.py`, add:

```python
def _terminal_bench_task_tags(
    store: EvolutionStore,
    input_artifact_ids: list[str],
    events: list[EventIngestRequest],
) -> list[str]:
    tags = ["terminal-bench"]
    task_ids: list[str] = []
    for event in events:
        _append_text_literal(task_ids, event.task_id)
    for artifact_id in input_artifact_ids:
        manifest_path = Path(_artifact_uri(store, artifact_id).removeprefix("file://"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        records_path = _dataset_records_path(manifest_path, manifest)
        for line in records_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if isinstance(record, dict):
                _append_text_literal(task_ids, record.get("task_id"))
    for task_id in _unique_nonempty_text(task_ids):
        tags.append(f"terminal-bench:{task_id}")
    return tags
```

Update `_terminal_bench_agent_system_job_config`:

```python
        "compatibility": {
            "agent_harness": ["terminal-bench-harbor"],
            "task_tags": _terminal_bench_task_tags(store, input_artifact_ids, events),
        },
```

- [ ] **Step 4: Run Polar bridge tests**

Run:

```bash
cd /home/ziyi/ProRL-Agent-Server
uv run pytest tests/evolution/test_terminal_bench_bridge.py -q
```

Expected: all tests in the file pass.

- [ ] **Step 5: Commit task-scoped metadata**

Run:

```bash
cd /home/ziyi/ProRL-Agent-Server
git add src/polar_evolution/cli.py tests/evolution/test_terminal_bench_bridge.py
git commit -m "feat: tag terminal bench evolution jobs by task"
```

---

### Task 3: Implement Per-Task Evolution Planning And Materialization

**Files:**
- Create: `/home/ziyi/ProRL-Agent-Server/src/polar_evolution/terminal_bench_per_task.py`
- Create: `/home/ziyi/ProRL-Agent-Server/tests/evolution/test_terminal_bench_per_task.py`

- [ ] **Step 1: Write failing materialization tests**

Create `tests/evolution/test_terminal_bench_per_task.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

from polar_evolution.terminal_bench_per_task import (
    ArtifactMaterializer,
    EvolutionArtifact,
    build_harbor_command,
    discover_agent_system_artifact_path,
    summarize_transition,
)


def test_agent_system_materializer_sets_harbor_kwargs(tmp_path: Path):
    artifact_path = tmp_path / "AGENTS.md"
    artifact_path.write_text("Inspect files first.\n", encoding="utf-8")
    materializer = ArtifactMaterializer()

    kwargs = materializer.materialize(
        EvolutionArtifact(
            artifact_type="agent_system",
            artifact_id="art-agent",
            path=artifact_path,
            task_id="fix-git",
            round=1,
            method="agent_system_reflector",
            source_dataset_artifact_ids=["dataset-r0"],
        )
    )

    assert kwargs == {"agent_system_path": str(artifact_path)}


def test_skill_and_memory_materializers_are_explicitly_skipped(tmp_path: Path):
    materializer = ArtifactMaterializer()
    skill = EvolutionArtifact(
        artifact_type="skill_bundle",
        artifact_id="art-skill",
        path=tmp_path / "skills",
        task_id="fix-git",
        round=1,
        method="skill_bundle",
        source_dataset_artifact_ids=[],
    )
    memory = EvolutionArtifact(
        artifact_type="memory",
        artifact_id="art-memory",
        path=tmp_path / "memory.md",
        task_id="fix-git",
        round=1,
        method="text_memory",
        source_dataset_artifact_ids=[],
    )

    assert materializer.materialize(skill) == {}
    assert materializer.materialize(memory) == {}
    assert materializer.skipped == [
        {
            "artifact_id": "art-skill",
            "artifact_type": "skill_bundle",
            "reason": "skill_bundle materialization is not implemented for Harbor Codex runs",
        },
        {
            "artifact_id": "art-memory",
            "artifact_type": "memory",
            "reason": "memory materialization is not implemented for Harbor Codex runs",
        },
    ]


def test_build_harbor_command_includes_agent_system_path_and_subscription_env():
    command = build_harbor_command(
        job_name="tb21-evolved-fix-git-r1",
        task_root=Path("/root/datasets/terminal-bench-2-1/tasks"),
        task_id="fix-git",
        model="gpt-5.5",
        env_json={"NO_PROXY": "localhost"},
        agent_kwargs={"agent_system_path": "/tmp/AGENTS.md"},
        verifier_env={"UV_NO_INDEX": "1"},
        n_concurrent=1,
    )

    assert command[:2] == ["harbor", "run"]
    assert "--include-task-name" in command
    assert "fix-git" in command
    assert "--ak" in command
    assert "mode=codex_subscription" in command
    assert "agent_system_path=/tmp/AGENTS.md" in command
    assert "env_json={\"NO_PROXY\":\"localhost\"}" in command
    assert "--verifier-env" in command
    assert "UV_NO_INDEX=1" in command


def test_discover_agent_system_artifact_path_reads_worker_manifest(tmp_path: Path):
    content = tmp_path / "workers" / "job-1" / "agent_system_reflector" / "agents.md"
    content.parent.mkdir(parents=True)
    content.write_text("rules\n", encoding="utf-8")
    job_payload = {
        "job": {
            "input_artifact_ids": ["dataset-r0"],
        }
    }
    completed_artifacts = [
        {
            "artifact_id": "art-agent",
            "type": "agent_system",
            "uri": content.resolve().as_uri(),
            "manifest": {"method": "agent_system_reflector"},
        }
    ]

    artifact = discover_agent_system_artifact_path(
        completed_artifacts,
        task_id="fix-git",
        round_number=1,
        job_payload=job_payload,
    )

    assert artifact.artifact_type == "agent_system"
    assert artifact.artifact_id == "art-agent"
    assert artifact.path == content
    assert artifact.source_dataset_artifact_ids == ["dataset-r0"]


def test_summarize_transition_classifies_pass_fail_changes():
    assert summarize_transition(0.0, 1.0) == "fail_to_pass"
    assert summarize_transition(1.0, 0.0) == "pass_to_fail"
    assert summarize_transition(1.0, 1.0) == "pass_to_pass"
    assert summarize_transition(0.0, 0.0) == "fail_to_fail"
```

- [ ] **Step 2: Run the failing per-task tests**

Run:

```bash
cd /home/ziyi/ProRL-Agent-Server
uv run pytest tests/evolution/test_terminal_bench_per_task.py -q
```

Expected: import fails because `polar_evolution.terminal_bench_per_task` does not exist.

- [ ] **Step 3: Implement the pure runner utilities**

Create `src/polar_evolution/terminal_bench_per_task.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EvolutionArtifact:
    artifact_type: str
    artifact_id: str
    path: Path
    task_id: str
    round: int
    method: str
    source_dataset_artifact_ids: list[str] = field(default_factory=list)


class ArtifactMaterializer:
    def __init__(self) -> None:
        self.skipped: list[dict[str, str]] = []

    def materialize(self, artifact: EvolutionArtifact) -> dict[str, str]:
        if artifact.artifact_type == "agent_system":
            return {"agent_system_path": str(artifact.path)}
        if artifact.artifact_type == "skill_bundle":
            self.skipped.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "reason": "skill_bundle materialization is not implemented for Harbor Codex runs",
                }
            )
            return {}
        if artifact.artifact_type == "memory":
            self.skipped.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "reason": "memory materialization is not implemented for Harbor Codex runs",
                }
            )
            return {}
        raise ValueError(f"unsupported evolution artifact type: {artifact.artifact_type}")


def build_harbor_command(
    *,
    job_name: str,
    task_root: Path,
    task_id: str,
    model: str,
    env_json: dict[str, str],
    agent_kwargs: dict[str, str],
    verifier_env: dict[str, str],
    n_concurrent: int,
) -> list[str]:
    command = [
        "harbor",
        "run",
        "--job-name",
        job_name,
        "--path",
        str(task_root),
        "--include-task-name",
        task_id,
        "--n-attempts",
        "1",
        "--n-concurrent",
        str(n_concurrent),
        "--agent-import-path",
        "task_packages.terminal_bench_v1.harbor_agent:EvoLabHarborAgent",
        "--model",
        model,
        "--ak",
        "mode=codex_subscription",
        "--ak",
        f"env_json={json.dumps(env_json, sort_keys=True, separators=(',', ':'))}",
    ]
    for key in sorted(agent_kwargs):
        command.extend(["--ak", f"{key}={agent_kwargs[key]}"])
    for key in sorted(verifier_env):
        command.extend(["--verifier-env", f"{key}={verifier_env[key]}"])
    return command


def discover_agent_system_artifact_path(
    completed_artifacts: list[dict[str, Any]],
    *,
    task_id: str,
    round_number: int,
    job_payload: dict[str, Any],
) -> EvolutionArtifact:
    for artifact in completed_artifacts:
        if artifact.get("type") != "agent_system":
            continue
        uri = artifact.get("uri")
        if not isinstance(uri, str) or not uri.startswith("file://"):
            raise ValueError(f"agent_system artifact has unsupported uri: {uri!r}")
        manifest = artifact.get("manifest")
        if not isinstance(manifest, dict):
            manifest = {}
        return EvolutionArtifact(
            artifact_type="agent_system",
            artifact_id=str(artifact["artifact_id"]),
            path=Path(uri.removeprefix("file://")),
            task_id=task_id,
            round=round_number,
            method=str(manifest.get("method") or "agent_system_reflector"),
            source_dataset_artifact_ids=list(
                job_payload.get("job", {}).get("input_artifact_ids", [])
            ),
        )
    raise ValueError("completed job did not produce an agent_system artifact")


def summarize_transition(before: float | None, after: float | None) -> str:
    before_passed = (before or 0.0) >= 1.0
    after_passed = (after or 0.0) >= 1.0
    if before_passed and after_passed:
        return "pass_to_pass"
    if before_passed and not after_passed:
        return "pass_to_fail"
    if not before_passed and after_passed:
        return "fail_to_pass"
    return "fail_to_fail"
```

- [ ] **Step 4: Run per-task utility tests**

Run:

```bash
cd /home/ziyi/ProRL-Agent-Server
uv run pytest tests/evolution/test_terminal_bench_per_task.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit pure per-task utilities**

Run:

```bash
cd /home/ziyi/ProRL-Agent-Server
git add src/polar_evolution/terminal_bench_per_task.py tests/evolution/test_terminal_bench_per_task.py
git commit -m "feat: add terminal bench per-task evolution utilities"
```

---

### Task 4: Add The Per-Task Evolution CLI

**Files:**
- Modify: `/home/ziyi/ProRL-Agent-Server/src/polar_evolution/terminal_bench_per_task.py`
- Modify: `/home/ziyi/ProRL-Agent-Server/src/polar_evolution/cli.py`
- Modify: `/home/ziyi/ProRL-Agent-Server/tests/evolution/test_terminal_bench_per_task.py`

- [ ] **Step 1: Write a failing dry-run CLI test**

Append to `tests/evolution/test_terminal_bench_per_task.py`:

```python
from polar_evolution.cli import main


def test_terminal_bench_per_task_evolution_cli_dry_run_writes_plan(tmp_path: Path):
    output = tmp_path / "summary.json"
    exit_code = main(
        [
            "terminal-bench-per-task-evolution",
            "--task-root",
            "/root/datasets/terminal-bench-2-1/tasks",
            "--task-id",
            "fix-git",
            "--run-root",
            str(tmp_path / "run"),
            "--model",
            "gpt-5.5",
            "--reflector-model",
            "gpt-5.5",
            "--rounds",
            "1",
            "--dry-run",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["tasks"] == [
        {
            "task_id": "fix-git",
            "rounds": 1,
            "artifact_types": ["agent_system"],
        }
    ]
```

- [ ] **Step 2: Run the failing dry-run CLI test**

Run:

```bash
cd /home/ziyi/ProRL-Agent-Server
uv run pytest tests/evolution/test_terminal_bench_per_task.py::test_terminal_bench_per_task_evolution_cli_dry_run_writes_plan -q
```

Expected: argparse fails because the subcommand does not exist.

- [ ] **Step 3: Implement the dry-run runner API**

Add to `terminal_bench_per_task.py`:

```python
def run_per_task_evolution_dry_run(
    *,
    task_root: Path,
    task_ids: list[str],
    run_root: Path,
    model: str,
    reflector_model: str,
    rounds: int,
    artifact_types: list[str],
) -> dict[str, Any]:
    run_root.mkdir(parents=True, exist_ok=True)
    return {
        "dry_run": True,
        "task_root": str(task_root),
        "run_root": str(run_root),
        "model": model,
        "reflector_model": reflector_model,
        "tasks": [
            {"task_id": task_id, "rounds": rounds, "artifact_types": artifact_types}
            for task_id in task_ids
        ],
    }
```

- [ ] **Step 4: Wire the CLI parser and command**

In `cli.py`, import:

```python
from polar_evolution.terminal_bench_per_task import run_per_task_evolution_dry_run
```

Add a parser:

```python
    tb_per_task = subparsers.add_parser(
        "terminal-bench-per-task-evolution",
        help="Run or plan per-task Terminal Bench evolution.",
    )
    tb_per_task.add_argument("--task-root", required=True)
    tb_per_task.add_argument("--task-id", action="append", default=[], required=True)
    tb_per_task.add_argument("--run-root", required=True)
    tb_per_task.add_argument("--model", required=True)
    tb_per_task.add_argument("--reflector-model", required=True)
    tb_per_task.add_argument("--rounds", type=int, default=1)
    tb_per_task.add_argument(
        "--artifact-type",
        action="append",
        default=["agent_system"],
        choices=["agent_system", "skill_bundle", "memory"],
    )
    tb_per_task.add_argument("--dry-run", action="store_true")
    tb_per_task.add_argument("--output", required=True)
```

Add a branch in `main()`:

```python
    if args.command == "terminal-bench-per-task-evolution":
        if not args.dry_run:
            raise ValueError("terminal-bench-per-task-evolution currently requires --dry-run")
        payload = run_per_task_evolution_dry_run(
            task_root=Path(args.task_root),
            task_ids=args.task_id,
            run_root=Path(args.run_root),
            model=args.model,
            reflector_model=args.reflector_model,
            rounds=args.rounds,
            artifact_types=args.artifact_type,
        )
        _write_json_output(payload, args.output)
        return 0
```

- [ ] **Step 5: Run CLI dry-run tests**

Run:

```bash
cd /home/ziyi/ProRL-Agent-Server
uv run pytest tests/evolution/test_terminal_bench_per_task.py -q
```

Expected: all per-task tests pass.

- [ ] **Step 6: Commit dry-run CLI**

Run:

```bash
cd /home/ziyi/ProRL-Agent-Server
git add src/polar_evolution/cli.py src/polar_evolution/terminal_bench_per_task.py tests/evolution/test_terminal_bench_per_task.py
git commit -m "feat: add terminal bench per-task evolution CLI"
```

---

### Task 5: Implement One-Round Local Orchestration

**Files:**
- Modify: `/home/ziyi/ProRL-Agent-Server/src/polar_evolution/terminal_bench_per_task.py`
- Modify: `/home/ziyi/ProRL-Agent-Server/src/polar_evolution/cli.py`
- Modify: `/home/ziyi/ProRL-Agent-Server/tests/evolution/test_terminal_bench_per_task.py`

- [ ] **Step 1: Write a failing orchestration test with fake command hooks**

Append:

```python
def test_one_round_orchestration_uses_existing_baseline_and_runs_harbor(tmp_path: Path):
    from polar_evolution.terminal_bench_per_task import run_per_task_evolution

    baseline = tmp_path / "baseline" / "fix-git__abc"
    (baseline / "agent").mkdir(parents=True)
    (baseline / "verifier").mkdir()
    (baseline / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "fix-git__abc",
                "task_name": "fix-git",
                "status": "COMPLETED",
                "agent_result": {
                    "metadata": {
                        "terminal_bench_harbor_agent": {
                            "task_id": "fix-git",
                            "model_name": "gpt-5.5",
                        }
                    }
                },
                "verifier_result": {"rewards": {"reward": 0.0}},
            }
        ),
        encoding="utf-8",
    )
    (baseline / "agent" / "instruction.txt").write_text("Find the missing git changes.", encoding="utf-8")
    (baseline / "agent" / "stdout.txt").write_text("missed the target branch\n", encoding="utf-8")
    (baseline / "verifier" / "reward.txt").write_text("0.0\n", encoding="utf-8")

    evolved_trial = tmp_path / "run" / "harbor" / "fix-git-r1" / "fix-git__r1"
    (evolved_trial / "agent").mkdir(parents=True)
    (evolved_trial / "verifier").mkdir()
    (evolved_trial / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "fix-git__r1",
                "task_name": "fix-git",
                "status": "COMPLETED",
                "agent_result": {
                    "metadata": {
                        "terminal_bench_harbor_agent": {
                            "task_id": "fix-git",
                            "model_name": "gpt-5.5",
                        }
                    }
                },
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        ),
        encoding="utf-8",
    )
    (evolved_trial / "agent" / "instruction.txt").write_text("Find the missing git changes.", encoding="utf-8")
    (evolved_trial / "agent" / "stdout.txt").write_text("fixed it\n", encoding="utf-8")
    (evolved_trial / "verifier" / "reward.txt").write_text("1.0\n", encoding="utf-8")

    commands = []

    def fake_run_command(command, cwd=None):
        commands.append(command)
        if "terminal-bench-agent-system-job" in command:
            job_path = Path(command[command.index("--output") + 1])
            job_path.parent.mkdir(parents=True, exist_ok=True)
            job_path.write_text(
                json.dumps(
                    {
                        "dataset": {"artifact_id": "dataset-r0"},
                        "job": {"input_artifact_ids": ["dataset-r0"]},
                    }
                ),
                encoding="utf-8",
            )
        return {}

    def fake_worker_runner(*, db_path, artifact_root):
        return [
            {
                "artifact_id": "art-agent",
                "type": "agent_system",
                "uri": (tmp_path / "AGENTS.md").resolve().as_uri(),
                "manifest": {"method": "agent_system_reflector"},
            }
        ]

    (tmp_path / "AGENTS.md").write_text("Inspect all branches before editing.\n", encoding="utf-8")
    summary = run_per_task_evolution(
        task_root=tmp_path / "tasks",
        task_ids=["fix-git"],
        run_root=tmp_path / "run",
        baseline_root=baseline.parent,
        model="gpt-5.5",
        reflector_model="gpt-5.5",
        rounds=1,
        env_json={},
        verifier_env={},
        command_runner=fake_run_command,
        worker_runner=fake_worker_runner,
        evolved_trial_locator=lambda task_id, round_number, run_root: evolved_trial,
    )

    assert summary["tasks"][0]["task_id"] == "fix-git"
    assert summary["tasks"][0]["baseline_reward"] == 0.0
    assert summary["tasks"][0]["rounds"][0]["reward"] == 1.0
    assert summary["tasks"][0]["rounds"][0]["transition"] == "fail_to_pass"
    assert any("agent_system_path=" in part for command in commands for part in command)
```

- [ ] **Step 2: Run the failing orchestration test**

Run:

```bash
cd /home/ziyi/ProRL-Agent-Server
uv run pytest tests/evolution/test_terminal_bench_per_task.py::test_one_round_orchestration_uses_existing_baseline_and_runs_harbor -q
```

Expected: import or attribute failure because `run_per_task_evolution` is not implemented.

- [ ] **Step 3: Implement orchestration with injectable command hooks**

Add functions to `terminal_bench_per_task.py`:

```python
import subprocess

from polar_evolution.methods import run_method
from polar_evolution.models import WorkerClaimRequest, WorkerCompleteRequest, WorkerFailRequest
from polar_evolution.store import EvolutionStore
from polar_evolution.terminal_bench_bridge import build_terminal_bench_events


def _default_command_runner(command: list[str], cwd: Path | None = None) -> dict[str, Any]:
    subprocess.run(command, cwd=cwd, check=True)
    return {}


def _default_evolved_trial_locator(task_id: str, round_number: int, run_root: Path) -> Path:
    round_root = run_root / "tasks" / task_id / f"r{round_number}"
    candidates = sorted(round_root.glob(f"**/{task_id}__*"))
    if not candidates:
        raise FileNotFoundError(f"no evolved trial found for {task_id} round {round_number} under {round_root}")
    return candidates[-1]


def _find_baseline_trial(baseline_root: Path, task_id: str) -> Path:
    direct = baseline_root / task_id
    if (direct / "result.json").is_file():
        return direct
    candidates = sorted(path for path in baseline_root.glob(f"{task_id}__*") if (path / "result.json").is_file())
    if not candidates:
        raise FileNotFoundError(f"no baseline trial found for {task_id} under {baseline_root}")
    return candidates[-1]


def _trial_reward(trial_dir: Path) -> float | None:
    [event] = build_terminal_bench_events(trial_dir)
    return event.reward


def _create_agent_system_job_command(
    *,
    active_trial: Path,
    run_root: Path,
    task_id: str,
    round_number: int,
    reflector_model: str,
    dataset_artifact_ids: list[str],
    job_path: Path,
) -> list[str]:
    method = "agent_system_reflector" if len(dataset_artifact_ids) == 0 else "agent_system_history_reflector"
    return [
        "uv",
        "run",
        "polar-evolution",
        "terminal-bench-agent-system-job",
        "--input",
        str(active_trial),
        "--db",
        str(run_root / "polar.db"),
        "--artifact-root",
        str(run_root / "polar_artifacts"),
        "--dataset-name",
        f"tb21_{task_id}_r{round_number - 1}",
        "--policy-version",
        f"tb21-{task_id}-r{round_number - 1}",
        "--method",
        method,
        "--reflector-provider",
        "codex_cli",
        "--reflector-model",
        reflector_model,
        "--output",
        str(job_path),
    ]


def _run_worker_once_local(*, db_path: Path, artifact_root: Path) -> list[dict[str, Any]]:
    store = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
    store.initialize()
    claim = store.claim_job(
        WorkerClaimRequest(
            worker_id="terminal-bench-per-task-runner",
            capabilities=[
                "agent_system_reflector",
                "agent_system_history_reflector",
                "agent_system_pareto_reflector",
            ],
        )
    )
    if claim.job is None:
        raise RuntimeError("no pending Polar evolution job to run")
    try:
        artifact_requests = run_method(claim.job, artifact_root=artifact_root)
        complete = store.complete_job(
            claim.job.job_id,
            WorkerCompleteRequest(
                lease_id=claim.job.lease_id,
                artifacts=artifact_requests,
                report={"method": claim.job.method, "artifact_count": len(artifact_requests)},
            ),
        )
    except Exception as exc:
        store.fail_job(
            claim.job.job_id,
            WorkerFailRequest(
                lease_id=claim.job.lease_id,
                error=str(exc),
                retryable=False,
            ),
        )
        raise
    artifact_ids = complete["artifact_ids"]
    return [
        {
            "artifact_id": artifact_id,
            "type": str(artifact.type),
            "uri": artifact.uri,
            "manifest": artifact.manifest,
        }
        for artifact_id, artifact in zip(artifact_ids, artifact_requests, strict=True)
    ]


def run_per_task_evolution(
    *,
    task_root: Path,
    task_ids: list[str],
    run_root: Path,
    baseline_root: Path,
    model: str,
    reflector_model: str,
    rounds: int,
    env_json: dict[str, str],
    verifier_env: dict[str, str],
    command_runner=_default_command_runner,
    worker_runner=_run_worker_once_local,
    evolved_trial_locator=_default_evolved_trial_locator,
) -> dict[str, Any]:
    run_root.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"dry_run": False, "tasks": []}
    for task_id in task_ids:
        task_summary: dict[str, Any] = {"task_id": task_id, "rounds": []}
        baseline_trial = _find_baseline_trial(baseline_root, task_id)
        task_summary["baseline_trial"] = str(baseline_trial)
        task_summary["baseline_reward"] = _trial_reward(baseline_trial)
        active_trial = baseline_trial
        dataset_artifact_ids: list[str] = []
        for round_number in range(1, rounds + 1):
            round_root = run_root / "tasks" / task_id / f"r{round_number}"
            round_root.mkdir(parents=True, exist_ok=True)
            job_path = round_root / "agent-system-job.json"
            command_runner(
                _create_agent_system_job_command(
                    active_trial=active_trial,
                    run_root=run_root,
                    task_id=task_id,
                    round_number=round_number,
                    reflector_model=reflector_model,
                    dataset_artifact_ids=dataset_artifact_ids,
                    job_path=job_path,
                )
            )
            job_payload = json.loads(job_path.read_text(encoding="utf-8"))
            if job_payload.get("dataset"):
                dataset_artifact_ids.append(job_payload["dataset"]["artifact_id"])
            completed_artifacts = worker_runner(
                db_path=run_root / "polar.db",
                artifact_root=run_root / "polar_artifacts",
            )
            artifact = discover_agent_system_artifact_path(
                completed_artifacts,
                task_id=task_id,
                round_number=round_number,
                job_payload=job_payload,
            )
            materializer = ArtifactMaterializer()
            agent_kwargs = materializer.materialize(artifact)
            harbor_command = build_harbor_command(
                job_name=f"tb21-evolved-{task_id}-r{round_number}",
                task_root=task_root,
                task_id=task_id,
                model=model,
                env_json=env_json,
                agent_kwargs=agent_kwargs,
                verifier_env=verifier_env,
                n_concurrent=1,
            )
            command_runner(harbor_command)
            evolved_trial = evolved_trial_locator(task_id, round_number, run_root)
            reward = _trial_reward(evolved_trial)
            task_summary["rounds"].append(
                {
                    "round": round_number,
                    "trial": str(evolved_trial),
                    "reward": reward,
                    "transition": summarize_transition(task_summary["baseline_reward"], reward),
                    "artifact": {
                        "artifact_id": artifact.artifact_id,
                        "artifact_type": artifact.artifact_type,
                        "path": str(artifact.path),
                        "method": artifact.method,
                    },
                    "skipped_artifacts": materializer.skipped,
                }
            )
            active_trial = evolved_trial
        summary["tasks"].append(task_summary)
    return summary
```

- [ ] **Step 4: Confirm the orchestration test uses explicit hooks**

Verify the fake test from Step 1 includes this command hook and worker hook:

```python
    def fake_run_command(command, cwd=None):
        commands.append(command)
        if "terminal-bench-agent-system-job" in command:
            job_path = Path(command[command.index("--output") + 1])
            job_path.parent.mkdir(parents=True, exist_ok=True)
            job_path.write_text(
                json.dumps(
                    {
                        "dataset": {"artifact_id": "dataset-r0"},
                        "job": {"input_artifact_ids": ["dataset-r0"]},
                    }
                ),
                encoding="utf-8",
            )
        return {}

    def fake_worker_runner(*, db_path, artifact_root):
        return [
            {
                "artifact_id": "art-agent",
                "type": "agent_system",
                "uri": (tmp_path / "AGENTS.md").resolve().as_uri(),
                "manifest": {"method": "agent_system_reflector"},
            }
        ]
```

- [ ] **Step 5: Run orchestration tests**

Run:

```bash
cd /home/ziyi/ProRL-Agent-Server
uv run pytest tests/evolution/test_terminal_bench_per_task.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Wire non-dry CLI execution**

Update the `terminal-bench-per-task-evolution` CLI branch to call `run_per_task_evolution` when `--dry-run` is absent. Add parser args:

```python
    tb_per_task.add_argument("--baseline-root")
    tb_per_task.add_argument("--env-json", default="{}")
    tb_per_task.add_argument("--verifier-env", action="append", default=[])
```

Parse env:

```python
def _parse_key_value_entries(entries: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for entry in entries:
        key, sep, value = entry.partition("=")
        if not sep or not key:
            raise ValueError(f"expected KEY=VALUE entry, got {entry!r}")
        parsed[key] = value
    return parsed
```

Use:

```python
        payload = run_per_task_evolution(
            task_root=Path(args.task_root),
            task_ids=args.task_id,
            run_root=Path(args.run_root),
            baseline_root=Path(args.baseline_root),
            model=args.model,
            reflector_model=args.reflector_model,
            rounds=args.rounds,
            env_json=json.loads(args.env_json),
            verifier_env=_parse_key_value_entries(args.verifier_env),
        )
```

- [ ] **Step 7: Commit orchestration**

Run:

```bash
cd /home/ziyi/ProRL-Agent-Server
git add src/polar_evolution/cli.py src/polar_evolution/terminal_bench_per_task.py tests/evolution/test_terminal_bench_per_task.py
git commit -m "feat: orchestrate per-task terminal bench evolution"
```

---

### Task 6: Document Usage

**Files:**
- Modify: `/root/EvoLabCore-terminal-bench-task-package/task_packages/terminal_bench_v1/README.md`
- Modify: `/home/ziyi/ProRL-Agent-Server/src/polar_evolution/README.md`

- [ ] **Step 1: Document wrapper injection**

Add to the Codex subscription section of the EvoLab Terminal Bench README:

```markdown
To evaluate a Polar-evolved agent system on one task, pass the generated
`AGENTS.md` path through `agent_system_path`. The wrapper prepends the file as
operating rules for the delegated Codex run and records the file path and SHA256
in `agent/result.json`.

```bash
harbor run \
  --path /root/datasets/terminal-bench-2-1/tasks \
  --include-task-name fix-git \
  --n-attempts 1 \
  --n-concurrent 1 \
  --agent-import-path task_packages.terminal_bench_v1.harbor_agent:EvoLabHarborAgent \
  --model gpt-5.5 \
  --ak mode=codex_subscription \
  --ak agent_system_path=/tmp/polar-tb/tasks/fix-git/r1/AGENTS.md
```
```

- [ ] **Step 2: Document per-task Polar runner**

Add to `src/polar_evolution/README.md`:

```markdown
### Per-task Terminal Bench evolution

Use this mode when each Terminal Bench task should evolve independently. The
first supported artifact type is `agent_system`; `skill_bundle` and `memory`
are represented in the runner interface but are skipped until their Harbor
materializers are implemented.

```bash
uv run polar-evolution terminal-bench-per-task-evolution \
  --task-root /root/datasets/terminal-bench-2-1/tasks \
  --task-id fix-git \
  --task-id filter-js-from-html \
  --baseline-root /tmp/tb21-compare-codex-vs-wrapper-20260622-082940/jobs/tb21-wrapper-codex-gpt55-subscription-10 \
  --run-root /tmp/tb21-per-task-evolution \
  --model gpt-5.5 \
  --reflector-model gpt-5.5 \
  --rounds 1 \
  --env-json '{"HTTP_PROXY":"http://172.17.0.8:7890","HTTPS_PROXY":"http://172.17.0.8:7890","ALL_PROXY":"http://172.17.0.8:7890","NO_PROXY":"localhost,127.0.0.1,::1"}' \
  --verifier-env UV_NO_INDEX=1 \
  --verifier-env UV_FIND_LINKS=http://172.17.0.8:8765/wheels \
  --verifier-env UV_DOWNLOAD_URL=http://172.17.0.8:8765 \
  --output /tmp/tb21-per-task-evolution/summary.json
```
```

- [ ] **Step 3: Commit docs**

Run:

```bash
cd /root/EvoLabCore-terminal-bench-task-package
git add task_packages/terminal_bench_v1/README.md
git commit -m "docs: document terminal bench agent system injection"

cd /home/ziyi/ProRL-Agent-Server
git add src/polar_evolution/README.md
git commit -m "docs: document per-task terminal bench evolution"
```

---

### Task 7: Verify And Run The First Experiment

**Files:**
- No code changes required unless verification exposes a bug.

- [ ] **Step 1: Run wrapper regression tests**

Run:

```bash
cd /root/EvoLabCore-terminal-bench-task-package
uv run --frozen pytest tests/test_terminal_bench_task_package.py tests/test_terminal_bench_direct_solver.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run Polar regression tests**

Run:

```bash
cd /home/ziyi/ProRL-Agent-Server
uv run pytest tests/evolution/test_terminal_bench_bridge.py tests/evolution/test_terminal_bench_per_task.py tests/evolution/test_worker_methods.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: Run a 3-task pilot**

Use the existing 10-task wrapper baseline as round 0:

```bash
HOST_IP=$(hostname -I | awk '{print $1}')
PROXY=http://$HOST_IP:7890
RUN_ROOT=/tmp/tb21-per-task-evolution-$(date -u +%Y%m%d-%H%M%S)
uv run polar-evolution terminal-bench-per-task-evolution \
  --task-root /root/datasets/terminal-bench-2-1/tasks \
  --task-id filter-js-from-html \
  --task-id fix-git \
  --task-id count-dataset-tokens \
  --baseline-root /tmp/tb21-compare-codex-vs-wrapper-20260622-082940/jobs/tb21-wrapper-codex-gpt55-subscription-10 \
  --run-root "$RUN_ROOT" \
  --model gpt-5.5 \
  --reflector-model gpt-5.5 \
  --rounds 1 \
  --env-json "{\"HTTP_PROXY\":\"$PROXY\",\"HTTPS_PROXY\":\"$PROXY\",\"ALL_PROXY\":\"$PROXY\",\"http_proxy\":\"$PROXY\",\"https_proxy\":\"$PROXY\",\"all_proxy\":\"$PROXY\",\"NO_PROXY\":\"localhost,127.0.0.1,::1\",\"no_proxy\":\"localhost,127.0.0.1,::1\"}" \
  --verifier-env NO_PROXY=localhost,127.0.0.1,::1,$HOST_IP \
  --verifier-env no_proxy=localhost,127.0.0.1,::1,$HOST_IP \
  --verifier-env UV_DOWNLOAD_URL=http://$HOST_IP:8765 \
  --verifier-env UV_FIND_LINKS=http://$HOST_IP:8765/wheels \
  --verifier-env UV_NO_INDEX=1 \
  --output "$RUN_ROOT/summary.json"
```

Expected: `summary.json` lists all three tasks with baseline reward, round 1
reward, transition, trial path, and active `agent_system` artifact path.

- [ ] **Step 4: Run the 10-task comparison if the pilot succeeds**

Run the same command with all 10 tasks from the wrapper comparison:

```bash
--task-id fix-git
--task-id git-multibranch
--task-id count-dataset-tokens
--task-id regex-log
--task-id filter-js-from-html
--task-id break-filter-js-from-html
--task-id cancel-async-tasks
--task-id sqlite-db-truncate
--task-id log-summary-date-ranges
--task-id overfull-hbox
```

Expected: same summary schema; compare pass/fail transitions against the baseline rewards.

- [ ] **Step 5: Report results**

Report:

```text
Run root: <path>
Baseline pass count: <n>/<k>
Evolved pass count: <n>/<k>
Improvements: <task ids>
Regressions: <task ids>
Unchanged failures: <task ids>
Unchanged passes: <task ids>
Known runner/verifier issues: <only if present>
```
