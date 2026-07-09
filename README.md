# OpenEvo: Core, Desktop, and Dev Kit

OpenEvo is an agent-system evolution product with three public surfaces:

- **OpenEvo Core**: the execution, dataset, job, artifact, method, context, and
  runtime contract used by all OpenEvo workflows.
- **OpenEvo Desktop**: the ordinary-user science UI for preparing and running
  OpenEvo tasks.
- **OpenEvo Dev Kit**: the developer wrapper for CLI, source, test, benchmark,
  method-development, artifact-inspection, and regression-fixture workflows.

The current Core focus is to turn completed trajectories or transcripts into
safer, auditable updates to `AGENTS.md`, then feed those updates back into later
rollouts without leaking held-out answers.

The original lower-level framework README is preserved as
[README.polar.md](README.polar.md). Runtime/data identity now uses the OpenEvo
contract: `OPENEVO_*`, `/openevo/session`, and `.openevo/evolution`.
Lower-level evolution backend usage lives in
[src/openevo/evolution/README.md](src/openevo/evolution/README.md).

## Architecture

```text
external harness or OpenEvo Core rollout
        |
        v
trajectory / transcript capture
        |
        v
Core events -> dataset artifact -> evolution job
        |
        v
method backend
        |
        v
typed artifacts: agent_system, skill_bundle, text_memory, parametric_memory
        |
        v
context resolver / harness injection -> next rollout
        |
        v
task evaluator -> sanitized feedback -> next evolution job
```

Main components:

- **Capture layer**: Core proxy traces support token-level data. Subscription or
  external harness runs use pure-text transcript capture with
  `token_level_metrics_available=false`.
- **Offline bridges**: Terminal Bench trial/job directories can be converted into
  Core events and datasets while excluding oracle solutions, reference patches,
  secrets, and protected literals.
- **Evolution backend**: datasets, jobs, leases, artifacts, lineage, compatibility
  filters, and context resolution are handled by the Core EvolutionStore/API.
- **Algorithm backends**: methods in `src/openevo/evolution/methods.py` consume
  dataset artifacts and produce typed artifacts.
- **Evaluators**: task-level evaluators live outside specific methods. For
  ground-truth tasks, they produce sanitized method feedback and leakage guards
  instead of exposing raw answers to the reflector.
- **Runtime injection**: promoted artifacts are resolved by explicit context IDs
  plus compatibility filters, then staged into the next agent session, for
  example as `AGENTS.md` for Codex or as Terminal Bench Harbor agent
  instructions.

## Implemented Algorithms

| Method | Artifact | Status | Purpose |
|---|---|---:|---|
| `agent_system` | `agent_system` | implemented | Baseline/manual registration of an existing agent-system file. |
| `agent_system_reflector` | `agent_system` | implemented | LLM-based reflector over one dataset artifact with audit and repair. |
| `agent_system_history_reflector` | `agent_system` | implemented | Reflects over multiple rounds, preserves round metrics, and marks regressions. |
| `agent_system_pareto_reflector` | `agent_system` | implemented | Generates multiple strategy candidates, records a candidate archive, and applies promotion gates. |
| `agent_system_gepa_reflector` | `agent_system` | implemented | GEPA-style closed-loop candidate generation for per-task evolution. |
| `text_memory` | `text_memory` | implemented | Distills successful records into Markdown memory. |
| `text_memory_reflector` | `text_memory` | implemented | LLM-based reflector that turns trajectories into reusable Markdown memory. |
| `text_memory_expel_reflector` | `text_memory` | implemented | ExpeL/Reflexion-style memory synthesis with required Do/Avoid/Validate sections. |
| `skill_bundle` | `skill_bundle` | implemented | Registers harness-loadable skill directories. |
| `skill_bundle_reflector` | `skill_bundle` | implemented | LLM-based reflector that writes a Codex `SKILL.md`; helper tools are represented as files inside the skill bundle. |
| `parametric_memory_register` | `parametric_memory` | implemented | Registers external adapter artifacts for later trainer/inference use. |
| `parametric_memory_lora_sft` | `parametric_memory` | implemented | Exports successful trajectories to SFT JSONL, invokes an external LoRA trainer, and registers the adapter. |

## OpenEvo Backend Runner

`openevo-backend run` is the server-side experiment runner used by OpenEvo
Core and Desktop-managed remote sessions. It uses technical defaults for
service URLs and evolution methods. A task with a workspace must also name the
runtime image because Core cannot upload a workspace into an implicit/default
runtime:

