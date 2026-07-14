# OpenEvo Productization And External Beta Spec

Status: canonical product and release specification
Tracking issues: #131, #154
Target: unsigned macOS External Beta
## Purpose

This document defines what OpenEvo is shipping, the boundaries that must remain
stable, and the evidence required to call the repository release-ready. It does
not prescribe every API field, CI job, JSON report, or implementation detail.
Those decisions belong in the issue and module documentation for the code that
implements them.

OpenEvo is ready for External Beta when a scientist can install OpenEvo Desktop
on a Mac, connect a remote server, run a science task through OpenEvo Core,
inspect non-parametric evolution, and reuse promoted artifacts without using a
command line or benchmark workflow.

## Non-Negotiables

- The only release-facing product surfaces are OpenEvo Core Backend and OpenEvo
  Desktop.
- `src/openevo/` is Core. `desktop/` is the ordinary-user application.
- Command-line entrypoints are backend launchers or maintainer automation. They
  are not a separate CLI or Dev Kit product.
- Benchmark-specific code lives outside Core and Desktop under `benchmarks/` or
  an external automation repository.
- OpenEvo always drives an existing agent harness. It does not bypass the
  harness and call a model API directly to obtain task answers.
- Existing evolution method logic, prompts, defaults, filtering, candidate
  policy, artifact semantics, and method IDs must not change during
  productization.
- Evolution carriers and evolution methods are separate contracts. One
  authoritative Core registry drives planning, worker dispatch, capabilities,
  and Desktop configuration; those consumers must not maintain method-specific
  branches or duplicate registries.
- Evolution and training outputs take effect only in the next task/session. Each task pins one immutable context/model/adapter revision for its full lifetime; Core never mutates a running task's revision.
- The next revision is one all-or-nothing Core commit across every enabled target, materialized context, and required serving change. A next task that requires that revision is explicitly queued or not ready until the commit succeeds; it never falls back to stale or partially activated state.
- Preserve the proven OpenEvo Core architecture inherited from the legacy
  upstream: gateway, rollout, runtime,
  capture/trajectory, evolution backend, worker, artifact, context resolution,
  and runtime injection keep their responsibilities and data flow. Public
  OpenEvo identity, paths, imports, packaging, API access, and missing product
  capabilities may change; the underlying architecture is not redesigned as
  part of productization.
- Runtime identity is OpenEvo-only: `OPENEVO_*`, `/openevo/session`,
  `.openevo/evolution`, and `openevo.session_completed`.
- Legacy upstream packages, wrappers, commands, runtime markers, and public
  product identity are not retained for compatibility.

## Product Model

```text
Local Mac
└── OpenEvo Desktop
    ├── Tauri/Rust native host
    ├── React user interface
    └── local sidecar and SSH tunnel

Remote server
└── OpenEvo Core Backend
    ├── remote bootstrap and service supervision
    ├── harness and runtime execution
    ├── transcript/trajectory capture
    ├── datasets, evolution jobs, and workers
    ├── artifacts, promotion, and context injection
    └── model serving management when self-deployed
```

Desktop wraps Core for ordinary users. Core owns execution and evolution.
Desktop must not duplicate the method registry, artifact semantics, experiment
compiler, runtime orchestration, or benchmark logic.

The repository dependency direction is:

```text
desktop -> Core contracts/API
benchmark automation -> Core contracts/API
Core -> neither Desktop nor benchmark automation
```

## OpenEvo Core Backend

Core is both the Python package under `src/openevo/` and the backend process
installed on the remote server. The package contains reusable contracts and
business logic; `openevo-backend` is the process entrypoint that exposes those
capabilities to Desktop and automation.

After it is installed and started, Core owns:

- environment inspection, OpenEvo-owned dependency repair, upgrade, and
  rollback;
- backend and child-service lifecycle;
- projects, runs, attempts, cancellation, retry, logs, and timelines;
- Docker/Apptainer runtime sessions and workspaces;
- harness invocation and capture-mode enforcement;
- completion, transcript, and trajectory construction;
- evolution target/method registration, per-run plan validation, and method
  capability discovery;
