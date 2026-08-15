# Pluggable Evolution Framework

Status: A2.3 Core plus A2.4 Desktop config/preflight, executable handlers, payload scanner, internal projection, and generic Core materializer implemented

Tracking: issues #137, #139, #141, #142, #144, #146, #148, and #150, productization step A2. A2.1
implements contracts for `PLUG-1` through `PLUG-4`; A2.2 catalogs the existing
implementations; A2.3 covers project configuration, per-round plans, durable
job identity, verified worker dispatch, and the remote registry capability
projection consumed by Desktop.

This contract makes evolution targets and methods pluggable without replacing
the existing OpenEvo Core architecture. A2.1 added the data models and
validation. A2.2 adds a deterministic built-in catalog and distribution-backed
entry-point verification. A2.3 now uses the canonical target map in Science,
experiments, and Desktop, resolves `agent_system=auto` from the round-start
dataset snapshot, persists the resulting plan with each new experiment job,
dispatches that job only through its verified method handle, and publishes
capabilities from the same frozen registry. Artifact registration keeps its
existing contract. The implemented A2.4 slice adds capability-driven Desktop
selection/config editing, compiler-owned config injection, registry-bound remote
project preflight, and a generic Core materializer for validated projections.
Strict v2 transport, Gateway injection, external-target acceptance, and removal
of remaining target-specific runtime switches remain assigned to unfinished
A2.4/A2.5 work.

Issue #154 defines one product-wide cross-session activation contract rather
than per-method scheduling: every admitted task pins one immutable
context/model/adapter revision; evolution and training consume the sealed
completed-task dataset outside inference; all enabled target outputs and any
required serving preparation become one all-or-nothing revision for the next
task. No method descriptor or config selects online/offline timing,
background/barrier behavior, or an in-session streaming ABI. This revision
lifecycle is specified but not implemented by the current internal A2
materializer; strict transport, Gateway cutover, admission, and atomic revision
state remain subsequent work.

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

The existing Core evolution path remains authoritative through artifact publication:

```text
session/task events -> dataset artifact -> existing evolution job and lease
  -> registered method -> ArtifactRegisterRequest -> existing artifact store
internal implemented path: promotion -> validated target handler -> generic materializer
current public path: promotion -> legacy v1 context resolve -> legacy Gateway injection
```

## Registry And Identity

Core constructs one registry during startup. Registration is explicit and
single-threaded. Duplicate IDs, unknown references, incompatible target/artifact
pairs, malformed entry-point identities, invalid schemas, and unsupported
renderer contracts fail startup. `freeze()` canonicalizes the registry and
prevents further registration. Startup then verifies every frozen identity and
publishes method/handler handles only after verification succeeds; freezing a
descriptor graph alone does not make it executable.

The registry contains target, method, and target-handler descriptors. It does
not retain a second method callable table after cutover: the method descriptor's
verified entry point is the dispatch source. A2.2 verifies entry points from a
locked installed Core distribution or an explicitly supplied research-plugin
lock and fails on an identity mismatch. It does not discover or auto-enable
installed plugins.

The expected distribution digest must come from an external release descriptor
or maintainer plugin lock. The loader never hashes the running code and then
trusts that self-generated value. For wheel installs it verifies, before target
module import:

- canonical distribution name, exact version, and wheel SHA-256;
- a unique non-editable installed distribution;
- wheel member safety and the installed file inventory byte-for-byte;
- no symlinked files or untracked importable modules under owned packages;
- module ownership, import origin, qualified attribute name, and callable
  signature.

Release mode rejects editable and source-tree installs. Maintainer source use
requires a separate explicit source lock; it is not a fallback in this loader.
The future Core install descriptor supplies the same wheel SHA-256 used by
Desktop bootstrap. A verified lock can describe a research plugin, but the
plugin remains trusted server code and is never sandboxed by this mechanism.

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

## A2.2 Built-In Catalog

`openevo.evolution.framework.builtins` registers four targets, four handler
descriptors, eleven legacy method callables, and two context-native methods:
`text_memory_memevolve` and `parametric_memory_sd_lora`. Target descriptors retain
non-executable identity anchors. Handler descriptors point to the four
pure callables in `builtin_handlers`; release loading verifies their exact wheel
inventory, entry point, signature, identity, and distribution attestation before
sealing them in `VerifiedExecutableRegistry.handler_handles`. The internal projection
resolver invokes these verified handles; the public legacy resolver and Gateway runtime
have not yet cut over.

| Target | Default method | Exposure | Renderer |
| --- | --- | --- | --- |
| `agent_system` | `agent_system_gepa_reflector` | Desktop | `markdown` |
| `text_memory` | `text_memory_expel_reflector` | Desktop | `markdown` |
| `skill_bundle` | `skill_bundle_reflector` | Desktop | `file_bundle` |
| `parametric_memory` | `parametric_memory_sd_lora` | internal | `adapter` |

