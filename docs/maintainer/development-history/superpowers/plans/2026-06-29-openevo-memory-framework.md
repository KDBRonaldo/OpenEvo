# OpenEvo Memory Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add OpenEvo controls for Codex-native memory policy, `parametric_memory` evolution, and strict memory-only context allowlisting.

**Architecture:** Keep the existing Polar artifact contract and worker method registry. Add narrow config/model/compiler support in OpenEvo, implement Codex-native memory cleanup inside the Codex harness, and make the evolution context resolver treat requested artifact ids as a strict allowlist for every artifact type.

**Tech Stack:** Python, Pydantic, pytest, existing Polar harness/runtime abstractions, existing OpenEvo compiler/runner, existing Polar Evolution store and method registry.

---

## File Structure

- `src/polar/agent/presets/codex.py`: validate `native_memory_policy` for Codex and clear known Codex memory files when requested.
- `tests/test_evolution_agent_harnesses.py`: focused Codex harness tests for preserve, clear, and invalid policy behavior.
- `src/openevo/experiment/models.py`: validate `agent.settings.native_memory_policy` and add `artifacts.parametric_memory`.
- `src/openevo/experiment/compiler.py`: compile `parametric_memory` method specs and include parametric artifact ids in rollout context flattening.
- `tests/openevo/test_experiment_compiler.py`: config/compiler tests for native memory policy and parametric memory controls.
- `src/openevo/experiment/runner.py`: track `parametric_memory` artifact ids in dry-run and live-run context maps.
- `tests/openevo/test_experiment_runner.py`: dry-run and live-run tests for parametric memory context propagation.
- `src/polar_evolution/store.py`: make `context_artifact_ids` a strict allowlist for `parametric_memory`.
- `tests/evolution/test_artifacts_context.py`: resolver regression for parametric memory allowlisting.
- `src/polar_evolution/methods.py`: pass `compatibility`, `lineage`, and `scores` through `parametric_memory_register`.
- `tests/evolution/test_worker_methods.py`: method regression for parametric metadata passthrough.
- `README.md`, `docs/architecture/evolution-backend.md`, `docs/architecture/evolution-runtime-context.md`: document config, context allowlist, and native memory policy.

Do not touch the existing dirty files unless a task explicitly lists them. In particular, avoid `src/polar_evolution/README.md` because it already has unrelated local changes.

## Task 1: Add Codex Native Memory Policy

**Files:**
- Modify: `tests/test_evolution_agent_harnesses.py`
- Modify: `src/polar/agent/presets/codex.py`

- [ ] **Step 1: Write failing Codex harness tests**

Add these tests after `test_codex_setup_overwrites_config_without_mcp_servers`:

```python
@pytest.mark.asyncio
async def test_codex_setup_preserves_native_memory_by_default(tmp_path):
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            env={"CODEX_HOME": "/polar/session/preauthenticated-codex"},
        )
    )
    runtime = RecordingRuntime(tmp_path)

    await harness.setup(runtime)

    joined = "\n".join(runtime.commands)
    assert "/polar/session/preauthenticated-codex/config.toml" in joined
    assert "/memories" not in joined
    assert "memories_" not in joined
    assert "auth.json" not in joined


@pytest.mark.asyncio
async def test_codex_setup_clears_native_memory_when_requested(tmp_path):
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            settings={"native_memory_policy": "clear"},
            env={"CODEX_HOME": "/polar/session/preauthenticated-codex"},
        )
    )
    runtime = RecordingRuntime(tmp_path)

    await harness.setup(runtime)

    cleanup_commands = [
        command
        for command in runtime.commands
        if "/memories" in command or "memories_" in command
    ]
    assert len(cleanup_commands) == 1
    cleanup = cleanup_commands[0]
    assert cleanup.startswith("rm -rf -- ")
    assert "/polar/session/preauthenticated-codex/memories" in cleanup
    assert "/polar/session/preauthenticated-codex/memories_*.sqlite" in cleanup
    assert "/polar/session/preauthenticated-codex/memories_*.sqlite-shm" in cleanup
    assert "/polar/session/preauthenticated-codex/memories_*.sqlite-wal" in cleanup
    assert "auth.json" not in cleanup
    assert "state_" not in cleanup
    assert "logs_" not in cleanup
    assert "history.jsonl" not in cleanup
    assert "session_index.jsonl" not in cleanup


@pytest.mark.asyncio
async def test_codex_setup_rejects_unknown_native_memory_policy(tmp_path):
    harness = CodexHarness(
        AgentSpec(
            harness="codex",
            settings={"native_memory_policy": "wipe"},
        )
    )
    runtime = RecordingRuntime(tmp_path)

    with pytest.raises(ValueError, match="native_memory_policy"):
        await harness.setup(runtime)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_evolution_agent_harnesses.py -k "native_memory_policy or preserves_native_memory or clears_native_memory" -q
```