- datasets, evolution jobs, workers, artifacts, lineage, compatibility,
  promotion, and context resolution;
- sealing task datasets and atomically publishing promoted, materialized evolution outputs as the next task revision;
- model download and vLLM lifecycle for the supported self-deployed profile;
- typed status, capabilities, doctor, repair, artifact, log, and diagnostics
  APIs.

The public backend contract must be typed and versioned. Exact endpoint and
payload definitions live in `docs/core/backend-api.md` and the corresponding
models/tests. User-facing failures must include a stable code, message,
retryability, and a concrete repair or user action. Raw shell output is
diagnostic detail, not the API contract.

Bootstrap has a strict ownership split. Before Core exists, the Desktop native
host/sidecar owns SSH transport, remote directory preparation, upload or
download of verified Core bytes, installation, process start, and tunnel
establishment. Once Core is healthy, Core owns doctor/repair, upgrades, child
services, runs, and artifacts. The sidecar then forwards typed requests; it must
not directly run science jobs or manage Core child services with ad hoc remote
commands. Pre-release runtime state is not a compatibility target; upgrades may
retain a backup for diagnostics, but unsupported state is reset or explicitly
exported rather than carried by a legacy runtime path.

## Execution And Capture Modes

External Beta supports two execution configurations. Both run on the remote
server and both use a real harness.

| Mode | Harness/model path | Capture | External Beta evolution scope |
| --- | --- | --- | --- |
| Codex Subscription Transcript | Remote subscription-authenticated Codex | Required transcript capture; token-level metrics unavailable | Textual memory, skill, and agent-system evolution |
| Self-Deployed Reference | Remote harness using a Core-managed vLLM reference profile | Transcript for current non-parametric workflows; proxy/token capture remains a Core capability | Textual memory, skill, and agent-system evolution |

### Codex Subscription Transcript

- Subscription is an authentication and execution mode, not a capture mode.
- Core must explicitly set transcript capture and
  `token_level_metrics_available=false`.
- Core rejects subscription runs without a readable transcript path.
- Desktop checks Codex availability and authentication and presents a guided
  user action when interactive authentication is required.
- Bootstrap may install, update, and verify a supported Codex CLI in user space;
  interactive subscription authentication remains a Desktop-mediated user
  action.
- Subscription mode does not claim token-level RL or parameter updates.
- Subscription accepts only non-parametric methods whose declared execution, capture, harness, and runtime requirements are compatible with the active profile; incompatible enabled targets fail validation.

### Self-Deployed Reference

- The release-supported reference profile uses `Qwen/Qwen3-Coder-30B-A3B-Instruct` with a pinned model revision, vLLM configuration, and hardware assumptions recorded in the shipped profile.
- The canonical profile path is `src/openevo/projects/science/profiles/self-deployed-reference-v1.json`. It is a versioned lockfile, not user prose. It must pin the Hugging Face commit, vLLM version and arguments, dtype/context length, host/port, GPU count and minimum VRAM, disk/cache assumptions, startup/download timeouts, proxy/cache behavior, health endpoint and expected served model, and compatible Core version range.
- Desktop may accept another Hugging Face model ID as an advanced best-effort configuration, but it is not release-supported unless added to this spec and the release test matrix.
- Desktop/Core can configure HTTP/HTTPS proxy settings, install supported user space dependencies, download model snapshots, start vLLM, and verify its health.
- Automatic repair stops before root-only system changes, driver installation, license acceptance, unavailable credentials, or unsupported hardware. The UI then reports the exact unresolved action.
- Self-deployed plans fail closed when an enabled method's trainer or serving requirements cannot be satisfied, including adapter load, serving restart, or health verification required for its next revision.
- The architecture must remain extensible to parametric evolution, but this release does not design or validate new parametric methods.

## Evolution Contract

This is the release target, not current status: strict v2 transport, Gateway generic staging, and cross-session revision activation remain unimplemented; the public Gateway path is legacy.

Core converts harness execution into evolution inputs and typed artifacts:

