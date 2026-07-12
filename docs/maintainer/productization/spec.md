# OpenEvo Productization And External Beta Spec

Status: canonical product and release specification

Tracking issue: #131

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
- Preserve the proven Polar-derived architecture: gateway, rollout, runtime,
  capture/trajectory, evolution backend, worker, artifact, context resolution,
  and runtime injection keep their responsibilities and data flow. Public
  OpenEvo identity, paths, imports, packaging, API access, and missing product
  capabilities may change; the underlying architecture is not redesigned as
  part of productization.
- Runtime identity is OpenEvo-only: `OPENEVO_*`, `/openevo/session`,
  `.openevo/evolution`, and `openevo.session_completed`.
- Legacy Polar packages, wrappers, commands, runtime markers, and public product
  identity are not retained for compatibility.

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
- datasets, evolution jobs, workers, artifacts, lineage, compatibility,
  promotion, and context resolution;
- staging promoted memory, skills, and agent-system artifacts before a later
  harness run;
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
commands.

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

### Self-Deployed Reference

- The release-supported reference profile uses
  `Qwen/Qwen3-Coder-30B-A3B-Instruct` with a pinned model revision, vLLM
  configuration, and hardware assumptions recorded in the shipped profile.
- The canonical profile path is
  `src/openevo/projects/science/profiles/self-deployed-reference-v1.json`. It is
  a versioned lockfile, not user prose. It must pin the Hugging Face commit,
  vLLM version and arguments, dtype/context length, host/port, GPU count and
  minimum VRAM, disk/cache assumptions, startup/download timeouts, proxy/cache
  behavior, health endpoint and expected served model, and compatible Core
  version range.
- Desktop may accept another Hugging Face model ID as an advanced best-effort
  configuration, but it is not release-supported unless added to this spec and
  the release test matrix.
- Desktop/Core can configure HTTP/HTTPS proxy settings, install supported user
  space dependencies, download model snapshots, start vLLM, and verify its
  health.
- Automatic repair stops before root-only system changes, driver installation,
  license acceptance, unavailable credentials, or unsupported hardware. The UI
  then reports the exact unresolved action.
- The architecture must remain extensible to parametric evolution, but this
  release does not design or validate new parametric methods.

## Evolution Contract

Core converts harness execution into evolution inputs and typed artifacts:

```text
session events or transcript
-> dataset artifact
-> evolution job and registered method
-> typed artifact
-> promotion and context resolution
-> staging before a later harness session
```

The release-supported non-parametric artifact families are:

| Artifact | Required runtime behavior |
| --- | --- |
| `text_memory` | Stage a readable memory payload and expose it to the next harness instruction. |
| `skill_bundle` | Stage a bundle containing `SKILL.md`; evolved skills take precedence over static skills. |
| `agent_system` | Stage the canonical instruction payload and write only to an allowlisted harness instruction path. |

Artifacts retain source dataset/job lineage, compatibility, scores when
produced by the method, promotion state, and payload integrity. Core and
Desktop consume one artifact contract; Desktop must not hide drift through a
separate adapter.

Candidate generation, evaluation, best-result selection, and promotion belong
to the evolution method. In particular, the existing
`agent_system_gepa_reflector` behavior that selects its best result for
promotion is protected algorithm logic and is allowed. Productization must
preserve that behavior exactly.

Core records and stages the method-selected promoted artifact; Desktop renders
the candidates and method-owned selection evidence; benchmark automation scores
the promoted output. None of those layers may replace the method's decision,
rerank candidates, or promote a different result. The agent-system release gate
must freeze the exact candidate-generation, evaluation, selection, and
promotion path that produced the historical 17/25 result. If that path cannot
be recovered, the gate remains blocked rather than guessing a replacement.

## Algorithm Preservation And Performance Gate

Three method families have already demonstrated useful evolution and block the
release if productization regresses them:

| Family | Canonical method ID | Frozen baseline-failed subset | Required pass@1 rescue count |
| --- | --- | ---: | ---: |
| Textual memory | `text_memory_expel_reflector` | 21 applicable tasks | at least 12/21 |
| Trajectory-to-skill | `skill_bundle_reflector` | 25 tasks | at least 14/25 |
| Agent-system | `agent_system_gepa_reflector` | 25 tasks | at least 17/25 |