Expected: the preserve test may pass, but the clear and invalid-policy tests fail because Codex setup does not yet consume `native_memory_policy`.

- [ ] **Step 3: Implement minimal Codex policy support**

In `src/polar/agent/presets/codex.py`, add constants near the class:

```python
_NATIVE_MEMORY_POLICY_PRESERVE = "preserve"
_NATIVE_MEMORY_POLICY_CLEAR = "clear"
_NATIVE_MEMORY_POLICIES = {
    _NATIVE_MEMORY_POLICY_PRESERVE,
    _NATIVE_MEMORY_POLICY_CLEAR,
}
```

In `CodexHarness.setup()`, after `mkdir -p` and before writing `config.toml`, add:

```python
        if _native_memory_policy(self.settings) == _NATIVE_MEMORY_POLICY_CLEAR:
            await runtime.exec(_clear_native_memory_command(codex_home))
```

Add helper functions near `_nonempty_env_path`:

```python
def _native_memory_policy(settings: dict[str, object]) -> str:
    raw_policy = settings.get("native_memory_policy")
    if raw_policy is None:
        return _NATIVE_MEMORY_POLICY_PRESERVE
    if not isinstance(raw_policy, str):
        raise ValueError("native_memory_policy must be 'preserve' or 'clear'")
    policy = raw_policy.strip()
    if policy not in _NATIVE_MEMORY_POLICIES:
        raise ValueError("native_memory_policy must be 'preserve' or 'clear'")
    return policy


def _clear_native_memory_command(codex_home: str) -> str:
    quoted_home = shlex.quote(codex_home)
    return (
        "rm -rf -- "
        f"{quoted_home}/memories "
        f"{quoted_home}/memories_*.sqlite "
        f"{quoted_home}/memories_*.sqlite-shm "
        f"{quoted_home}/memories_*.sqlite-wal"
    )
```

- [ ] **Step 4: Run focused Codex tests**

Run:

```bash
pytest tests/test_evolution_agent_harnesses.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git status --short
git add tests/test_evolution_agent_harnesses.py src/polar/agent/presets/codex.py
git commit -m "feat: add codex native memory policy"
```

Only stage the two listed files.

## Task 2: Add OpenEvo Parametric Memory Config And Compiler Support

**Files:**
- Modify: `tests/openevo/test_experiment_compiler.py`
- Modify: `src/openevo/experiment/models.py`
- Modify: `src/openevo/experiment/compiler.py`

- [ ] **Step 1: Write failing compiler/config tests**

Add these tests after `test_rollout_metadata_uses_sanitized_agent_summary`:

```python
def test_agent_native_memory_policy_is_preserved_in_rollout_settings() -> None:
    compiled = compile_experiment(
        _config(
            agent={
                "preset": "codex",
                "model": "gpt-5.1-codex-mini",
                "settings": {"native_memory_policy": "clear"},
            }
        )
    )

    payload = compiled.tasks[0].rollout_payload_for_round(0, context_artifact_ids=[])

    assert payload["agent"]["settings"]["native_memory_policy"] == "clear"


def test_agent_native_memory_policy_rejects_unknown_value() -> None:
    try:
        _config(
            agent={
                "preset": "codex",
                "model": "gpt-5.1-codex-mini",
                "settings": {"native_memory_policy": "wipe"},
            }
        )
    except ValueError as exc:
        assert "native_memory_policy" in str(exc)
    else:
        raise AssertionError("expected ValueError")
```

Replace `test_evolution_methods_are_ordered_text_memory_skill_bundle_agent_system` with:

