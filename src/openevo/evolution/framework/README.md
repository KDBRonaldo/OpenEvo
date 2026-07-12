# Evolution Framework

The authoritative A2 framework contract is
[`docs/architecture/evolution-framework.md`](../../../../docs/architecture/evolution-framework.md).

A2.2 adds a deterministic built-in catalog and a distribution-backed loader on
top of the A2.1 contracts. It still does not replace current method dispatch,
worker/store persistence, capabilities, Science Project compilation, Desktop,
or target-specific runtime projection. Later A2 steps own those cutovers in the
order fixed by the architecture contract.

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
- `builtins.py`: the four current targets/handler identity anchors and twelve
  legacy method descriptors.

`build_builtin_registry()` is safe for deterministic catalog inspection.
Release startup must first obtain a `VerifiedDistribution` from the exact wheel
and external SHA-256 lock, then call `load_verified_builtin_registry()`. Never
derive an expected digest from the running package and immediately trust it.

During A2.2, `run_method()` and `METHOD_REGISTRY` remain the runtime path.
`load_builtin_method_handles()` only proves descriptor entry points resolve to
those exact objects. Target/handler anchors are non-executable identities until
A2.4 supplies and integrates real contribution handlers.

Release reflector schemas require an explicit model and `codex_cli`; direct API
credentials and endpoints are not user config. Audit policy, evaluator results,
and promotion support are Core-owned execution-envelope fields. Multi-dataset
legacy methods use an `explicit_inputs` binding so each existing caller's
dataset order survives the adapter unchanged.
