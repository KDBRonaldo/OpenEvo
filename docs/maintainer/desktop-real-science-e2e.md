# Release Desktop Real-Science E2E

This maintainer-only gate runs the exact v0.1.10 macOS Desktop composition
against a real remote science workspace. It is part of #163 and the lifecycle
release blocker #220. It is not a user-facing CLI and never calls Core Control
directly.

## Required environment

Run the gate on the macOS release-verification host with:

- the exact candidate app copied from the candidate DMG, normally at
  `/Applications/OpenEvo Desktop.app`;
- one literal, selectable `Host` alias in the current user's
  `~/.ssh/config` (for example `evolab`);
- a system OpenSSH configuration for which `/usr/bin/ssh <alias>` can reach
  the server using the user's normal `IdentityFile`, agent/Keychain,
  `ProxyJump`, `User`, port, and host-trust settings;
- a Linux x86-64 server that passes Daemon preflight, can run the managed
  Docker runtime, has sufficient storage and outbound access, and has a
  working Codex subscription login for the remote account; running inside an
  approved root user container is allowed;
- the exact candidate Core wheel, framework lock, Linux Daemon bundle and
  manifest, and managed Science runtime archive;
- the exact `release-candidate.json`, `app-bundle-smoke.json`, packaged-web
  manifest, and candidate Playwright evidence downloaded from the same
  candidate; and
- a clean checkout of the candidate source commit, whose
  `desktop/packaging/web` tree matches the packaged-web manifest.

The alias is the only remote selector accepted by the runner. Do not supply a
hostname, IP address, username, port, private-key path, host-key fingerprint,
or `SSH_AUTH_SOCK`. Desktop enumerates the literal aliases and delegates the
connection to `/usr/bin/ssh <alias>`; system OpenSSH is the final authority for
routing, identity, authentication, and trust.

## Run the exact candidate

```bash
uv run python scripts/e2e/desktop_real_science_e2e.py \
  --ssh-host-alias <literal-ssh-config-alias> \
  --app-bundle '/Applications/OpenEvo Desktop.app' \
  --core-wheel <candidate-openevo-wheel> \
  --framework-lock <candidate-framework-lock.json> \
  --managed-runtime-archive <candidate-managed-runtime.tar> \
  --daemon-bundle <candidate-openevo-daemon-linux-x86_64> \
  --daemon-manifest <candidate-openevo-daemon-bundle.json> \
  --release-candidate-manifest <candidate-release-candidate.json> \
  --app-bundle-smoke <candidate-app-bundle-smoke.json> \
  --packaged-web-manifest <candidate-packaged-web-manifest.json> \
  --playwright-candidate-evidence <candidate-playwright-evidence.json> \
  --packaged-web-root <candidate-checkout>/desktop/packaging/web \
  --output desktop-real-science-e2e-evidence.json
```

There is no local-build, smoke-only, single-Task, manual-host, or renderer-skip
mode in the publication path. The model and effort are fixed to
`gpt-5.3-codex-spark` and `high`; subscription execution uses explicit
transcript capture and never claims token-level metrics.

Before launch, held file descriptors bind the validated bytes to the exact
files subsequently used. The runner verifies that:

- the installed app's packaged sidecar digest equals the sidecar digest in the
  mounted-DMG app smoke;
- the installed `openevo-ssh-askpass` helper digest, size, mode, relative path,
  architecture policy, and ad-hoc signature equal the candidate manifest;
- the framework lock binds the exact wheel and verified registry digest;
- the Daemon bundle, manifest, and managed runtime match the candidate; and
- the candidate commit, DMG, packaged web, Playwright evidence, and local
  source checkout all share one immutable identity.

## Product-boundary flow

The runner performs all business actions through the authenticated Desktop
Local API v2:

1. Launch the sidecar and askpass helper directly from the candidate app bundle
   using the native listener/executable FD handoff and one bounded credential
   frame.
2. Negotiate the strict `/version` v2 identity for release `0.1.10`, require
   mutation major 2 and Core major 2, reject the legacy shell route, and prove
   authenticated/unauthenticated `/desktop/v2/state` behavior.
3. Read `/desktop/v2/ssh-hosts`, select the requested literal alias, create one
   `system_openssh` profile, and connect it through a durable lifecycle
   operation. No manual connection fields are sent.
4. Reserve one cold generation-zero scratch-project lifecycle with all
   evolution targets disabled. Require HTTP 202 and an authoritative operation
   within the bounded renderer deadline, then require the remote work to remain
   active for more than 15 seconds. Observe at least two ordered phases and
   actual sanitized `ssh_*` or `daemon_*` output through the lifecycle log
   route. Force one SSE reconnect and one packaged-sidecar relaunch, resume the
   same operation ID without issuing another create request, and require one
   successful project result. After shutdown, verify exactly one Core project,
   one Desktop/Core mapping, and one applied `create_project_v2` mutation for
   the stable action ID. Fetch capabilities through that project's active Core
   tunnel, then patch the same project to enable `text_memory` and
   `skill_bundle` with their supported remote effective defaults and
   `agent_system` with the supported Core-owned `auto` resolver.
   Immediately after the required sidecar relaunch, repeat the complete
   `/version` and Desktop-session negotiation against the new process. Every
   release-composition field must equal the initial candidate identity, while
   the instance-bound `build_id` must change. After that negotiation succeeds,
   the runner records the accepted current identity and pins its new `build_id`
   into the packaged-renderer handoff; a pre-relaunch bootstrap identity is
   never reused. A failed renegotiation retains only the last verified identity
   plus its closed failure code as evidence.