```yaml
version: 1
experiment:
  name: biology-components
agent:
  preset: codex
  model: gpt-5.1-codex-mini
  auth: subscription
  settings:
    capture_mode: transcript
    native_memory_policy: preserve
runtime:
  image: my-task-image:latest
tasks:
  - id: component-extraction-train
    instruction: Extract biological components into final_components.json.
    workspace: /root/codex54minitest/five_article_agentic_workflow_subset
artifacts:
  text_memory:
    enabled: true
    method: text_memory_expel_reflector
  parametric_memory:
    enabled: false
    method: parametric_memory_register
    config: {}
  skill_bundle:
    enabled: false
  agent_system:
    enabled: false
```

`native_memory_policy` controls harness-native memory only. For Codex, `clear`
removes `CODEX_HOME/memories/` and `CODEX_HOME/memories_*.sqlite*` while keeping
subscription auth state. Core evolution memory is controlled separately through
`artifacts.text_memory` and `artifacts.parametric_memory`.

Textual memory is rendered into the agent instruction and works in both
proxy/local inference runs and transcript-only subscription runs. Parametric
memory requires proxy/local inference because subscription harnesses cannot
select serving-time adapters. Subscription experiment config rejects
`parametric_memory`, and context resolution skips adapter artifacts when the
request auth mode is subscription. To test parametric memory, use
`agent.auth: proxy` and enable only the memory artifact controls needed for the
ablation:

```yaml
agent:
  preset: codex
  auth: proxy
  model: Qwen/Qwen3.6-35B-A3B
artifacts:
  text_memory:
    enabled: true
  parametric_memory:
    enabled: true
    method: parametric_memory_lora_sft
    config:
      base_model: Qwen/Qwen3.6-35B-A3B
      output_adapter_id: memory-lora
      training_projection:
        type: response_tail
        response_tail_chars: 4096
      trainer:
        command: python
        args:
          - /opt/polar/train_lora.py
          - --train-file
          - "{training_dataset}"
          - --output-dir
          - "{adapter_dir}"
  skill_bundle:
    enabled: false
  agent_system:
    enabled: false
```

Task IDs are used as rollout polling URL path segments, so they must be stable
slugs and cannot contain `/`.

Defaults:

- `rollout.url`: `$OPENEVO_ROLLOUT_URL` or `http://127.0.0.1:8080`
- `evolution.backend_url`: `$OPENEVO_EVOLUTION_URL` or `http://127.0.0.1:8200`
- `evolution.rounds`: `1`
- `runtime.kind`: `docker`; `runtime.workdir`: `/openevo/session/workspace`
- `runtime.image`: required when any task sets `workspace`, or when overriding
  runtime fields such as `runtime.env`, `runtime.kind`, or `runtime.workdir`
- enabled artifacts: `text_memory_reflector`, `skill_bundle_reflector`, and
  agent-system `auto`; `parametric_memory` is available but disabled by default
- subscription agents use sandboxed `codex_cli` reflection by default; proxy
  agents use OpenAI-compatible chat reflection by default
- subscription agents require explicit transcript capture. Supported
  `agent.settings.capture_mode` values include `transcript`, `agent_transcript`,
  and `pure_text`.
- `evolution.promotion_gate.mode`: `none` by default. Set it to `human` or `llm`
  to keep evolved artifacts unpromoted until a runner/backend-level gate approves
  them.

Promotion gates run after an evolution worker registers artifacts and before
OpenEvo adds those artifact IDs to the next rollout. With a gate enabled, jobs
are created with `promoted=false`; approved artifacts are promoted through the
backend artifact API. Algorithms are expected to put support material in
`manifest.promotion_support`:

```yaml
evolution:
  rounds: 3
  promotion_gate:
    mode: human
    human_input: auto
    # For mode: llm, also set min_score and llm.model.
    artifact_types: [agent_system, skill_bundle, text_memory]
    require_support: true
    max_artifact_content_chars: 12000
```

