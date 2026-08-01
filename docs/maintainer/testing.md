# Testing

The release Desktop process-boundary rehearsal is documented in
[`desktop-real-science-e2e.md`](desktop-real-science-e2e.md). Its structural
test is part of `tests/ci`; release evidence is valid only when the runner uses
the exact candidate macOS sidecar and askpass helper, one real System OpenSSH
alias, the real Daemon/Core v2 lifecycle, and two immutable science Tasks with
an adjacent successor Project Head chain against a remote host.
`--structural-check` is explicitly not E2E evidence.

Use focused tests for ordinary changes and broaden the suite when touching
shared contracts.

Version `0.1.9` is the current public unsigned, non-gating Preview. Its real mounted
and copied DMG, Tahoe-compatible packaged sidecar, native askpass helper,
System OpenSSH alias path, packaged renderer, self-contained Daemon Bundle,
managed runtime, startup diagnostics, and immutable asset roundtrip are current
Preview evidence. Checked-in, signed exact-candidate evidence also covers two
real remote Codex subscription Tasks, all three text-evolution targets,
adjacent successor Project Heads, next-session Runtime Context reuse, and live
Desktop v2 renderer observability. It is still not G2, G3, G12, or full
External Beta evidence. Earlier Preview releases remain historical evidence.

Version `0.1.10` is the active Preview release target. In addition to retaining
the `0.1.9` system-OpenSSH and two-Task path, it must prove that project creation
returns a durable operation reservation before the renderer's bounded Local API
deadline, continues for more than 15 seconds, survives an SSE reconnect and one
sidecar relaunch, and reconciles to exactly one Core project, one Desktop/Core
mapping, and one applied create mutation. The same shared operation presentation
must cover every implemented long-running authority: Desktop-owned profile,
host-key, Daemon, native-workspace, and project lifecycle work; native startup;
and Core-owned Tasks, successor transitions, services, diagnostics, and
maintenance operations. Lifecycle output may include actual sanitized SSH and
Daemon stdout/stderr. Commands, environment values, credentials, tokens, Core
endpoints, and absolute host paths remain forbidden.

The real-science runner treats a sidecar relaunch as a new Local API instance.
It must repeat strict release and authenticated-session negotiation, require all
candidate composition fields to remain identical, require the instance-bound
`build_id` to change, and pass only that current identity to the packaged
renderer. Retaining the pre-relaunch `build_id` is a closed acceptance failure,
not a reason to weaken renderer bootstrap equality.

## Focused Tests

Run the smallest meaningful test set first, then broaden when a change touches a
shared contract. Harness changes should cover harness fixtures; gateway/runtime
changes should cover gateway and trajectory tests; evolution backend changes
should cover datasets, jobs, artifacts, workers, context resolver, and runtime
injection. Do not claim a release gate is protected by a focused test unless the
test checks the exact contract named in the spec.

## Core Backend

```bash
ruff check src tests scripts
PYTHONPATH=src:. python -m pytest \
  tests/ci \
  tests/config \
  tests/platform \
  tests/test_evolution_agent_harnesses.py \
  tests/backend \
  tests/evolution \
  tests/gateway \
  tests/runtime \
  tests/trajectory \
  tests/rollout \
  tests/openevo/remote \
  tests/openevo/science \
  tests/openevo/sidecar \
  tests/openevo/desktop \
  tests/openevo/test_experiment_compiler.py \
  tests/openevo/test_experiment_models.py \
  tests/openevo/test_experiment_runner.py \
  tests/openevo/test_core_capabilities.py \
  -q
```

The Core CI regression step runs with `umask 077` so security-boundary tests
create provider, store, and session state with owner-private permissions. Its
test dependencies explicitly include `wheel`, which is required by regression
coverage that invokes `python -m build --no-isolation`.
Desktop contract regressions exercise both materialized and deferred FastAPI
included-router representations and require provider binding to preserve the
frozen endpoint signatures. This is a compatibility gate, not permission to pin
away a newer supported FastAPI release when nested routes would otherwise remain
contract-only. The macOS packaging, native smoke, and candidate jobs also run the
provider-store ancestor-alias regression before building: SQLite may canonicalize
`/var` as `/private/var`, but the opened and managed database must still be the
same verified device/inode. The same jobs perform a durable mutation through the
alias and recover a subprocess-crash rollback journal. This prevents Linux
`/dev/fd` behavior from masking Darwin's pathname-based journal semantics.

