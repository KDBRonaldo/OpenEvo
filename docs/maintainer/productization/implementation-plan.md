# OpenEvo Productization Execution Plan

Status: non-normative execution tracker
Canonical design: `docs/maintainer/productization/spec.md`
Tracking issue: #131
Base branch: `stable`

## Purpose

This file is not a second specification. It records current implementation gaps,
workstreams, and execution order. Product behavior, supported scope, and
release acceptance are defined only by `spec.md`.

When implementation exposes a missing product decision, update the canonical
specification first. Otherwise keep detailed design, API fields, migration
protocols, test matrices, and implementation history in the owning issue,
architecture document, module README, test, or pull request.

## Working Rules

- Work from `stable` on focused branches and merge through PRs linked to #131 or
  a scoped child issue.
- Commit as `ivowang <ziyiwang@ieee.org>` and push reviewed branches promptly.
- Do not modify the protected evolution algorithms.
- Preserve the existing gateway, rollout, runtime, capture, trajectory,
  evolution, artifact, context-resolution, and runtime-injection architecture.
- Keep Desktop, Core/Daemon, and benchmark automation within their declared
  repository boundaries.
- Write focused regressions before behavior-changing fixes.
- Update the owning documentation with each behavior or contract change.
- At each workstream boundary, use fresh independent `gpt-5.6-terra` reviewers
  with high reasoning and resolve findings before merge.
- Run focused tests, broader affected regressions, `git diff --check`, and a
  complete diff review before commit.

Simulator and component tests can accelerate development. They cannot satisfy a
packaged-app, real-host, protected-performance, or release-candidate gate.

## Current Priorities

Version `0.1.7` is the current public, installable unsigned exhibition Preview.
It proves the real mounted and copied DMG, bounded Desktop startup, packaged
sidecar and renderer, isolation from corrupt older Preview state, a
self-contained Daemon Bundle, managed-runtime packaging, and immutable release
asset roundtrip.
Signed candidate-bound evidence additionally proves two real remote Codex
Subscription sessions, all three text-evolution targets, and next-session
artifact reuse through the packaged renderer and live Desktop Local API. It has
no final macOS Tauri-to-remote-host E2E or clean-host matrix evidence, and it is
intentionally non-gating. It does not prove G2, G3, G12, or a complete
ordinary-user qualification matrix, and it is not the candidate that will
satisfy G1-G12. Earlier Preview releases are retained as historical evidence.
System, Files, and History remain incomplete; the v2 authority cutover, G4, and
G7-G12 remain open.

Current priority is:

1. assign the clean-host workstream owner and complete a G3-shaped rehearsal,
   including mediated Subscription authentication and the direct/proxy matrix;
2. complete and independently review the durable System recovery owner;
3. cut product authority over to the next negotiated API contract major before
   extending deep Task, Files, or History state;
4. complete the Desktop System, Files, History, diagnostics, and recovery views;
5. close Self-Deployed, atomic cross-session evolution, benchmark, security,
   lifecycle, and release-evidence gates.

This order changes implementation scheduling only. It does not reduce any
blocking requirement in the canonical specification.

The published Preview is suitable only for a controlled packaging exhibition. A full
External Beta must support the canonical ordinary-user lifecycle, both
execution modes, clean-host and network matrices, authoritative recovery and
maintenance views, protected performance, security, upgrade/rollback, and one
immutable G1-G12 candidate.

## Critical Contract Migration

The current frozen v1 models are an implementation snapshot, not the release
contract. Before expanding the Task, Evolution, Files, or History workflows,
the next negotiated API contract major must:

- use distinct opaque Project Head, Evolution Revision, Runtime Context
  Snapshot, and verified effective execution identities;
- make task admission immutable and let infrastructure retry append attempts
  without clearing or recomputing its head/execution pins;
- reject submission before Task creation while a successor is unresolved,
  removing the legacy uncommitted-revision queued-run path;
- expose idempotent, compare-and-set-protected close, transition retry,
  replacement-plan, abandon, and historical-restore actions;
- migrate run, artifact, history, event, sidecar, and Desktop schemas together,
  with no context-dependent reinterpretation of the generic v1 revision type.

Core also still contains Terminal-Bench-named parametric training projections.
They must move to benchmark-owned automation or a benchmark-owned verified
extension before release, with wheel/source boundary tests strengthened, while
the protected non-parametric algorithm bodies remain unchanged.

## A. Protected And Pluggable Evolution

**Outcome:** the three protected methods retain their algorithm behavior and
historical effectiveness, while targets and methods remain registry-driven and
extensible without Desktop method tables or product-layer algorithm branches.

**Current state:** verified descriptors, registry loading, plan-bound jobs,
capability projection, protected-method fixtures, and internal materialization
primitives exist. Product runs, atomic project-head transitions, generic runtime
consumption, and release-grade benchmark automation are not yet one closed path.
The checked-in Terminal Bench gate profiles are historical-configuration
records, not the executable closed manifests, baseline/evolved launch ledgers,
and per-task evidence required by G7.

**Owning issues:** #134, #150, #154, and #156.

**Next work:** complete the contract migration above, finish the transition
owner and generic runtime cutover, reconstruct the three closed G7 manifests
and aggregators, then prove G1, G5, G6, and G7 without changing protected
method implementations.

## B. Daemon Product Convergence

**Outcome:** one remote Daemon owns durable setup, projects, runs, services,
artifacts, revisions, recovery, and both execution modes.

