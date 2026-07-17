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
- At each workstream boundary, use fresh independent `gpt-5.6-sol` reviewers
  with high reasoning and resolve findings before merge.
- Run focused tests, broader affected regressions, `git diff --check`, and a
  complete diff review before commit.

Simulator and component tests can accelerate development. They cannot satisfy a
packaged-app, real-host, protected-performance, or release-candidate gate.

## Current Priorities

The exhibition requires the packaged Desktop path first. Current priority is:

1. make the real mounted and copied DMG reach the product shell on a clean Mac;
2. rehearse the ordinary-user Subscription science-task path on the existing
   exhibition server as non-gating integration evidence;
3. complete the visible Desktop Files, History, System, recovery, and diagnostics
   workflows required for that path;
4. finish the self-contained Daemon bundle and clean-host preparation;
5. prove the clean-host Subscription gate, then close Self-Deployed, atomic
   cross-session evolution, benchmark, security, and release-evidence gaps.

This order changes implementation scheduling only. It does not reduce any
blocking requirement in the canonical specification.

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
service supervision exist. The shipped bundle is not yet self-contained, raw
Subscription credential isolation is incomplete, Self-Deployed is unavailable,
and upgrade/rollback and maintenance operations are incomplete.

**Owning issues:** #159, #160, #167, and #168.

**Next work:** finish the exact offline Daemon bundle and install descriptor,
then close clean Subscription deployment, credential isolation, doctor/repair,
upgrade/rollback, and Self-Deployed preparation. These changes prove G3, G4,
G8, G9, and the Daemon portions of G11.

## C. Desktop Product Maturity

**Outcome:** an ordinary scientist completes the supported workflow entirely in
the packaged macOS application.

**Current state:** release-mode server/project setup, host-key review, remote
capabilities, task execution, retry/cancel, timeline/transcript, and evolution
inspection are implemented. The latest real DMG candidate still fails during
renderer/provider bootstrap, and Files, History, System maintenance, Keychain
credential choices, and several recovery flows remain incomplete.

**Owning issues:** #158, #163, and #203.

**Next work:** issue #203 is the immediate blocker. First make the exact packaged
app pass clean first-run and relaunch. Then consume the migrated contract,
finish the real Subscription E2E, and complete the missing ordinary-user views
and recovery actions required by G2 and G10.

## D. Repository, Documentation, And Release Engineering

**Outcome:** source layout, public documentation, package inventories, and
GitHub releases present one coherent Desktop-plus-Daemon product.

**Current state:** the repository has explicit `src/openevo/`, `desktop/`, and
`benchmarks/` boundaries and one canonical product specification. Some active
documentation, issue state, package inventory, release metadata, dependency
evidence, and cleanup work still reflect intermediate implementation states.

**Owning issues:** #131 and #193.

**Next work:** update documentation alongside each completed workflow; remove
obsolete active paths rather than adding wrappers; and make the draft-release
workflow bind the exact DMG, Daemon bundle, manifest, checksums, source, and
evidence required by G1 and G12. PyPI remains outside this release.

## E. Release Candidate Validation

**Outcome:** one immutable candidate satisfies every G1-G12 gate.

**Current state:** component and subsystem tests exist, but there is no candidate
with complete clean-host Desktop/Daemon evidence, both execution modes, all
evolution gates, protected performance, recovery/security matrices, and
downloaded draft-release verification.

**Owning issue:** #131.

**Next work:** after A-D are complete, run every canonical gate against the same
manifest-bound candidate. Infrastructure reruns follow the policy in the spec;
product or benchmark failures require a corrected candidate.

Before publication, two fresh-context `gpt-5.6-sol` high-effort reviewers
independently review product/spec compliance and release risk/evidence.

## Immediate Execution Order

1. Resolve #203 and prove G2 with a real mounted and copied DMG.
2. Implement and adopt the critical contract migration before adding more
   business UI to the frozen v1 model.
3. Rehearse the packaged Desktop-to-Daemon Subscription workflow on the existing
   exhibition server; record it as integration evidence, not G3.
4. Complete the Desktop views and recovery actions needed for the exhibition and
   G10.
5. Ship the self-contained Daemon bundle, exact install provenance, and
   clean-host automated preparation, then prove G3.
6. Complete the Self-Deployed reference profile and end-to-end path for G4.
7. Unify product run admission, workspace result, sealed dataset, evolution
   transition, materialization, and successor project-head commit for G5, G6,
   and G8.
8. Run the three independent protected-performance gates for G7.
9. Finish security, data lifecycle, upgrade/rollback, documentation, repository,
   dependency, and draft-release evidence for G9, G11, and G12.

Every PR follows `AGENTS.md`, links its issue, states protected-algorithm impact,
records focused and broad verification, and documents user-visible behavior.