All current legacy `METHOD_REGISTRY` keys have exactly one method descriptor whose
entry point is `openevo.evolution.methods:<method_id>`. The A2.2
`load_builtin_method_handles` check remains an anti-drift test for exactly that
legacy subset. `text_memory_memevolve` and `parametric_memory_sd_lora` are loaded
from their independently verified context-method entry points and are absent from
both legacy method tables. Production plan-bound jobs dispatch from
`VerifiedExecutableRegistry.method_handles`; they never fall back to
`METHOD_REGISTRY`. The legacy table remains only for unplanned benchmark jobs
until their A2.5 migration.

Only the three performance-protected methods are Desktop-exposed in the A2.2
catalog, and they remain `experimental` until the release performance gates
pass. Other text methods are maintainer-visible. Incomplete parameter methods
remain internal. Pareto and GEPA descriptors declare both `agent_system` and
`report` output. Manual skill, agent-system, and parametric materializers
declare optional current-dataset and prior-target inputs so their existing
experiment job projection is preserved even though the algorithms do not
require those inputs.

Descriptor config schemas expose only bounded algorithm settings. Core-owned
lineage, compatibility, scores, tags, promotion fields, audit policy, evaluator
results, credentials, endpoints, and arbitrary trainer commands are not user
schema fields. Reflector plans require a model and the `codex_cli` harness
provider; the catalog does not expose legacy direct `openai_chat` connection
settings. Core-owned audit/evaluator fields remain available to the exact legacy
adapter without becoming user-controlled schema.

Existing callers disagree on whether a current dataset precedes or follows
history datasets. Multi-dataset legacy methods therefore use one
`explicit_inputs` dataset binding followed by prior target artifacts. The Core
caller supplies its existing ordered dataset sequence, and the adapter does not
reinterpret it. This preserves both experiment-runner and protected benchmark
ordering instead of imposing a new global current/history order.
The experiment compiler preserves its pre-framework projection: ExpeL receives
only the current-round dataset, while agent-system history, Pareto, and GEPA
receive current then prior datasets. New methods request history explicitly with
`history_datasets`; they do not inherit a legacy method-ID convention.

`parametric_memory_sd_lora` is an internal `method_context_v1` method and is not
present in the legacy `METHOD_REGISTRY`. It is self-deployed only and requires
`adapter_serving`, `gpu`, and `sd_lora_continual_trainer`. The launcher publishes
the latter two capabilities only when the Daemon has the complete optional
training profile and CUDA is available. Its closed schema contains bounded
hyperparameters and an exact model revision; it has no trainer command,
endpoint, credential, task router, or benchmark-specific projection field.
Skill bundles permit common text, code, data, and image MIME types, with
`application/octet-stream` as the inventory fallback for other auxiliary files.

The execution envelope reconstructs the existing flat `job.config` immediately
before invoking a legacy ABI method. This preserves current algorithm inputs
without allowing user config to shadow Core-owned controls.

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

`ProjectEvolutionTargetSelection` is the single project/experiment map-value
contract. Its fields are exactly `enabled`, `method`, and `config`; enabled
targets require a method, while disabled targets may retain draft method/config
state. The former Science booleans and experiment-level `artifacts` object are
removed rather than accepted through aliases. Desktop may keep checkbox state
internally, but every saved or submitted Core project uses this target map.
Selection config uses canonical JSON backing and returns defensive object
copies; validated Core/Science target maps are read-only views. Python inputs
cannot coerce non-string target keys into IDs. Because Desktop must preserve
unknown method config through JavaScript, project config integers are limited to
the inclusive `[-9007199254740991, 9007199254740991]` range, finite integral
floats are normalized to integers, and other finite floats retain binary64
semantics. Larger integer identities must be represented as strings.

The current automation defaults remain explicit in project configuration while
the registry migration is in progress: `text_memory_reflector`,
`skill_bundle_reflector`, `agent_system=auto`, and disabled
`parametric_memory_register`. They are not silently replaced by the catalog's
release-profile defaults. Capability-driven Desktop defaults are applied only
when Desktop consumes the connected Core registry in A2.4.

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
value. The merged result must satisfy the method schema. Unknown enabled targets,
methods, fields, and incompatible profiles fail before a job is created. An
unknown disabled target remains as draft state but is absent from the plan.

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

Every method descriptor has one closed invocation ABI that is part of its
canonical identity:

- `legacy_worker_job_v1`: `(WorkerClaimedJob, Path) -> artifacts`, invoked only
  through `invoke_legacy_method`;
- `method_context_v1`: `(MethodExecutionContext) -> artifacts`, with inference
  available only through `CoreHarnessService`.