```python
def test_evolution_methods_default_to_text_memory_skill_bundle_agent_system() -> None:
    compiled = compile_experiment(_config())

    assert [spec.artifact_type for spec in compiled.evolution_methods_for_round(0)] == [
        "text_memory",
        "skill_bundle",
        "agent_system",
    ]
```

Add this test after it:

```python
def test_evolution_methods_include_parametric_memory_when_enabled() -> None:
    compiled = compile_experiment(
        _config(
            artifacts={
                "text_memory": {"enabled": True},
                "parametric_memory": {
                    "enabled": True,
                    "method": "parametric_memory_register",
                    "config": {
                        "adapter_uri": "file:///adapters/parser-memory",
                        "base_model": "gpt-5.1-codex-mini",
                        "adapter_id": "parser-memory",
                    },
                },
                "skill_bundle": {"enabled": True},
                "agent_system": {"enabled": True},
            }
        )
    )

    specs = compiled.evolution_methods_for_round(0)

    assert [spec.artifact_type for spec in specs] == [
        "text_memory",
        "parametric_memory",
        "skill_bundle",
        "agent_system",
    ]
    assert specs[1].method == "parametric_memory_register"
    assert specs[1].config["adapter_uri"] == "file:///adapters/parser-memory"
    assert specs[1].config["base_model"] == "gpt-5.1-codex-mini"
    assert specs[1].config["adapter_id"] == "parser-memory"
    assert "reflector_llm" not in specs[1].config
```

Add this assertion to `test_rollout_context_excludes_internal_dataset_history`:

```python
            "parametric_memory": ["adapter_0"],
```

and update the expected ids:

```python
    assert payload["metadata"]["evolution"]["context_artifact_ids"] == [
        "memory_0",
        "adapter_0",
    ]
```

- [ ] **Step 2: Run compiler tests to verify they fail**

Run:

```bash
pytest tests/openevo/test_experiment_compiler.py -k "native_memory_policy or parametric_memory or rollout_context_excludes" -q
```

Expected: failures because `artifacts.parametric_memory` is forbidden, invalid native memory policy is not validated, and rollout context flattening ignores parametric memory.

- [ ] **Step 3: Implement config model support**

In `src/openevo/experiment/models.py`, add:

```python
_NATIVE_MEMORY_POLICIES = {"preserve", "clear"}
```

In `AgentConfig._require_transcript_capture_for_subscription()`, before `return self`, add:

```python
        native_memory_policy = self.settings.get("native_memory_policy")
        if native_memory_policy is not None and (
            not isinstance(native_memory_policy, str)
            or native_memory_policy not in _NATIVE_MEMORY_POLICIES
        ):
            raise ValueError("agent.settings.native_memory_policy must be 'preserve' or 'clear'")
```

After `TextMemoryArtifactConfig`, add:

```python
class ParametricMemoryArtifactConfig(_StrictModel):
    enabled: bool = False
    method: str = "parametric_memory_register"
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("method")
    @classmethod
    def _strip_method(cls, value: str) -> str:
        return _strip_non_empty(value, "artifacts.parametric_memory.method")
```

Update `ArtifactControls`:

```python
class ArtifactControls(_StrictModel):
    agent_system: AgentSystemArtifactConfig = Field(default_factory=AgentSystemArtifactConfig)
    text_memory: TextMemoryArtifactConfig = Field(default_factory=TextMemoryArtifactConfig)
    parametric_memory: ParametricMemoryArtifactConfig = Field(
        default_factory=ParametricMemoryArtifactConfig
    )
    skill_bundle: SkillBundleArtifactConfig = Field(default_factory=SkillBundleArtifactConfig)
```

- [ ] **Step 4: Implement compiler support**

In `src/openevo/experiment/compiler.py`, update:

```python
_EVOLUTION_ORDER = ("text_memory", "parametric_memory", "skill_bundle", "agent_system")
_ROLLOUT_CONTEXT_ARTIFACT_TYPES = _EVOLUTION_ORDER
```

Replace `_compile_method_spec()` with this structure:

```python
def _compile_method_spec(
    config: ExperimentConfig,
    *,
    artifact_type: str,
    round_index: int,
    reflector_llm: dict[str, str],
) -> CompiledEvolutionMethodSpec | None:
    base_config: dict[str, Any] = {}
    if artifact_type == "text_memory":
        if not config.artifacts.text_memory.enabled:
            return None
        method = config.artifacts.text_memory.method
        base_config["reflector_llm"] = dict(reflector_llm)
    elif artifact_type == "parametric_memory":
        if not config.artifacts.parametric_memory.enabled:
            return None
        method = config.artifacts.parametric_memory.method
        base_config.update(config.artifacts.parametric_memory.config)
    elif artifact_type == "skill_bundle":
        if not config.artifacts.skill_bundle.enabled:
            return None
        method = config.artifacts.skill_bundle.method
        base_config["reflector_llm"] = dict(reflector_llm)
    elif artifact_type == "agent_system":
        if not config.artifacts.agent_system.enabled:
            return None
        method = _resolve_agent_system_method(config.artifacts.agent_system.method, round_index)
        base_config["reflector_llm"] = dict(reflector_llm)
        base_config["target_path"] = config.artifacts.agent_system.target_path
    else:
        raise ValueError(f"Unsupported artifact_type: {artifact_type}")

    return CompiledEvolutionMethodSpec(
        artifact_type=artifact_type,
        method=method,
        job_type=method,
        config=base_config,
    )
```

- [ ] **Step 5: Run compiler tests**

Run:

```bash
pytest tests/openevo/test_experiment_compiler.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

Run:

```bash
git status --short
git add tests/openevo/test_experiment_compiler.py src/openevo/experiment/models.py src/openevo/experiment/compiler.py
git commit -m "feat: add openevo parametric memory controls"
```

Only stage the three listed files.

## Task 3: Track Parametric Memory In OpenEvo Runner Context

**Files:**
- Modify: `tests/openevo/test_experiment_runner.py`
- Modify: `src/openevo/experiment/runner.py`

- [ ] **Step 1: Write failing runner tests**

Update `test_dry_run_emits_three_evolution_jobs_per_task_round` name and assertions:

```python
def test_dry_run_emits_default_evolution_jobs_per_task_round() -> None:
    plan = dry_run_experiment(_config(), rounds_override=2)

    rounds = plan["tasks"][0]["rounds"]

    assert len(rounds) == 2
    assert [job["method"] for job in rounds[0]["evolution_jobs"]] == [
        "text_memory_reflector",
        "skill_bundle_reflector",
        "agent_system_reflector",
    ]
    assert [job["method"] for job in rounds[1]["evolution_jobs"]] == [
        "text_memory_reflector",
        "skill_bundle_reflector",
        "agent_system_history_reflector",
    ]
```

Add this test after `test_dry_run_shows_multi_round_context_placeholders`:

```python
def test_dry_run_tracks_parametric_memory_placeholders_when_enabled() -> None:
    plan = dry_run_experiment(
        _config(
            artifacts={
                "text_memory": {"enabled": True},
                "parametric_memory": {
                    "enabled": True,
                    "config": {
                        "adapter_uri": "file:///adapters/parser-memory",
                        "base_model": "gpt-5.1-codex-mini",
                    },
                },
                "skill_bundle": {"enabled": False},
                "agent_system": {"enabled": False},
            }
        ),
        rounds_override=2,
    )

    round_0, round_1 = plan["tasks"][0]["rounds"]

    assert [job["method"] for job in round_0["evolution_jobs"]] == [
        "text_memory_reflector",
        "parametric_memory_register",
    ]
    assert round_1["rollout_payload"]["metadata"]["evolution"][
        "context_artifact_ids"
    ] == [
        "<text_memory_artifact:component-extraction-train:round-0>",
        "<parametric_memory_artifact:component-extraction-train:round-0>",
    ]
```

Add this test after `test_live_runner_rollouts_use_only_latest_evolved_artifacts`:

```python
def test_live_runner_tracks_latest_parametric_memory_artifacts(tmp_path: Path) -> None:
    rollout = FakeRolloutClient()
    worker = UniqueArtifactWorkerRunner()

    result = run_experiment(
        _config(
            artifacts={
                "text_memory": {"enabled": True},
                "parametric_memory": {
                    "enabled": True,
                    "config": {
                        "adapter_uri": "file:///adapters/parser-memory",
                        "base_model": "gpt-5.1-codex-mini",
                    },
                },
                "skill_bundle": {"enabled": False},
                "agent_system": {"enabled": False},
            }
        ),
        rounds_override=2,
        output_dir=tmp_path / "run",
        rollout_client=rollout,
        evolution_client=FakeEvolutionClient(),
        worker_runner=worker,
        poll_interval_seconds=0.0,
        max_poll_attempts=1,
    )

    second_context = rollout.submitted[1]["metadata"]["evolution"]["context_artifact_ids"]

    assert result["status"] == "completed"
    assert second_context == [
        "text_memory_reflector-artifact-1",
        "parametric_memory_register-artifact-1",
    ]
    assert result["tasks"][0]["rounds"][0]["artifact_ids"]["parametric_memory"] == [
        "parametric_memory_register-artifact-1"
    ]