Required support fields are `trajectory_findings`, `proposed_changes`,
`expected_benefits`, `risks`, and `validation_checks`. Built-in reflectors write
these fields automatically; custom algorithms should make them specific to the
trajectory problems they found and the changes they made. The backend forces
outputs from jobs with `config.promoted=false` to remain unpromoted even if a
worker submits `promoted=true`. The human gate writes review packets under
`promotion_reviews/`; for multi-candidate jobs it writes every packet in the
review set before waiting for `<artifact_id>.decision.json` files containing
`{"approved": true}` plus an optional finite `0 <= score <= 1`. The wait is
bounded by one shared `decision_timeout_seconds` window for the full review set
and polls every `decision_poll_interval_seconds`; malformed or partially written
decision JSON stays pending until a valid decision appears or the timeout
expires. Set the timeout to `0` to emit packets and return `pending_review`
immediately. `human_input: auto` uses an interactive terminal prompt when stdin
and stdout are TTYs, and otherwise falls back to decision files; set
`human_input: file` to force file review or `human_input: tui` to require a
terminal prompt. Human decisions can also include structured `human_feedback`
with `observed_issues`, `suggested_changes`, `risks`, and `validation_checks`;
the gate preserves this feedback in promotion reviews for later inspection or
follow-up evolution, while promotion still depends on `approved` and the score
contract.
When the evolution backend exposes HITL review APIs, the runner also creates
durable asynchronous review requests. If query-decision recording is available,
the runner embeds the current deterministic `ask_human` policy payload in the
review request so the backend can create and link the `query_decision_id`
atomically. Validated, redacted feedback can later enter datasets as
`evolution_feedback.human` and be consumed by methods; raw reviewer payloads stay
audit-only. Backend review APIs sanitize packets before
hashing/storage and sanitize normalized feedback before it becomes available for
resume or evolution; resume applies feedback only when the review request artifact
hash still matches the current artifact. See
`docs/architecture/evolution-backend.md` and
`docs/architecture/evolution-api-and-method-integration.md` for the full HITL
lifecycle.
Review packets include a bounded `file://` artifact content excerpt, so human and
LLM reviewers can inspect the generated `memory.md`, `AGENTS.md`, or `SKILL.md`
content alongside the support material. The runner only reads artifact content
from its artifact output root; `file://` URIs outside that root are marked
unavailable in the review packet. Review packets sanitize artifact metadata URI
and absolute local path fields before they are sent to an LLM reviewer or backend
review API, removing local `file://` paths, userinfo, fragments, and query
strings from top-level and nested manifest URI values, including relative URI
references. The LLM gate sends the sanitized review packet to the configured
reviewer and requires `approved=true` plus a
present numeric score satisfying finite `0 <= score <= 1` and
`score >= min_score`; missing or non-numeric scores are rejected. Promotion
updates must call `PATCH /v1/artifacts/{artifact_id}/promotion` with an explicit
`{"promoted": true}` or `{"promoted": false}` body; empty payloads are rejected.
If a method emits multiple candidate artifacts, the gate can partially approve
the set: approved candidates are promoted and rejected candidates are left
unpromoted. If a gated job produces no artifact of the expected type, the gate
rejects it as `missing_target_artifact`.

Useful commands:

```bash
openevo-backend run examples/openevo/experiment.yaml --dry-run --json
openevo-backend run examples/openevo/experiment.yaml --rounds 3 --output-dir runs/biology
openevo-backend run examples/openevo/experiment.yaml --task-id task-a --task-id task-b
```

Without `--output-dir`, live runs write summaries and worker artifacts under a
run-scoped directory: `.openevo/runs/<experiment>/<run_id>/`.

Terminal Bench can use this runner by expressing each benchmark case as an
explicit task entry. A task generator is intentionally left out of the first
version; tool evolution is handled as helper files inside `skill_bundle`
artifacts rather than as a separate artifact type.

Benchmark adapters belong in OpenEvo Dev Kit. They should translate benchmark
tasks and results into Core records, datasets, metrics, jobs, artifacts, and
context inputs rather than implementing a separate evolution backend or method
registry.

## OpenEvo Desktop Release and Dev Kit Launchers

The ordinary-user macOS release artifact is the OpenEvo Desktop `.dmg` produced
by the Tauri release workflow. A scientist should install that app, configure a
remote server inside Desktop, and avoid Python packaging details in the common
path.

The installable Python distribution is still named `openevo`. It bundles the
OpenEvo Desktop web shell, OpenEvo Core Backend modules, the exact
remote-install wheel used by Desktop bootstrap, and runtime modules for rollout,
gateway, trajectory, and evolution backend implementation details. The only
console script exposed by the Python package in this phase is the backend
launcher:

```bash
openevo-backend --help
openevo-backend serve --help
```

`openevo-backend serve` starts the typed Core Backend API for Desktop and remote
operation. The current API scaffold exposes health, status, environment,
project, run, artifact, service, and capabilities routes; later productization
phases connect those routes to the full remote supervisor.

