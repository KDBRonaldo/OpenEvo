# Polar Evolution Backend Design

Date: 2026-06-14

## Summary

Build an independent Evolution Backend as an asynchronous control plane for
skill and memory evolution. Polar remains the rollout execution system. The
Evolution Backend receives fine-grained rollout events, stores them in a local
event log, builds evolution datasets, schedules external evolution jobs, tracks
artifacts, and resolves reusable context for future Polar sessions.

The first version supports both natural-language memory and parametric memory
concepts. Natural-language memory, skill bundles, datasets, and LoRA adapters all
share a common artifact registry. LoRA loading or merging is not performed by
the Evolution Backend; it returns adapter selection and merge specs for trainer,
inference-server management, or future gateway hooks to apply.

## Decisions

- Evolution style: asynchronous control plane.
- Artifact scope: natural-language memory and LoRA pool both present in v1.
- LoRA execution boundary: backend returns merge specs only; external systems
  load or merge adapters.
- Evolution methods: external worker/job protocol only.
- Triggering: Polar emits fine-grained session/task events; backend aggregates
  them into datasets and jobs.
- Persistence: SQLite metadata plus local filesystem artifacts.
- Polar injection: gateway writes a standard evolution context directory inside
  each runtime before agent execution.

## Goals

- Decouple skill/memory research methods from Polar rollout execution.
- Support multiple evolution methods without changing Polar core.
- Maintain versioned natural-language memories, skill bundles, datasets, and
  LoRA adapters with lineage and scores.
- Provide a stable `EvolutionContext` that Polar can inject into any agent
  harness through files and environment variables.
- Keep v1 deployable on one machine with no service dependencies beyond Python,
  SQLite, and the local filesystem.
- Leave clear extension points for distributed storage, multi-node workers, and
  backend-specific LoRA hot loading later.

## Non-Goals

- The Evolution Backend does not train LoRA adapters itself.
- The Evolution Backend does not serve LLM inference.
- The Evolution Backend does not sit in the per-token or per-agent-step hot path.
- The first version does not require Postgres, Redis, object storage, Ray, Slurm,
  or Kubernetes.
- The first version does not define a universal memory algorithm. It defines the
  protocol that lets algorithms plug in as external workers.

## Current Polar Integration Surface

Polar already has the required data path:

- `TaskRequest.metadata` and `SessionDispatchRequest.metadata` are free-form
  dictionaries that flow through rollout and gateway.
- The gateway is the right place to request context before agent execution
  because it owns runtime preparation and environment injection.
- The gateway post-run stage already builds a `Trajectory`, evaluates it, and
  produces a terminal `SessionResult`.
- `Trajectory.metadata`, `Trace.metadata`, and `SessionResult.metadata` can carry
  evolution ids, policy versions, rollout steps, and selected memory metadata.
- Trainer bridges can consume result metadata without requiring every trainer to
  understand Polar internals.

## Architecture

The backend has six logical modules.

### 1. Event Log

Receives immutable events from Polar and stores raw payloads on disk with indexed
metadata in SQLite. Events are fine-grained and cheap to append.

Primary event types:

- `polar.session_completed`
- `polar.task_completed`
- `trainer.rollout_step_completed`
- `artifact.external_registered`

The event log is the source of truth for later dataset construction. Event
ingest is idempotent by `(source, event_type, source_event_id)`.

### 2. Dataset Builder

Builds versioned evolution datasets from event filters. A dataset is a frozen
manifest pointing to selected events, traces, completions, rewards, and optional
artifact files.

Example dataset policies:

- high-reward successful traces for skill distillation.
- repeated low-reward failures for reflective memory mining.
- long-term successful clusters for parametric memory consolidation.
- task-family-specific slices for adapter specialization.

Dataset building is controlled by backend policy, not by Polar. Polar reports
facts; backend decides when enough evidence exists to evolve.

### 3. Job Registry and Scheduler

Creates external jobs from dataset builds and artifact policies. The scheduler
tracks state and leases jobs to workers. It does not execute algorithms in
process.

Job states:

- `pending`
- `claimed`
- `running`
- `succeeded`
- `failed`
- `cancelled`
- `expired`