The verified loader checks the exact signature selected by that field and the
worker dispatches by the field, never by signature guessing. Missing or unknown
ABIs, alternate parameter names/kinds, and variadic signatures fail closed
before the registry becomes executable.

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

The managed reference worker now supplies the first production implementation
of that surface for `harness_id=codex`. It invokes the existing Codex CLI
subscription backend directly; it does not add an LLM HTTP endpoint or accept a
method-supplied endpoint, API key, environment, command, credential path, or
working directory. Core copies the remote user's verified `~/.codex/auth.json`
into a private one-turn `CODEX_HOME`, uses an empty private home/workspace, sends
the prompt over stdin, and runs `codex exec` with user config/rules ignored,
ephemeral state, a read-only sandbox, and shell, unified-exec, and standalone
web-search features disabled. It accepts only a completed bounded JSON
transcript, redacts credential values from errors and the final text, and
scrubs the staged credential before removing the private run tree. No host path
is returned as a transcript reference.

### MemEvolve Declarative Adaptation

`text_memory_memevolve` is an experimental `method_context_v1` method available
for Codex transcript or token-level runs in subscription and self-deployed
execution profiles. It receives ordered dataset artifacts plus optional prior
`text_memory`, and all model calls go through `CoreHarnessService`; it accepts no
endpoint, API key, command, or host model path.

For each configured candidate, the method independently analyzes trajectory
evidence and prior memory, then asks Codex for one static Markdown candidate.
It rejects generated `BaseMemoryProvider` implementations and validates output
and forbidden literals before a final Codex evidence judge selects one candidate.
The method registers exactly one `text_memory` artifact with method-derived
`quality`, full input lineage, and an explicit
`adaptation_scope=declarative_text_memory_v1` / `paper_equivalent=false`
manifest.

This is not a faithful reproduction of the upstream MemEvolve runtime. The
upstream algorithm generates executable providers with retrieval, online
ingestion, management, simulation validation, and task-executed Pareto
tournaments. OpenEvo neither executes LLM-generated Python in the Daemon nor
runs candidate methods inside the immutable source task. Performance reports
must therefore call this the OpenEvo declarative adaptation and must not compare
its score as though it were the upstream paper implementation. A faithful
future design requires a separately reviewed closed declarative provider ABI
and successor-session candidate evaluation contract.

This Codex implementation currently supports model selection and timeout. A
method request that selects another harness or supplies `temperature` or
`max_output_tokens` fails closed because Codex CLI does not provide those
controls through this contract. This service is model-only algorithm inference;
ordinary Science task execution continues to use the full
`TaskRequest -> rollout -> Gateway -> CodexHarness` path and its managed runtime
credential-isolation profile.

Project value `agent_system.method=auto` is not a method, plugin, or generic
schedule policy. Job materialization calls the single narrow
`resolve_agent_system_method(requested_method, prior_dataset_artifact_ids)`:
no prior dataset selects `agent_system_reflector`; otherwise it selects
`agent_system_history_reflector`. Plans/jobs store the concrete method, while
lineage stores the requested value and prior dataset IDs. GEPA's internal
candidate/round selection is separate algorithm-owned behavior.

Experiment dry-run and live-run materialization pass the same snapshot of
datasets completed before the current round. The current round's dataset is not
added until that round finishes. This means a later round without history still
selects the plain reflector, while round zero with explicitly supplied history
selects the history reflector. The compiler validates each enabled target and
method against an explicit frozen registry/profile, assigns a stable plan ID
from experiment/run/task/round identity plus ordered prior datasets, and sends
the full plan to `POST /v1/planned-jobs`.

The compiler emits every enabled plan selection. It preserves the protected
built-in execution order and appends external targets by stable ID; it never
silently drops an unknown-to-the-UI target. Artifact type comes from the target
descriptor rather than the target ID. Input artifacts are projected in descriptor
order from `current_dataset`, `history_datasets`, `current_target_artifacts`, or
`explicit_inputs`, including multiple bindings with the same artifact type. A
method descriptor may bind a config field to a closed Core injection source.
The compiler removes any project value for that field, writes the authoritative
source value, and then performs full registry normalization; it never infers an
injection from a target ID or coincidental property name.