These are independent gates. A run enables one family only and cannot borrow
artifacts or successes from another family.

Before further structural changes, maintainers must freeze the current source
locations and normalized contents of each canonical method, its prompts,
defaults, filtering, candidate/selection helpers, and artifact construction.
Moving files and changing imports are allowed only when normalized behavior is
unchanged.

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
- treat method-internal candidate evaluation and best-result promotion as part
  of the protected evolution algorithm, not as extra pass@k task attempts;
- archive enough configuration, selected-artifact, injection, and result data
  to reproduce the count;
- block release when any family misses its threshold.

The historical raw runs are no longer available. The counts above are the
maintainer-confirmed targets; they must not be silently lowered. The first
clean reproducible run becomes the durable release evidence while source and
behavioral guards protect against productization drift.

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

Code location does not determine algorithm ownership. The current
`terminal_bench_per_task.py` also contains GEPA candidate evaluation,
best-result selection, and promotion behavior that produced the protected
17/25 result. A1 must classify and freeze those exact helpers. During benchmark
migration they move mechanically to an algorithm-owned Core module or existing
method boundary, with identical behavior; only task acquisition, Harbor/
Terminal Bench execution, verifier adaptation, and benchmark reporting move to
`benchmarks/terminal_bench/`.

The release performance run must exercise the real Core path through dataset,
job, worker, artifact registration, context resolution, runtime injection, and
harness execution. A direct call to a method function is useful for unit tests
but is not release performance evidence.

## OpenEvo Desktop

Desktop is a macOS application for scientists, not a developer dashboard or a
passive wrapper around an unrelated web product.

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
-> reuse promoted artifacts in a later run
-> export redacted diagnostics when needed
```

Desktop release requirements:

- real first-run, empty, loading, offline, reconnecting, degraded, failure, and
  success states;
- remote profile, proxy, workspace, model, and evolution configuration;
- safe bootstrap progress with retry and user-action boundaries;
- run list/detail, cancellation, retry, timeline, logs, and service health;
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

The release-facing repository presents OpenEvo, not its Polar history. Active
source, package metadata, examples, workflows, README, user docs, and Desktop
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
| Core | Clean install starts a managed backend; both execution modes, run lifecycle, artifact resolution, injection, logs, doctor/repair, and typed errors pass integration tests. |
| Desktop | A packaged DMG completes first-run, remote setup, a science workflow, monitoring, artifact inspection/reuse, restart recovery, and diagnostics export without CLI use. |
| Science workflow | In both execution modes, each of `text_memory`, `skill_bundle`, and `agent_system` is produced, rendered, promoted, staged, and consumed by a follow-up run through Desktop and Core. |
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
   renders, promotes, stages, and consumes the selected artifacts in later runs.
6. Security, privacy, diagnostics, documentation, clean-install, packaged-DMG,
   and release-artifact checks pass on the release commit.
7. The GitHub Release clearly identifies the build as unsigned External Beta
   and does not claim unsupported methods, leaderboard status, or PyPI support.

## Out Of Scope

- Designing or improving evolution algorithms.
- Making unfinished parametric memory or OPSD methods release-blocking.
- Supporting additional harnesses beyond Codex in this release.
- A public CLI or Dev Kit product.
- Benchmark controls in Desktop.
- Windows or Linux Desktop distribution.
- Signing, notarization, or automatic updates for the unsigned External Beta.
- Compatibility with pre-release Polar runtime data or package imports.

## Change Process

This spec is intentionally concise. Implementation details belong next to the
code in architecture docs, module READMEs, issues, tests, and workflows. Amend
this document only when the product boundary, supported mode, protected
algorithm behavior, release artifact, or release acceptance criterion changes.

Any proposal to change the protected Polar-derived architecture or evolution
algorithm is a separate research/architecture decision outside this goal and
must not be bundled into release cleanup.

Implementation work follows `implementation-plan.md`, issue #131, and the
repository process in `AGENTS.md`. Each substantial PR records focused tests,
docs impact, and whether protected evolution behavior can be affected. Fresh
independent reviews use `gpt-5.6-sol` with high reasoning at workstream and
release boundaries.