Workers claim jobs, download or read dataset manifests, run their method, write
artifacts, and call completion APIs. Heartbeats keep leases alive.

### 4. Artifact Store

Stores artifact payloads on the local filesystem and artifact metadata in
SQLite. Every artifact has a stable id, type, version, lineage, compatibility
metadata, and score fields.

Artifact types:

- `text_memory`
- `skill_bundle`
- `parametric_memory`
- `dataset`
- `report`
- `context_snapshot`

### 5. Registry Layer

Maintains queryable registries for memories, skills, and adapters.

The registry supports:

- versioning and lineage.
- task/model/agent compatibility filters.
- quality scores and confidence.
- promotion/demotion.
- deprecation without deletion.
- artifact composition, such as a skill bundle generated from a memory snapshot
  or a LoRA adapter trained from a dataset.

### 6. Context Resolver

Computes the `EvolutionContext` for a future Polar session. It considers task
metadata, agent harness, base model, policy version, rollout step, and optional
user filters.

The resolver returns:

- selected natural-language memory refs and rendered memory text.
- selected skill bundle refs.
- adapter merge spec for parametric memory.
- context lineage and selection reasons.

The resolver is called before agent execution, not during model generation.

## API Design

All APIs are versioned under `/v1`.

### Health

`GET /v1/health`

Response:

```json
{
  "status": "ok",
  "db": "ok",
  "artifact_root": "/path/to/evolution/artifacts"
}
```

### Event Ingest

`POST /v1/events`

Request:

```json
{
  "source": "polar",
  "event_type": "polar.session_completed",
  "source_event_id": "session:ses_abc",
  "created_at": "2026-06-14T00:00:00Z",
  "task_id": "task_001",
  "session_id": "ses_abc",
  "policy_version": "policy_42",
  "rollout_step": 1200,
  "agent": {
    "harness": "codex",
    "model_name": "openai/gpt-5.4"
  },
  "base_model": "Qwen/Qwen3.6-27B",
  "reward": 1.0,
  "status": "COMPLETED",
  "payload": {
    "session_result": {}
  }
}
```

Response:

```json
{
  "event_id": "evt_01",
  "ingested": true,
  "duplicate": false
}
```

### Dataset Build

`POST /v1/datasets`

Creates a frozen dataset manifest from an explicit event query.

Request:

```json
{
  "name": "calculator_success_policy_42",
  "purpose": "skill_distillation",
  "query": {
    "event_types": ["polar.session_completed"],
    "status": ["COMPLETED"],
    "reward_min": 0.8,
    "policy_version": "policy_42",
    "task_tags": ["calculator"]
  },
  "limits": {
    "max_events": 10000,
    "max_traces": 50000
  }
}
```

Response:

```json
{
  "dataset_id": "ds_01",
  "artifact_id": "art_dataset_01",
  "event_count": 128,
  "trace_count": 476
}
```

### Job Creation

`POST /v1/jobs`

Request:

```json
{
  "method": "lora_consolidation.qwen_math_v1",
  "job_type": "parametric_memory_train",
  "input_artifact_ids": ["art_dataset_01"],
  "config": {
    "base_model": "Qwen/Qwen3.6-27B",
    "rank": 16,
    "target_modules": ["q_proj", "v_proj"]
  },
  "priority": 100
}
```

Response:

```json
{
  "job_id": "job_01",
  "state": "pending"
}
```

### Worker Claim

`POST /v1/jobs/claim`

Request:

```json
{
  "worker_id": "worker_local_01",
  "capabilities": ["text_memory_mining", "parametric_memory_train"],
  "lease_seconds": 600
}
```

Response when a job is available:

```json
{
  "job_id": "job_01",
  "lease_id": "lease_01",
  "job_type": "parametric_memory_train",
  "method": "lora_consolidation.qwen_math_v1",
  "input_artifacts": [
    {
      "artifact_id": "art_dataset_01",
      "type": "dataset",
      "uri": "file:///.../artifacts/datasets/ds_01/manifest.json"
    }
  ],
  "config": {}
}
```

Response when no job is available:

```json
{
  "job": null
}
```

### Worker Heartbeat

`POST /v1/jobs/{job_id}/heartbeat`

Request:

```json
{
  "lease_id": "lease_01",
  "progress": 0.4,
  "message": "training epoch 2/5"
}
```

### Worker Completion

`POST /v1/jobs/{job_id}/complete`

Request:

```json
{
  "lease_id": "lease_01",
  "artifacts": [
    {
      "type": "parametric_memory",
      "name": "pmem_calculator_policy_42_rank16",
      "uri": "file:///.../artifacts/parametric_memory/pmem_01",
      "manifest": {
        "base_model": "Qwen/Qwen3.6-27B",
        "adapter_format": "lora",
        "rank": 16,
        "target_modules": ["q_proj", "v_proj"]
      },
      "scores": {
        "heldout_reward_delta": 0.08
      }
    }
  ],
  "report": {
    "summary": "adapter trained successfully"
  }
}
```

### Worker Failure

`POST /v1/jobs/{job_id}/fail`

Request:

```json
{
  "lease_id": "lease_01",
  "error": "training command exited with status 1",
  "retryable": true
}
```

### Context Resolve

`POST /v1/contexts/resolve`

Request:

```json
{
  "task_id": "task_002",
  "instruction": "Implement a recursive descent calculator parser.",
  "agent": {
    "harness": "codex",
    "model_name": "openai/gpt-5.4"
  },
  "base_model": "Qwen/Qwen3.6-27B",
  "policy_version": "policy_43",
  "rollout_step": 1201,
  "metadata": {
    "task_tags": ["calculator", "parser"]
  },
  "limits": {
    "max_memory_chars": 12000,
    "max_skill_bundles": 4,
    "max_adapters": 2
  }
}
```

Response:

```json
{
  "context_id": "ctx_01",
  "memory": {
    "artifact_ids": ["art_mem_01"],
    "rendered_text": "Relevant long-term memory..."
  },
  "skills": [
    {
      "artifact_id": "art_skill_01",
      "name": "calculator-parser-skill",
      "uri": "file:///.../artifacts/skills/calculator-parser-skill"
    }
  ],
  "adapter_merge_spec": {
    "base_model": "Qwen/Qwen3.6-27B",
    "merge_mode": "runtime_lora",
    "adapters": [
      {
        "artifact_id": "art_pmem_01",
        "adapter_id": "pmem_calculator_policy_42_rank16",
        "uri": "file:///.../artifacts/parametric_memory/pmem_01",
        "weight": 0.7,
        "format": "lora"
      }
    ]
  },
  "selection": {
    "reasons": [
      "task tags matched calculator/parser",
      "adapter compatible with base model"
    ]
  }
}
```

## Polar Integration

### Topology Configuration

Add an optional block to Polar topology:

```yaml
evolution:
  enabled: true
  backend_url: http://127.0.0.1:8200
  context:
    target_dir: /polar/session/evolution
    timeout_seconds: 10
    fail_open: true
  event_export:
    enabled: true
    timeout_seconds: 10
    fail_open: true
```

`fail_open: true` means Polar continues rollout if the Evolution Backend is
unreachable. The failure is recorded in session metadata.

### Runtime Injection

Before harness setup/run, gateway resolves context and writes:

- `/polar/session/evolution/context.json`
- `/polar/session/evolution/memory.md`
- `/polar/session/evolution/skills/`
- `/polar/session/evolution/adapters.json`

Gateway injects environment variables:

- `POLAR_EVOLUTION_CONTEXT=/polar/session/evolution/context.json`
- `POLAR_MEMORY_FILE=/polar/session/evolution/memory.md`
- `POLAR_SKILLS_DIR=/polar/session/evolution/skills`
- `POLAR_ADAPTER_MERGE_SPEC=/polar/session/evolution/adapters.json`

Harnesses can use these paths directly. Existing harnesses do not have to use
them immediately; the files are a stable surface for future presets and custom
harnesses.

### Event Export

After `SessionResult` is built and normalized, gateway posts
`polar.session_completed` to the backend. This is best-effort and must not block
the result callback to rollout for long.

Task-level aggregation can be exported by rollout after all sessions complete as
`polar.task_completed`. Session events are sufficient for v1 correctness; task
events improve dataset policy convenience.

## Parametric Memory Pool

