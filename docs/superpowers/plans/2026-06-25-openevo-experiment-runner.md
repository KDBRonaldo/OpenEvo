# OpenEvo Experiment Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `openevo run experiment.yaml` as a user-facing wrapper that runs ordinary Polar tasks with agent-system, skill/tool, and text-memory evolution enabled by default.

**Architecture:** Add a thin `openevo` package that parses a compact experiment config, compiles it into explicit rollout/evolution operations, and reuses existing Polar rollout and Polar Evolution APIs. Add LLM reflector worker methods for `text_memory_reflector` and `skill_bundle_reflector`; keep tools inside skill bundles. Do not change the underlying `polar submit` or worker protocols.

**Tech Stack:** Python 3.11, Pydantic v2, YAML, httpx, pytest, existing `polar_evolution` models/worker/method registry.

---

## Task 1: Add `openevo` Package Skeleton And Config Models

**Files:**
- Create `src/openevo/__init__.py`
- Create `src/openevo/cli.py`
- Create `src/openevo/experiment/__init__.py`
- Create `src/openevo/experiment/models.py`
- Update `pyproject.toml`
- Add `tests/openevo/test_experiment_models.py`

**Implementation:**
- Add console script:
  - `openevo = "openevo.cli:main"`
- Add setuptools package discovery include:
  - `openevo*`
- Define Pydantic config models that accept this minimal YAML:

```yaml
version: 1
experiment:
  name: biology-components
agent:
  preset: codex
  model: gpt-5.1-codex-mini
tasks:
  - id: component-extraction-train
    instruction: Extract biological components into final_components.json.
    workspace: /root/codex54minitest/five_article_agentic_workflow_subset
```

- Supported artifact controls:
  - `artifacts.agent_system.enabled` default `true`
  - `artifacts.agent_system.method` default `"auto"`
  - `artifacts.agent_system.target_path` default `"AGENTS.md"`
  - `artifacts.text_memory.enabled` default `true`
  - `artifacts.text_memory.method` default `"text_memory_reflector"`
  - `artifacts.skill_bundle.enabled` default `true`
  - `artifacts.skill_bundle.method` default `"skill_bundle_reflector"`
- Supported runtime/service defaults:
  - `rollout.url`: `$OPENEVO_ROLLOUT_URL` else `http://127.0.0.1:8080`
  - `evolution.backend_url`: `$OPENEVO_EVOLUTION_URL` else `http://127.0.0.1:8200`
  - `evolution.rounds`: default `1`
  - `evolution.worker.mode`: default `"local_once"`
  - `agent.auth`: default `"proxy"`
  - `runtime.kind`: default `"docker"`
  - `runtime.workdir`: default `"/polar/session/workspace"`
- Implement `load_experiment_config(path: Path) -> ExperimentConfig`.

**Tests:**
- Minimal YAML loads with all defaults.
- Environment URL defaults are honored.
- `rounds` must be at least 1.
- Unknown top-level keys are rejected or surfaced clearly.

**Run:**
```bash
uv run pytest tests/openevo/test_experiment_models.py
```

---

## Task 2: Compile Compact Config Into Explicit Operations

**Files:**
- Create `src/openevo/experiment/compiler.py`
- Add `tests/openevo/test_experiment_compiler.py`

**Implementation:**
- Define `CompiledExperiment` with:
  - `experiment_id`
  - `round_count`
  - `tasks: list[CompiledTask]`
  - resolved service URLs
  - ordered evolution method specs
- Define `CompiledTask` with:
  - `task_id`
  - `policy_version_for_round(round_index: int) -> str`
  - `rollout_payload_for_round(round_index, context_artifact_ids) -> dict`
- Compile ordinary Polar task payloads using existing concepts:
  - `task_id`
  - `instruction`
  - `agent`
  - `runtime`
  - `metadata`
- Always put deterministic task-round policy versions in rollout metadata and dataset query:
  - `openevo:{experiment_name}:{task_id}:round-{round_index}`
- Implement method auto resolution:
  - agent-system `"auto"` resolves to `"agent_system_history_reflector"` when previous dataset/artifact context exists, else `"agent_system_reflector"`.
  - text memory default resolves to `"text_memory_reflector"`.
  - skill bundle default resolves to `"skill_bundle_reflector"`.
- Evolution order per round:
  - create dataset from completed rollout events for exact task-round policy version
  - text memory job
  - skill bundle job
  - agent system job
- Include `reflector_llm` defaults:
  - `model` = `agent.model`
  - `provider` = `"codex_cli"` when `agent.auth == "subscription"` or `agent.provider == "codex_cli"`, otherwise `"openai_chat"`

**Tests:**
- Compiled policy versions are deterministic and per task/round.
- No dataset query uses a broad/latest fallback.
- Evolution order is text memory, skill bundle, agent system.
- Agent-system `"auto"` resolves differently for first vs later rounds.
- Subscription agents default reflector provider to `codex_cli`.