```text
task/session pinned to revision N
-> completed task dataset sealed
-> evolution/training jobs run outside inference
-> all enabled target outputs staged and materialized
-> required adapter load or serving restart passes health checks
-> revision N+1 committed atomically
-> next task/session admitted on revision N+1
```

The release-supported non-parametric artifact families are:

| Artifact | Required runtime behavior |
| --- | --- |
| `text_memory` | Materialize a readable memory payload in the next revision and expose it to the next harness instruction. |
| `skill_bundle` | Materialize a bundle containing `SKILL.md` in the next revision; evolved skills take precedence over static skills. |
| `agent_system` | Materialize the canonical instruction payload in the next revision and write only to an allowlisted harness instruction path. |

Artifacts retain source dataset/job lineage, compatibility, scores when
produced by the method, promotion state, and payload integrity. Core and
Desktop consume one artifact contract; Desktop must not hide drift through a
separate adapter.

### Task Revision And Activation

Issue #154 defines one activation contract for every evolution and training method. Method latency and implementation differ, but activation timing does not:

| Requirement | Contract |
| --- | --- |
| `REV-1 Task pinning` | Task admission resolves exactly one immutable context/model/adapter revision. Every inference request and retry in that task uses it; evolution, training, materialization, adapter loading, and serving lifecycle work cannot alter it. |
| `REV-2 Sealed transition input` | Task completion seals the transition dataset. Evolution and training consume sealed inputs in workers outside the inference process. Parameter training may be long-running but cannot update an active task. |
| `REV-3 Atomic next revision` | All enabled targets form one transition. Outputs remain staged until every output validates, context materialization completes, and required adapter load or serving restart passes health checks without disturbing active revision leases. Core then publishes one next revision atomically; failure publishes none. |
| `REV-4 Admission barrier` | A next task that requires evolution is queued or gets typed not-ready until the revision commits. Failure is explicit; Core, Desktop, and automation cannot admit it on the previous revision or a subset of enabled outputs. |
| `REV-5 Uniform method contract` | Update timing is not method config or descriptor metadata. There is no per-method online/offline timing, background-versus-barrier choice, or streaming/in-session ABI. Methods differ through ordered inputs, execution/capture/harness/runtime requirements, typed config, and output contracts. |

Candidate generation, evaluation, and best-result selection belong to the
evolution algorithm. This protected algorithm logic includes GEPA orchestration
currently co-located with Terminal Bench. Product, benchmark, and Desktop
layers may record or reuse the algorithm-selected output but may not rerank it
or replace its decision. Generic artifact lifecycle state is a Core concern and
must not be confused with or used to redefine algorithm selection.

## Pluggable Evolution Targets And Methods

OpenEvo separates what is evolved from how it is evolved:

| Contract | Responsibility |
| --- | --- |
| `EvolutionTarget` | A carrier such as text memory, skill bundle, agent system, or a future parameter carrier. Its handler defines artifact validation, context projection, constrained runtime consumption, and presentation metadata. |
| `EvolutionMethod` | One algorithm for one target. It declares typed config, execution/capture/harness/runtime requirements, input/output artifact types, and immutable implementation identity. Internal reflection, candidate generation, evaluation, rounds, and best-result selection remain method-owned algorithm logic. |
| `EvolutionPlan` | The immutable resolved selections for one run: enabled targets, chosen methods, canonical normalized config, execution profile, and plan-reachable implementation identities. Runtime state continues to use the existing Core job/lease/worker/artifact contracts. |

These are concept names. The public contracts are
`EvolutionTargetDescriptor`, `EvolutionMethodDescriptor`,
`TargetHandlerDescriptor`, and `EvolutionPlan`.

The following stable requirement IDs are the acceptance anchors for this
framework. Tests and the implementation plan reference the IDs instead of exact
document wording.

- `PLUG-1 Plan and selection`: each target is independently enabled and selects
  one compatible method. Methods declare capture, runtime, harness, input, and
  output requirements, preserve ordered input bindings, and use Core-provided
  harness execution.
  Generic execution modes (`subscription`, `self_deployed`) remain independent
  from capture modes (`transcript`, `token_level`); the release profile narrows
  subscription to Codex. Method code does not open a model endpoint directly.
  User method config cannot shadow Core job fields. Unknown enabled targets,
  methods, config fields, or incompatible modes fail validation; disabled
  unknown targets remain non-executable draft state. Project
  `agent_system.method=auto` is one narrow prior-dataset resolver, not a method
  plugin or scheduler; plans/jobs store its concrete result. Method descriptors
  do not declare activation timing; every selection follows `REV-1` through
  `REV-5`.