The dedicated macOS anonymous Core transport job clears the runner's ambient
`SSH_AUTH_SOCK` and runs the exact anonymous socketpair metadata, identity,
FD-transfer, cancellation, fail-closed, and real child-relay nodes. The child
relay node is not an end-to-end OpenSSH authentication gate. The full SSH
transport suite remains in the Linux Core job and also runs in the Desktop
macOS native smoke with a short pytest base directory, private umask, and no
ambient runner agent. The focused macOS job must retain the real relay node and
must not be reduced to metadata-only tests. The stable-only Desktop candidate
gate runs the complete macOS SSH suite under the same private, agent-free,
short-path environment before packaging a DMG. On GitHub macOS runners, the
complete suite creates that short root directly below `$HOME` with mode
`0700`; it must not place SSH authority fixtures below the pre-existing
`$RUNNER_TEMP` ancestry. Injected Core-child fixtures retain the transferred
socket peer only for the fake child's lifetime and close it as soon as simulated
exit is established. Short-leader fixtures deterministically let the leader
become a zombie before observer installation, proving that Darwin's kqueue plus
non-reaping `ps` snapshot closes the registration gap. These rules model the
production ownership lifecycle without weakening its fail-closed checks.

The v0.1.10 system-OpenSSH gate is
`scripts/ci/run_desktop_system_ssh_integration.py --require-complete`. It is a
required macOS candidate step and fails when the Apple `sshd`, SSH tools, C
compiler, loopback fixture, askpass broker, or any asserted workflow is
unavailable. The gate creates an owner-private fixture beneath the current
account's canonical home and runs the production alias-only master, command,
control, and `-W` tunnel builders through exact `/usr/bin/ssh`.
It streams fixture bytes through the owned SSH command's stdin and does not
require local or remote `rsync`. It covers a controlled agent, `IdentityFile`,
encrypted-key
askpass, a real password prompt (successful authentication only when the host
account supports the generated fixture value), ProxyJump, ProxyCommand,
first-host accept/cancel, strict first-use refusal, changed and repeatedly
changed keys, master reuse, ambient-agent/master isolation, cancellation, and
complete process/socket cleanup. The emitted evidence is a closed boolean
summary and never contains fixture paths or response values.

Focused sidecar fault tests additionally force the owned master and askpass
worker to outlive their first cleanup deadline. They require the session owner
and rich transport to retain the same authority for a later action, require a
fresh disconnect action to finish without repeating completed cleanup phases,
and require restart recovery of failed or running disconnect work to persist
`ssh_cleanup_authority_lost` instead of `disconnected`.

The hermetic local-`sshd` fixture supplies its generated SSH config with a
test-only `-F` adapter after separately asserting that the production plans are
the unchanged alias-only builders. Product code never uses that adapter: the
installed app still executes the selected literal alias against the user's
normal OpenSSH configuration. Run the gate locally on the supported Mac with:

```bash
unset SSH_AUTH_SOCK
uv run python scripts/ci/run_desktop_system_ssh_integration.py --require-complete
```

Native workspace tests reject sparse allocation through deterministic metadata
and extent-map contract tests. Filesystem integration fixtures skip only when
the host filesystem physically allocates the complete logical file; they must
not reinterpret a fully allocated file as sparse.

Run relevant module suites when touching shared behavior:

- Packaged launcher diagnostics must keep the producer, smoke allowlist, and
  launcher phase set identical. Cover both primary failures with best-effort
  cleanup and otherwise-normal exits that fail closed as `shutdown_failed`.
  The launcher factory test must also prove that packaged ownership disables
  the app-level ASGI shutdown close hook.
- A real subprocess/Uvicorn regression must send `SIGTERM` to the packaged
  launcher path and prove provider cleanup completes before a zero exit.
- Factory failure tests must inject a simultaneous cleanup failure and prove the
  app/Core-runtime factory preserves the original construction exception while
  attempting each acquired resource close.
- Runtime close tests must inject independent bridge, broker, and store failures,
  assert every cleanup runs, and assert the first failure is propagated.
  A concurrent-close test must hold bridge shutdown open and prove another
  caller cannot close broker/store or return before relay join completes.
