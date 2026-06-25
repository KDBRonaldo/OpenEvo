# OpenEvo Experiment Runner Design

## Goal

Provide a user-friendly `openevo run experiment.yaml` entry point for new tasks
that should run through Polar with subscription-mode agents and multi-artifact
evolution. The runner is a thin orchestration layer: it compiles a high-level
experiment config into existing Polar rollout and evolution backend calls
instead of replacing those systems.

The first implementation must support three evolved artifact families:

- `agent_system`: harness instruction artifacts such as `AGENTS.md`.
- `text_memory`: natural-language memory distilled from prior trajectories.
- `skill_bundle`: loadable skill directories containing `SKILL.md` and optional
  helper files.

Tool evolution is represented as part of `skill_bundle`. A tool is a helper file
or script shipped inside a skill bundle and described by `SKILL.md`; the first
version does not introduce a separate `tool_bundle` artifact type.

## Scope

The initial runner supports ordinary Polar tasks, not a Terminal Bench-specific
job wrapper. Terminal Bench can still be represented as many independent Polar
tasks by listing each task explicitly in `tasks:`. A later generator may expand
Terminal Bench task ids into task entries, but that is not part of the first
implementation.

For each task, the runner will:

1. Validate the experiment config and run preflight checks.
2. Compile each task entry into a Polar `TaskRequest`.
3. Submit baseline round 0 to the rollout server and wait for completion.
4. Convert the completed trajectory/result into a task-scoped dataset artifact.
5. Run the configured evolution methods for memory, skill/tool, and agent system.
6. Submit the next round with evolved context available through the evolution
   backend.
7. Repeat until the configured round count is exhausted.
8. Write per-task and aggregate summaries.

The runner connects to already running services. It does not start rollout,
gateway, Docker, Apptainer, or the evolution backend itself.

## High-Level Config

The config is intentionally task-author oriented. It hides `TaskRequest`
internals such as `capture_mode`, `builder.strategy`, and evolution job details
when they can be derived safely.

Minimal subscription-mode config:

```yaml
experiment: my-subscription-evo

agent:
  harness: codex
  model: gpt-5.5
  auth: subscription

runtime:
  image: my-task-image:latest
  prepare:
    - npm install -g @openai/codex@0.121.0

eval:
  type: test_on_output
  command: pytest -q
  expected:
    tests: PASSED

tasks:
  - id: filter-js-from-html
    instruction: Fix the implementation so the tests pass.
    metadata:
      task_tags: ["terminal-bench", "filter-js-from-html"]

  - id: fix-git
    instruction: Fix the git workflow task.
    metadata:
      task_tags: ["terminal-bench", "fix-git"]
```

The runner expands that into the full lower-level config using defaults.
Advanced users can override any default:

```yaml
output_dir: /tmp/openevo-runs/my-subscription-evo

rollout:
  url: http://127.0.0.1:8080

evolution:
  enabled: true
  backend_url: http://127.0.0.1:8200
  rounds: 1
  artifacts:
    memory:
      enabled: true
      method: text_memory_reflector
      max_chars: 12000
    skill:
      enabled: true
      method: skill_bundle_reflector
      max_bundles: 4
      allow_files:
        - SKILL.md
        - scripts/**
        - bin/**
        - templates/**
    agent_system:
      enabled: true
      method: auto
      target_path: AGENTS.md
  reflector:
    provider: codex_cli
    model: gpt-5.5

runtime:
  backend: docker
  network: host
  workdir: /polar/session/workspace
```

## Defaults

Technical parameters should have deterministic defaults so a task author does
not need to know internal backend names.

Default resolution order:

1. CLI flag.
2. Explicit experiment config.
3. Environment variable where listed.
4. Built-in default.

Built-in defaults:

| Field | Default |
|---|---|
| `output_dir` | `./openevo_runs/<experiment>-<timestamp>` |
| `rollout.url` | `$OPENEVO_ROLLOUT_URL`, else `http://127.0.0.1:8080` |
| `evolution.enabled` | `true` |
| `evolution.backend_url` | `$OPENEVO_EVOLUTION_URL`, else `http://127.0.0.1:8200` |
| `evolution.rounds` | `1` evolution round after baseline round 0 |
| `evolution.artifacts.memory.enabled` | `true` |
| `evolution.artifacts.memory.method` | `text_memory_reflector` |
| `evolution.artifacts.memory.max_chars` | `12000` |
| `evolution.artifacts.skill.enabled` | `true` |
| `evolution.artifacts.skill.method` | `skill_bundle_reflector` |
| `evolution.artifacts.skill.max_bundles` | `4` |
| `evolution.artifacts.skill.allow_files` | `SKILL.md`, `scripts/**`, `bin/**`, `templates/**` |
| `evolution.artifacts.agent_system.enabled` | `true` |
| `evolution.artifacts.agent_system.method` | `auto` |
| `evolution.artifacts.agent_system.target_path` | `AGENTS.md` |
| `evolution.reflector.provider` | `codex_cli` for subscription agents, otherwise `openai_chat` |
| `evolution.reflector.model` | `agent.model` |
| `runtime.backend` | `docker` |
| `runtime.network` | `host` |
| `runtime.workdir` | `/polar/session/workspace` |
| `agent.auth` | `proxy` |