```

Update `FakeWorkerRunner.__call__()` artifact map:

```python
            "parametric_memory_register": "artifact-parametric-memory",
```

Update `FakeEvolutionClient.get_artifact()` artifact type inference:

```python
            if "text-memory" in artifact_id
            else "parametric_memory"
            if "parametric-memory" in artifact_id or "parametric_memory" in artifact_id
            else "skill_bundle"
```

- [ ] **Step 2: Run runner tests to verify they fail**

Run:

```bash
pytest tests/openevo/test_experiment_runner.py -k "parametric_memory or default_evolution_jobs or multi_round_context" -q
```

Expected: parametric tests fail because runner context maps do not include `parametric_memory`.

- [ ] **Step 3: Implement runner context support**

In `src/openevo/experiment/runner.py`, update `_empty_context_artifact_ids()`:

```python
def _empty_context_artifact_ids() -> dict[str, list[str]]:
    return {
        "dataset": [],
        "text_memory": [],
        "parametric_memory": [],
        "skill_bundle": [],
        "agent_system": [],
    }
```

In `_run_compiled_experiment()`, replace both manually initialized context dictionaries and `next_rollout_context_artifact_ids` with `_empty_context_artifact_ids()`:

```python
        history_context_artifact_ids = _empty_context_artifact_ids()
        rollout_context_artifact_ids = _empty_context_artifact_ids()
```

and later:

```python
            next_rollout_context_artifact_ids = _empty_context_artifact_ids()
```

- [ ] **Step 4: Run runner tests**

Run:

```bash
pytest tests/openevo/test_experiment_runner.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

Run:

```bash
git status --short
git add tests/openevo/test_experiment_runner.py src/openevo/experiment/runner.py
git commit -m "feat: track parametric memory in openevo runner"
```

Only stage the two listed files.

## Task 4: Make Context Artifact Ids A Strict Allowlist

**Files:**
- Modify: `tests/evolution/test_artifacts_context.py`
- Modify: `src/polar_evolution/store.py`

- [ ] **Step 1: Write failing resolver regression**

In `test_context_resolver_honors_explicit_context_artifact_ids`, replace:

```python
    assert context.adapter_merge_spec.adapters[0]["artifact_id"] == adapter.artifact_id
```

with:

```python
    assert context.adapter_merge_spec.adapters == []
    assert adapter.artifact_id not in context.selection["artifact_ids"]
```

Then add an explicit adapter allowlist request:

```python
    adapter_context = store.resolve_context(
        ContextResolveRequest(
            task_id="task-a",
            instruction="continue task",
            base_model="gpt-5.1-codex-mini",
            metadata={
                "task_tags": ["openevo_run_task:run-1:task-a"],
                "evolution": {
                    "context_artifact_ids": [
                        latest_memory.artifact_id,
                        adapter.artifact_id,
                    ]
                },
            },
        )
    )

    assert adapter_context.memory["artifact_ids"] == [latest_memory.artifact_id]
    assert adapter_context.adapter_merge_spec.adapters[0]["artifact_id"] == adapter.artifact_id
```

- [ ] **Step 2: Run resolver test to verify it fails**

Run:

```bash
pytest tests/evolution/test_artifacts_context.py::test_context_resolver_honors_explicit_context_artifact_ids -q
```

Expected: FAIL because `_artifact_id_allowed()` currently allows all `parametric_memory` when any context allowlist is present.

- [ ] **Step 3: Implement strict allowlist**

In `src/polar_evolution/store.py`, replace `_artifact_id_allowed()` with:

```python
def _artifact_id_allowed(
    row: dict[str, object],
    requested_artifact_ids: set[str] | None,
) -> bool:
    if requested_artifact_ids is None:
        return True
    artifact_id = row.get("artifact_id")
    return isinstance(artifact_id, str) and artifact_id in requested_artifact_ids
```

- [ ] **Step 4: Run context tests**

Run:

```bash
pytest tests/evolution/test_artifacts_context.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

Run:

```bash
git status --short
git add tests/evolution/test_artifacts_context.py src/polar_evolution/store.py
git commit -m "fix: enforce strict context artifact allowlists"
```

Only stage the two listed files.

## Task 5: Preserve Parametric Memory Metadata

**Files:**
- Modify: `tests/evolution/test_worker_methods.py`
- Modify: `src/polar_evolution/methods.py`

- [ ] **Step 1: Write failing method test**

Add this test after `test_parametric_memory_register_preserves_configured_adapter_id`:

```python
def test_parametric_memory_register_preserves_routing_metadata(tmp_path):
    adapter_dir = tmp_path / "adapter"
    adapter_dir.mkdir()
    job = _job(
        "parametric_memory_register",
        tmp_path,
        config={
            "adapter_uri": adapter_dir.as_uri(),
            "base_model": "Qwen/Qwen3.6-27B",
            "compatibility": {
                "base_model": ["Qwen/Qwen3.6-27B"],
                "task_tags": ["terminal-bench"],
            },
            "lineage": {"input_artifact_ids": ["dataset_1"]},
            "scores": {"quality": 0.82, "heldout_reward_delta": 0.1},
        },
    )

    artifacts = run_method(job, artifact_root=tmp_path / "artifacts")

    artifact = artifacts[0]
    assert artifact.compatibility == {
        "base_model": ["Qwen/Qwen3.6-27B"],
        "task_tags": ["terminal-bench"],
    }
    assert artifact.lineage == {"input_artifact_ids": ["dataset_1"]}
    assert artifact.scores == {"quality": 0.82, "heldout_reward_delta": 0.1}
```

- [ ] **Step 2: Run method test to verify it fails**

Run:

```bash
pytest tests/evolution/test_worker_methods.py::test_parametric_memory_register_preserves_routing_metadata -q
```

Expected: FAIL because `parametric_memory_register` does not yet pass through compatibility, lineage, or scores.

- [ ] **Step 3: Implement metadata passthrough**

In `src/polar_evolution/methods.py`, update the `ArtifactRegisterRequest` inside `parametric_memory_register()`:

```python
        ArtifactRegisterRequest(
            type=ArtifactType.PARAMETRIC_MEMORY,
            name=str(job.config.get("name") or adapter_id),
            uri=adapter_uri,
            manifest=manifest,
            lineage=_dict_config(job.config.get("lineage")),
            compatibility=_dict_config(job.config.get("compatibility")),
            scores=_scores_config(job.config.get("scores")),
            tags=_string_list(job.config.get("tags")),
            promoted=bool(job.config.get("promoted", False)),
        )
```

- [ ] **Step 4: Run worker method tests**

Run:

```bash
pytest tests/evolution/test_worker_methods.py -k "parametric_memory_register or parse_capabilities_defaults" -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 5**

Run:

```bash
git status --short
git add tests/evolution/test_worker_methods.py src/polar_evolution/methods.py
git commit -m "fix: preserve parametric memory routing metadata"
```

Only stage the two listed files.

## Task 6: Update Documentation And Run Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture/evolution-backend.md`
- Modify: `docs/architecture/evolution-runtime-context.md`

- [ ] **Step 1: Update README config documentation**

In `README.md`, update the OpenEvo experiment example to include:

```yaml
agent:
  settings:
    capture_mode: transcript
    native_memory_policy: preserve

artifacts:
  text_memory:
    enabled: true
    method: text_memory_reflector
  parametric_memory:
    enabled: false
    method: parametric_memory_register
    config: {}
  skill_bundle:
    enabled: false
  agent_system:
    enabled: false
```

Add a short paragraph:

```markdown
`native_memory_policy` controls harness-native memory only. For Codex, `clear`
removes `CODEX_HOME/memories/` and `CODEX_HOME/memories_*.sqlite*` while keeping
subscription auth state. Polar evolution memory is controlled separately through
`artifacts.text_memory` and `artifacts.parametric_memory`.
```