The store atomically persists the immutable plan and a job execution envelope
containing the target, method identity, canonical user/Core config, ordered
input artifact snapshots, and declared output types, plus an independent digest
over the complete envelope. `(plan_id, target_id)` is
unique: an identical create retries idempotently, while a conflicting retry
fails. Plan-bound claims require both queue capabilities and exact verified
method-ID-to-identity-digest capabilities; a worker without identities can claim
only legacy jobs. Before granting a lease, the store validates persisted plan,
envelope/digest, config, input snapshots, and output declarations against the
active frozen registry. A corrupt plan-bound row is quarantined as failed before
a lease is issued. The worker then revalidates
them against its executable registry and renews the lease every one third of the
claim duration. Completion repeats that validation before staging and publish,
rejects undeclared output types, keeps job-owned outputs staged and invisible
until artifact publication and job success commit atomically, and
adds store-owned execution lineage without rewriting the algorithm's payload.
Failure, lease expiry, startup recovery, and successful retry remove stale staged rows and manifests.

Workers are trusted Core processes on the loopback backend boundary. Identity
capability matching prevents stale or wrong locked workers from consuming a
job; it is not cryptographic attestation against an arbitrary process running as
the same remote user. Backend authentication/process isolation belongs to the
Core lifecycle security workstream and must not be inferred from this contract.

The generic `POST /v1/jobs` and `run_method()` path is bounded to benchmark
automation that has not yet migrated. It cannot execute a plan-bound job or act
as a fallback after identity verification fails.

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
are bounded. For text projection, the opaque payload service performs the full
source identity/digest verification while streaming and returns only a
`read_utf8_prefix` result bounded by requested character and UTF-8 byte limits;
handlers cannot request an inventory-sized in-memory read. `TargetHandlerOutput`
may contain only:

Each handler descriptor identifies input v1, renderer v1, and output/contribution
v2 independently; Core checks all three at invocation. Their limits are: 128
ranked/consumed artifacts, 256 total output contributions, 256 inventory/renderer
files, depth 32, 4,096-character relative
paths, 8 GiB per inventory entry, 16 GiB per payload tree, 1 MiB semantic text,
and 256 KiB for each canonical manifest/scores payload. Renderer JSON remains
bounded to the worst-case JSON escaping of one 1 MiB text contribution plus
fixed envelope overhead, so an otherwise valid text projection cannot fail only
because it is rendered. Payload bytes are streamed/verified by the later
materializer rather than loaded as one in-memory value.

An optional bounded `instruction_preamble` belongs to the handler descriptor identity.
It preserves target-owned runtime framing without teaching the materializer or Gateway
any target ID. The contribution text, renderer text, and staged payload remain the
handler's evolved content; the generic materializer applies the preamble only when
building the ordered runtime instruction prefix.

- instruction contributions with source artifact IDs;
- staged payloads referencing the verified inventory or bounded inline merged
  text, plus digest/MIME, destination scope, and safe relative destination;
- adapter contributions with source artifact, approved payload digest/size,
  ID/format, model, and weight;
- Core-approved environment bindings to staged contributions;
- Core-approved `scope_root` environment bindings for one descriptor-allowlisted
  logical destination root, such as the shared harness skills directory;
- one renderer payload referencing output contribution IDs.

The three non-parametric target handlers can also publish one closed, versioned
runtime-control JSON file through the existing staged-payload/environment
vocabulary when an artifact explicitly contains `manifest.runtime_control`.
This is the stable boundary between Core-owned evolution semantics and a harness
adapter:

- `memory` declares `read_timing`, `write_timing`, and the invariant that an
  accepted update is visible only to the next session;
- `skill` declares harness discovery/loading semantics while the skill contents
  remain an ordinary verified bundle;
- `agent_system` declares native instruction-file loading and may carry a
  data-only `AgentSpawnPlanV1` containing bounded agent roles and instructions.

The spawn plan cannot contain commands, executables, environment variables,
credentials, model credentials, or host paths. A verified harness adapter owns
actual spawning and must fail closed when it does not support the declared
control version. When the field is absent, the built-in defaults reproduce the
pre-control behavior without adding another contribution:
memory is read at session start and updated after session close, skills use
harness discovery, agent-system uses native instruction files, and no structured
agents are spawned. Existing artifacts without `manifest.runtime_control`
therefore behave unchanged. A Core method may evolve the policy by returning the
same typed manifest field; Desktop does not inspect or hard-code these controls.

Output/contribution v2 adds the mandatory approved payload digest and byte size to
adapter contributions. This is intentionally versioned separately from renderer v1;
v1 handler outputs are not accepted as v2, so installed handlers and consumers cannot
silently disagree about adapter provenance.

Each descriptor allowlists contribution kinds, destination scopes, MIME types,
URI schemes, and environment names. Renderer v1 has closed `markdown`,
`file_bundle`, `structured_summary`, and `adapter` data. Renderer content must
match referenced runtime contributions; no renderer accepts executable markup
or arbitrary extension data. `structured_summary` v1 fields exactly mirror a
referenced instruction/inline-text value; richer computed summaries require a
future typed contract. Directory entries are each checked against the MIME
allowlist, and adapter ID/format/base-model values must match both the request
and the Core-issued source artifact manifest.

