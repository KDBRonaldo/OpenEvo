# Pluggable Evolution Framework

Status: A2.1 contract only; runtime cutover is deferred

Tracking: issue #137, productization step A2. A2.1 implements contracts for
`PLUG-1` through `PLUG-4`; A1 froze `PLUG-5`, which A2.2 migrates and verifies.

This contract makes evolution targets and methods pluggable without replacing
the existing OpenEvo Core architecture. A2.1 adds data models, validation, and a
frozen registry only. Current method dispatch, jobs, worker leases, artifact
registration, promotion, context resolution, gateway injection, Science config,
and Desktop behavior remain unchanged until their assigned cutovers.

## Boundary

The framework adds four concepts:

| Contract | Responsibility |
| --- | --- |
| Target | Names one carrier, artifact type, target handler, renderer, context order, and default method. It contains no algorithm. |
| Method | Names one algorithm for one target and declares config, ordered input bindings, output artifact types, execution/capture/harness/runtime compatibility, and implementation identity. |
| Plan | Stores the enabled target selections resolved for one run as canonical config plus exact reachable implementation identities. It contains no runtime progress. |
| Target handler | Projects resolver-ranked artifacts for one target into versioned, data-only context, staging, adapter, environment, and renderer contributions. Core performs and validates mutations. |

The public DTO names are `EvolutionTargetDescriptor`,
`EvolutionMethodDescriptor`, `TargetHandlerDescriptor`, and `EvolutionPlan`;
Target and Method below are concept names.

An evolution method may contain multiple generations, candidates, evaluations,
or rounds. Those steps and their winner selection remain algorithm-owned. The
framework does not introduce a generic schedule engine, stage ledger, or score
reranker. In particular, `agent_system_gepa_reflector` remains one method whose
existing GEPA behavior is protected by equivalence and performance gates.

The existing Core path remains authoritative:

```text
session/task events -> dataset artifact -> existing evolution job and lease
  -> registered method -> ArtifactRegisterRequest -> existing artifact store
  -> promotion/context resolve -> validated target handler -> gateway injection
```

## Registry And Identity

Core constructs one registry during startup. Registration is explicit and
single-threaded. Duplicate IDs, unknown references, incompatible target/artifact
pairs, malformed entry-point identities, invalid schemas, and unsupported
renderer contracts fail startup. `freeze()` canonicalizes the registry and
prevents further registration.

The registry contains target, method, and target-handler descriptors. It does
not retain a second method callable table after cutover: the method descriptor's
verified entry point is the dispatch source. During A2.1 no entry point is
imported or executed. The verified loader added in A2.2 resolves entry points
from the installed Core distribution or an explicitly enabled research plugin
and fails startup on an identity mismatch.

Each descriptor identity includes:

- descriptor kind and stable ID;
- canonical execution-relevant descriptor content;
- distribution name, version, immutable digest, and entry point;
- descriptor and implementation contract versions.

Canonical JSON uses sorted string keys, UTF-8, no insignificant whitespace, and
finite JSON numbers. Identity input contains no timestamps, host paths, callables,
or mutable objects. Distribution names are canonical lower-case/hyphen names;
distribution/contract versions and entry points are normalized, bounded, and
control-free. Registration order and process identity do not affect identity.

The frozen catalog exposes `registry_digest` over all descriptors for install
auditing. A plan stores a separate `registry_snapshot_digest` over only the
target, handler, and method identities reachable from enabled selections.
Installing an unrelated plugin therefore does not invalidate a plan. Descriptor
defaults referenced by an enabled target are either included in that closure or
excluded from its execution-identity projection; a half-hashed reference is not
allowed.

Snapshot access uses immutable canonical backing or defensive descriptor views.
Descriptor schema/default dicts and lists are recursively immutable. Canonical
snapshot backing and fresh defensive views additionally ensure even an explicit
base-class mutation bypass cannot alter later normalization or stored identity.

## Selection And Plan