**Current state:** a real versioned control API, durable project/run stores,
remote bootstrap, tunnel routing, Subscription prerequisite checks, and child
service supervision exist. The published `0.1.7` Preview ships a self-contained
Daemon Bundle and has packaging, managed-runtime, managed-upgrade, and signed
candidate-bound Docker plus Codex science evidence. It has no clean-host matrix
evidence. Clean-host preparation and lifecycle evidence, mediated
Subscription authentication, the direct/proxy matrix, Self-Deployed,
upgrade/rollback, and release-grade maintenance operations remain incomplete.
The maintenance owner is now part of the release composition. Generic
development/test construction leaves it disabled by default, while the release
launcher enables it only alongside the verified registry, service supervisor,
and run owner. The matching Desktop release negotiates the complete
`service_control`, `diagnostics`, and `maintenance` feature set and fails closed
against older or incomplete Daemon compositions.

**Owning issues:** #159, #160, #167, and #168.

**Next work:** establish one owner for the complete G3 clean-host matrix and
close clean Subscription deployment, mediated authentication, proxy behavior,
and lifecycle evidence. Then complete and independently review doctor/repair,
upgrade/rollback, and
Self-Deployed preparation. These changes prove G3, G4, G8, G9, and the Daemon
portions of G11.

## C. Desktop Product Maturity

**Outcome:** an ordinary scientist completes the supported workflow entirely in
the packaged macOS application.

**Current state:** release-mode server/project setup, host-key review, remote
capabilities, task execution, retry/cancel, timeline/transcript, and evolution
inspection are implemented. First launch exposes two read-only synthetic
science projects, each demonstrating three cross-session tasks and
textual-memory, trajectory-to-skill, and agent-system evolution without
creating authoritative or remote state. The public `0.1.7` Preview launches
from the real DMG with its packaged renderer and startup-bounded sidecar. G2
clean-user lifecycle evidence, Files, History, System maintenance, mediated
credential choices, and several recovery flows remain incomplete.

**Owning issues:** #158, #163, and #203.

**Next work:** complete the narrow System recovery slice, then consume the
migrated authority contract before deepening Task, Files, or History state.
First validate the current read-only examples and fail-closed System surface in
the packaged Preview. Finish the missing ordinary-user views and full
clean-user lifecycle required by G2 and G10.

## D. Repository, Documentation, And Release Engineering

**Outcome:** source layout, public documentation, package inventories, and
GitHub releases present one coherent Desktop-plus-Daemon product.

**Current state:** the repository has explicit `src/openevo/`, `desktop/`, and
`benchmarks/` boundaries and one canonical product specification. Some active
documentation, issue state, package inventory, release metadata, dependency
evidence, and cleanup work still reflect intermediate implementation states.
The exact source, tag, assets, checksums, and publication evidence for the
public `0.1.7` Preview must remain available as non-gating release records;
earlier Preview releases remain historical evidence.

**Owning issues:** #131 and #193.

**Next work:** update documentation alongside each completed workflow; remove
obsolete active paths rather than adding wrappers; keep Preview publication
truthful about its missing gates; and make the final candidate workflow bind the
exact DMG, Daemon Bundle, manifest, checksums, source, and evidence required by
G1 and G12. PyPI remains outside this release.

## E. Release Candidate Validation

**Outcome:** one immutable candidate satisfies every G1-G12 gate.

**Current state:** component and subsystem tests exist, and the published
`0.1.7` Preview has complete packaging, downloaded-release verification, and
signed candidate-bound two-session science evidence, but there is no candidate
with complete clean-host Desktop/Daemon evidence, both
execution modes, all evolution gates, protected performance, recovery/security
matrices, and full G1-G12 evidence. The published Preview cannot be reused as
that candidate.

**Owning issue:** #131.

**Next work:** after A-D are complete, run every canonical gate against the same
manifest-bound candidate. Infrastructure reruns follow the policy in the spec;
product or benchmark failures require a corrected candidate.

Before publication, two fresh-context `gpt-5.6-terra` high-effort reviewers
independently review product/spec compliance and release risk/evidence.

## Immediate Execution Order

1. Run the candidate-bound real Codex Subscription Desktop/Daemon science E2E
   against the exact published product composition, then create a new candidate
   if any product or evidence change is required.
2. Give the clean-host workstream one owner and rehearse fresh-host Daemon
   installation, mediated Codex Subscription authentication, Docker execution,
   and the supported direct/proxy matrix. This is non-gating integration
   evidence, not G3 proof.
3. Complete and independently review the System recovery owner around
   authoritative status, diagnostics, reconnect, and bounded repair. The
   production owner is wired; this item now means release evidence and clean
   host validation rather than the initial owner implementation.
4. Implement and adopt the critical v2 authority migration before extending
   deep Task, Files, or History state on the frozen v1 model.
5. Complete the Desktop System, Files, History, diagnostics, and recovery views,
   then prove the full G2 clean-user lifecycle and G10 product quality.
6. Complete the Self-Deployed reference profile and end-to-end path for G4.
7. Unify product run admission, workspace result, sealed dataset, evolution
   transition, materialization, and successor project-head commit for G5, G6,
   and G8.
8. Run the three independent protected-performance gates for G7.
9. Finish security, data lifecycle, upgrade/rollback, documentation, repository,
   dependency, and draft-release evidence for G9, G11, and G12.
10. Freeze one immutable candidate and run canonical G1-G12, including the final
   G3 proof, against that exact candidate.

Every PR follows `AGENTS.md`, links its issue, states protected-algorithm impact,
records focused and broad verification, and documents user-visible behavior.