Text source entries, not only normalized inline outputs, must satisfy the
handler descriptor MIME allowlist. A `skill_bundle` must contain a root
`SKILL.md`; its renderer lists files in ranked bundle order and canonical path
order within each bundle, and the frozen validator proves that order.

`target_data` resolves under shared `/openevo/session/evolution`, retaining the
canonical `memory.md`, `agent_system.md`, `skills/`, and `adapters.json` layout.
Harness scopes are resolved by the harness adapter and instruction paths use the
existing allowlist. Contributions contain neither commands nor final host paths.

Within a target, its handler performs the current semantic merge first: memory
and agent-system text keep current clipping/concatenation, skills/adapters keep
their count/order, and subscription suppresses adapters. Memory emits an
instruction plus its canonical file; agent-system emits canonical and native
harness instruction files but no task-prompt instruction. Each final
destination is emitted once. Contribution provenance and renderer order must
preserve the ranked artifact sequence; only projections with exactly equal
source artifact IDs and text are charged once. Canonical renderer text and
memory instructions must each remain within the source budget. Derived
target-file projections are charged independently under a two-times checked
projection budget, while combined instruction/file output remains capped at
three times the source character and UTF-8 byte budgets. Existing clipping
remains character-based, while a separate 1 MiB UTF-8 byte limit bounds
resource use.
Core resolves logical scopes to final runtime roots before checking context-wide
destination/environment conflicts, and binds adapters to the requested base
model and actual runtime application limit. Missing or empty adapter manifest
identity fields use the same deterministic name/artifact-ID, `lora`, and
requested-model fallbacks in both the handler and validator. No last-writer-wins
behavior is allowed.

A2.1 validates DTO inventory/digest consistency. Current A2.4 loads the built-in
handlers as verified callables and implements the Core-owned local payload
scanner in `openevo.evolution.artifact_payloads`. The scanner accepts only
normalized authority-free `file://` URIs contained by the configured
Core-managed artifact root. Construction walks from the absolute filesystem anchor,
fixes each configured root component no-follow, records its identity, and retains a
verified root FD. Every operation revalidates the held-FD/path binding and traverses
only relative to that FD with no-follow, nonblocking opens. On the current Linux Core
release, `O_PATH|O_NOFOLLOW` first fixes and validates every untrusted node before Core
obtains a readable fd from that fixed object, so a stat/open race cannot open a substituted
device as data.
The scanner rejects symlinks and non-regular/non-directory nodes; bounds all
visited file/directory nodes before sorting; checks pre-open, opened, post-read,
and final descendant path identities; streams bounded inventory digests; and
validates the entire UTF-8 file even when returning only a prefix. A verified
reread aborts immediately if bytes exceed the issued size. Opaque handles are
random, process-local, collision-checked, and invalidated when the service
closes. Regular files with a link count other than one are rejected, so a managed
path cannot authorize an inode that remains linked outside the managed root. All
snapshot scans and verified rereads in one request-scoped service share aggregate
node, file, and byte budgets in addition to per-entry limits. Failed attempts do not
refund enumeration or hashing resources. Unknown suffixes become
`application/octet-stream`; descriptor MIME
validation remains authoritative. Platforms without Linux `O_PATH` fail closed
until an equivalent fixed-object implementation exists; this does not affect
the macOS Desktop host because Core payload scanning runs on the remote server.

An issued inventory is not an immutable copy or filesystem lease. The final
descendant stability pass rejects mutation observed during scanning, but a
source may change immediately afterward. Therefore inventory metadata alone
never authorizes byte consumption: `read_utf8_prefix`, verified stream copy, and
full payload content verification reopen the fixed object and revalidate identity,
exact size, and digest. Drift fails closed; Core never returns or stages changed
bytes.

