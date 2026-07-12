# Evolution Framework

The authoritative A2 framework contract is
[`docs/architecture/evolution-framework.md`](../../../../docs/architecture/evolution-framework.md).

A2.1 is contract-only. This package may define frozen DTOs and fail-closed
registry/schema validation, but it does not replace the current method dispatch,
worker/store persistence, capabilities endpoint, Science Project compiler,
Desktop behavior, or evolution algorithms. Later A2 steps own those migrations
and cutovers in the order fixed by the architecture contract.

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
- `registry.py`: startup graph validation, frozen snapshot, and plan compilation.
