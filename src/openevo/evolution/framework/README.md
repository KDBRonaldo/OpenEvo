# Evolution Framework

The authoritative A2 framework contract is
[`docs/architecture/evolution-framework.md`](../../../../docs/architecture/evolution-framework.md).

A2.2 adds a deterministic built-in catalog and a distribution-backed loader on
top of the A2.1 contracts. A2.3 uses it for generic project compilation,
durable plan-bound jobs, verified worker dispatch, and remote Core capabilities
proxied by Desktop. Desktop now consumes that projection for target/method
selection and bounded-schema config editing. The implemented A2.4 slice adds
verified generic handler execution, renderer payloads, internal projection,
and generic Core materialization. Strict transport, Gateway cutover,
external-target acceptance, and removal of the remaining target-specific
runtime path remain assigned to the rest of A2.4/A2.5.

Keep implementation-independent rules in the architecture document. Keep module
usage and code-specific invariants here as the package is adopted.

Module ownership:

- `contracts.py`: shared enums, identities, canonical JSON, and path validation;
- `descriptors.py`: target, method, and handler registration descriptors;
- `plan.py`: editable selections and canonical resolved plans;
- `execution.py`: ordered method inputs, separated execution envelope, Core
  harness service, and the one behavior-preserving current-callable adapter;
- `contributions.py`: versioned data-only handler output and renderer payloads;
- `handlers.py`: trusted payload inventories and target-handler invocation input;
- `builtin_handlers.py`: attested built-in projection callables, without algorithm logic;
- `handler_validation.py`: payload, renderer, limit, and context-wide conflict
  validation;
- `support.py`: shared four-axis method support evaluation;
- `capabilities.py`: target-rooted versioned frozen-registry projection;
- `profiles.py`: the sole product-release-mode to generic-profile mapping;
- `resolution.py`: the narrow existing `agent_system.method=auto` resolver;
- `schema.py`: bounded config-schema validation and normalization;
- `registry.py`: startup graph validation, frozen snapshot, and plan compilation;
- `loading.py`: externally locked wheel/install and entry-point verification;
- `runtime.py`: external framework-lock parsing and executable-registry startup;
- `builtins.py`: four target anchors, four executable handler descriptors, and
  twelve legacy method descriptors.

`build_builtin_registry()` is safe for deterministic catalog inspection.
Release startup calls `load_verified_framework_registry()` with the external
`framework-lock.json` written by Desktop or maintainer release automation. The
lock identifies a sibling exact wheel by version and SHA-256; startup verifies
the installed distribution before publishing handles. Never derive an expected
digest from the running package and immediately trust it.
`VerifiedExecutableRegistry` retains the resulting `VerifiedDistribution`
attestations. Their digest set must equal the distribution digest set referenced
by the frozen snapshot, every implementation identity must match its attestation,
and the target-anchor, handler-handle, and method-handle sets must each exactly
match the snapshot. A manually
assembled catalog without that evidence is not a release executable registry.
Their public constructors are closed; only verified install and exact
entry-point loading paths publish sealed instances. The public distribution
verifier always discovers real installed package metadata; provider injection is
confined to the repository testkit and is not a production API. Target
descriptors also own project selection resolvers. Capability `methods` remain audience-visible,
while `accepted_methods` preserves valid hidden selections without exposing
their config schema as a Desktop choice. Capability DTO validation revalidates
the bounded schema and partial default, caps encoded contract size, and rejects
integers outside the JavaScript safe range before the local sidecar forwards a
remote payload to Desktop. Capability parsing also applies global node,
collection, and text budgets plus JavaScript-safe outer integer validation. A
method can declare top-level `project_config_injections`, each binding a field to
a closed Core source. Registry freeze requires those fields to exist in the full
schema; capability projection removes them from Desktop schema/default/required
metadata, while compilation removes stale values, injects the authoritative
source, and validates the complete config before method execution. Root object
`default`, `const`, and `enum` annotations must not embed injected fields because
their correlated projection would be ambiguous.
Project target maps accept at most 128 entries. Each project method config is
bounded by depth, node, collection, and text budgets before recursive JSON
normalization. The Core project-validation HTTP boundary additionally limits the
actual UTF-8 request body to 1 MiB and rejects excessive JSON nesting before
parsing; Desktop applies the same byte limit before transport.

