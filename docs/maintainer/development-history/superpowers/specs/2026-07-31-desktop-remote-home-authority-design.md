# OpenEvo Desktop Remote System-Home Authority Design

Status: approved 2026-08-01

Date: 2026-07-31

Owning issue: [#265](https://github.com/CompLifeLab-ZJU/OpenEvo/issues/265)

Related release issue: [#220](https://github.com/CompLifeLab-ZJU/OpenEvo/issues/220)

Target release: OpenEvo Desktop v0.1.10

## Purpose

OpenEvo Desktop must connect through the user's literal system-OpenSSH alias,
bootstrap the matching Daemon, create a project, and run a task when the selected
Linux account has any supported writable system home. The home does not have to
be `/root` or `/home/<user>`.

This design fixes the authority gap found by the real macOS-to-Linux v0.1.10
E2E. It refines the implementation of the existing supported-host requirement
in `docs/maintainer/productization/spec.md`; it does not add a user-configurable
remote path or change the Desktop/Daemon product boundary.

## Confirmed Decisions

1. The default connection remains equivalent to `/usr/bin/ssh <alias>`.
   System OpenSSH remains final authority for host, user, port, identity,
   authentication, route, and host-trust behavior.
2. Desktop does not ask the user for an IP address, username, home directory,
   workspace root, or Daemon path.
3. After the owned OpenSSH master is authenticated, Desktop privately discovers
   the exact effective account and its NSS-configured home.
4. That result becomes a process-local, connection-generation-bound
   `RemoteHomeAuthority`. It is not a renderer DTO, Desktop Local API field,
   persisted profile field, event payload, or log field.
5. Workspace and Daemon bundle roots derive from that authority. The release
   system-OpenSSH path never derives them from a username convention or from
   `$HOME`.
6. Home-derived remote stages revalidate the effective account and NSS home.
   Identity or path drift fails closed before the stage may continue.
7. `/root`, `/home/<user>`, and an arbitrary safe absolute path such as
   `/srv/research/alice` use the same code path.

## Problem And Evidence

The current v2 lifecycle starts the correct system-OpenSSH master and then runs
`id -un`. It uses the returned username to construct an internal
`RemoteProfileConfig`. Two later defaults independently assume conventional
Linux paths:

- `RemoteProfileConfig.effective_workspace_root` falls back to
  `/home/<user>/.openevo/workspaces`.
- `daemon_bundle_service_root_for_user()` returns `/root/...` for root and
  `/home/<user>/...` for every other account.

The exact source-bound Desktop candidate connected successfully to a valid
Ubuntu account whose passwd/NSS home was outside those prefixes. Daemon staging
then attempted to create the conventional path and failed with permission
denied. Core asset and Core runtime stages already use
`pwd.getpwuid(os.geteuid()).pw_dir`; the incorrect assumption is confined to
the pre-Daemon Desktop/deployment path.

The canonical host baseline requires SSH access to a writable user home. It
does not require a particular prefix. Therefore the observed failure is a
release bug, not an unsupported-host result.

## Scope

### In scope

- private discovery of effective remote username, UID, and NSS home through the
  already-authenticated OpenSSH master;
- a closed, immutable, process-local remote-home authority;
- custom-home derivation for the v2 internal workspace root and Daemon bundle
  root;
- account/home/path revalidation for system-OpenSSH lifecycle commands and
  Daemon bundle staging;
- sanitized typed failure and lifecycle-progress integration;
- focused unit, contract, integration, packaged-app, and real remote E2E tests;
- architecture, sidecar, handoff, and release-evidence documentation updates.

### Out of scope

- a renderer or Local API field for a remote home or arbitrary host path;
- manual host/user/port configuration;
- reading shell startup files or trusting a remote `$HOME` environment value;
- privilege elevation, `sudo`, remote account creation, or changing NSS data;
- supporting a home containing unsafe path components or a symlinked physical
  path;
- returning to SSH for project, task, artifact, service, or evolution business
  operations after Daemon compatibility negotiation;
- expanding the legacy explicit-profile maintainer transport into an ordinary
  user product path.

## Architecture

```text
renderer selects literal alias
  -> Desktop Local API v2 profile-connect operation
  -> owned system-OpenSSH master authenticates `ssh <alias>`
  -> private fixed remote-account probe (not observed or logged)
  -> validate exact user + UID + NSS home + physical directory
  -> connection-generation-bound RemoteHomeAuthority
       |- derive <home>/.openevo/workspaces
       |- derive <home>/.openevo/daemon-bundles
       `- guard home-derived remote command followers
  -> stage/verify/start exact Daemon Bundle
  -> negotiate active-project private tunnel
  -> all business operations use Core Control API v2
```

The renderer-visible profile remains the existing literal-alias model. No
Desktop Local API or Core Control API schema gains a host path.

## Components

### 1. Remote-home authority primitive

A small deployment-owned module defines `RemoteHomeAuthority` and the closed
probe/guard builders. The authority binds:

- Desktop profile ID;
- OpenSSH connection generation;
- effective remote username;
- effective numeric UID;
- exact NSS home.

The type is frozen and slot-based. Its home field and all derived roots are
excluded from `repr`. Callers obtain it only from the verified probe parser;
the release composition does not deserialize it from JSON, YAML, SQLite, an
environment variable, or a renderer request.

It exposes only the internal operations needed by consumers:

- derive the exact workspace root;
- derive the exact Daemon bundle root;
- validate that an internal profile and follower belong to the same
  profile/generation/account;
- construct the closed account guard for an already-built trusted remote
  command.

This keeps account discovery, path validation, and suffix derivation in one
unit instead of reproducing them in lifecycle, profile, and transport code.

### 2. Private account discovery

`SystemOpenSshSession` gains one fixed-purpose private account-discovery
operation. It is not a generic renderer-callable command facility.

The remote probe uses the authenticated master and a closed `/bin/sh` program
to obtain:

- `id -un`;
- `id -u`;
- exactly one `getent passwd <effective-uid>` record;
- the home field from that exact NSS record;
- owner, writability, and physical-path checks for the home directory.

The probe requires the NSS username and UID to equal the two `id` results. It
emits one bounded versioned record containing only the username, decimal UID,
and home. Multiple passwd records, extra output fields, malformed encoding, a
nonzero result, timeout, or over-budget output is rejected.

Both stdout and stderr bypass the lifecycle output observer. The returned home
therefore cannot appear even briefly in SSH/Daemon operation logs before the
normal absolute-path sanitizer runs. The result is parsed under a fixed 8 KiB
aggregate output budget and converted directly into
`RemoteHomeAuthority`.

Discovery is repeated for every new OpenSSH connection generation, including
restart recovery and reconnect. No previous home is reused as authority.

### 3. Supported home contract

A supported remote home must satisfy all of the following:

- it is the exact home in the single NSS record for the effective UID;
- it is an absolute normalized path of at most 4,096 UTF-8 bytes;
- it has at least one component and has no empty, `.` or `..` component;
- every component uses the existing release-safe ASCII grammar
  `[A-Za-z0-9._@%+=,-]+`;
- physical resolution is byte-for-byte equal to the NSS path, so a symlinked
  component is rejected;
- the final directory exists, is owned by the effective UID, and is writable by
  the effective account.

The home itself may have an ordinary safe mode such as `0700` or `0755`.
OpenEvo-created `.openevo` service roots retain their stricter owner-only mode
requirements.

These restrictions remove the incorrect prefix assumption while preserving the
closed path grammar already used by Core asset and runtime staging. Paths with
spaces, control characters, shell metacharacters outside the grammar, traversal,
or symlink resolution are not part of the v0.1.10 supported matrix.

### 4. Lifecycle and transport wiring

`SystemOpenSshRemoteLifecycleV2` replaces its public-output `id -un` call with
the private discovery operation. It creates its internal
`RemoteProfileConfig` with:

- the literal alias as host;
- the verified authority username as user;
- an explicit `<home>/.openevo/workspaces` workspace root;
- the existing internal system-OpenSSH authentication marker.

This system-generated config is never serialized or persisted, and its
workspace-root representation is suppressed. The host path exists only long
enough to configure the generation-bound deployment transport.

The system transport factory receives the exact `RemoteHomeAuthority`, not a
second free-form user or home string. `SystemOpenSshFollowerTransportAuthority`
checks the authority against the owned session snapshot and carries it only in
process. `SshRemoteExecutorTransport` requires the profile user and explicit
workspace root to match that authority when the system-OpenSSH follower is
used. It derives the Daemon bundle root from the authority.

The conventional `RemoteProfileConfig` fallback remains only for historical
explicit maintainer inputs. An invariant test ensures the v2 release
system-OpenSSH composition never reaches that fallback. Likewise, the
username-based Daemon-root helper is removed from the system-OpenSSH path; it
cannot silently regain authority through a later refactor.

### 5. Remote-stage revalidation

Every rich deployment command issued through the system-OpenSSH follower's
`command_argv` path runs a silent closed guard before the trusted command. The
guard re-reads `id -un`, `id -u`, and the single NSS passwd record and requires
them to match the connection-generation authority. It repeats normalized
physical-home, owner, and writability checks. A mismatch stops the follower
without invoking the requested command.

The Core tunnel uses the existing non-shell `ssh -W` channel and therefore has
no home-derived command to guard. Initial Daemon transfer does not use `rsync`.
Later Core asset/runtime transfer consumers retain their independent
`pwd.getpwuid(euid)` and service-root validation; the legacy generic SSH upload
path is not part of the v0.1.10 ordinary-user composition.

Daemon bundle staging independently performs the same checks before creating
`<home>/.openevo/daemon-bundles`. The staging script then:

- requires the requested root to equal the verified home plus the fixed suffix;
- rejects an existing symlink or a physical path that differs from the lexical
  root;
- creates only owner-private OpenEvo directories;
- pins the resulting root device/inode and rechecks it around lock, stream,
  hash, and publication steps;
- preserves the existing exact-size, digest, link-count, mode, lock, and
  idempotent-reuse checks.

Core asset, Core runtime, managed-runtime, and Daemon service code continue to
resolve the effective account through `pwd.getpwuid(euid)` and to validate their
own service-root bindings. They do not accept the Desktop-discovered path as a
replacement authority.

`getent` becomes an explicit requirement of the Ubuntu Daemon-bundle host
profile. The transfer path still requires no remote Python, package manager, or
`rsync` for the initial Daemon bundle.

### 6. Privacy and logging boundary

The remote home is operational authority, but it is still an absolute host
path. It must not appear in:

- renderer requests or responses;
- `RemoteWorkspaceProfileV2`, lifecycle operation, event, or error payloads;
- provider/Core-bridge persistence;
- exception messages or object representations;
- SSH/Daemon lifecycle log entries;
- release evidence.

The discovery subprocess is completely unobserved. Later child output continues
through `LifecycleOutputSanitizerV2`, whose mandatory absolute-host-path
replacement is defense in depth. Command lines and environments remain outside
the logging path by construction.

Successful discovery advances the existing authoritative
`resolving_system_openssh`/`connecting` lifecycle checkpoints. It does not add
an estimated progress percentage. #220 remains authority for asynchronous
operation persistence, progress, log pagination, cancellation, and exact
ambiguous retry.

## Failure Semantics

Discovery and account-binding failures use the stable sanitized code
`ssh_remote_account_unavailable`. The Desktop projection states that it could
not verify a supported writable remote account home, sets `retryable=true`, and
uses the existing `administrator_action`. It never includes the username, UID,
home, failed command, NSS record, or raw stderr.

The following conditions fail closed before Daemon transfer or activation:

- missing or unavailable NSS lookup;
- zero or multiple matching records;
- `id` and NSS username/UID mismatch;
- relative, non-normalized, over-budget, or unsafe home;
- missing, non-directory, non-owned, non-writable, or symlink-resolved home;
- profile/session/connection-generation mismatch;
- NSS home or account drift during a later stage;
- requested workspace or Daemon root not equal to the authority-derived root.

An exact retry reconnects or resumes under the same #220 lifecycle-operation
identity as appropriate. A new connection generation always rediscovers the
account; it never treats a cached path as proof.

## Compatibility

- There is no renderer or HTTP schema addition and no manual migration prompt.
- Existing `/root` and `/home/<user>` hosts continue through the new discovery
  and guard path.
- A persisted v2 profile still contains only its literal alias and display
  metadata. Sidecar restart invalidates the process-owned session and discovers
  the home again during reconnect.
- Legacy explicit-profile maintainer automation retains its current
  conventional fallback in v0.1.10, but it is not callable by the ordinary-user
  renderer and is never a fallback from system OpenSSH.
- The source change requires a new source-bound wheel, framework lock, Daemon
  bundle, managed runtime composition, app, candidate manifest, and DMG. Older
  local candidate artifacts cannot serve as v0.1.10 release evidence.

## Test Strategy

Implementation follows test-driven development. Focused failures are written
and observed before production changes.

### Authority and parser tests

- accept `/root`, `/home/researcher`, and a multi-component custom home;
- reject malformed, duplicate, extra, non-UTF-8, and over-budget probe output;
- reject username/UID mismatches, traversal, unsafe characters, trailing or
  duplicate separators, symlink resolution, wrong owner, and non-writable home;
- prove the authority representation and exceptions do not contain the home.

### System-OpenSSH session and lifecycle tests

- prove account discovery stdout/stderr never reaches the configured output
  observer for both the production and injected runner paths;
- prove the lifecycle passes the literal alias only to OpenSSH;
- prove the transport factory receives one generation-bound authority and an
  explicit custom-home workspace root;
- prove reconnect rediscovers rather than reusing the previous authority;
- prove malformed discovery fails with sanitized connection state and no
  transport construction.

### Deployment transport tests

- prove custom, root, and conventional homes derive the exact Daemon root;
- prove profile/user/workspace/authority mismatch is rejected;
- prove the system follower guards home-derived commands while Core tunnel
  construction remains the existing non-command path;
- prove the stage script accepts a verified custom home and retains exact retry;
- prove NSS drift, unsafe root, symlink substitution, ownership mismatch, and
  inode replacement fail before publication;
- retain all existing digest, size, cancellation, predecessor, and receipt
  regression tests.

### Contract and log tests

- assert no v2 public model or persisted provider row gains a home/path field;
- inject the home into SSH and Daemon output and prove the sanitizer persists
  only `[REDACTED_HOST_PATH]`;
- assert the typed failure and lifecycle events contain no username, UID, home,
  command, or exception chain.

### Release and real E2E

1. Rebuild every source-bound candidate input from the final commit.
2. Run focused sidecar/deployment suites, full relevant Python regressions,
   renderer tests, Rust tests, candidate validation, packaged Playwright, and
   DMG mount/copy/LaunchServices smokes.
3. Install the exact candidate in `/Applications`.
4. Create a temporary Ubuntu SSH account whose real NSS home uses a custom
   non-`/home` prefix, with no privilege elevation by OpenEvo.
5. Connect using a literal local SSH alias, bootstrap the exact Daemon, create
   one project, and complete the real science-task/evolution flow.
6. Quit, relaunch, reconnect, and prove the account is rediscovered and the
   authoritative project resumes without duplicate mutations.
7. Retain only sanitized evidence, then remove the temporary alias, account,
   authorized credential copy, service, and home while proving the original
   server service remains untouched.

## Documentation Changes During Implementation

- update `docs/architecture/desktop-core-contract-v2.md` with the private
  account/home authority and no-path boundary;
- update `desktop/sidecar/README.md` with discovery, restart, and logging rules;
- update `docs/architecture/openevo-desktop-release.md` with the v0.1.10
  custom-home release composition;
- update `docs/maintainer/macos-desktop-development-handoff.md` with the
  reproduced defect, fix, verification commands, and remaining publication
  state;
- keep `docs/maintainer/productization/spec.md` unchanged unless implementation
  discovers a true product-contract ambiguity, because its writable-home
  requirement is already correct.

## Acceptance Criteria

The design is complete only when all of the following are true:

1. The v2 Desktop release path uses the SSH account's verified NSS home and has
   no username-prefix fallback.
2. Root, conventional, and arbitrary safe custom homes pass the same tests and
   runtime path.
3. Discovery and subsequent home-derived stages fail closed on account or path
   drift.
4. The home never crosses the renderer, persistence, error, log, or evidence
   boundary.
5. The exact rebuilt macOS candidate completes the real custom-home remote
   workspace and science-task flow without duplicate mutations.
6. All relevant local release gates pass and the final source/artifact identities
   are synchronized to `stable` before publication.