- Provider close tests must prove later runtime/lifecycle/store failures neither
  replace the first failure nor prevent remaining owned-resource cleanup.

```bash
PYTHONPATH=src:. python -m pytest tests/backend tests/evolution tests/gateway tests/trajectory tests/rollout -q
```

`tests/runtime` is part of the deterministic Core PR gate. Its Docker CLI unit
tests use controlled fakes; the real-daemon ownership probes remain a separate
candidate gate so a contributor machine or ordinary PR runner without Docker
does not fail merely because the daemon/image is unavailable.

For a release candidate, manually dispatch **OpenEvo Core Backend checks** with
`require_real_docker=true`. The `real-docker-runtime-probes` job first requires
`docker info` and pulls `python:3.12-slim-bookworm`, then runs with
`OPENEVO_REQUIRE_REAL_DOCKER=1`. Missing Docker, a missing image, or either
name-collision/cancel ownership probe is a hard failure rather than a pytest
skip. Record the job URL against the exact candidate commit.

The focused productization regression boundary is indexed in
`docs/architecture/protected-behavior.md`. It protects observable method and
cross-stage behavior without a source-hash manifest; it is not a substitute for
the final benchmark performance gates.

## Preview Publication Tests

A new Preview packaging draft may be published only after the exact mounted DMG and
detached copied app both launch their real Tauri executable, sidecar, and
renderer; the embedded self-contained Daemon Bundle and managed runtime
identities verify; checksums match; and every declared asset passes a
clean-directory download roundtrip. A separate Subscription rehearsal is valid
only when its canonical evidence records the exact Preview bytes, remote Docker
execution, and preinstalled, signed-in Codex identity used on the host.

For `0.1.10`, the packaged-sidecar smoke also starts from an exact retained
provider schema-v2 fixture, requires packaged migration to schema v3 without
losing the profile, relaunches with a recoverable lifecycle operation and
sanitized process-log entry, and proves exact replay does not create a second
operation. The signed real-science rehearsal—not this injected smoke—must supply
the real SSH/Daemon output, sub-15-second reservation, longer-than-15-second
terminal duration, SSE reconnect, sidecar relaunch, renderer-visible phase/log
proof, generated secret-canary absence, and exactly-one remote mutation
evidence.

Preview evidence must record the exact source, tag, assets, checksums, and
known missing gates. It must identify unsigned/not-notarized status and the
tested quarantine-removal path. It must not be counted as clean-user G2,
clean-host/mediated-auth/proxy-matrix G3, G12, protected benchmark, or full
External Beta evidence. An unchanged validated draft may be published as a
Preview, but it cannot be edited or reused as the G1-G12 candidate.

## Release Candidate Gate Tests

Release gate tests are stricter than local smoke tests. Their output must identify
the candidate commit, exact inputs, configuration, result, and produced release
artifact where applicable. Use the smallest durable report that proves the
behavior; do not create a schema/validator/report stack for every check.
Desktop and release workflows pin `setup-uv` to `uv 0.11.29`; they must not
resolve a moving `latest` release through the GitHub API during a candidate run.
Upgrade that pin explicitly and keep the workflow contract test in sync.

Packaged-sidecar startup failures are the exception to ordinary process-log
capture: the smoke directs combined child output to a bounded OS pipe, reads at
most 32 KiB for parsing plus one unrendered truncation sentinel byte, and
publishes only closed `OPENEVO_STARTUP_V1` records. It does not create a
disk-backed process log. Tests must include path, token, URL,
traceback, malformed-record, and oversized-output canaries and prove that none
can enter the rendered CI failure. A silent bootloader exit is a failed gate,
not permission to print arbitrary child output or fall back from descriptor
authority to a pathname.
Tests must keep the packaged Python producer and smoke allowlists exactly equal,
and must prove that a typed release-composition failure preserves only its fixed
phase code while redacting the original exception and chained cause.
The macOS packaged-sidecar gate must exercise the Darwin-native FD checks: the
bootloader must prove FD 3 through `proc_pidfdinfo` plus its loopback endpoint,
and the SSH gate must construct an agent source through kqueue directory
monitoring, `getpeereid`, `LOCAL_PEERPID`, and `LOCAL_PEERTOKEN`. Passing only the
Linux `SO_ACCEPTCONN`/inotify branches is not macOS release evidence.
The smoke must also retain the unreaped leader while checking readiness and
prove full process-group cleanup. Tests force process-table observation failure
and require the anchored whole-group `SIGKILL` fallback to remove a descendant
that retains the output pipe while the candidate gate still fails closed.