The scanner does not download or invent inventory for `hf`, `https`, or `s3`
references, and it does not extract archives. The internal projection v1 resolver
now preserves the existing compatibility filter and global candidate ranking,
issues contiguous per-target snapshots, invokes only the exact callable from the
verified executable registry, validates each output and the context-wide aggregate,
and persists the ordered `TargetHandlerOutput` collection with registry digest,
runtime destination roots, and actual consumed-artifact selection. Snapshot-local
handles and source URIs never enter that persisted contract. Individual artifacts
that cannot be safely snapshotted are excluded; handler, registry, or aggregate
validation failures fail closed without a legacy fallback. Subscription execution
skips adapter-only handlers using contribution vocabulary rather than a target ID.
Skip records contain only an artifact ID and bounded `unsupported_uri_scheme`,
`payload_policy_rejected`, `metadata_policy_rejected`, or
`unbound_legacy_metadata` code. Artifact semantics come from deterministic immutable
`manifest_json` committed in the artifact registration transaction; projection never
trusts the mutable legacy manifest file. A migrated artifact without that immutable
binding is quarantined with `unbound_legacy_metadata` and must be registered again
rather than backfilled from a file. A malformed registration binding fails the
projection closed; metadata that is valid for legacy registration but outside the
internal projection policy is quarantined.
The request is a strict closed Core-to-Core schema: agent harness/auth and task
tags/explicit artifact IDs are allowed, while arbitrary agent env and metadata are
not. Its collection elements and complete canonical JSON representation are bounded.
Explicit artifact IDs are applied in the SQL query. Store-level and per-target attempt
limits apply before payload scanning; implicit selection gives local manifest-bound
candidates priority over bounded remote/unbound/metadata-policy skip rows. The internal
resolver bounds DB compatibility/scores before filtering or ranking without changing
legacy registration or worker-completion acceptance. SQL returns only bounded
compatibility routing data plus identity/reason markers for rejected rows; it never
returns their source URI, name, manifest, or scores. Resolver validates compatibility
before persisting either a selection or a typed skip, and omits rows whose compatibility
cannot be established. Static snapshot metadata is validated
before payload I/O, while every attempted node/file and every byte read for scan or
verified-reread hashing consumes the request-scoped aggregate budget immediately; a
failed snapshot/read does not refund those resources.
Adapter contributions retain the approved payload inventory digest and total size,
so later materialization can reject source drift.

A safely scanned but semantically invalid promoted artifact is not an artifact-local
transport failure. Handler errors such as a missing root `SKILL.md`, invalid target
semantics, invalid output, or context-wide conflicts fail the full projection. Core
does not invoke handlers speculatively per artifact to guess a valid subset, because
that would change handler semantics and hide registry defects.

The internal generic materializer is bound to the same sealed registry digest as the
projection and verifies the projection's canonical request digest. It reissues required
staged/adapter inventories, streams each selected file into a private random-ID,
digest-verified blob, rehashes complete adapter payloads, flattens
directory contributions without extracting archives, and derives env/instruction/
adapter values solely from validated contribution vocabulary. Handler descriptor
`instruction_preamble` preserves existing framing without a target-ID switch; only the
instruction view trims per-projection leading/trailing whitespace to match legacy Gateway,
while staged bytes remain unchanged. The
serialized result contains runtime destinations and opaque blob IDs, never source
URIs, host paths, or scanner handles. A temp tree plus file/directory fsync and rename
publishes each bundle atomically. Context and materialization metadata are committed
together under the same cross-process lock used by startup recovery. An ephemeral
publication receipt binds canonical manifest bytes, the context/blob-directory identities,
and every blob identity; Store precommit revalidates it against the same locked root FD
before SQLite commit. Final publication uses FD-relative atomic no-replace rename and fails
closed where that primitive is unavailable. Failed persistence discards the receipt-bound
bundle only after proving no DB row committed; committed or unknown state is preserved.
Temporary-directory setup failures use the same inode-bound quarantine rule, and Store
rechecks the materialization-root binding after the callback and on normal lock exit.
If final-path rebinding fails after rename, cleanup leaves the
unreferenced path for startup recovery instead of deleting a potentially substituted name.
A mode-0700, owner-verified materialization root fd is locked once and passed through
publish, precommit verification, rollback discard, and startup reconciliation. Cleanup
binds the initially observed inode, moves a matching candidate to a random quarantine,
clears only safely fixed content, and retains a maintenance-owned quarantine/tombstone
entry. That is conservative containment, not immediate deletion. Identity mismatch is
preserved and fails closed; later recovery keeps failing until the preserved entry is
handled explicitly. Beyond fresh or recognized pending bootstrap states, startup recognizes
only exact allowlisted historical/current schema fingerprints and independently requires
the exact `store_identity` table/row and both markers; only a complete fingerprint may claim
existing managed recovery state. Forged or near-match identity state fails before any
cleanup and remains untouched. A fresh database refuses pre-existing Core-managed
recovery state. A legacy database authorizes migration only after that recognition, before
any identity DDL/row or marker is written; table-name presence alone is insufficient.
A fresh database treats context snapshots, materializations, and managed artifact manifests
as unclaimed state. Base schema DDL is one explicit SQLite transaction, and the exact
historical allowlist includes directly upgradable first-parent layouts rather than requiring
users to launch intermediate releases. Base DDL, additive migrations, and startup recovery
DB changes commit in that same transaction, so no base-only schema can survive a crash.
A DB store ID/resolved-root binding must match identical fsynced markers at the artifact
root and materialization root before reconciliation quarantines unreferenced
bundle/temp/symlink entries. Identity DDL and its first pending row are one SQLite
transaction. Initial and legacy binding then uses a recoverable pending -> both markers
fsync -> bound protocol; a bound row never recreates either missing marker.