- `PLUG-2 Registry and identity`: one startup-frozen Core registry is the only
  source for planning, dispatch, capabilities, lineage, and defaults. Every
  target, method, and target handler has an identity built from its canonical
  descriptor, immutable distribution digest/version, entry point, and contract
  version, including an explicit legacy or context-plugin invocation ABI. Both
  ABIs are whole-job worker contracts, not streaming or in-session update ABIs.
  `registry_snapshot_digest` hashes only identities reachable from the enabled
  selections. Release methods are loaded from the verified Core install;
  explicitly enabled research plugins bind their own immutable digest. Core
  publishes the descriptor graph and all reachable handles atomically only
  after every implementation verifies; partial graphs and fallback callable
  tables are forbidden.
- `PLUG-3 Existing Core boundary`: registry-driven dispatch uses plan-bound job
  materialization on the current worker claim/lease/complete flow, `ArtifactRegisterRequest`,
  artifact store, promotion, context resolver, and gateway injection. It does not
  introduce a second scheduler, stage ledger, publication store, compatibility
  runtime, or generic score-based winner selection. A method may be internally
  multi-round; Core records the output selected by that algorithm without
  reinterpreting it. One tested adapter reconstructs the current flat
  `WorkerClaimedJob.config` and exact input-artifact order for mechanically moved
  methods. Each persisted job binds the plan, registry snapshot, concrete method
  identity, normalized user/Core config, full-envelope digest, and resolved input
  snapshots. Corrupt rows fail before lease. Claiming
  retains run-scoped `job_type` capabilities but also filters exact loaded method
  identity digests. Workers renew the existing lease while a method runs;
  deterministic registry/identity failures are non-retryable. Outputs remain
  transiently invisible until artifact publication and job success commit
  together, then remain unavailable to task admission until the complete
  `REV-3` transition commits.
- `PLUG-4 Safe target integration`: a target handler consumes the current
  resolver-ranked artifacts for one target without reranking and returns
  versioned data-only
  contributions for instruction text, target-scoped staged payloads, optional
  harness instruction/skill destinations, optional adapter selection, Core-owned
  environment bindings, and one supported renderer payload. Core validates URI
  schemes, MIME types, relative paths, containment, payload integrity, reserved
  environment names, bounded payload inventories, contribution/renderer
  consistency, and context-wide conflicts before use.
  A target using an existing contribution and renderer vocabulary requires no
  compiler, resolver, gateway, or Desktop target-ID branch. Capability config
  uses a bounded JSON Schema 2020-12 subset: scalar/object/array types,
  title/description,
  properties, required, `additionalProperties: false`, enum/const/default,
  scalar and collection bounds, and nullable `anyOf` only. References, recursion,
  regex/patterns, arbitrary combinators, and executable formats are forbidden;
  secrets are opaque Core-owned references and never value-bearing schema data.
  Desktop accepts only versioned `markdown`, `file_bundle`,
  `structured_summary`, or `adapter`
  renderer kinds, treats all content as untrusted, sanitizes Markdown/links, and
  executes no plugin HTML or JavaScript. Renderers never fetch remote images,
  media, fonts, previews, or metadata; links require explicit user action, and
  Desktop CSP permits network access only to its authenticated local/tunnel Core
  endpoints. Unknown schema/renderer kinds fail closed. Exact depth/count/length
  limits live in the versioned architecture contract.
- `PLUG-5 Behavior preservation`: existing methods move mechanically behind
  these contracts with stable IDs, algorithms, prompts, defaults, filters,
  artifact semantics, and candidate policy. GEPA equivalence fixtures preserve
  that every generation's candidate trials feed the next generation, only the
  selected round winner feeds the next round, history datasets accumulate, and
  `auto` resolves to the same methods. They also freeze per-task reward versus
  group-objective selection, `None` as the lowest score, and existing generation
  then candidate-index tie-breaking. Productization does not reinterpret any of
  these choices.