- [ ] **Step 2: Update architecture docs**

In `docs/architecture/evolution-backend.md`, add a note near the context resolver section:

```markdown
When `metadata.evolution.context_artifact_ids` is present, context resolution
treats it as a strict allowlist for every artifact type, including
`parametric_memory`. This is required for controlled ablations because promoted
compatible artifacts from other runs must not be injected unless the rollout
explicitly selected them.
```

In `docs/architecture/evolution-runtime-context.md`, add a note near the
parametric memory section:

```markdown
Parametric memory participates in the same explicit context allowlist as
textual memory, skills, and agent-system artifacts. If an OpenEvo rollout passes
`context_artifact_ids`, only listed adapter artifacts are converted into
`adapter_merge_spec`.
```

- [ ] **Step 3: Run focused tests**

Run:

```bash
pytest tests/test_evolution_agent_harnesses.py -q
pytest tests/openevo/test_experiment_compiler.py -q
pytest tests/openevo/test_experiment_runner.py -q
pytest tests/evolution/test_artifacts_context.py -q
pytest tests/evolution/test_worker_methods.py -k "parametric_memory_register or parse_capabilities_defaults" -q
pytest tests/gateway/test_server_parametric_memory.py -q
```

Expected: all commands PASS.

- [ ] **Step 4: Run patch checks**

Run:

```bash
git diff --check
git status --short
```

Expected: `git diff --check` has no output. `git status --short` shows only this task's doc files plus pre-existing unrelated dirty files.

- [ ] **Step 5: Review final diff**

Run:

```bash
git diff -- README.md docs/architecture/evolution-backend.md docs/architecture/evolution-runtime-context.md
git diff -- src/polar/agent/presets/codex.py src/openevo/experiment/models.py src/openevo/experiment/compiler.py src/openevo/experiment/runner.py src/polar_evolution/store.py src/polar_evolution/methods.py
git diff -- tests/test_evolution_agent_harnesses.py tests/openevo/test_experiment_compiler.py tests/openevo/test_experiment_runner.py tests/evolution/test_artifacts_context.py tests/evolution/test_worker_methods.py
```

Check that:

- `clear` removes only Codex memory paths and no auth/session files.
- `parametric_memory` is disabled by default.
- `context_artifact_ids` filters `parametric_memory`.
- No unrelated dirty files are staged.

- [ ] **Step 6: Commit Task 6**

Run:

```bash
git status --short
git add README.md docs/architecture/evolution-backend.md docs/architecture/evolution-runtime-context.md
git commit -m "docs: document openevo memory controls"
```

Only stage the three listed doc files.

## Final Verification

- [ ] **Step 1: Run full focused suite**

Run:

```bash
pytest tests/test_evolution_agent_harnesses.py \
  tests/openevo/test_experiment_compiler.py \
  tests/openevo/test_experiment_runner.py \
  tests/evolution/test_artifacts_context.py \
  tests/evolution/test_worker_methods.py \
  tests/gateway/test_server_parametric_memory.py -q
```

Expected: PASS.

- [ ] **Step 2: Run lint on touched Python files**

Run:

```bash
ruff check \
  src/polar/agent/presets/codex.py \
  src/openevo/experiment/models.py \
  src/openevo/experiment/compiler.py \
  src/openevo/experiment/runner.py \
  src/polar_evolution/store.py \
  src/polar_evolution/methods.py \
  tests/test_evolution_agent_harnesses.py \
  tests/openevo/test_experiment_compiler.py \
  tests/openevo/test_experiment_runner.py \
  tests/evolution/test_artifacts_context.py \
  tests/evolution/test_worker_methods.py
```

Expected: PASS.

- [ ] **Step 3: Confirm SSH push dry-run still works**

Run:

```bash
GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes -i /root/.ssh/id_ed25519' \
  git push --dry-run openevo HEAD:refs/heads/codex/openevo-experiment-runner
```

Expected: dry-run reports the branch update and does not print `Write access to repository not granted`.

- [ ] **Step 4: Final status review**

Run:

```bash
git status --short
git log --oneline -6
```

Expected: working tree may still show pre-existing unrelated user changes, but no uncommitted changes from this implementation. Recent commits should include the task commits above.