Required release-gate families are:

- protected algorithm behavior, benchmark boundaries, and source identity;
- Core install artifact, backend API, remote bootstrap, and runtime injection;
- Desktop unit, web build, Tauri Rust, DMG build/smoke, mounted E2E, visual and
  accessibility, upgrade/rollback, diagnostics, and privacy;
- docs/security, supply chain, release workflow inventory, release asset
  validation, and final candidate consistency.

## Benchmark Performance Gate

The benchmark performance gate protects only the already validated non-parametric
families for External Beta: textual memory, trajectory-to-skill, and
agent-system evolution. The gate uses pass@1 rescue counts on the frozen
baseline-failed Terminal Bench subsets and must rerun with the final Core
artifact for the release candidate. Benchmark automation remains outside Core and Desktop; it
may call Core APIs but must not ship as an ordinary-user product surface.

## Desktop

```bash
source .venv/bin/activate
cd desktop
npm ci
npm audit --audit-level=high
npm test -- --run
npm run typecheck
npm run build:openevo
python packaging/build_sidecar.py --core-wheel-output-dir ../.openevo-release-inputs
# The output directory is a closed pair: one openevo-*.whl plus framework-lock.json.
cd src-tauri
# Ubuntu/Linux CI runners need Tauri native packages such as
# libwebkit2gtk-4.1-dev, libayatana-appindicator3-dev, libgtk-3-dev,
# libxdo-dev, librsvg2-dev, and patchelf.
cargo metadata --locked --format-version 1
cargo test --locked
```

## Desktop Smoke

Desktop smoke must exercise the packaged app, not only the Vite web surface.
Preview publication uses the narrower packaging and real Subscription evidence
defined above. The minimum External Beta candidate smoke covers DMG creation,
checksum verification, mounted app launch, local state creation, remote
profile setup, remote bootstrap, Codex Subscription transcript mode,
Self-Deployed Reference mode, run monitor, artifact inspection, diagnostics
export, deletion/cleanup, and upgrade/rollback state migration. Fake transports
may be used in CI only when the evidence is clearly marked as a non-release
substitute; the release candidate needs real canary evidence for the supported
release modes.