Candidate generation, evaluation, transition, and best-result selection remain
inside algorithm-owned method execution. Core artifact lifecycle state records
the algorithm result; it does not redefine it. Auxiliary report outputs never
enter target context, and absence of a method-selected target result fails closed.

Project configuration uses a generic target map rather than one Pydantic class
or compiler branch per carrier:

```yaml
evolution:
  targets:
    text_memory:
      enabled: true
      method: text_memory_expel_reflector
      config: {}
    skill_bundle:
      enabled: false
    agent_system:
      enabled: true
      method: agent_system_gepa_reflector
      config:
        candidate_count: 2
    parametric_memory:
      enabled: false
```

Adding a method for an existing target requires only a method plugin, typed
config, registration, and tests. It must not require a compiler `if/elif`, a
new Science Project field, a gateway branch, or a Desktop method list. Adding a
new target is intentionally broader: it requires an artifact contract, context
and constrained runtime handler, capability metadata, and a supported renderer
kind when exposed in Desktop. Registration with an existing renderer must be
sufficient; central compiler, resolver, gateway, and Desktop target switches are
forbidden.

Desktop obtains target toggles, compatible method choices, defaults, config
schemas, execution/capture/harness/runtime requirements, renderer contracts, and
four-axis support reasons from a target-rooted, versioned capability projection
generated by the connected remote Core frozen registry. The local sidecar only
proxies/caches that remote response; it never rebuilds capabilities from the
Desktop-bundled Core package or falls back to a local method table. Offline
state is explicit and any last-known cache is labeled with its remote/profile
and registry digest. Core supplies the configured default plus a nullable
profile-supported effective default; Desktop never guesses a fallback algorithm.
Desktop contains no method registry or algorithm logic. The UI presents friendly names rather than
method IDs, saves and reloads every target selection, and renders a newly
registered target with an existing renderer without code changes.

Release profiles load supported built-ins only. Explicit method plugins are
trusted server code with worker-process permissions. Core-loaded target handlers
have Core-process permissions unless later isolated. Both require maintainer
configuration, are never installed automatically by Desktop, and are not
claimed to be sandboxed by contract validation.

The exact framework contract is `docs/architecture/evolution-framework.md`.
A2.3 planning, verified dispatch, store identity, remote capabilities, and
capability-driven Desktop configuration are implemented. The implemented A2.4
slices add verified handlers, payload scanning, internal projection, and the
generic Core materializer. Strict v2 transport, Gateway cutover,
external-target E2E, and removal of the legacy target switches remain. These
cutovers preserve the worker/job/artifact boundaries and protected algorithm
behavior.

## Algorithm Preservation And Performance Gate

Three method families have frozen historical thresholds and block the release if
the final candidate does not meet them:

| Family | Canonical method ID | Frozen baseline-failed subset | Required pass@1 rescue count |
| --- | --- | ---: | ---: |
| Textual memory | `text_memory_expel_reflector` | 21 applicable tasks | at least 12/21 |
| Trajectory-to-skill | `skill_bundle_reflector` | 25 tasks | at least 14/25 |
| Agent-system | `agent_system_gepa_reflector` | 25 tasks | at least 17/25 |

These are independent gates. A run enables one family only and cannot borrow
artifacts or successes from another family.

Before structural changes, focused fixtures must freeze each canonical method's
observable prompts, defaults, filtering, outputs, candidate/selection behavior,
and artifact construction. Historical source resolves ambiguity but is not a
second source-hash specification. Moving files and imports is allowed only when
the fixtures and performance gates remain unchanged.

Release-candidate benchmark rules:

- use the frozen Terminal Bench 2.1 baseline-failed task lists and model/harness
  configuration associated with the historical results;
- run exactly one authoritative final harness attempt per applicable task for
  pass@1 after the frozen evolution/candidate-selection pipeline completes;
