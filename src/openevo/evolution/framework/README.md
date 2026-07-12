# Evolution Framework

The authoritative A2 framework contract is
[`docs/architecture/evolution-framework.md`](../../../../docs/architecture/evolution-framework.md).

A2.2 adds a deterministic built-in catalog and a distribution-backed loader on
top of the A2.1 contracts. A2.3 uses it for generic project compilation,
durable plan-bound jobs, verified worker dispatch, and remote Core capabilities
proxied by Desktop. Generic Desktop configuration/rendering and target-specific
runtime projection remain assigned to A2.4/A2.5 in the architecture contract.

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
- `builtins.py`: the four current targets/handler identity anchors and twelve
  legacy method descriptors.

`build_builtin_registry()` is safe for deterministic catalog inspection.
Release startup calls `load_verified_framework_registry()` with the external
`framework-lock.json` written by Desktop or maintainer release automation. The
lock identifies a sibling exact wheel by version and SHA-256; startup verifies
the installed distribution before publishing handles. Never derive an expected
digest from the running package and immediately trust it.
`VerifiedExecutableRegistry` retains the resulting `VerifiedDistribution`
attestations. Their digest set must equal the distribution digest set referenced
by the frozen snapshot, every implementation identity must match its attestation,
and the target/handler anchor set must exactly match the snapshot. A manually
assembled catalog without that evidence is not a release executable registry.
Their public constructors are closed; only verified install and exact
entry-point loading paths publish sealed instances. The public distribution
verifier always discovers real installed package metadata; provider injection is
confined to the repository testkit and is not a production API. Target
descriptors also own project selection resolvers. Capability `methods` remain audience-visible,
while `accepted_methods` preserves valid hidden selections without exposing
their config schema as a Desktop choice.

Plan-bound jobs dispatch only through `VerifiedExecutableRegistry`. The worker
publishes exact method identity digests at claim, then checks the plan,
execution envelope, method identity, and artifact snapshots before invoking the
descriptor's explicit ABI. `run_method()` and
`METHOD_REGISTRY` remain temporarily available only for unplanned benchmark
jobs. Target/handler anchors are non-executable identities until A2.4 supplies
and integrates real contribution handlers.

Release reflector schemas require an explicit model and `codex_cli`; direct API
credentials and endpoints are not user config. Audit policy, evaluator results,
and promotion support are Core-owned execution-envelope fields. Multi-dataset
legacy methods use an `explicit_inputs` binding so each existing caller's
dataset order survives the adapter unchanged.
