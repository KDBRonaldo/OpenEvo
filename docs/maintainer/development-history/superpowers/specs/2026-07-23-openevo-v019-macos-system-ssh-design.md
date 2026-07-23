# OpenEvo 0.1.9 macOS Startup and System OpenSSH Design

Status: approved implementation design

Date: 2026-07-23

Owning issue: [#131](https://github.com/CompLifeLab-ZJU/OpenEvo/issues/131)

## Purpose

OpenEvo 0.1.9 must be the first Preview whose installed macOS Desktop is
actually usable for the ordinary remote-workspace workflow. It must start on
the target Apple Silicon Tahoe Mac, use the user's system OpenSSH configuration
as the connection authority, install or attach the matching remote Daemon, and
complete a real two-session Codex Subscription evolution flow entirely from
Desktop.

This design refines the canonical product contract in
`docs/maintainer/productization/spec.md`. It does not reduce the release gates
in that specification and does not change evolution algorithm behavior.

## Confirmed Product Decisions

The following decisions are inputs, not open questions:

1. The normal connection is equivalent to invoking `/usr/bin/ssh <alias>`.
   OpenSSH, not OpenEvo, is authoritative for host resolution, user, port,
   identities, agent and Keychain behavior, jump/proxy routing, and host trust.
2. Desktop lists usable hints from the local OpenSSH configuration instead of
   asking the ordinary user to copy an IP address, user, port, or key path.
3. The first-host prompt, encrypted-key passphrase prompt, password prompt, and
   changed-host-key review are all mediated by the Desktop experience.
4. OpenEvo never becomes a second SSH credential or known-host authority.
5. The installed application, not a browser build or source sidecar, is the
   acceptance subject.
6. A successful connection alone is insufficient. The exact candidate must run
   two real remote sessions and prove next-session evolution reuse.
7. The current frozen `ssh_agent` contract is not silently reinterpreted. The
   authority change is represented by a new contract major together with the
   already-required project-head authority migration.

## Current Evidence

### Packaged startup failure

The installed 0.1.8 Tauri process starts, spawns its packaged sidecar, and then
reports exit 255 before the Local API is ready. The retained native log proves
that no recognized OpenEvo startup marker was accepted.

A bounded local harness recovered the discarded stock loader failure. The
PyInstaller one-file child extracts `Python.framework`, then macOS hardened
runtime library validation rejects that framework because it and the outer
ad-hoc executable do not share a Developer ID Team identity. Both objects are
ad-hoc signed and have no Team identifier, while the outer executable carries
the runtime flag.

Two controls isolate the cause:

- adding the broad disable-library-validation entitlement lets the exact
  sidecar remain alive; and
- re-signing the exact sidecar ad-hoc without hardened runtime makes the full
  production FD-handoff sidecar smoke pass.

The release is explicitly unsigned and non-notarized. Its current combination
of an ad-hoc identity and hardened runtime is therefore internally
inconsistent; it is not evidence that the one-file archive or FD authority must
be weakened.

### SSH authority mismatch

The qualifying remote host is reachable from the Mac through an OpenSSH alias
whose effective route uses normal user configuration. The equivalent clean
environment `/usr/bin/ssh <alias> true` succeeds. The current Desktop transport
cannot reproduce it because it supplies explicit host/user/port values, uses
`-F /dev/null`, disables default identities, owns a separate known-host file,
and relays only an isolated agent.

This is a product-contract mismatch, not a server-substrate failure. The
qualifying server has the required Linux, Docker, GPU, writable state, Codex
installation, and subscription authentication substrate. Python and `uv` may
be provisioned by the exact Daemon Bundle path and are not host prerequisites.

## Scope

### In scope

- unsigned Tahoe-compatible packaged startup;
- bounded cross-layer startup and connection diagnostics;
- OpenSSH config host discovery for UI hints;
- a system-OpenSSH connection owner and native askpass helper;
- first and changed host-key handling;
- legacy explicit-profile rebind;
- Desktop Local API and Core Control API next-major authority cutover for the
  resources used by the supported Subscription workflow;
- production task admission, attempts, sealed transition, atomic successor
  project head, and next-session context reuse;
- renderer migration and packaged Mac/remote-host acceptance;
- exact Daemon Bundle compatibility, installation, tunnel, reconnect, and
  upgrade handling.

### Out of scope

- local Codex execution on the Mac;
- changing protected memory, skill, or agent-system algorithms;
- adding another harness;
- signing or notarizing 0.1.9;
- claiming Self-Deployed mode before its verified model/runtime path is ready;
- using a local Core or bundled simulator in a release build;
- making SSH a post-bootstrap business-operation fallback;
- sourcing a shell startup file to reconstruct a Terminal environment.

## Architecture Overview

```text
React renderer
  | authenticated /desktop/v2 only
  v
packaged Python sidecar
  |- durable local resources and v1 read-only migration
  |- bounded SSH-config catalog (discovery hints only)
  |- system OpenSSH process/control-socket owner
  |- single-use askpass capability broker
  |- remote lifecycle before compatible Daemon exists
  `- strict /v2 tunnel client after negotiation
            |
            | sidecar-owned system OpenSSH session
            v
Tauri native host + sealed askpass helper
  |- exact packaged sidecar/helper inventory
  |- native secure askpass prompts
  |- sidecar process-group ownership
  `- bounded native/startup diagnostics
            |
            | alias semantics remain owned by OpenSSH
            v
remote Linux host
  |- exact release-matched OpenEvo Daemon Bundle
  |- loopback Core Control API /v2
  |- managed runtime and existing Codex subscription
  `- authoritative project/task/evolution state
```

The renderer never receives an SSH command, effective config path, identity
path, secret reference, prompt secret, remote Core URL, backend token, remote
host path, or benchmark concept.

## 1. Unsigned macOS Startup Composition

### 1.1 Release signing policy

The 0.1.9 unsigned release configuration sets Tauri macOS
`hardenedRuntime=false`. It retains deterministic ad-hoc signing and every
existing executable, archive, inherited-FD, release-input, and identity check.
It does not add `com.apple.security.cs.disable-library-validation`, because that
entitlement is broader than the Preview needs and would hide an incoherent
runtime/signing composition.

The candidate verifier requires all of the following:

- the app, native host, sidecar, and helper have the expected architecture;
- the final app has valid ad-hoc signatures and no unexpected entitlements;
- the unsigned sidecar does not carry the runtime flag;
- the embedded PyInstaller inventory and exact release inputs still match;
- the mounted-DMG app and detached copied app both execute the same verified
  bytes;
- Gatekeeper/quarantine handling remains the documented unsigned **Open
  Anyway** flow rather than a test-only xattr deletion claim.

If OpenEvo later adopts Developer ID signing, the identity must be passed into
PyInstaller while its collected libraries are built. Post-signing only the
outer one-file executable is not a supported replacement.

### 1.2 Startup diagnostic classification

The native scanner continues to discard arbitrary child output. Before
discard, it applies a bounded byte/line parser to reviewed stock loader
signatures. The Tahoe failure maps to a closed event such as:

```text
component=sidecar
stage=embedded_python_loader
code=python_shared_library_validation_failed
```

No raw loader line, extracted path, home directory, argv, or environment value
is persisted. Unknown output records only bounded counts, a category, and a
one-way fingerprint.

Every startup attempt has an opaque attempt ID and monotonic sequence. The
exported diagnostic envelope records the last completed stage and first failed
stage across native initialization, bundle verification, spawn/FD handoff,
bootloader, Python, sidecar entry, state store, Local API, renderer bootstrap,
SSH, Daemon, tunnel, task, and transition. It includes bounded OS/build,
architecture, app-location, quarantine, and translocation categories without
user paths.

### 1.3 Recovery behavior

Retained Preview state is never deleted as a startup workaround. The new local
store opens in a new versioned namespace, inventories the previous store as
read-only migration input, and either imports a validated resource or exposes
a typed rebind action. A malformed old store produces a bounded recoverable
diagnostic rather than preventing the Local API from starting.

## 2. OpenSSH Host Catalog

OpenSSH has no authoritative host-enumeration API. Desktop therefore separates
discovery from connection:

- catalog parsing produces hints;
- selection and connection use the literal alias; and
- only the real `/usr/bin/ssh` process decides whether the alias is usable.

### 2.1 Lexical discovery

The sidecar reads the user's default config root and statically resolvable
`Include` files with explicit limits for file count, total bytes, per-file
bytes, include depth, glob expansion, line length, and alias count. Cycles and
over-budget inputs produce typed partial-catalog warnings. No config content or
path is returned to React.

The catalog includes only non-negated `Host` tokens with no OpenSSH wildcard
characters. Wildcard-only patterns, `Match`-dependent names, canonicalized
destinations, and aliases known only to external tools cannot be enumerated.
The UI provides a bounded manual alias field for those cases; it still does
not ask for IP/user/port.

The parser never runs `Match exec`, `ProxyCommand`, shell expansion, or an SSH
connection while populating the list. It handles quoting and comments only to
the degree needed to find literal `Host` and static `Include` tokens. Unknown
syntax is ignored with a non-secret warning because OpenSSH remains the final
parser.

### 2.2 Selection probe

Desktop may run `/usr/bin/ssh -G <alias>` only after the user explicitly
selects or connects to an alias. The result is bounded and parsed into a
non-authoritative display snapshot. It may be omitted when evaluation would
execute a conditional or cannot be classified safely. It is never flattened
into connection arguments or persisted as authority.

The profile persists:

- a local display name;
- the literal OpenSSH alias;
- `connection_authority=system_openssh`;
- a bounded last-observed display snapshot and catalog generation, when safe;
- connection state, typed failure, and timestamps.

It does not persist host, user, port, key path, password, passphrase, known-host
path, ProxyCommand, or resolved command.

## 3. System OpenSSH Session Owner

### 3.1 Executable and environment

Release code executes the exact platform `/usr/bin/ssh`, `/usr/bin/ssh-keygen`,
and other explicitly allowlisted system binaries. It does not search `PATH` for
the SSH client and does not invoke a shell to construct the SSH command.

The child receives a closed environment. `HOME`, locale, the exact inherited
`SSH_AUTH_SOCK` when present, the askpass variables/authority, and a
deterministic command-search path derived from macOS system path configuration
are allowlisted. Desktop does not source `.zshrc`, `.zprofile`, or another
shell file. A user `ProxyCommand` that depends on an unlisted relative
executable fails with a typed remediation asking the user to make that command
absolute or system-discoverable.

### 3.2 Options OpenEvo may own

OpenEvo may override only session-safety and ownership behavior:

- no user-owned or ambient ControlMaster reuse;
- no local command;
- no unrequested remote command or TTY;
- no unowned local, remote, dynamic, or agent forwarding;
- explicit connect/keepalive/deadline behavior;
- exact OpenEvo-owned control socket and tunnel forwarding when that operation
  requires them;
- exit-on-forward-failure and bounded process cleanup.

It must not supply `-F /dev/null`, `-p`, `-l`, `-i`, `IdentitiesOnly`,
`UserKnownHostsFile`, `GlobalKnownHostsFile`, a proxy route, or an auth method.
It must not change `StrictHostKeyChecking` away from the user's effective
policy except for the separately reviewed first-host interaction described
below.

The exact interaction between `ClearAllForwardings` and OpenEvo's intentional
`-L` tunnel is proven against the supported macOS OpenSSH before implementation
is accepted. The command and tunnel option sets are separate closed builders,
not string concatenation.

### 3.3 Owned multiplexed session

One connection operation creates one OpenEvo-owned OpenSSH master under a
short, owner-private sidecar runtime directory. The sidecar binds the master's
PID/birth identity, process group, socket device/inode, alias, and connection
generation. Reconnect never adopts an ambient master. The Tauri host continues
to own the complete sidecar process group, so app shutdown remains a final
bounded cleanup boundary.

Bootstrap commands, uploads, and the Core loopback tunnel reuse only that owned
session. This gives password users one interactive authentication sequence and
prevents each lifecycle subprocess from reopening credential prompts. Stop,
profile replacement, app exit, or failed ownership checks close and reap the
master and descendants within a hard deadline.

After a compatible Daemon session is established, the SSH owner retains only
the private tunnel and maintenance authority. Project, task, service, artifact,
history, evolution, and diagnostic business operations go through Core Control
API v2. A Core error never activates an SSH business-command fallback.

## 4. Native Askpass And Host Trust

### 4.1 Helper boundary

The app bundles a minimal Rust askpass helper as a separately inventoried and
ad-hoc-signed executable. OpenSSH invokes it through `SSH_ASKPASS` with
`SSH_ASKPASS_REQUIRE=force`. The helper accepts requests only when all of these
hold:

- its direct parent is the exact Apple `/usr/bin/ssh` process;
- the process ancestry is bound to the current OpenEvo-owned connection;
- a single-use capability is accepted by the current sidecar askpass broker;
- the request fits a closed prompt kind and byte budget; and
- the connection generation has not been cancelled or replaced.

The helper uses a native secure field for password and passphrase prompts and a
native confirmation surface for first-host trust. The sidecar broker authorizes
the connection generation and prompt kind but never receives the response.
Secret text flows only from the secure control to helper stdout and then to
OpenSSH. It never enters React, the Local API, native/sidecar logs, diagnostics,
argv, persisted state, or an OpenEvo Keychain item.

OpenSSH remains free to use the user's existing agent and macOS Keychain
configuration. OpenEvo neither duplicates nor disables `UseKeychain`,
`AddKeysToAgent`, or equivalent user choices.

Unknown, repeated beyond budget, malformed, or concurrent prompts fail closed.
Cancellation returns a typed user-cancelled connection result and tears down
the owned session.

### 4.2 First host key

The real OpenSSH handshake supplies the host identity and prompt. The native
confirmation surface shows only bounded host/fingerprint/algorithm information
classified from that prompt. Approval is delivered to the same OpenSSH
process; OpenEvo does not copy a key into a private trust database.

The effective user policy may intentionally forbid interactive first use. In
that case Desktop reports the policy refusal and does not override it.

### 4.3 Changed host key

A changed key always blocks the operation. Desktop displays the bounded
fingerprint evidence reported by the failed real handshake and requires a
separate explicit **Review changed key** action. It never treats a prior
successful alias connection as approval for the new key.

Automatic repair is available only when a bounded `ssh -G` inspection proves
one ordinary writable `UserKnownHostsFile`, no `KnownHostsCommand`, no
ambiguous `HostKeyAlias`, and one deterministic host/port lookup token.
Desktop then invokes exact `/usr/bin/ssh-keygen` for that target after explicit
approval and reconnects through the normal first-host flow. Multiple trust
files, global-only trust, command-provided trust, unsupported hashing/layout,
or any uncertainty fails closed with typed in-app remediation.

## 5. Contract Major And State Migration

### 5.1 Why v2 is required

Desktop Local API v1 requires host/user/port and describes the release SSH path
as isolated `ssh_agent`. It also reuses a generic Core revision type for several
different product identities. Treating a system alias as an old agent profile
or calling the old revision a complete project head would change frozen field
meaning and hide authority loss.

0.1.9 therefore negotiates Desktop Local API v2 and Core Control API v2. The
release renderer accepts v2 only. Release startup fails closed if only v1,
simulator, scaffold, dry-run, direct backend, or legacy routes are available.
The unversioned discovery endpoint reports supported majors and exact OpenAPI,
event-schema, release, and feature digests before mutation.

### 5.2 Distinct v2 identities

V2 has no context-dependent generic `revision`. It exposes distinct closed
references:

| Resource | Required identity |
| --- | --- |
| Project Head | opaque head ID, project ID, generation, manifest digest |
| Evolution Revision | opaque artifact-set ID and manifest digest |
| Runtime Context Snapshot | opaque materialization ID and canonical digest |
| Effective Execution Snapshot | opaque verified snapshot ID, digest, producer ID |
| Workspace Snapshot | opaque content snapshot ID and digest |
| Task Admission | immutable task/admission ID and admission digest |
| Attempt | append-only attempt ID and ordinal under one admission |
| Transition | opaque successor transition ID and expected predecessor head |

Project-head responses bind the exact workspace, evolution revision, runtime
context, effective execution snapshot, registry digest, and predecessor. Run,
artifact, history, context, and event payloads use those exact typed references
instead of aliases.

### 5.3 Admission and successor behavior

A saved task remains a Desktop draft while a successor transition, settings
transition, runtime-context rebind, or workspace publication is unresolved.
Submission returns typed not-ready and creates no Task, admission, or attempt.

Successful admission atomically creates one immutable Task and closed admission
that pins the current head and all execution/context identities. Infrastructure
retry appends an attempt under that admission and cannot change any pin.

After completion, the run owner seals the dataset, executes enabled verified
methods outside the inference process, validates and materializes every output,
and atomically commits one successor Project Head containing both the accepted
workspace result and complete successor evolution/runtime context. Any method,
validation, materialization, or commit failure leaves the predecessor active
and keeps the next task not ready. No stale-head fallback is allowed.

V2 exposes compare-and-set/idempotent close, transition retry, replacement
plan, abandon, and historical restore actions with replayable events. The
0.1.9 renderer may expose only the subset needed for Subscription recovery, but
the provider models and state transitions remain complete and closed.

### 5.4 Production execution snapshot issuer

The Subscription issuer seals an effective execution snapshot from verified
inputs: Codex harness identity, subscription model reference, transcript
capture, `token_level_metrics_available=false`, exact managed runtime image,
runtime policy, task-network policy, and no model-serving endpoint. It is the
first production issuer accepted by the project-head store; ordinary callers
cannot construct or persist a verified snapshot.

Self-Deployed remains a typed unavailable capability until its managed model
deployment, serving readiness, and attestation issuer are production-complete.

### 5.5 Local migration

V1 state stays readable and immutable. At v2 startup:

- explicit v1 remote profiles appear as `legacy_explicit` entries that cannot
  connect;
- the user selects **Rebind to configured SSH host**, chooses an alias, and
  creates a new v2 profile; no inferred hostname match performs conversion;
- local v1 project drafts may be copied only after the user selects a v2
  profile and the content validates against the v2 schema;
- authoritative active projects are adopted only from a compatible v2 Daemon
  response that proves all typed identities; cached v1 revision state is never
  promoted into v2 authority;
- corrupt or over-budget legacy rows are quarantined logically and surfaced as
  diagnostics without blocking unrelated startup.

## 6. Renderer Experience

Before configuration, both built-in read-only science examples remain visible
and perform no SSH, Daemon, or external-network mutation.

**Add remote workspace** opens a real setup sheet immediately. Its default flow
is:

1. load configured SSH aliases and any bounded warnings;
2. select or type one alias and choose a local display name;
3. connect with visible native prompt/cancellation state;
4. review first or changed host identity when required;
5. observe typed host, Docker, disk, Codex, and Daemon checks;
6. install/upgrade or attach the exact matching Daemon;
7. negotiate and pin the v2 tunnel session; and
8. create or select a project.

Advanced manual IP/user/port/key forms are not the default and are not shipped
as a hidden fallback in 0.1.9. Unsupported aliases remain visible with a typed
reason and retry/rescan actions.

The task view distinguishes draft, admitted Task, infrastructure Attempt,
active Project Head, successor transition, Evolution Revision, and Runtime
Context Snapshot. It never labels the artifact set alone as the project
revision. Evolution targets remain independently selectable, invalid enabled
targets remain visible and block submission, and capabilities always come from
the active verified Core registry.

## 7. Daemon Bootstrap And Tunnel

The Desktop release manifest binds an exact Linux Daemon Bundle and managed
runtime input. The SSH lifecycle may inspect the host, stage that bundle,
ensure one release generation, activate or roll back it, and establish the
private loopback tunnel. It never points at a remote development checkout.

Compatibility requires exact release/build identity, API/event schema digests,
required feature set, and verified registry identity. An older compatible
Daemon may be inspected or upgraded; it cannot receive a v2 mutation until the
predicate is satisfied. Retained remote `.openevo` state is preserved and is
part of reconnect/upgrade acceptance.

The qualifying 0.1.9 server profile is Linux x86_64 with Docker Engine access,
sufficient writable space, Codex installed, and a pre-existing authenticated
subscription. Python and `uv` are provisionable implementation inputs, not
manual user prerequisites. Desktop never reads or transfers the Codex auth
file.

## 8. Error Model

Every failure crosses the renderer boundary as a closed code with retryability,
affected resource, safe summary, and one action from a bounded set such as
retry, rescan, review-host-key, rebind-profile, reconnect, install-daemon,
repair-daemon, or administrator-action.

Important distinct codes include:

- packaged sidecar loader/library-validation failure;
- local store migration failure;
- SSH config partial/over-budget catalog;
- alias unresolved;
- ProxyCommand executable unavailable;
- user-cancelled askpass;
- unsupported or malformed prompt;
- first-host approval required or policy-forbidden;
- changed host key and ambiguous trust store;
- SSH timeout/process ownership failure;
- Daemon install, compatibility, registry, or tunnel failure;
- project not ready due to successor transition;
- verified execution snapshot unavailable;
- immutable admission conflict;
- method/materialization/successor failure.

Raw SSH stderr, commands, prompt secrets, config paths, remote paths, Core
payloads, and Pydantic/OS exception strings do not cross that boundary.

## 9. Verification Strategy

### 9.1 Test-driven component gates

Write failing regressions before each behavior change:

- packaging policy tests for unsigned runtime flags and forbidden entitlement;
- native scanner tests for bounded stock loader classification and secret/path
  canaries;
- catalog fixtures for `Include`, quoting, literal/wildcard/negated hosts,
  cycles, limits, and hostile config text;
- exact SSH argument/environment tests proving user authority is not
  overridden;
- native helper ancestry/capability/prompt/cancellation tests;
- local `sshd` integration for agent, encrypted key, password, first key,
  changed key, ProxyJump, ProxyCommand, owned master, upload, tunnel, and
  cleanup;
- strict Python/OpenAPI/Zod provider and consumer conformance for v2;
- v1 read-only migration and explicit rebind tests;
- production issuer, immutable admission/attempt, not-ready, fault-injected
  transition, atomic successor, and next-session materialization tests;
- renderer setup/recovery/accessibility tests;
- release-policy tests proving simulator, dry-run, legacy route, and source
  fallback exclusion.

Hermetic SSH integration uses temporary keys, known-host data, controlled local
servers, and production spawn builders. It never uses the runner's ambient
credentials. A missing required test substrate fails the release gate instead
of silently skipping it.

### 9.2 Local Mac gates

Before dispatching a candidate:

1. run focused and affected Python, TypeScript, Rust, lint, format, type, and
   release-policy suites;
2. build the packaged sidecar and local unsigned DMG;
3. install via Finder/LaunchServices on the Tahoe Mac;
4. verify retained-state startup, clean-state startup in a disposable account,
   injected retry, quit/relaunch, and no orphan process;
5. verify the host catalog and real alias route, including its configured
   identity and jump/proxy behavior;
6. install/attach the exact local-build Daemon and rehearse the two-session
   Subscription flow; and
7. export diagnostics and scan secret/path canaries.

This is development evidence, not release evidence.

### 9.3 Exact candidate gates

GitHub builds one immutable 0.1.9 candidate and publishes a draft manifest,
DMG, Daemon Bundle, managed runtime input, checksums, and evidence index. The
candidate workflow repeats exact-input packaging and hermetic contract/SSH
gates.

The downloaded manifest-bound DMG is then installed on the same M3 Pro Tahoe
machine and repeats:

- unsigned approval and startup;
- retained and clean state;
- Retry, quit/relaunch, and functional Add Remote Workspace;
- the real configured alias, host prompts, Daemon install/compatibility, and
  active tunnel;
- a `gpt-5.3-codex-spark`, high-effort Subscription task;
- independent evolution target evidence and a successor project head;
- a second task proving accepted context is consumed only in the next session;
- relaunch/reconnect to authoritative remote state; and
- bounded diagnostic export.

No local build or separately signed evidence may substitute for those exact
candidate bytes.

## 10. Implementation Order

1. Freeze this design and update the canonical/architecture contract language.
2. Add startup regressions, remove the incompatible unsigned hardened-runtime
   composition, and prove a locally installed app reaches Local API readiness.
3. Define v2 OpenAPI/event schemas and the v1 read-only migration boundary.
4. Implement catalog, system-SSH builders, native askpass, host trust, owned
   master, upload, and tunnel with hermetic integration tests.
5. Implement the verified Subscription execution issuer and v2 Daemon run owner
   through atomic successor commit and next-session context use.
6. Cut the sidecar bridge/provider and renderer to v2 with no release fallback.
7. Complete local packaged Mac plus qualifying-host two-session rehearsal.
8. Run independent high-effort review, resolve findings, build the exact
   candidate, repeat target-Mac acceptance, and only then publish 0.1.9.

Each step is a reviewable checkpoint linked to #131. If implementation exposes
a product decision absent from the canonical specification, update that
specification before continuing. If the scope changes, record it in the owning
issue before changing code.

## Success Definition

The work is complete only when an ordinary scientist can download the exact
0.1.9 DMG, start it on the supported Mac, choose an existing SSH-configured
server without transcribing connection details, complete any necessary native
authentication/trust interaction, let Desktop install and control the exact
Daemon, run two real Subscription tasks with atomic next-session evolution
reuse, relaunch, reconnect, and diagnose failures without using a terminal or
exposing secrets.