- count a task as rescued only when the authoritative evolved attempt passes;
- permit a targeted rerun only for a recorded infrastructure failure that
  prevented scoring;
- do not add best-of-candidate, attempt, artifact, or rerun selection beyond the
  candidate policy already frozen for that historical method gate;
- treat algorithm-owned candidate evaluation and best-result selection as part
  of the protected evolution algorithm, not as extra pass@k task attempts;
- archive enough configuration, selected-artifact, injection, and result data
  to reproduce the count;
- block release when any family misses its threshold.

Historical raw runs are unavailable. Skill bundle's 14/25 is an unverified, user-provided aggregate with no per-task evidence; this migration did not run the gate, so it proves no algorithm performance.
Thresholds cannot be lowered: every final candidate must rerun each complete frozen gate, including all 25 skill-bundle tasks, and retain task-level evidence as the durable release record.

Methods still under development, including parametric memory and OPSD paths,
may retain tests but do not satisfy or block these three performance gates
unless this spec is deliberately amended.

## Benchmark Automation

Benchmark code is developer automation, not a product surface. Each benchmark
team implements a standalone adapter that:

- obtains benchmark tasks and verifier results;
- creates Core-compatible projects, datasets, jobs, and runs;
- invokes Core through stable contracts;
- records benchmark-specific configuration and results;
- validates performance without changing Core method behavior.

Terminal Bench automation is the first such package and must live under
`benchmarks/terminal_bench/`. Future scientific or general-agent benchmarks
are sibling packages. Core may expose generic reusable contracts, but it must
not contain Terminal Bench runners, scorers, materializers, task lists, or
benchmark CLI commands. Desktop exposes no benchmark controls.

Code location does not determine algorithm ownership. The protected GEPA
candidate evaluation, best-result selection, and cross-generation/round
transition policy remains in Core's `agent_system_gepa_kernel.py`. The
standalone `benchmarks/terminal_bench/` runner calls that kernel while owning
task acquisition, Harbor/Terminal Bench execution, verifier adaptation, and
benchmark reporting.

The release performance run must exercise the real Core path through dataset,
job, worker, artifact registration, context resolution, runtime injection, and
harness execution. A direct call to a method function is useful for unit tests
but is not release performance evidence.

Benchmark automation decides when tasks are requested and therefore preserves
benchmark-specific cadence. The A3 mechanical package migration does not yet
bind those requests to the `REV-1` through `REV-5` admission contract. That
queued/not-ready integration is required in the next Issue #156 PR before a
release performance run can claim the cross-session contract.

## OpenEvo Desktop

Desktop is a macOS application for scientists, not a developer dashboard or a
passive wrapper around an unrelated web product.

The normative renderer/sidecar/Core ownership, versioning, route, error, and
simulator boundary is `docs/architecture/desktop-core-contract-v1.md`.

The Tauri/Rust host owns native lifecycle, sidecar supervision, secure local
state, Keychain integration, file pickers, and app-level recovery. React owns
the user experience. The local sidecar forwards typed operations and manages
the SSH/Core connection; it does not become a second Core.

The ordinary-user flow is:

```text
install DMG
-> create or open a science project
-> add a remote server and verify SSH host identity
-> configure proxy/network settings when needed
-> run doctor and automatic bootstrap/repair
-> choose Codex subscription or self-deployed reference mode
-> choose a workspace and describe the science task
-> select memory, skill, and/or agent-system evolution
-> start and monitor the run
-> inspect transcripts, rounds, artifacts, lineage, and diffs
-> observe the next task queued while its required revision is prepared
-> run the next task on the atomically committed revision
-> export redacted diagnostics when needed
```

Desktop release requirements:

- real first-run, empty, loading, offline, reconnecting, degraded, failure, and
  success states;
- remote profile, proxy, workspace, model, and evolution configuration;
- safe bootstrap progress with retry and user-action boundaries;
- run list/detail, cancellation, retry, timeline, logs, and service health;
- pinned revision, transition progress, and explicit queued/not-ready state for the next task;
- memory preview/diff, skill bundle contents, agent-system diff, lineage, and
  promotion/reuse state;