The packaging-level native smoke is
`scripts/ci/smoke_openevo_desktop_bundle.py`. It reads
`CFBundleExecutable` from `Info.plist` and launches that exact
`Contents/MacOS` process; directly executing the bundled sidecar is not app
evidence. The release candidate requires this executable identity to be
`openevo-desktop`, matching the effective Tauri main binary. The product/display
name is `OpenEvo Desktop`, while the app bundle directory is
`OpenEvo Desktop.app`; a source preflight test prevents those identities from
drifting. On macOS it requires the credential-free V2 native process marker,
the renderer-ready marker from the committed product shell, a non-empty `main`
WebView, the packaged sidecar's
IPv4 loopback listener on inherited FD 3, and a regular executable FD 4 whose
device, inode, and size match the native marker whose verified digest binds it
to the bundled externalBin. The marker PID's Darwin birth identity is re-read
with `proc_pidinfo(PROC_PIDTBSDINFO)` before FD observation.
The private launch pathname remains within the documented same-UID trust
boundary until native cleanup. The smoke does not reopen the pathname reported
by `lsof`; it trusts only the marker-bound live FD identity. Its macOS probes
share the 120-second app-smoke readiness deadline and run in a private process
group within the caller's macOS login session. Do not use `setsid` for this app
launch: on macOS Tahoe that synthetic non-login-session launch can prevent
WKWebView from delivering a completed Tauri invoke reply even though the native
command and sidecar startup both completed. The private process group preserves
bounded cleanup authority without changing the user-visible launch session.
The native host retains a separate 60-second bounded allowance for a cold
PyInstaller onefile startup; the larger outer deadline also covers renderer and
FD observation. Cleanup subsequently keeps its separate 5-second
termination and 15-second process-group disappearance bounds. Timeout cleanup uses
a bounded ancestry snapshot to kill an observed child that escaped with
`setsid`, then kills the root group and reaps the direct leader. Observation
also caps each sidecar group. It requires bounded
disappearance of every captured app/sidecar process group after main-app
termination on success or failure. The smoke signals the app group only while
its unreaped child reserves the leader PID; already reaped app groups and all
historical sidecar group numbers are observation-only. Candidate runs retain
separate `app-bundle-smoke.json` and `dmg-copy-smoke.json` outputs. The former
declares `launch_origin=mounted_dmg`;
the latter declares `launch_origin=detached_copy` after `ditto` and a successful
DMG detach. Both schema-v3 reports bind the exact source DMG, Tauri executable,
and packaged sidecar SHA256 values, and candidate validation requires the
reports to agree. The dependent Linux candidate job must exercise the installed
Core service at the OS-account-derived canonical `~/.openevo/core` root. It
must not pass a run-scoped `$RUNNER_TEMP` service root, because the product
contract intentionally rejects alternate per-project or per-run daemons. The
lifecycle step must generate its bearer-bearing attachment as
`bootstrap-<32 lowercase hex>.json` with the exact installed candidate
interpreter; GitHub run IDs and attempts are public and do not satisfy that
attachment contract. Failure cleanup best-effort consumes and unlinks any
published attachment before stopping the service, so an interrupted smoke does
not leave the bearer-bearing file behind. The preceding release-mode Clippy
gate compiles all Tauri targets with warnings denied, so test-only platform
helpers cannot leak into the shipped host binary as dead code. The macOS PR and
candidate gates run the complete release Rust suite with one test thread
because its native process-lifecycle cases mutate process-global environment
and load the shared OS process table. Security-path fixtures canonicalize the
macOS system temporary-directory alias before exercising no-follow path
traversal; the production traversal remains strict. The bundle smoke also rejects symbolic
links in the app, `Info.plist`, Tauri executable, sidecar, or their in-bundle
ancestors and revalidates binary identity and digest after cleanup. Candidate
JSON fixtures cover duplicate keys at both top-level and nested locations.
Native stderr is drained through a nonblocking bounded pipe and marker parsing
is byte/line bounded. Before reporting a byte or line overflow, the parser
registers every complete valid process marker still inside those budgets so
failure cleanup cannot lose an already observed sidecar group. StrictMode lifecycle
replacement selects the latest process marker while retaining all observed
process groups for disappearance checks. Only the app group backed by an
unreaped child authority may be signalled; reaped app groups and historical
sidecar PGIDs are never signalled. A failure reports the deepest stage reached
and an observed-stage set drawn only from this closed vocabulary:
`native_marker_absent`, `native_process_unavailable`,
`listener_fd_unavailable`, `executable_fd_unavailable`,
or `renderer_ack_absent`. Probe deadline exhaustion and transient shallower
probe failures cannot replace the deepest product readiness stage reached;
timeout diagnostics also list only the closed stages observed during that
launch. Renderer diagnostics are also restricted to the closed
`OPENEVO_DESKTOP_RENDERER_STAGE_V1` vocabulary. Its renderer-owned subset is
the fixed `bootstrap_context_{validated,failed}`,
`local_api_version_{verified,failed}`, `retry_recovery_{ready,failed}`,
`provider_adapter_{ready,failed}`, `provider_{created,create_failed}`,
`initial_snapshot_failed`, and `product_committed` stages; tests prove the
native command rejects native-owned and unknown values. No error text or
runtime value is permitted in this channel. These stages never count as success
evidence. The V2 renderer marker is
host-bound to a non-empty `main` WebView after the authoritative product
snapshot commits; unit coverage rejects a zero-sized or non-main invoking
window, while logical visibility remains diagnostic on direct-binary runners.
A Local API regression separately verifies exact Tauri origins, methods,
standard safelisted plus renderer-owned request headers, including WebKit's
`Cache-Control` and `Pragma` preflight generated by `cache: "no-store"`, preflight-before-session
ordering, and CORS coverage outside the server-error boundary.
A source-config regression requires `bundle.macOS.infoPlist` to name the
checked-in plist and rejects broad ATS keys. The exact mounted-DMG smoke repeats
that check against the final merged app bytes before launch, requiring only
the `127.0.0.1` exception and rejecting local-network or broad allowances.
A macOS-only native-host regression creates retry-recovery state through the
lexical `/var` spelling of a directory under `/private/var`, proving that the
fixed Darwin alias is normalized before no-follow traversal. The surrounding
recovery tests continue to reject arbitrary symlink roots, non-private roots,
hard-linked files, stale compare-and-swap authority, oversized records, and
cross-process races.
A macOS-only contract test
calls `proc_pidinfo` for the live test process and compares `lsof -FDiT` output
against a real regular file and loopback listener before packaging. Both macOS
gates additionally repeat the
blocked pre-exec cancellation test's internal scenario twenty times so serial
suite scheduling does not hide the watchdog, handoff, and bounded-cleanup
transition. The suite also covers Darwin's `EPERM` response when a retained
process group has no signalable member, requiring a separate empty-group proof
before reap. It separately rejects `EPERM` from leader inspection, a live leader,
a reported descendant, and failed group inspection. PyInstaller environment
sanitization runs through the platform's real release execution path: inherited
FD execution on Linux and the identity-bound private named path on macOS.