Context snapshot reconciliation is separately authorized by SQLite at startup: the store
passes canonical request/response bytes for every referenced snapshot and fails if disk
differs. Ordinary snapshot reads and inventories accept only link-count-one mode-`0600`
regular files. A distinct startup migration may accept the narrowly safe historical mode
set solely to tighten it to `0600`; normal reads never inherit that legacy allowance.

Blob transport and consumers may enter only through an `EvolutionStore`-owned API. That
entry holds and revalidates the same locked materialization-root fd, both store-identity
markers, and the root owner/mode/inode binding. It loads the authoritative
`MaterializedContext` from SQLite `context_materializations.manifest_json`, then passes
that expected manifest and the same root fd to the lower-level materializer. The on-disk
`manifest.json` must byte-match the authoritative canonical DB manifest, so replacing a
blob and rewriting the disk manifest cannot self-authorize new content. The lower-level
reader opens the blob no-follow relative to the anchored root, revalidates exact size,
digest, and path identity, and exposes only a controlled read-only stream, never a raw fd
or host path.
Adapter final verification also rebinds the payload-root pathname after the complete
inventory rehash, so an atomically replaced source root fails closed.

This materializer remains an internal Core path. Public `/v1/contexts/resolve` and
Gateway still use the legacy response, so there is no Gateway dual-parser or shadow
response. The next cutover must add strict v2 metadata/blob transport and atomically
switch Gateway generic staging before removing v1. Until then, the legacy Gateway
skill URI copy remains a documented TOCTOU risk.
The legacy resolver also retains its original subscription alias set; generalized
`*_subscription` recognition is internal to projection execution-profile validation.
Legacy artifact GET/list responses continue to read the legacy manifest file; the
immutable DB binding is internal to projection until a versioned public API migration.
Setting a target's `max_artifacts` to zero disables that target for the projection and
does not invoke its handler.
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
validates that response; the current implementation does not use an offline
cache. `GET /capabilities?execution_mode=<release-mode>` is evaluated by Core,
and `GET /openevo-api/desktop/capabilities?execution_mode=<release-mode>` is the
token-protected sidecar proxy. A missing verified registry, tunnel, or valid
remote payload fails closed and never selects a Desktop-bundled catalog. Each
target includes friendly metadata, renderer contract,
configured default, nullable profile-supported effective default with its typed
support result, and handler identity. Target and handler/default exposure must
be audience-compatible. `methods` contains audience-visible choices, while
`accepted_methods` carries identity and support for every explicit method the
Core registry still accepts without making it a Desktop picker option.
`selection_resolvers` carries Core-owned project values such as
`agent_system.method=auto`, their possible concrete methods, identities, and
support. Every resolved entry must exactly match the identity and support of the
same ID in `accepted_methods`; contradictory projections are invalid. Each visible method includes schema/defaults,
ordered inputs, identity, separate execution/capture/harness/runtime declarations,
and four support axes. Axis states are `supported`, `unsupported`, or
`unavailable`, with stable reason codes and missing requirements.

Method schemas may include top-level execution fields that the project compiler,
rather than an ordinary user, owns. The descriptor declares each
`project_config_injections` entry as a field name plus a closed Core source,
currently `reflector_llm` or `agent_model`; every field must exist in the full
method schema. Capability projection removes those properties from the Desktop
schema, its required list, and its default config. The experiment compiler reads
the descriptor metadata, removes stale project values, and writes the
authoritative source before verified-registry normalization. This is
configuration ownership metadata only: it does not remove fields from the
method's runtime config or change method behavior. A same-named field without an
injection declaration remains user-owned and is never guessed or overwritten.
Root object `default`, `const`, and `enum` annotations cannot embed injected
fields because their correlated project-schema projection would be ambiguous.

The executable registry retains the verified distribution evidence used to
load it. The attestation digest set must equal the complete distribution digest
set referenced by the snapshot; every implementation identity must match its
attestation, and the non-executable target anchors plus executable handler and
method handle sets must each exactly match the frozen descriptor set.
Capability projection therefore cannot be enabled by supplying
an unverified snapshot and callable map alone. Both verified distribution and
executable registry constructors are closed; only wheel/inventory verification
and exact entry-point loading can publish their sealed production instances.
The public wheel verifier always uses installed-distribution discovery and does
not accept an injected metadata provider; isolated fixtures use the private
repository testkit path.