`agent_system.method: auto` resolves per task and round:

- first dataset only: `agent_system_reflector`;
- multiple historical datasets: `agent_system_history_reflector`;
- GEPA/Pareto methods are opt-in because they are more expensive and have
  candidate-selection semantics.

## Compilation Rules

Each entry in `tasks:` compiles to one independent `TaskRequest`.

Agent compilation:

- `agent.harness` maps to `TaskRequest.agent.harness`.
- `agent.model` maps to `TaskRequest.agent.model_name`.
- `agent.auth: subscription` sets
  `agent.settings.auth_mode=subscription`.
- Subscription auth also sets `agent.settings.capture_mode=transcript` unless a
  compatible transcript mode is already present.
- Subscription auth sets `builder.strategy=agent_transcript` by default.
- `agent.codex_home` maps to `agent.env.CODEX_HOME` for Codex.
- User-provided `agent.settings` and `agent.env` are merged after derived
  defaults, except they cannot disable transcript capture in subscription mode.

Runtime compilation:

- `runtime.prepare` accepts strings as shorthand for
  `{type: exec, command: <string>}`.
- Other runtime fields map directly to `RuntimeSpec`.

Evaluator compilation:

- `eval.type: test_on_output` maps to
  `evaluator.strategy=test_on_output`.
- `eval.command` maps to `config.test_command`.
- `eval.expected` maps to `config.expected_output_json`.

Metadata compilation:

- Top-level experiment metadata and task metadata are merged.
- `metadata.task_tags` is preserved because the evolution context resolver uses
  it for artifact compatibility.
- Runner-added metadata records `experiment`, `round`, `policy_version`, and the
  prior round's active artifact ids when available.

## Artifact Evolution

The first version must support these methods as runner targets:

- `agent_system`, `agent_system_reflector`,
  `agent_system_history_reflector`, `agent_system_pareto_reflector`,
  and `agent_system_gepa_reflector`.
- `text_memory` and a new `text_memory_reflector`.
- `skill_bundle` and a new `skill_bundle_reflector`.

`text_memory_reflector` consumes one or more dataset artifacts plus optional
prior memory artifacts. It writes a concise Markdown memory artifact focused on
reusable task-solving lessons, failure patterns, and verifier observations. It
must not copy raw held-out answers or long transcripts.

`skill_bundle_reflector` consumes one or more dataset artifacts plus optional
prior skill bundles. It writes a skill directory containing at least `SKILL.md`.
If configured for tool-like outputs, it may also write helper files under
allowed paths such as `scripts/` or `bin/`. It must not execute transcript
content while generating files.

Tools remain files inside skill bundles:

- `SKILL.md` explains when to use the helper.
- Helper files may be scripts or static templates.
- The artifact type remains `skill_bundle`.
- Runtime injection continues through `POLAR_SKILLS_DIR`.
- No extra system permissions, MCP servers, package installers, or executable
  artifact type are introduced in the first version.

## Per-Round Data Flow

For each task, the runner keeps an isolated task directory:

```text
<output_dir>/
  experiment.json
  summary.json
  summary.md
  tasks/<task-id>/
    r0/
      task_request.json
      rollout_result.json
      dataset.json
    r1/
      evolution_jobs/
      artifacts/
      task_request.json
      rollout_result.json
    r2/
      ...
```

Round 0 is the baseline. Rounds 1..N use artifacts generated from the previous
round's dataset and any retained history.

The evolution order within a round is fixed:

1. Build or locate the latest dataset artifact.
2. Evolve `text_memory`.
3. Evolve `skill_bundle` including tool helper files.
4. Evolve `agent_system`.
5. Submit the next rollout with all promoted compatible artifacts available for
   context resolution.

Agent system runs after memory and skill because it may reference the existence
of those artifact types at a methodological level. It must not embed generated
skill file contents directly.

## Dataset Creation

Each compiled `TaskRequest` gets a deterministic task-round `policy_version`,
for example `<experiment>.<task-id>.r0`. The gateway event export path should
send completed session events to the evolution backend with that
`policy_version` and task metadata.

After a round completes, the runner asks the evolution backend to create a
dataset for that exact task-round selector. It must not rely on "latest events"
or broad purpose filters that could mix tasks or rounds. The dataset manifest
should include:

- `experiment`
- `task_id`
- `round`
- `policy_version`
- rollout result path
- reward summary
- active artifact ids from the previous round

If no exported events are available for the selector, the task records a
`dataset` phase error for that round. The runner does not silently fall back to a
different task, a different round, or all completed events in the database.

## Promotion And Context

The first implementation uses existing artifact promotion fields and context
resolver compatibility. It does not need per-artifact causal attribution.

Default policy:

- Baseline methods such as `text_memory` and `skill_bundle` honor their method
  `promoted` config.
- LLM reflector methods register artifacts as experimental unless the method
  already has a promotion gate.
- The runner records which artifacts were active for each round.
- If an artifact fails audit, registration fails and the task round records the
  error without stopping other tasks.

Future promotion can add paired reward deltas, diversity checks, and
artifact-specific ablations. The schema should leave room for these fields, but
the first version should not block on them.

## Preflight

Preflight has two levels.

Hard config errors stop before any task submission:

- Missing or duplicate task ids.
- `rounds < 1`.
- Unknown artifact family or method.
- Subscription auth on a harness that does not support subscription mode.
- User config attempts to disable transcript capture while using subscription
  auth.
- Evolution is enabled but no artifact family is enabled.

Warnings are written to summary and do not stop the run:

- The runner cannot verify that Codex or another CLI exists inside the runtime
  image.
- The runner cannot verify that `CODEX_HOME` contains valid login state.
- The runtime image cannot be inspected locally.
- Evolution backend is reachable, but no always-on worker is detected; the
  runner may use one-shot local worker execution if configured.

Service checks:

- Rollout URL must pass `/health` or an equivalent lightweight request.
- If evolution is enabled, backend URL must pass `/health` or an equivalent
  lightweight request.

## Error Handling

Config errors fail the entire experiment before submission.

Per-task runtime errors are isolated. If one task fails in round 1, other tasks
continue. The failing task records:

- round number
- phase (`rollout`, `dataset`, `memory`, `skill`, `agent_system`, `context`)
- error message
- whether retry is safe

Evolution errors for one artifact family do not automatically prevent later
families from running unless the missing artifact is explicitly required by the
config. For example, memory failure can still allow skill and agent-system
evolution, but a configured `require_memory: true` flag may make that task round
fail closed.

## CLI

Initial command:

```bash
uv run openevo run experiment.yaml
```

Useful flags:

- `--dry-run`: parse config, run config-level preflight, write compiled
  `TaskRequest` files, and exit without submitting.
- `--output-dir <path>`: override config output directory.
- `--task-id <id>`: run a subset of task ids.
- `--rounds <n>`: override configured round count for quick pilots.
- `--json`: print the final summary JSON path and compact status JSON.

The package should expose a new script entry point named `openevo`. Existing
`polar` and `polar-evolution` CLIs remain unchanged.

Packaging must add the new `openevo*` package namespace to setuptools discovery.

## Testing

Unit tests:

- Parse a minimal subscription experiment config.
- Apply default rollout and evolution backend URLs when omitted.
- Apply default memory, skill, and agent-system evolution methods when omitted.
- Resolve `agent_system.method: auto` to the correct concrete method for one
  dataset versus multi-round history.
- Reject duplicate task ids.
- Reject subscription auth without transcript-compatible capture if a user
  explicitly overrides capture incorrectly.
- Compile multiple task entries into multiple `TaskRequest` payloads.
- Compile string runtime prepare entries into `PrepareAction` objects.
- Compile `test_on_output` evaluator fields.
- Compile `agent.codex_home` into `agent.env.CODEX_HOME`.
- Compile `tasks[*].metadata.task_tags` without losing top-level metadata.

Method tests:

- `text_memory_reflector` writes a bounded Markdown memory artifact from dataset
  records.
- `skill_bundle_reflector` writes `SKILL.md`.
- `skill_bundle_reflector` may write allowed helper files and rejects paths
  outside the allowed file globs.
- Skill/tool reflection does not execute transcript content.

Runner tests with fake clients:

- Round 0 submits every task and records results.
- One task failure does not stop other tasks.
- Enabled artifact families create jobs in memory, skill, agent-system order.
- Next round metadata references active artifact ids from the previous round.
- `--dry-run` writes compiled task requests and submits nothing.

Focused integration smoke:

- One local toy task with Codex proxy mode.
- One local toy task with subscription mode if a pre-authenticated runtime is
  available.

## Non-Goals

- No Terminal Bench task generator in the first implementation.
- No separate `tool_bundle` artifact type.
- No automatic process manager for rollout, gateway, evolution backend, Docker,
  or Apptainer.
- No arbitrary execution of generated tools during evolution.
- No full promotion/ablation framework in the first version.
- No token-level RL support for subscription-mode runs; subscription mode uses
  transcript trajectories only.