5. Validate the project against the exact remote registry before each Task.
6. Submit two immutable Tasks. For each Task, verify its admission and
   authoritative attempt, required v2 timeline event types, exact predecessor
   Project Head and Runtime Context, committed successor transition, adjacent
   successor generation, and an Evolution Revision output count of three.
7. Prove Task 1 uses generation 0, Task 2 pins Task 1's generation-1 successor,
   and the Task-2 Runtime Context equals the Runtime Context committed by that
   successor. The final active Project Head must be generation 2.
8. Run the candidate-bound packaged renderer against the same live Desktop v2
   session. It must display the real project, both Tasks, Project Head,
   Evolution Revision, Runtime Context, Effective Execution, three independent
   target controls with the exact selected methods, and the System OpenSSH
   workspace. Network access is limited to the packaged origin, authenticated
   loopback v2 reads, and the exact idempotent terminal-operation acknowledgement
   required after native-journal reconciliation; every other renderer mutation
   remains blocked.
9. Disconnect the profile and terminate the complete sidecar/renderer process
   groups. macOS uses a non-reaping `kqueue` process-exit observer so the group
   leader remains authoritative until descendants have been closed.

v0.1.10 ordinary refresh and release verification do not depend on the v2
Task-artifact collection endpoints. Core now serves the authoritative task
artifact collection plus project-scoped artifact metadata and content routes;
the Desktop bridge uses only the active project tunnel when those resources are
requested. Artifact content, SSH commands, process environments, backend
tokens, Core URLs, and absolute host paths are not used as fallback evidence.
Artifact content remains renderer-visible only through its closed Desktop v2
projection. The lifecycle log route is the narrow exception for process
output: it may carry bounded, terminal-control-stripped, sanitized SSH and
Daemon stdout/stderr, but never command lines or environment values and never
as success authority. The
committed Evolution Revision's typed `artifact_count` remains the Task-output
boundary.

## Evidence and privacy policy

The output is canonical JSON, mode `0600`, and at most 128 KiB. Its schema is
v3 and uses a closed field allowlist. It records only candidate asset
digests/sizes, closed Desktop/Core identities, Project Head composition digests,
Task/admission/attempt/transition digests and counts, required timeline event
types, successor-reuse booleans, renderer observations, lifecycle reservation
and duration buckets, ordered phases, process-log source/digest proof, stable
action/operation identity, SSE/relaunch recovery, exactly-one project/mapping/
mutation counts, generated secret-canary absence, and cleanup results.

It does not retain:

- the SSH alias (or a hash of it), hostname, IP address, username, port,
  fingerprint, SSH config, SSH command, or identity path;
- Desktop session, native handoff/readiness, mutation, Core bearer, password,
  passphrase, private-key, or Codex authentication values;
- opaque raw project/Task/admission/attempt/transition IDs;
- host paths, task objective, transcript, lifecycle log messages, artifact
  content, or runtime file content.

The renderer screenshot masks the alias, raw IDs, and objective before it is
retained. A generated canary is placed only where the gate can detect an
accidental leak; publication fails if it appears in lifecycle payloads,
renderer text, screenshot bytes, support output, or evidence. Failure evidence
contains only a bounded stage, typed code, optional HTTP status, partial
allowlisted observations, and cleanup state. A nominally passing run becomes
failed if ownership cleanup is incomplete.

## Sign and publish

After the exact-candidate run passes, place its canonical evidence at the fixed
candidate-tag path and sign it with the release-host key stored outside the
repository:

```bash
uv run python scripts/ci/desktop_real_science_e2e_attestation.py sign \
  release-evidence/<candidate-tag>/desktop-real-science-e2e.json \
  --private-key /root/.openevo/release-attestation/desktop-real-science-e2e-v1 \
  --public-key release-trust/desktop-real-science-e2e-v1.pub \
  --signature release-evidence/<candidate-tag>/desktop-real-science-e2e.json.sig
```

After candidate creation, the publication-policy commit may add exactly those
two files and no source, workflow, validator, documentation, or policy change.
The protected Preview publisher verifies the caller-supplied evidence and
signature digests, the exact candidate manifest/app smoke, the frozen public
key and protected trust anchor, the complete candidate-to-policy Git delta,
both Tasks and adjacent successor heads, Task-2 context reuse, renderer v2
observation, and ownership cleanup before making the draft public.

## Structural verification

```bash
uv run python scripts/e2e/desktop_real_science_e2e.py --structural-check
uv run pytest -q tests/ci/test_desktop_real_science_e2e.py
uv run pytest -q tests/ci/test_validate_desktop_real_science_e2e.py
uv run pytest -q tests/ci/test_desktop_real_science_e2e_attestation.py
```

`--structural-check` verifies only the frozen v2/System OpenSSH and native
boundary; it does not launch Desktop, contact the remote server, or write
evidence. Unit tests use synthetic v2 Project Heads/Tasks and local child
processes and never claim a real remote run occurred.