`scripts/ci/openevo_release_candidate.py` creates and validates the closed
candidate inventory. Validation includes the source commit, actual runner
architecture, final Core wheel/framework lock/registry identity, self-contained
Daemon Bundle and manifest, managed runtime identity, DMG,
`core-install-artifact.json`, canonical `SHA256SUMS`, release notes, both native
smokes, and dependency/license/security evidence. The Linux job and the
redownloaded draft run the same validator before using any Core or Daemon bytes.
A valid packaging manifest is not evidence for the still-separate science,
benchmark, privacy, ad-hoc-signature, or quarantine-policy gates.

Release notes are generated by one release-tool-owned renderer, and validation
requires an exact byte-for-byte match; extra sections or contradictory claims fail. The
canonical packaging-only draft explicitly reports zero
of three benchmark gates complete and all textual-memory,
trajectory-to-skill, and agent-system rescue counts as pending; it cannot imply
that packaging smoke is algorithm-performance evidence. The same notes must
report Self-Deployed Reference mode and credential-canary verification as
unavailable or pending, state that the app is unsigned and not notarized, and
document the exact tested quarantine-removal path without claiming a signing
gate. This checksum-bound packaging draft may be published unchanged only as a
Preview; a future External Beta path must create and revalidate a new candidate
inventory.
It also states that uninstalling the application retains current local data
under `~/Library/Application Support/org.openevo.desktop` (including run-retry
recovery state), preserved legacy Preview data under `~/.openevo/desktop`, and
remote Core state, task data, models, and caches.

After asset redownload, the candidate workflow queries the GitHub draft and
validates its exact body, title, tag, target commit, draft flag, and prerelease
flag. Its HTTPS review URL must belong to the expected repository; GitHub uses
an opaque `untagged-*` URL slug for drafts, so the separately validated
`tagName` remains the candidate tag-name authority. The repository-bound API
URL must contain the immutable numeric release ID used for cleanup, preventing a
same-tag replacement between validation and deletion from being deleted. The ID
authority file must be created once with mode `0600` and must never be replaced.
The body must contain the canonical notes followed only by the 128-bit random
ownership marker generated for that workflow attempt. It stores that discrete,
point-in-time record as a run-attempt-qualified immutable Actions artifact; this
does not make an atomic claim about workflow completion. The
GitHub draft itself remains administratively mutable, so any post-run edit
invalidates it. Tests require exact Git-ref validation, pre-creation release/tag
absence, ownership-bound cleanup, and real-tag absence after both success and
failure. Release absence comes from the authenticated paginated inventory,
because GitHub's single-release-by-tag REST endpoint returns `404` for private
drafts. Tests also require cleanup to delete only the immutable ID emitted by
metadata validation. Cleanup never deletes a Git tag.