Parametric memory artifacts represent LoRA-style adapters or future parameter
efficient memories. The backend tracks them but does not load them into models.

Required manifest fields:

```json
{
  "adapter_format": "lora",
  "base_model": "Qwen/Qwen3.6-27B",
  "rank": 16,
  "target_modules": ["q_proj", "v_proj"],
  "dtype": "bf16",
  "framework": "peft",
  "created_from_dataset_id": "ds_01"
}
```

Selection constraints:

- `base_model` must match unless an artifact declares compatibility aliases.
- Adapter format must be supported by the downstream loader.
- Only promoted or experimental artifacts allowed by request policy are
  returned.
- If multiple adapters are returned, the merge spec includes ordering and
  weights.

Merge modes:

- `runtime_lora`: downstream inference server loads adapters at request/runtime.
- `offline_merge`: downstream trainer or model manager produces a merged
  checkpoint.
- `reference_only`: adapter is selected for metadata or analysis but not loaded.

## SQLite Schema

The schema uses string ids generated by the backend. Raw payloads and manifests
are stored as JSON files on disk; SQLite stores indexed fields and paths.

### `events`

- `event_id TEXT PRIMARY KEY`
- `source TEXT NOT NULL`
- `event_type TEXT NOT NULL`
- `source_event_id TEXT NOT NULL`
- `created_at TEXT NOT NULL`
- `ingested_at TEXT NOT NULL`
- `task_id TEXT`
- `session_id TEXT`
- `policy_version TEXT`
- `rollout_step INTEGER`
- `agent_harness TEXT`
- `agent_model TEXT`
- `base_model TEXT`
- `status TEXT`
- `reward REAL`
- `payload_path TEXT NOT NULL`
- `UNIQUE(source, event_type, source_event_id)`

### `datasets`

- `dataset_id TEXT PRIMARY KEY`
- `name TEXT NOT NULL`
- `purpose TEXT NOT NULL`
- `state TEXT NOT NULL`
- `created_at TEXT NOT NULL`
- `query_json TEXT NOT NULL`
- `manifest_path TEXT NOT NULL`
- `event_count INTEGER NOT NULL`
- `trace_count INTEGER NOT NULL`
- `artifact_id TEXT`

### `dataset_events`

- `dataset_id TEXT NOT NULL`
- `event_id TEXT NOT NULL`
- `PRIMARY KEY(dataset_id, event_id)`

### `jobs`

- `job_id TEXT PRIMARY KEY`
- `job_type TEXT NOT NULL`
- `method TEXT NOT NULL`
- `state TEXT NOT NULL`
- `priority INTEGER NOT NULL`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`
- `claimed_by TEXT`
- `lease_id TEXT`
- `lease_expires_at TEXT`
- `input_artifact_ids_json TEXT NOT NULL`
- `config_json TEXT NOT NULL`
- `error TEXT`
- `attempt_count INTEGER NOT NULL`

### `artifacts`

- `artifact_id TEXT PRIMARY KEY`
- `type TEXT NOT NULL`
- `name TEXT NOT NULL`
- `version INTEGER NOT NULL`
- `state TEXT NOT NULL`
- `created_at TEXT NOT NULL`
- `uri TEXT NOT NULL`
- `manifest_path TEXT NOT NULL`
- `lineage_json TEXT NOT NULL`
- `compatibility_json TEXT NOT NULL`
- `scores_json TEXT NOT NULL`
- `tags_json TEXT NOT NULL`
- `promoted INTEGER NOT NULL`

### `artifact_lineage`

- `parent_artifact_id TEXT NOT NULL`
- `child_artifact_id TEXT NOT NULL`
- `relation TEXT NOT NULL`
- `PRIMARY KEY(parent_artifact_id, child_artifact_id, relation)`

### `contexts`

- `context_id TEXT PRIMARY KEY`
- `created_at TEXT NOT NULL`
- `request_json TEXT NOT NULL`
- `response_json TEXT NOT NULL`
- `selected_artifact_ids_json TEXT NOT NULL`

## Filesystem Layout

Default root: `.polar_evolution/`

```text
.polar_evolution/
  evolution.db
  events/
    2026/06/14/evt_01.json
  datasets/
    ds_01/
      manifest.json
      traces.jsonl
  artifacts/
    text_memory/
      art_mem_01/
        manifest.json
        memory.md
    skills/
      art_skill_01/
        manifest.json
        skills/
    parametric_memory/
      art_pmem_01/
        manifest.json
        adapter_config.json
        adapter_model.safetensors
    reports/
      art_report_01/
        manifest.json
        report.json
  contexts/
    ctx_01.json