- long operations remain responsive and resumable after restart;
- no raw shell panel, worker internals, benchmark UI, fake ready data, or local
  method registry;
- usable keyboard navigation, readable contrast, and layouts that fit supported
  Mac window sizes;
- a mounted/copied DMG smoke test on supported macOS architecture.

## Remote Bootstrap And Recovery

The user provides an SSH profile, workspace location, optional proxy settings,
and the selected execution mode. OpenEvo should automate setup on a fresh GPU
server as far as user-level permissions and declared policy allow.

Core/Desktop may create OpenEvo-owned directories and environments, upload and
verify the exact Core artifact, install supported user-space dependencies,
download models and runtime assets, start services, retry transient downloads,
and repair OpenEvo-owned state.

OpenEvo must not silently modify system package managers, Docker daemon
configuration, drivers, firewall policy, global shell profiles, or SSH private
keys. When those are required, doctor returns a specific user action and the UI
preserves completed work for retry.

Proxy and mirror settings apply to remote package/model downloads and services,
not only the local Desktop process. Diagnostics must show which endpoint and
proxy policy failed without exposing credentials.

## Security, Privacy, And Data

- No analytics, crash reporting, telemetry, or diagnostics upload is enabled by
  default.
- Passwords, API tokens, subscription credentials, private keys, and proxy
  credentials are never stored as plaintext project configuration.
- Desktop uses Keychain or secret references; Core uses protected remote secret
  references/files. Resolved secret values are excluded from logs, API errors,
  diagnostics, benchmark evidence, and release artifacts.
- Sidecar and Core APIs bind only to local interfaces and are reached through
  authenticated local IPC or an SSH tunnel.
- Diagnostics export is explicit, inspectable, and redacted.
- Deletion is path-contained and confirms the local and remote data affected.
- User documentation explains local and remote state locations, retention,
  deletion, uninstall behavior, model/runtime caches, and data sent to the
  user-selected Codex or model provider.

Resolved credentials, authorization headers, proxy userinfo, and app/backend
session tokens are redacted before persistence into backend databases, events,
timelines, harness transcripts, transcript-derived datasets, or any part of an
artifact record. The artifact boundary includes registration requests,
manifests, metadata, lineage, compatibility, scores, tags, URIs, payloads, and
rendered excerpts. The same rule applies to logs, diagnostics, exports, and all
other user-shareable files. This credential-redaction boundary does not
silently rewrite user-provided scientific task data; diagnostics and sharing
flows still require explicit review for research-data privacy.

Security tests use distinct canary secrets across config, environment, harness
input, and service responses. They inspect backend database fields, timelines,
transcripts, transcript-derived dataset records, complete artifact records and
payloads, logs, API errors, diagnostics, and release files and fail if a
credential canary persists or reaches a public artifact.

## Repository And Documentation

The release-facing repository presents OpenEvo, not legacy upstream history.
Active source, package metadata, examples, workflows, README, user docs, and Desktop
assets must contain only current OpenEvo identities and product paths.

Required release-facing documentation:

- README overview and architecture boundary;
- Desktop installation and unsigned macOS launch instructions;
- first-run, remote server, proxy, subscription, and self-deployed workflows;
- science task, run monitoring, artifact reuse, diagnostics, deletion, and
  uninstall guidance;
- Core architecture, backend API, runtime injection, and method integration;
- maintainer testing, benchmark gate, repository structure, and release process;
- limitations, privacy/security behavior, and troubleshooting by typed error.

Historical design records may remain under maintainer history paths when clearly
marked non-current. They must not appear in ordinary-user navigation or contain
runnable commands presented as current release behavior.

## Release Artifacts

External Beta publishes through GitHub Releases:

- one macOS OpenEvo Desktop `.dmg` for each supported architecture, or one
  declared universal build;
- SHA256 checksum files;
- the exact Core install artifact and a small descriptor containing version,
  platform compatibility, and checksum;
- release notes with supported modes, known limitations, privacy/security
  behavior, benchmark results, install/upgrade/uninstall instructions, and the
  unsigned/not-notarized warning;
- dependency lock and practical vulnerability/license evidence for shipped
  Python, npm, and Rust components.