`Codex Subscription Transcript` may be a release-profile display label, but it
is not a framework mode ID. The sole release mapping converts it to
`subscription + transcript + codex` and Self-Deployed to
`self_deployed + transcript + codex`; it never infers token-level availability.
Desktop renders target toggles, friendly method selectors, and bounded-schema
config editors from this remote projection; it carries no method/target
registry. Picker choices come only from visible `methods` and Core-owned
`selection_resolvers`. Hidden `accepted_methods` may remain as an opaque saved
selection but cannot become a new Desktop choice and expose no editable schema.
Keeping a selection preserves its config. Selecting another visible method
atomically replaces config with that method's remote default; selecting a
resolver starts with an empty override because resolver capabilities declare no
schema/default. Null, removed, or unsupported selections rebind only when the
user enables through a supported remote effective default or explicitly chooses
a supported option. Desktop never invents a default when
`effective_default_method_id` is null.

The editor implements the same closed JSON Schema subset as Core. It keeps the
stored project value as a recursive partial override, so omitted defaults are not
materialized into the project file and missing, explicit null, and concrete
values remain distinct. Validation recursively merges schema defaults,
descriptor defaults, and the project override, then validates the complete
effective config; a required user-owned field must therefore come from one of
those layers. Config schema, defaults, and values containing non-finite numbers
or integers outside the JavaScript safe range fail closed before browser editing.
Invalid enabled visible-method config blocks an evolution-changing save and run
with field-level errors; disabling the target remains a valid repair and its
opaque config stays preserved. An unrelated project edit may still be saved when
the evolution map is unchanged. Any evolution change requires a capability snapshot
for the selected execution mode; loading, missing, failed, or mismatched
capabilities block that save. Resolver, hidden, stale, and unknown config remains
opaque and round-trips without schema guessing. Enabled invalid or unknown
selections remain repairable and block run launch; disabled unknown selections
remain in the canonical map even when omitted from the ordinary setup surface.
The sidecar streams the remote capability response through a fixed byte limit,
caps global node/collection/text budgets and schema sizes, rejects unsafe outer
integers and non-Desktop exposure, and redacts remote error details before
forwarding them. Unsaved drafts never authorize the active session. Before every
run launch, the sidecar re-fetches capabilities and calls remote Core
`POST /evolution/project-validation` with the expected registry digest. Core
validates visible methods, hidden accepted methods, and every possible concrete
resolver method using the complete verified registry and compiler-owned
injections; registry drift or invalid config fails before a run thread exists.
The validation endpoint accepts at most 1 MiB of UTF-8 request bytes. Its ASGI
guard checks declared and actually received bytes plus JSON nesting before
parsing; the project contract then limits target count to 128 and applies
depth/node/collection/text budgets to every method config. The sidecar serializes
the exact outgoing JSON bytes and enforces the same 1 MiB limit before opening a
remote request.
A test target using an existing contribution vocabulary
and renderer must cross compiler, resolver, gateway, capabilities, persistence,
and Desktop without a target-ID switch.

## Algorithm And Artifact Preservation

Methods continue to accept the existing claimed-job input and return existing
`ArtifactRegisterRequest` values until a separately justified API change. Their
declared output artifact types must include the target artifact type. Candidate
generation, evaluation, history accumulation, tie-breaking, and best-result
selection stay inside the method. Core does not infer a winner from generic
scores and does not rewrite algorithm output during promotion or context reuse.
The runner routes only target-typed outputs: an auxiliary `report` remains
observable but never becomes target history. Without an external gate, only a
method-promoted target output is reusable; absence of one fails closed.

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
| A2.3 | Generic selections, durable plans, verified registry dispatch, and remote registry capabilities on existing jobs/workers/artifacts. |
| A2.4 | Generic context/runtime contribution engine and capability-driven Desktop configuration/rendering. |
| A2.5 | Remove duplicate registries and target switches, document extension paths, and pass behavior/performance gates. |

A2.1 verification lives in `tests/evolution/test_framework_*.py` and proves
strict DTOs, schema bounds, default precedence, profile compatibility, immutable
snapshot backing, registration-order/fresh-process identity, reachable closure,
safe handler contributions, and plan consistency. Existing algorithm, store,
worker, gateway, trajectory, and rollout regressions prove the contract-only
stage has no runtime effect.

A2.2 verification adds:

- `test_framework_builtins.py` for catalog completeness, defaults, entry-point
  equality, closed schemas, ordered inputs, and fresh-process identity;
- `test_framework_loading.py` for wheel/install tamper, path, symlink, shadowing,
  origin, qualified-name, signature, and identity failures;
- protected worker and Terminal Bench fixtures for GEPA history ordering,
  objective/`None` ordering, generation and candidate tie-breaks, and round
  transitions.

The release-shaped smoke builds the OpenEvo wheel, installs it into a fresh
environment outside the repository, writes the same external framework lock
used by Desktop bootstrap, verifies the wheel against its SHA-256, and loads 12
exact method handles, four exact handler handles, and four target anchors. This does not
replace the final Terminal Bench performance gates.