Remote capability discovery has an additional artifact-level gate. In a clean
environment containing the exact Core wheel, run
`scripts/ci/smoke_openevo_remote_capabilities.py --wheel <exact-core-wheel>
--framework-lock <build-generated-framework-lock> --sidecar <packaged-sidecar>`.
The lock must be the exact sibling artifact emitted by the sidecar build, not a
runtime-generated substitute. The smoke must start Core with that external lock
through the installed Core supervisor API, exercise the packaged sidecar through its native
inherited-listener/credential-frame launch contract, and use a full 40-character
`--source-commit` matching the release checkout. This gate runs only on its
dedicated GitHub-hosted Linux worker because the supervisor derives its required
canonical host-global root from the OS account database, not from `HOME` or a
caller-selected path.
The smoke stops a process only through `stop_core_service_if_generation`, which
rechecks the attachment generation and release identity under the supervisor
locks before signalling or removing publication state. A failed ensure, an
attachment to an existing process, or a replaced generation has no cleanup
authority. It
validates the two packaged process boundaries;
the separate remote-profile/SSH/active-project gate validates their production
forwarding composition. A source `TestClient` or local fake capability catalog
does not satisfy either gate.

The pull-request release workflow splits this coverage across operating systems
without splitting artifact identity. `macos-packaging-smoke` is the only job
that builds the exact Core wheel and `framework-lock.json`; it writes and
verifies `SHA256SUMS`, exports the manifest digest, and uploads those three files
under an artifact name qualified by source commit, workflow run, and run
attempt. `linux-core-smoke` has an
explicit dependency on that job, downloads the same artifact, verifies the
manifest digest and both payload digests before installation, then owns the
actual Core service ensure/attachment/capability/stop smoke. It never rebuilds
the release inputs or constructs a later outer wheel. Because a macOS Mach-O
sidecar cannot execute on the Linux Core runner, Linux rebuilds a packaged Linux
sidecar from the same checkout solely as the executable native-process fixture;
it is not candidate artifact evidence and does not replace the macOS packaged or
app-bundle smokes. The Linux remote smoke combines that fixture with the exact
transferred wheel and lock. The macOS job owns candidate sidecar packaging and
exact-pair publication on its GitHub-hosted ephemeral runner; it does not call
the Linux-only Core service lifecycle. Installed framework verification follows
the same boundary: macOS explicitly runs `smoke_evolution_framework_wheel.py
--mode installed-registry`, while Linux runs the strict superset
`--mode linux-context-projection` against the transferred bytes. The latter
must report both its mode and a passed context-projection result before its
registry digest is accepted as candidate evidence; no platform-driven implicit
skip is allowed. Tests behaviorally prove that only the Linux mode invokes the
context-projection helper. Hostile Python site bootstrap and concurrent
same-owner install replacement are a separate final-publication hardening gate,
tracked in GitHub issue #193 rather than claimed by this packaging-only
candidate.

The sidecar process smoke loads `desktop/release-contract.json`, validates the
closed `VersionV1` and `DesktopStateV1` models, requires the frozen OpenAPI digest
and every renderer-required feature flag, and checks that state negotiation binds
the same digest. The renderer imports that JSON directly; anti-drift tests bind
the Rust native-host digest to it.

The exact Core wheel/lock export test contract is intentionally small. The
requested output must not exist, and existing directories or symlinks are
rejected without mutation. Tests verify that the builder opens stable private
regular-file inputs, validates the canonical lock against the wheel filename and
SHA-256, copies both files into a private random sibling directory, fsyncs and
revalidates the exact two-member inventory, and publishes the directory with an
atomic no-replace rename. An injected publish failure leaves the requested path
absent and preserves only a clearly non-authoritative random staging directory.
A successful publish contains exactly the verified wheel and lock; a second
publish fails without changing them.

Sidecar target publication has a separate fault-injection contract. Tests prove
that a verified staging file atomically replaces an existing externalBin with
mode `0755`, and that a replacement failure preserves the old target byte for
byte while leaving the new file only under a non-authoritative staging name.

The workflow guard requires a GitHub-hosted ephemeral runner. The build UID and
its processes are part of the release-build trust boundary; this publisher does
not claim protection from malicious same-UID code or provide restart recovery
for a persistent shared workspace. The Actions artifact manifest, candidate
manifest, Linux clean install, embedded-archive checks, and final draft-asset
roundtrip remain the durable cross-job identity evidence. Tests also retain the
reproducible wheel build, authoritative `FrameworkDistributionLock` loader,
raw PyInstaller TOC multiplicity, embedded byte equality, output-overlap, and
failure-before-artifact gates.

## Release Identity

```bash
python scripts/ci/audit_openevo_identity.py
python scripts/ci/check_openevo_release.py --wheel dist/*.whl
python scripts/ci/write_sha256.py dist/*.whl
git diff --check
```