**Run:**
```bash
uv run pytest tests/openevo/test_experiment_compiler.py
```

---

## Task 3: Add Text Memory And Skill Bundle Reflector Methods

**Files:**
- Update `src/polar_evolution/methods.py`
- Update `tests/evolution/test_worker_methods.py`

**Implementation:**
- Add `text_memory_reflector(job, artifact_root)`.
  - Requires one dataset artifact.
  - Reads records with existing `_read_dataset_records`.
  - Renders a prompt focused on reusable task memory, recurring failure modes, and validation habits.
  - Uses the existing audited/generic reflector LLM path where practical.
  - Writes `memory.md`.
  - Registers artifact type `TEXT_MEMORY`.
  - Manifest includes method, source dataset, record counts, reflector provider/model.
- Add `skill_bundle_reflector(job, artifact_root)`.
  - Requires one dataset artifact.
  - Optionally reads current skill bundle input artifact as base skill.
  - Renders a prompt for a Codex skill bundle, including tools/helper scripts as files inside the bundle when present in the LLM output contract.
  - First version may only create `SKILL.md`; manifest must keep `files: ["SKILL.md"]`.
  - Registers artifact type `SKILL_BUNDLE`.
- Add both methods to `METHOD_REGISTRY`.
- Reuse the hardened Codex CLI reflection path:
  - sandbox `read-only`
  - shell tool disabled
  - temp cwd
- Avoid leaking dataset-specific literals into generic skill/memory prompts when audit config provides forbidden literals.

**Tests:**
- `run_method` can produce text-memory artifact from fixture dataset using monkeypatched LLM output.
- `run_method` can produce skill-bundle artifact from fixture dataset using monkeypatched LLM output.
- Registry contains both new methods.
- Generated manifests record method/provider/model/source dataset.

**Run:**
```bash
uv run pytest tests/evolution/test_worker_methods.py -k "text_memory_reflector or skill_bundle_reflector or registry"
```

---

## Task 4: Implement Runner, Clients, Dry Run, And CLI

**Files:**
- Create `src/openevo/experiment/clients.py`
- Create `src/openevo/experiment/runner.py`
- Update `src/openevo/cli.py`
- Add `tests/openevo/test_experiment_runner.py`
- Add `tests/openevo/test_cli.py`

**Implementation:**
- `openevo run CONFIG` flags:
  - `--dry-run`
  - `--output-dir`
  - `--task-id`
  - `--rounds`
  - `--json`
- `--dry-run`:
  - prints or writes the compiled plan
  - includes rollout payloads, dataset create requests, job create requests, method order
  - never contacts services
- Live runner:
  - submit rollout task to `POST /rollout/task/submit`
  - poll `GET /rollout/task/{task_id}` until `completed` or `failed`
  - create dataset via `POST /v1/datasets` with exact policy version query
  - create jobs via `POST /v1/jobs`
  - if worker mode is `local_once`, call existing `polar_evolution.worker.run_once` with method capabilities until the expected job count for that round is processed or a bounded no-job loop is reached
- Result summary:
  - output JSON contains experiment name, task ids, round statuses, dataset ids, job ids, and artifact ids if worker completion response returns them.
  - human output summarizes per task/round.
- Keep HTTP clients small and testable; use injected fake clients in tests.

**Tests:**
- Dry run emits three evolution job specs per task/round.
- `--task-id` filters compiled tasks.
- `--rounds` overrides config rounds.
- Fake live runner calls rollout, dataset creation, job creation, and local worker in the correct order.
- CLI returns nonzero with a clear error on invalid config.

**Run:**
```bash
uv run pytest tests/openevo/test_experiment_runner.py tests/openevo/test_cli.py
```

---

## Task 5: Documentation And Full Verification

**Files:**
- Update `README.md`
- Optionally add `examples/openevo/experiment.yaml`

**Implementation:**
- Document `openevo run` minimal config.
- Explain that technical URLs and method names have defaults.
- Explain first-version scope:
  - ordinary Polar tasks
  - Terminal Bench should be represented as many explicit tasks for now
  - tool evolution is represented by helper files inside skill bundles

**Run:**
```bash
uv run pytest tests/openevo tests/evolution/test_worker_methods.py -k "reflector or registry"
uv run ruff check src/openevo src/polar_evolution/methods.py tests/openevo tests/evolution/test_worker_methods.py
openevo run examples/openevo/experiment.yaml --dry-run --json
```

---

## Notes For Subagents

- Do not change Polar rollout or worker API schemas unless a test proves it is necessary.
- Do not implement a Terminal Bench task generator in this pass; explicit task entries are enough.
- Do not add a separate tool artifact type; tool files belong inside skill bundles.
- Keep generated artifacts generic and method-level. They may mention task categories, validation checks, and workflow rules, but should not hardcode held-out task IDs, table names, article titles, or exact expected outputs.
- Prefer small modules with pure functions so the main agent can review and merge incrementally.