Plan-bound jobs dispatch only through `VerifiedExecutableRegistry`. The worker
publishes exact method identity digests at claim, then checks the plan,
execution envelope, method identity, and artifact snapshots before invoking the
descriptor's explicit ABI. `run_method()` and
`METHOD_REGISTRY` remain temporarily available only for unplanned benchmark
jobs. Target descriptors retain non-executable identity anchors; target-handler
descriptors now load exact attested callables into
`VerifiedExecutableRegistry.handler_handles`. Resolver invocation, secure payload
projection persistence, payload scanning, and internal generic materialization are
implemented. Strict v2 API/Gateway cutover and removal of legacy v1 remain unfinished
A2.4 work.

Every target-handler descriptor identifies input v1, renderer v1, and
output/contribution v2 independently, and invocation validates all three. Output v2
requires adapter contributions to bind the approved payload inventory digest and byte
size; v1 outputs are rejected rather than inferred.
Handler descriptors may also declare a bounded `instruction_preamble`; generic
materialization applies it in projection order, so Gateway never needs target-specific
instruction framing.

`context_materialization.py` consumes only a projection bound to the same sealed
registry and canonical request digest. It streams verified file/directory contributions
into private random-ID, digest-verified blobs, fully rehashes adapter payloads, derives
environment and instruction values from the generic contribution contract, and atomically
publishes a manifest-backed bundle. Provenance retains target IDs, but no target-ID
dispatch branch, source URI, host path, or scanner handle is part of materialization or
blob transport. Publish, precommit, discard, and recovery reuse one owner-verified,
mode-0700 locked root fd. Its ephemeral publication receipt binds canonical manifest
bytes, context/blob-directory identities, and every blob identity for precommit
revalidation. Cleanup moves only an identity-bound candidate to random quarantine,
clears safely fixed content, and retains a maintenance-owned quarantine/tombstone entry;
it does not promise immediate deletion. Identity-changing candidates are preserved and
fail closed. Beyond fresh or recognized pending bootstrap states, startup accepts only
exact allowlisted historical/current schemas. It independently requires the exact
`store_identity` schema/row and both markers; only a complete fingerprint may claim
existing managed recovery state, and forged identity fails before cleanup. Context
snapshot reconciliation is DB-authorized startup work; normal reads stay strict at
link-count-one mode `0600`, with a separate
explicit migration that only tightens eligible historical modes. Blob reads hold an
anchored verified fd instead of returning a path, and adapter verification finally
rebinds the complete source-root pathname.

`ArtifactPayloadService` anchors the configured managed root component by component with
no-follow opens, retains a stable verified root FD, and revalidates its pathname binding
around every FD-relative scan or reread.

Text handlers have separate source-consumption and bounded runtime-projection
budgets. Only equal source IDs plus equal text are deduplicated; derived target
files are checked under a two-copy projection budget and combined output under
a three-copy budget. Source MIME is checked against the descriptor allowlist,
and renderer JSON is bounded to the worst-case escaped representation of one
1 MiB contribution. Text memory emits an instruction plus `memory.md`;
agent-system emits canonical and native harness instruction files without a
task-prompt instruction. Skill bundles require root `SKILL.md` and canonical
ranked renderer order. Adapter fallback identity is recomputed from the trusted
artifact snapshot by the validator rather than accepted from handler output.

Release reflector schemas require an explicit model and `codex_cli`; direct API
credentials and endpoints are not user config. Audit policy, evaluator results,
and promotion support are Core-owned execution-envelope fields. Multi-dataset
legacy methods use an `explicit_inputs` binding so each existing caller's
dataset order survives the adapter unchanged.