Release wheels are built from the OpenEvo Core package metadata and exclude
Desktop facade code and Desktop static assets. OpenEvo-only Desktop assets are
kept under the top-level `desktop/packaging/web/` path for Desktop release and
smoke validation.
The `OpenEvo release artifact` GitHub Actions workflow runs the audited release
smoke path on `v*` tags and manual dispatch, then uploads both the exact
`openevo-wheel` artifact and the `openevo-desktop-dmg` macOS artifact.
The `Publish OpenEvo to PyPI` workflow uses PyPI trusted publishing through
`pypa/gh-action-pypi-publish@release/v1` and runs when a GitHub release is
published. Before the first publish, configure the PyPI trusted publisher for
project `openevo`, repository `CompLifeLab-ZJU/OpenEvo`, workflow
`.github/workflows/openevo-publish-pypi.yml`, and environment `pypi`.

Before publishing a wheel or release artifact, run the release smoke flow on
Node 22, refresh the top-level Desktop assets, and validate the installed Core
wheel. The installed-wheel smoke serves `/openevo` from `desktop/packaging/web`
while loading `openevo` from the installed wheel, then exercises the
config-backed Desktop lifecycle in dry-run mode, including Core capabilities,
method metadata, project config save, workspace, bootstrap, services, service
status, run launch, artifact summary parsing, and artifact content reading:

```bash
cd web
npm ci
npm audit --audit-level=high
npm test -- --run
npm run build:openevo
cd ..
diff -qr web/dist desktop/packaging/web
rm -rf .openevo-remote-wheel src/openevo/wheels
python -m build --wheel --outdir .openevo-remote-wheel
mkdir -p src/openevo/wheels
cp .openevo-remote-wheel/openevo-*.whl src/openevo/wheels/
rm -rf dist
python -m build --wheel
python scripts/ci/check_openevo_release.py --wheel dist/*.whl
python -m venv .openevo-wheel-smoke
.openevo-wheel-smoke/bin/python -m pip install --upgrade pip
.openevo-wheel-smoke/bin/python -m pip install dist/*.whl
.openevo-wheel-smoke/bin/openevo-backend --help
.openevo-wheel-smoke/bin/openevo-backend serve --help
.openevo-wheel-smoke/bin/openevo-backend run --help
PYTHONPATH=. .openevo-wheel-smoke/bin/python scripts/ci/smoke_openevo_desktop_wheel.py
```

For the focused OpenEvo Python regression check used by CI:

```bash
ruff check src/openevo tests/openevo
PYTHONPATH=src:. python -m pytest tests/ci/test_openevo_python_workflow.py tests/openevo -q
```

Shared infrastructure that is already implemented:

- Golden-standard evaluator for sequence/component extraction: article-scoped
  TP/FP/FN, precision, recall, F1, duplicate counting, and leakage checks.
- Terminal Bench transcript bridge and per-task evolution runner.
- LLM reflector providers for OpenAI-compatible HTTP APIs and sandboxed Codex CLI
  subscription-mode reflection.
- Agent-system audit/repair pass to catch held-out literals and over-specific
  updates before artifact registration.

## Internal Development Evidence

Local benchmark and science-run snapshots are tracked as internal development
evidence, not as a published benchmark suite. See
[docs/dev/current-dataset-performance.md](docs/dev/current-dataset-performance.md)
for the current snapshot table and interpretation notes.

## Roadmap

- Make benchmark results first-class repo artifacts instead of ad hoc `/tmp`
  summaries.
- Add a stable biology 5-train/23-test split runner with canonical article-id
  mapping in the task description rather than hidden pipeline state.
- Add a Terminal Bench task-list generator on top of the new `openevo-backend run`
  config, while keeping explicit task entries as the stable interchange format.
- Extend runner/backend promotion policies with paired evaluator scores, leakage
  audit, regression limits, and candidate diversity.
- Improve transcript capture fidelity for external harnesses while preserving the
  no-oracle/no-secret boundary.
- Support multi-round evolution from all historical trajectories, not only the
  most recent round.
- Add dashboards for coverage collapse, repeated failure modes, and over-specific
  reflector updates.
- Integrate parametric-memory training and adapter promotion as a backend, not
  just artifact registration.

## Entry Points

- Backend launcher: `src/openevo/backend/launcher.py`
- Core Backend modules: `src/openevo/`
- Minimal runner config: `examples/openevo/experiment.yaml`
- Evolution method implementations: `src/openevo/evolution/methods.py`
- Golden-standard evaluator: `src/openevo/evolution/golden_standard.py`
- Terminal Bench bridge: `src/openevo/evolution/terminal_bench_bridge.py`
- Per-task Terminal Bench loop: `src/openevo/evolution/terminal_bench_per_task.py`
- Architecture docs: `docs/architecture/evolution-api-and-method-integration.md`
- Historical lower-level framework README: [README.polar.md](README.polar.md)