The DMG contains or can fetch only the descriptor-matched Core bytes. Remote
bootstrap verifies the checksum before installation. A source checkout,
package-relative fallback wheel, or locally rebuilt substitute is not a release
path.

PyPI is not an External Beta release surface. Publishing remains disabled until
a later decision defines who consumes it and how it is tied to the same
validated Core artifact.

## Release Gates

All gates are blocking. Evidence should be the smallest durable output that
proves the behavior, normally a test report, benchmark summary, build artifact,
or screenshot. A prose-only signoff cannot replace a failed executable gate.

| Gate | Release acceptance |
| --- | --- |
| Algorithm preservation | Protected method behavior is unchanged and all three pass@1 rescue thresholds pass. |
| Core | Clean install starts a managed backend; both execution modes, pinned task revisions, atomic next-revision activation, queued/not-ready admission, run lifecycle, artifact resolution, injection, logs, doctor/repair, and typed errors pass integration tests. |
| Desktop | A packaged DMG completes first-run, remote setup, a science workflow, monitoring, artifact inspection/reuse, restart recovery, and diagnostics export without CLI use. |
| Science workflow | In both execution modes, each of `text_memory`, `skill_bundle`, and `agent_system` is produced, rendered, promoted, materialized, atomically committed with all other enabled outputs, and consumed by a follow-up task through Desktop and Core. |
| Product boundary | Core/Desktop packages contain no benchmark automation, public CLI/devkit surface, legacy product identity, or release-only development overrides. |
| Security and privacy | Secret canary, local-bind/auth, diagnostics redaction, deletion containment, and no-default-telemetry checks pass. |
| Repository and docs | Required docs exist, links work, examples match current behavior, and release claims are conservative. |
| Packaging and release | Final Core bytes, DMG, checksums, release notes, dependency evidence, and GitHub Release contents are mutually consistent. |

## Definition Of Done

OpenEvo is External Beta ready only when:

1. A new user can install the unsigned DMG and complete the ordinary science
   workflow described above on a supported Mac and remote server.
2. Core, Desktop, and benchmark automation obey the repository and dependency
   boundaries in this spec.
3. Codex subscription transcript and self-deployed reference workflows pass
   end to end without direct model-API task execution.
4. Textual memory, trajectory-to-skill, and agent-system performance gates meet
   12/21, 14/25, and 17/25 respectively with unchanged algorithm behavior.
5. The complete three-artifact-by-two-mode science matrix demonstrably produces,
   renders, promotes, materializes, atomically commits, and consumes selected
   artifacts in the next task without stale or partial revision fallback.
6. Security, privacy, diagnostics, documentation, clean-install, packaged-DMG,
   and release-artifact checks pass on the release commit.
7. The GitHub Release clearly identifies the build as unsigned External Beta
   and does not claim unsupported methods, leaderboard status, or PyPI support.

## Out Of Scope

- Designing or improving evolution algorithms.
- Per-method activation timing, background-versus-barrier policy choices, or streaming/in-session evolution method ABIs.
- Making unfinished parametric memory or OPSD methods release-blocking.
- Supporting additional harnesses beyond Codex in this release.
- A public CLI or Dev Kit product.
- Benchmark controls in Desktop.
- Windows or Linux Desktop distribution.
- Signing, notarization, or automatic updates for the unsigned External Beta.
- Compatibility with pre-release legacy upstream runtime data or package imports.

## Change Process

This spec is intentionally concise. Implementation details belong next to the
code in architecture docs, module READMEs, issues, tests, and workflows. Amend
this document only when the product boundary, supported mode, protected
algorithm behavior, release artifact, or release acceptance criterion changes.

Any proposal to change the protected OpenEvo architecture or evolution
algorithm is a separate research/architecture decision outside this goal and
must not be bundled into release cleanup.

Implementation work follows `implementation-plan.md`, issues #131 and #154, and the
repository process in `AGENTS.md`. Each substantial PR records focused tests,
docs impact, and whether protected evolution behavior can be affected. Fresh
independent reviews use `gpt-5.6-sol` with high reasoning at workstream and
release boundaries.