```

## External Worker Contract

A worker is any process that can call the backend APIs. It may be a Python
script, training framework job, Slurm job, Ray job, or manual research command.

Worker responsibilities:

1. Claim a compatible job.
2. Read input artifact manifests.
3. Execute the method.
4. Write output files under a provided or agreed artifact directory.
5. Complete or fail the job through the API.
6. Include enough manifest metadata for future compatibility checks.

Backend responsibilities:

1. Lease jobs atomically.
2. Expire leases that miss heartbeat deadlines.
3. Register output artifacts on completion.
4. Preserve input/output lineage.
5. Keep failed job errors queryable.

## Error Handling

- Event ingest is idempotent. Duplicate events return the original `event_id`.
- Context resolution is fail-open from Polar's perspective when configured.
- Failed context resolution is recorded in `SessionResult.metadata.evolution`.
- Worker lease expiry moves jobs from `claimed/running` back to `pending` until
  retry policy is exhausted.
- Invalid artifact manifests fail job completion and keep the job in `failed`.
- Missing artifact files mark artifacts as `broken`; they are excluded from
  context resolution.
- SQLite write operations use transactions around event ingest, dataset build,
  job claim, and job completion.

## Security and Isolation

The first version is local-trusted. It does not expose multi-tenant auth.

Basic safeguards:

- Bind to `127.0.0.1` by default.
- Normalize file paths under the artifact root before accepting local artifact
  registrations.
- Do not execute worker-provided commands inside the backend.
- Store payloads as data; never import code from artifacts automatically.

## Testing Strategy

Unit tests:

- event idempotency.
- dataset query filtering.
- atomic job claiming and lease expiry.
- artifact registration validation.
- context resolver compatibility filtering.
- SQLite migration/schema initialization.

Integration tests:

- create events from synthetic Polar `SessionResult` payloads.
- build a dataset from events.
- claim and complete a fake external worker job.
- resolve context containing memory, skill bundle, and adapter merge spec.
- verify Polar gateway writes expected evolution files when backend is available.
- verify Polar gateway continues when backend is unavailable and `fail_open=true`.

Manual smoke test:

1. Start evolution backend.
2. Start Polar rollout/gateway with evolution enabled.
3. Submit a small calculator task.
4. Verify session event appears in backend.
5. Build dataset and complete a fake memory job.
6. Submit another task and verify `/polar/session/evolution/*` files exist.

## Milestones

### Milestone 1: Backend Skeleton and Storage

- CLI/server for Evolution Backend.
- SQLite schema initialization.
- local artifact root management.
- health endpoint.
- event ingest endpoint.
- artifact registry primitives.

### Milestone 2: Dataset and Job Protocol

- dataset build endpoint.
- job creation, claim, heartbeat, complete, fail.
- fake worker script for tests.
- artifact lineage recording.

### Milestone 3: Context Resolver

- memory/skill/adapter artifact filtering.
- `EvolutionContext` response schema.
- context snapshots persisted.
- deterministic selection policy based on tags, base model, promoted state, and
  score.

### Milestone 4: Polar Integration

- topology config block.
- gateway context resolve before harness run.
- runtime file injection.
- environment variable injection.
- best-effort session event export.

### Milestone 5: First External Methods

- a simple text-memory mining worker.
- a skill-bundle packaging worker.
- a mock LoRA registration worker that validates parametric memory registry and
  merge spec without training a model.

## Future Extensions

- Postgres and object-store storage backend.
- distributed workers with stronger auth.
- online sidecar query path for agents that need dynamic memory retrieval.
- inference-backend adapters for vLLM/SGLang LoRA hot loading.
- promotion policies based on A/B rollout evaluation.
- embedding/vector index for memory selection.
- model-family compatibility graph for adapter reuse.