Project editing uses a generic target map:

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
```

Desktop may retain config for a disabled target in its editing draft. Compiled
plans contain enabled targets only. Each resolved selection stores target,
handler, and concrete method IDs, canonical normalized config JSON and digest,
and all three identity digests. The plan also stores its validated execution
profile and reachable registry digest. Canonical strings make it deeply
immutable.

Config precedence is fixed:

```text
schema defaults < descriptor default_config < project selection config
```

Object values merge recursively; arrays and scalars replace the lower-precedence
value. The merged result must satisfy the method schema. Unknown targets,
methods, fields, and incompatible profiles fail before a job is created.

## Execution Compatibility

Execution and capture are independent axes:

- execution: `subscription` or `self_deployed`;
- capture: `transcript` or `token_level`.

`EvolutionExecutionProfile` also names the Core-owned harness identity and its
available harness/runtime capabilities. Methods explicitly declare supported
harness IDs plus required harness/runtime capabilities. Core derives the profile
from its verified harness and remote-server state; a caller cannot self-assert
capabilities. Subscription execution requires transcript capture. The External
Beta release profile permits Codex for subscription, while the generic contract
can later register other transcript-capable harnesses.

Examples of runtime capabilities include `gpu`, `trainer`, `adapter_serving`,
and `dynamic_adapter_loading`. They are compatibility declarations, not a
parametric algorithm design. Current non-parametric methods may require none.

## Method Inputs And Invocation

Each method owns an ordered tuple of `MethodInputBinding` values. A binding
declares source, artifact type, and minimum/maximum count. Core flattens bindings
in descriptor order while preserving each source's order and duplicates; it
never reconstructs worker inputs from a sorted artifact-type set. The resolved
binding records every artifact ID plus the canonical full input snapshot digest,
so duplicate IDs with different type/URI/name cannot be reordered undetected.

`MethodExecutionEnvelope` separates schema-validated user config from
Core-owned lineage, compatibility, score, tag, task, and round fields. User
schemas cannot claim Core-reserved top-level fields, user values cannot shadow
them, and secrets remain opaque Core references. During migration, the one
legacy adapter merges both maps and constructs a fresh `WorkerClaimedJob`;
method ID, flat `job.config`, and ordered `input_artifacts` must remain equivalent
to the current worker call.

New plugins receive `MethodExecutionContext` and the `CoreHarnessService`
inference surface. Its request has harness/model and generation inputs, not an
endpoint, headers, arbitrary metadata, API keys, or tokens. Prompt/system and
response text are capped at 1 MiB, requested output at 1,048,576 tokens, timeout
at 24 hours, and transcript references at 4,096 characters. A2.2 binds current
callables through the legacy adapter without changing signatures, prompts,
retries, or algorithm bodies. Method plugins run with worker-process permissions;
validation is not a Python sandbox.

Project value `agent_system.method=auto` is not a method, plugin, or generic
schedule policy. Job materialization calls the single narrow
`resolve_agent_system_method(requested_method, prior_dataset_artifact_ids)`:
no prior dataset selects `agent_system_reflector`; otherwise it selects
`agent_system_history_reflector`. Plans/jobs store the concrete method, while
lineage stores the requested value and prior dataset IDs. GEPA's internal
candidate/round selection is separate algorithm-owned behavior.

## Bounded Config Schema

Method config uses a closed JSON Schema 2020-12 subset:

- types: object, array, string, integer, number, and boolean;
- keywords: type, title, description, properties, required,
  `additionalProperties: false`, items, enum, const, default, numeric/string/
  collection bounds, and nullable `anyOf` only;
- every object declares properties and is closed; every array declares one item
  schema; defaults and enum/const values validate locally;
- root-inclusive depth <= 8, nodes <= 256, object properties <= 64, enum values
  <= 128, strings <= 4,096 characters, and arrays <= 256 items.

References, recursion, patterns/regex, formats, content annotations, arbitrary
combinators, conditionals, and unknown keywords are forbidden. Secret fields
must be explicit `*_ref` strings containing a Core-issued
`openevo-secret:<id>` reference. Other sensitive names and resolved values are
forbidden; references cannot appear in default, enum, or const. Violations fail
closed without echoing the rejected value.

## Target Handler Contributions

After existing compatibility filtering, Core retains the current descending
`quality`/`heldout_reward_delta`, `created_at`, and `artifact_id` ranking. One
handler call receives that target's ordered artifacts, existing target limits,
execution profile, and base model. It consumes an ordered subsequence and must
not rerank candidates.

`TargetHandlerInput` contains Core-issued opaque payload handles and a canonical
file inventory, never a host source path or raw artifact URI. It also contains
Core-resolved absolute paths inside the agent runtime for target data, harness
skills, and harness instructions; handlers cannot choose those roots. Entries
contain relative path, MIME, byte size, and SHA-256. A directory digest is SHA-256 over canonical JSON
`{contract_version: "1", entries: [...]}`, with entries sorted and relative to
the selected root. File count, depth, bytes, artifacts, contributions, and text
are bounded. `TargetHandlerOutput` may contain only:

Handler contract v1 limits are: 128 ranked/consumed artifacts, 256 total output
contributions, 256 inventory/renderer files, depth 32, 4,096-character relative
paths, 8 GiB per inventory entry, 16 GiB per payload tree, 1 MiB semantic text,
and 256 KiB for each canonical manifest/scores or renderer JSON payload. Payload
bytes are streamed/verified by the later materializer rather than loaded as one
in-memory value.

- instruction contributions with source artifact IDs;
- staged payloads referencing the verified inventory or bounded inline merged
  text, plus digest/MIME, destination scope, and safe relative destination;
- adapter contributions with source artifact, ID/format, model, and weight;
- Core-approved environment bindings to staged contributions;
- one renderer payload referencing output contribution IDs.

Each descriptor allowlists contribution kinds, destination scopes, MIME types,
URI schemes, and environment names. Renderer v1 has closed `markdown`,
`file_bundle`, `structured_summary`, and `adapter` data. Renderer content must
match referenced runtime contributions; no renderer accepts executable markup
or arbitrary extension data. `structured_summary` v1 fields exactly mirror a
referenced instruction/inline-text value; richer computed summaries require a
future typed contract. Directory entries are each checked against the MIME
allowlist, and adapter ID/format/base-model values must match both the request
and the Core-issued source artifact manifest.

`target_data` resolves under shared `/openevo/session/evolution`, retaining the
canonical `memory.md`, `agent_system.md`, `skills/`, and `adapters.json` layout.
Harness scopes are resolved by the harness adapter and instruction paths use the
existing allowlist. Contributions contain neither commands nor final host paths.

Within a target, its handler performs the current semantic merge first: memory
and agent-system text keep current clipping/concatenation, skills/adapters keep
their count/order, and subscription suppresses adapters. It emits each final
destination once. Contribution provenance and renderer order must preserve the
ranked artifact sequence; the same semantic text projected to instruction and
file is charged once. Existing clipping remains character-based, while a
separate 1 MiB UTF-8 byte limit bounds resource use. Core resolves logical scopes to final runtime roots before
checking context-wide destination/environment conflicts, and binds adapters to
the requested base model and actual runtime application limit. No
last-writer-wins behavior is allowed.

A2.1 validates DTO inventory/digest consistency only. Before A2.4 cutover, the
Core payload scanner/materializer must bind opaque handles to roots and implement
realpath containment, no-follow opens, symlink/device/FIFO/socket rejection,
archive limits, post-open identity, and verified staging. An inventory supplied
by a handler is never proof that host bytes were read safely.
Method plugins run with worker permissions. Target handlers are Core-loaded and
run with Core-process permissions unless a future isolation boundary says
otherwise. Neither contract claims to sandbox arbitrary Python.

Existing carriers map without changing behavior:

| Target | Contributions |
| --- | --- |
| `text_memory` | Instruction text, staged memory file, current Core-owned memory binding, Markdown renderer. |
| `skill_bundle` | Verified bundle staged to `harness_skills`, file-bundle renderer. |
| `agent_system` | Instruction text plus allowlisted `harness_instruction` target, Markdown renderer. |
| `parametric_memory` | Adapter contribution and adapter renderer; inactive for the current non-parametric release scope. |

## Capability And Desktop Projection

`EvolutionCapabilitiesV1` is generated by the remote Core frozen registry. It
contains schema version, Core version, full registry digest, evaluated generic
profile, and target-rooted method options. The local sidecar only proxies or
caches that response. Each target includes friendly metadata, renderer contract,
configured default, nullable profile-supported effective default with its typed
support result, and handler identity. Target and handler/default exposure must
be audience-compatible. Each method includes schema/defaults,
ordered inputs, identity, separate execution/capture/harness/runtime declarations,
and four support axes. Axis states are `supported`, `unsupported`, or
`unavailable`, with stable reason codes and missing requirements.

`Codex Subscription Transcript` may be a release-profile display label, but it
is not a framework mode ID. The sole release mapping converts it to
`subscription + transcript + codex` and Self-Deployed to
`self_deployed + transcript + codex`; it never infers token-level availability.
Desktop renders target toggles, friendly
method selection, and schema-driven config from capabilities. It never carries a
method/target registry. A test target using an existing contribution vocabulary
and renderer must cross compiler, resolver, gateway, capabilities, persistence,
and Desktop without a target-ID switch.

## Algorithm And Artifact Preservation

Methods continue to accept the existing claimed-job input and return existing
`ArtifactRegisterRequest` values until a separately justified API change. Their
declared output artifact types must include the target artifact type. Candidate
generation, evaluation, history accumulation, tie-breaking, and best-result
selection stay inside the method. Core does not infer a winner from generic
scores and does not rewrite algorithm output during promotion or context reuse.

The following remain frozen during adaptation:

- `text_memory_expel_reflector` prompts, defaults, filters, and artifact output;
- `skill_bundle_reflector` synthesis and artifact output;
- `agent_system_gepa_reflector` candidate, generation, round, objective,
  history, `None`, and tie-break behavior;
- separate project `agent_system.method=auto` prior-dataset resolution.

## Delivery Order

| Step | Deliverable |
| --- | --- |
| A2.1 | Contracts, bounded schema, ordered input/legacy invocation adapter, handler input/output validation, capability DTOs, frozen registry, identity, and focused tests only. |
| A2.2 | Mechanically register current methods/targets/handlers and preserve GEPA behavior; no dispatch cutover. |
| A2.3 | Generic project selections, plan compiler, capabilities, and registry dispatch on existing jobs/workers/artifacts. |
| A2.4 | Generic context/runtime contribution engine and capability-driven Desktop configuration/rendering. |
| A2.5 | Remove duplicate registries and target switches, document extension paths, and pass behavior/performance gates. |

A2.1 verification lives in `tests/evolution/test_framework_*.py` and proves
strict DTOs, schema bounds, default precedence, profile compatibility, immutable
snapshot backing, registration-order/fresh-process identity, reachable closure,
safe handler contributions, and plan consistency. Existing algorithm, store,
worker, gateway, trajectory, and rollout regressions prove the contract-only
stage has no runtime effect.
