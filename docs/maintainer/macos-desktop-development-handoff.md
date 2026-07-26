# macOS Desktop Development Handoff

Status: active maintainer handoff for the `0.1.8` startup incident and the next
Desktop patch release

Last updated: 2026-07-26

This document transfers Desktop development from the Linux/GPU development
container to a real Apple Silicon Mac. It is an implementation handoff, not a
second product specification. Product scope and release acceptance remain
authoritative in `docs/maintainer/productization/spec.md`.

## 1. Immediate Objective

Produce the next immutable patch release, necessarily `0.1.9` or later, that:

1. starts from a downloaded unsigned DMG on the target Mac;
2. reaches the packaged Desktop Local API without a startup error banner;
3. makes **Add remote workspace** open and complete the remote setup flow;
4. installs, connects to, and controls the matching OpenEvo Daemon without the
   user opening a server shell;
5. runs a real Codex Subscription science task on the remote server;
6. displays task progress, transcript-derived execution data, and the selected
   evolution targets and artifacts;
7. preserves the two built-in read-only science examples; and
8. exports diagnostics that identify the failed component and stage without
   exposing credentials, user data, or host paths.

Local Codex execution on the Mac is deferred to `0.2.0`. Do not mix that feature
into this startup repair.

Do not change the implementation or behavior of any evolution algorithm while
repairing Desktop packaging, startup, diagnostics, or remote control.

## 2. Product Topology During Development

Use both machines instead of trying to make either one cover the complete
product:

```text
Apple Silicon Mac
|- OpenEvo repository and Desktop development toolchain
|- React/Vite renderer
|- Tauri native host
|- packaged Desktop sidecar
`- installed-DMG and LaunchServices acceptance
              |
              | SSH and private Core tunnel
              v
Linux GPU server or user container
|- OpenEvo Daemon
|- managed science runtime
|- existing Codex installation and subscription authentication
`- real task, transcript capture, evolution, and artifact reuse
```

The Linux server remains headless. Browser or GUI testing on the Linux server is
not product evidence. All ordinary-user actions must originate in Desktop.

Never commit an SSH hostname, username, private key, Codex authentication file,
backend token, or release-host credential. Keep those values in the local
machine configuration or process environment.

### 2.1 Use the existing GPU server through `evolab`

The target Mac already has an SSH configuration entry for the current Linux
user container. A maintainer can reach it with:

```bash
ssh evolab
```

Use that command for developer-only diagnostics and for confirming that the Mac
can reach the test host. It is not an instruction for ordinary users to operate
the Daemon manually.

The 0.1.8 Desktop transport deliberately invokes OpenSSH with `-F /dev/null`
and explicit trust/authentication options, so it cannot load `Host evolab` from
the user's configuration. That historical behavior is not an acceptance
workaround. The 0.1.9 path must present the configured-host catalog and let the
user select the `evolab` alias directly.

For developer-only diagnosis, inspect the alias without printing identity-file
contents:

```bash
ssh -G evolab | awk '
  $1 == "hostname" || $1 == "user" || $1 == "port" ||
  $1 == "proxyjump" || $1 == "proxycommand" { print }
'
ssh-add -l
```

Create the Desktop workspace with a local display name and the `evolab` catalog
entry. Do not transcribe the resolved hostname, user, port, identity path,
ProxyJump, or ProxyCommand into Desktop. The real `/usr/bin/ssh evolab`
invocation remains authoritative for all of those values and for the user's
agent/Keychain and known-host policy.

Desktop mediates any first-host, encrypted-key passphrase, or password prompt
through the sealed native askpass surface. It does not copy a key, password, or
passphrase into renderer, Local API, argv, logs, diagnostics, or OpenEvo state.
A changed key remains a separate blocking review and must not be approved merely
because `ssh evolab` previously worked.

As of this handoff, the remote container has the required first-release profile:

- Linux `x86_64` in a Docker user container;
- an SSH account that may resolve to UID 0 inside that existing user container, as
  permitted by the canonical `docker_user_container_v1` boundary; this does not
  permit bare-host root SSH or additional privilege elevation;
- a working Docker CLI and mounted Docker Engine socket;
- Codex installed and an existing subscription authentication file under the
  remote account;
- a writable remote home with sufficient free space for the current smoke; and
- retained `$HOME/.openevo` releases, state, and artifacts from earlier tests.

Treat those as facts to re-check, not permanent assumptions. Safe maintainer
preflight from the Mac is:

```bash
ssh evolab '
  set -eu
  uname -srm
  command -v codex >/dev/null
  test -f "$HOME/.codex/auth.json"
  command -v docker >/dev/null
  test -S /var/run/docker.sock
  docker version --format "client={{.Client.Version}} server={{.Server.Version}}"
  df -Pk "$HOME" | tail -1
'
```

Never print or copy `~/.codex/auth.json`. Use `gpt-5.3-codex-spark` for the real
subscription acceptance run because it is the product-owner-selected fast,
separately metered smoke profile and is already used by the `0.1.8` release E2E
record. Keep `high` reasoning effort.

The existing checkout in the remote container is a development workspace, not
a Daemon installation input. Desktop must transfer and activate its own exact
release-matched Daemon Bundle. Do not point Desktop at the remote Git checkout,
start the Daemon from that checkout, or pre-clean `$HOME/.openevo`; retained
state is valuable upgrade and reconnect coverage.

For the end-to-end acceptance:

1. start from the installed Mac app and add the configured `evolab` workspace;
2. save, connect, verify the host key, and let Desktop install or upgrade the
   Daemon;
3. create a Subscription project and independently select the evolution targets
   under test;
4. run the first science task and inspect its timeline, transcript summary, and
   produced artifacts;
5. wait for the successor project head to become ready;
6. run a second task and verify that accepted first-task context is injected;
7. quit and reopen Desktop, reconnect, and verify authoritative remote state;
   and
8. use `ssh evolab` only if the Desktop diagnostics leave a server-side problem
   unresolved.

## 3. Repository And Toolchain Setup On The Mac

First verify the local development prerequisites:

```bash
xcode-select -p
command -v git
command -v uv
command -v node
command -v npm
command -v rustup
git --version
uv --version
node --version
rustup --version
```

If a command is missing, install it with the Mac owner's approved package or
toolchain manager before continuing. Do not run an unreviewed remote installer
as part of a release build. The exact versions required for candidate parity are
listed below.

Clone the public repository over HTTPS, or use SSH only if GitHub SSH access is
already configured, and work from `stable`:

```bash
git clone https://github.com/CompLifeLab-ZJU/OpenEvo.git
# Equivalent when GitHub SSH access is already configured:
# git clone git@github.com:CompLifeLab-ZJU/OpenEvo.git
cd OpenEvo
git checkout stable
git pull --ff-only origin stable

git config user.name ivowang
git config user.email ziyiwang@ieee.org
```

If the clone names the GitHub remote `origin`, keep that name. Do not rename it
only to match commands copied from the previous server.

Read `AGENTS.md` before editing. Then read these boundaries:

- `docs/maintainer/productization/spec.md`
- `docs/architecture/desktop-core-contract-v1.md`
- `docs/architecture/openevo-desktop-release.md`
- `desktop/sidecar/README.md`
- `docs/maintainer/release-process.md`
- `docs/maintainer/testing.md`

Match the candidate workflow toolchain before producing release evidence:

| Tool | Candidate version |
| --- | --- |
| Python | `3.11.9` on Apple Silicon macOS |
| uv | `0.11.29` |
| Node.js | `22.23.1` |
| Rust | `1.95.0` with `rustfmt` and `clippy` |
| Xcode | Current installation compatible with the target macOS SDK |

Initial dependency setup:

```bash
uv python install 3.11.9
uv sync --frozen --group dev --python 3.11.9
export PATH="$PWD/.venv/bin:$PATH"

cd desktop
npm ci
cd ..

rustup toolchain install 1.95.0 --component rustfmt --component clippy
rustup override set 1.95.0
```

Run the `PATH` export again from the repository root in each new development
shell so Tauri's debug launcher resolves the locked virtual-environment Python.
Do not update lockfiles merely because a newer local tool is available. Any
intentional dependency or toolchain update needs its own reviewed change and
release evidence.

## 4. Current Public Release

The current public artifact is the immutable unsigned `0.1.8` Preview:

- source commit: `dde71c6a940d7e17bbfdb7c41ae7f7ee098618b9`
- candidate run: `29947490201`
- publication run: `29949667800`
- tag: `openevo-desktop-v0.1.8-v018-startup-logs.29947490201.1`
- DMG: `OpenEvo-Desktop-0.1.8-aarch64.dmg`
- DMG SHA-256:
  `8aa5b4c6b06f4883b51fb003f97eccdf28913fff81312641bc2f5818b4689e34`

Do not modify, replace, or relabel that release. It is historical evidence, not
a working-release claim for the Mac described below.

The candidate passed the GitHub-hosted macOS 14 mounted-DMG and copied-app
smokes. It also has separate two-session remote Codex evidence. Those facts do
not reproduce the real user's Desktop startup environment and therefore do not
prove that `0.1.8` is usable on macOS Tahoe.

## 5. Reproduced User Failure

Target machine:

- Apple M3 Pro;
- macOS Tahoe `26.5.2`;
- app copied to `/Applications`;
- the same user previously ran older OpenEvo Desktop Preview releases.

Visible error:

```text
OpenEvo Desktop could not start
The OpenEvo Desktop sidecar exited before it became ready. Sidecar exit: code=255.
```

The `0.1.8` exported log establishes this sequence:

```text
application_started
sidecar_stop_requested
sidecar_stop_succeeded
sidecar_start_requested
sidecar_unstructured_output_discarded / bounded_output_discarded
sidecar_exited_before_ready / exit_code=255
sidecar_pre_python_exit / sidecar_exited_during_startup / exit_code=255
sidecar_start_failed
renderer bootstrap and provider creation fail
```

The evidence supports these conclusions:

- Tauri starts and can spawn the sidecar process.
- The process exits before the native scanner receives a valid allowlisted
  `OPENEVO_STARTUP_V1` marker.
- At least one non-empty unstructured stderr line was observed, but the `0.1.8`
  scanner retained neither its content, count, nor provenance.
- The event name `sidecar_pre_python_exit` is a heuristic used when no marker was
  retained; it is not proof that the embedded Python interpreter never started.
  A Python bootstrap/import failure before the guarded entrypoint remains
  possible.
- There is no evidence that provider SQLite migration, remote SSH, Daemon
  bootstrap, or Codex execution started. Deprioritize those layers until the
  missing stderr line or a more precise startup stage is recovered, but do not
  claim they are formally excluded by the current log.
- Older Desktop state is a lower-probability explanation because no
  provider-store marker was retained, but the current evidence does not exclude
  it. Acceptance must cover both retained Preview state and a clean
  application-data state.

Do not describe this as a user-specific setup problem. Multiple users have seen
the same startup failure, and the product must diagnose and handle it.

### 5.1 Confirmed macOS Tahoe root cause

The retained `0.1.8` installation has now been reproduced on the target macOS
Tahoe `26.5.2` host. Both the Tauri app and bundled PyInstaller sidecar are
ad-hoc signed with the hardened-runtime CodeDirectory flag and have no Team
identifier. PyInstaller extracts a separately signed Python framework before
the guarded Python entrypoint. Tahoe's library validation rejects that framework
because the unsigned outer executable and nested library have no shared Team
identity, so the sidecar exits `255` before it can emit a recognized startup
marker. This explains why renderer, provider SQLite, SSH, Daemon bootstrap, and
Codex never start in the reported incident.

A controlled copy of the exact `0.1.8` sidecar was re-signed ad hoc without the
hardened-runtime flag and passed the FD-handoff packaged-sidecar smoke. That is
diagnostic evidence, not permission to mutate the installed `0.1.8` app.

The `0.1.9` bounded stock-output classifier also recognizes the exact retained
Tahoe/PyInstaller failure as
`embedded_python_loader/python_shared_library_validation_failed`. Running the
new direct-sidecar smoke against the unchanged installed `0.1.8` sidecar
produced that closed classification and exposed none of the raw path, URL, or
credential canaries used by the regression tests. Native bundle and
LaunchServices smokes consume only the corresponding closed native/log event;
unknown output retains only category, count, and a one-way fingerprint.

The `0.1.9` unsigned Preview repair is therefore closed and narrow:

- re-sign the generated macOS sidecar as plain ad hoc before validating and
  publishing the external binary;
- set the Tauri release bundle's hardened runtime to false;
- add no entitlements, especially not
  `com.apple.security.cs.disable-library-validation`;
- verify the final mounted and copied apps, their native executables, and their
  bundled sidecars with exact `/usr/bin/codesign` policy checks before launch;
  and
- bind that policy plus the separately bundled SSH askpass helper's exact
  architecture, mode, digest, and plain ad-hoc signature in release-candidate
  manifest schema version 9.

Developer ID signing remains future work. When introduced, the nested
PyInstaller composition must be signed coherently at build time under a
separately reviewed policy; broad library-validation bypass is not an accepted
repair.

## 6. Highest-Value First Investigation

The real Mac is now the primary reproduction host. Do not start by dispatching
another GitHub candidate workflow.

### 6.1 Preserve the failing installation

Before replacing `/Applications/OpenEvo Desktop.app`:

1. quit every OpenEvo Desktop process;
2. retain the downloaded `0.1.8` DMG and its checksum;
3. retain the exported diagnostics;
4. retain, but do not commit, the existing application-data directory; and
5. record whether the failure is identical after one normal reboot.

Do not ask the user to delete state as a workaround. A separate copied test
profile may be used to distinguish retained-state behavior from clean-state
behavior.

### 6.2 Recover the failure before the first recognized marker

Inspect these files first:

- `desktop/src-tauri/src/main.rs`
  - `StartupDiagnosticScanner`
  - `StartupDiagnosticSink`
  - `release_sidecar_launch_spec`
  - `command_from_launch_spec`
- `desktop/src-tauri/src/desktop_log.rs`
- `desktop/packaging/build_sidecar.py`
- `desktop/packaging/sidecar_entry.py`
- `.github/workflows/openevo-desktop-candidate.yml`
- `scripts/ci/smoke_openevo_desktop_launchservices.py`

For local diagnosis only, capture the complete bounded stderr line from the
sidecar before the scanner discards it. A temporary debug-only echo, LLDB
breakpoint, or local diagnostic harness is acceptable. Do not commit arbitrary
raw stderr persistence, because stock bootloader and Python errors may contain
user paths.

The packaged sidecar is PyInstaller `6.21.0` one-file output with a custom
bootloader. The native host passes:

```text
FD 3: pre-bound IPv4 loopback listener
FD 4: verified sidecar executable/archive
OPENEVO_NATIVE_EXECUTABLE_PATH: exact bundle sidecar path on macOS
```

The custom bootloader already emits closed markers for its resolver, archive,
descriptor handoff, restore, restart, exec, and child-finalization failures.
The absence of one of those markers makes a stock PyInstaller one-file failure,
embedded Python initialization/import failure before the guarded entrypoint,
dynamic-loader failure, or Tahoe-specific process behavior the leading
category. Confirm the actual line before selecting a fix.

### 6.3 Compare the exact environments

Record, without raw user paths:

- `sw_vers` product version and build;
- `uname -m`;
- Tauri and sidecar Mach-O slices from `file` and `lipo -archs`;
- `codesign --verify --deep --strict` result;
- Gatekeeper assessment category;
- quarantine presence on the app and nested sidecar;
- whether the app is translocated;
- app location class (`applications`, `mounted_dmg`, `translocated`, or
  `other`);
- temporary-directory availability and free-space category; and
- the last successful native, bootloader, Python, and Local API startup stages.

The CI test currently removes a synthetic quarantine attribute recursively
before LaunchServices startup and runs on macOS 14. That is not equivalent to a
real browser download followed by Tahoe's interactive **Open Anyway** flow.

### 6.4 Choose the packaging repair from evidence

Prefer the smallest durable fix, but do not preserve the one-file design merely
because it already exists.

- If the failure is a bounded, well-understood Tahoe incompatibility in the
  custom bootloader, repair that path and add a direct regression.
- If the failure is inherent to one-file extraction, restart, quarantine, or
  embedded-library loading, replace the macOS sidecar with a packaged one-folder
  runtime or another deterministic bundled Python composition. Keep the same
  Desktop Local API and native trust boundary.
- Do not weaken executable, archive, or release-input verification to make the
  process start. Adapt verification to the new package inventory if the package
  shape changes.
- Do not move canonical project or run behavior into Rust. The sidecar remains
  a private Desktop transport/state adapter, and the remote Daemon remains the
  backend.

## 7. Diagnostics Required For The Next Release

The current event envelope is too coarse for product support. The next release
must retain a bounded structured startup trace across these layers:

1. application and native-host initialization;
2. bundle and executable verification;
3. process spawn and inherited-descriptor handoff;
4. bootloader archive/extraction or packaged-runtime initialization;
5. embedded Python initialization;
6. sidecar entrypoint and release-metadata validation;
7. Desktop state-store open and migration;
8. Local API bind, authentication bootstrap, and `/version` readiness;
9. renderer session creation and initial snapshot; and
10. remote connection, Daemon bootstrap, tunnel, run, and evolution lifecycle.

Each stage should record, where applicable:

- a stable component, event, stage, and result code;
- startup-attempt identity and monotonic sequence;
- elapsed duration or bounded duration bucket;
- exit code, signal, or errno;
- product version, source commit, architecture, and OS version/build;
- app-location and quarantine/translocation categories; and
- the last completed stage and first failed stage.

Known stock bootloader errors should be mapped to closed, reviewed codes at the
source or parser boundary. Unknown output may retain bounded byte length and a
one-way fingerprint, but a fingerprint alone is not a useful diagnosis and
must not replace a generic failure category.

Diagnostics must not retain:

- raw environment variables;
- access tokens, session tokens, SSH commands, host keys, or private-key data;
- usernames, home directories, arbitrary absolute paths, or project contents;
- unbounded stdout/stderr; or
- raw Codex authentication files or transcripts in the Desktop support log.

Add a schema-versioned environment summary to the exported diagnostics rather
than encoding every fact into one event `code` string. Keep the in-app summary
brief; the exported file is the detailed support artifact.

## 8. Fast Local Development Loop

Use the Mac to keep most iterations off GitHub Actions.

Run source-level checks first:

```bash
uv run pytest -q tests/ci/test_build_sidecar.py \
  tests/ci/test_sidecar_startup_diagnostics.py

cd desktop
npm test -- --run
npm run typecheck
npm run build:openevo

cd src-tauri
cargo fmt --check
cargo test --locked --release -- --test-threads=1
cargo clippy --locked --release --all-targets -- -D warnings
```

For renderer and source-sidecar work, use the debug Tauri composition before
building a DMG:

```bash
cd desktop
npm run tauri:dev
```

For a packaged-sidecar iteration that does not need final remote release assets:

```bash
cd desktop
npm run build:sidecar
npm run tauri:build -- --ci
find src-tauri/target/release/bundle/dmg -maxdepth 1 -name '*.dmg' -print
```

The resulting development DMG is useful for the native launch and Local API
startup loop. Because this command omits the exact Linux Daemon and managed
runtime release inputs, it is not remote-workspace or release evidence. Install
it through Finder and LaunchServices instead of directly running Tauri's build
tree.

Do not treat a Vite browser test or source Python process as packaged-app
evidence. Once the startup fix is stable, build a local DMG and install its app
through the same Finder/LaunchServices path used by an ordinary user. Exercise
the retained Preview state in the normal account. Exercise clean state in a
separate disposable macOS user account so the retained user's application data
does not need to be renamed or deleted.

Avoid repeatedly rebuilding the 352 MB managed runtime or dispatching the
multi-platform workflow while debugging a failure before the first recognized
startup marker. Reuse locally verified immutable release inputs by digest.
Perform a clean exact-input build only after the fast loop passes.

## 9. Patch-Release Acceptance

Do not publish the next patch until all of the following pass on the M3 Pro,
macOS Tahoe `26.5.2` machine:

### Packaged Desktop startup

- The exact downloaded DMG checksum matches its candidate manifest.
- The app is copied to `/Applications` and allowed through the documented
  unsigned-app flow.
- The app starts without `OpenEvo Desktop could not start`.
- Local API `/version` identifies the exact release build.
- Quit and relaunch succeed without orphaned sidecar processes.
- Startup succeeds with retained older-Preview state.
- Startup succeeds with a clean application-data state.
- **Retry** recovers from an injected bounded startup failure.
- **Add remote workspace** immediately opens its functional setup UI.

### Remote ordinary-user workflow

- The user performs every action from Desktop.
- Desktop connects to the configured Linux host over SSH.
- Desktop installs or upgrades the exact matching Daemon automatically.
- Desktop detects the existing remote Codex installation and authenticated
  subscription without receiving `~/.codex/auth.json`.
- A task runs with `gpt-5.3-codex-spark` and `high` reasoning effort for the
  acceptance smoke unless a separately reviewed release profile changes it.
- Evolution targets are independently selectable. The user is not forced to
  enable textual memory, skill bundle, and agent system together.
- At least one run exercises each currently supported target and displays its
  selected method, progress, output artifact, and successor revision.
- A second task proves that accepted first-task artifacts affect only the next
  session and are actually injected there.
- The two built-in science examples remain visible without configuration and do
  not contact local or remote services.

### Diagnostics

- A successful startup trace identifies all completed layers.
- An injected bootloader failure identifies the bootloader stage and code.
- An injected Python-entry failure identifies the Python stage and code.
- An injected state-store failure is distinguishable from bootloader and
  embedded-Python initialization failures.
- Export survives application restart and remains bounded.
- Secret canaries and representative user paths do not appear in the export.

### Regression boundary

- No evolution algorithm source behavior changed as part of this patch.
- Existing Core, sidecar, renderer, Rust, contract, and release tests pass.
- The real remote Codex two-session acceptance passes on the exact candidate.
- Local builds are debug evidence only. After the GitHub workflow rebuilds the
  candidate, download its manifest-bound DMG to the target M3 Pro and repeat the
  packaged startup, retained/clean-state, diagnostics, and two-session remote
  acceptance against those exact candidate bytes.

## 10. Git And Publication Discipline

- Develop from `stable` and pull with `--ff-only` before each publication
  decision.
- Use `ivowang <ziyiwang@ieee.org>` for every commit.
- Commit narrowly; never stage unrelated evolution-method work.
- Push useful reviewed checkpoints promptly so the Linux and Mac environments
  share one source of truth.
- Use implementation subagents only after giving them the complete relevant
  context. Use fresh-context subagents for review. Requested model:
  `gpt-5.6-terra`, high effort.
- Run a fresh independent review before the candidate commit.
- Do not use GitHub Actions as the inner debug loop.
- Dispatch the unsigned candidate workflow only after local packaged-app
  acceptance passes.
- Validate the draft assets on the real Mac. Publish the unchanged draft through
  the guarded Preview workflow only after the exact downloaded DMG passes.
- Update README and user guides to the new immutable URL only after publication.

If the candidate fails, fix the source, create a new commit, and build a new
candidate. Never replace bytes inside an existing public release.

## 11. Completion Record

When the incident is resolved, update this document with:

- the exact root cause;
- the fix and why it is durable;
- the local Mac acceptance commands and evidence digests;
- the candidate source commit and workflow run;
- the final immutable release tag and DMG SHA-256; and
- any remaining unsupported macOS versions or installation flows.

After those facts are copied into the durable release and architecture
documentation, this handoff may be archived as an incident record.

### 11.1 Local `0.1.9` rehearsal on the target Mac

On 2026-07-26, source commit
`36e0e660df675d60e71459e2647a96d5a599b23b` passed a source-equivalent local
rehearsal on the target Apple Silicon Mac and the configured `evolab` server.
This is local pre-candidate evidence. It is not a GitHub-built immutable
candidate, a public release, notarization evidence, or permission to skip the
downloaded-candidate repetition required above.

The exact locally composed artifacts were:

- DMG `OpenEvo Desktop_0.1.9_aarch64.dmg`:
  `b29e3155ddf2688661d32baa70805cc1a2212b7f8d90d93519b77fe9694dba69`;
- packaged native executable:
  `55df83eedc55f2ae8c6fca35e53498bdf38569b879edd610818992c3c5a8fba0`;
- packaged Desktop sidecar:
  `69da08579d0e85efea2d9bbd06e25b4d871b501aafdfdd3fc45458536f4b1e62`;
- packaged SSH askpass helper:
  `b382ace061b2615688f4b4017b4613ca7da87a8497479c2ae7958e3f78e20e82`;
- Linux `x86_64` Daemon Bundle:
  `f3566d4651f63ffa8fe47ab8b69a57e36f9ee08056764752d4210ba323f8e857`;
- Core wheel:
  `e90a2b4b962299685947407fc155006139727dc432fd53194e68118fcd7c8c32`;
- framework lock:
  `4a9b748bbdc366bf75b29b41a1a8b9b53cdaffb249a231a095a4a0a07c793801`;
- managed science runtime:
  `ad9c5ebd69b5785b94dd52dc077d93ababfa9cf8cbcbf92940f60bee48a91149`;
  and
- verified executable registry:
  `07061d2d0bff99783f4b5354d61753f22dd5a856d6b030ba8e74f70210f8ac5b`.

The app reached native, renderer, and packaged-sidecar readiness when launched
from the read-only mounted DMG, from a detached copy, and from
`/Applications/OpenEvo Desktop.app` through LaunchServices after the documented
synthetic-quarantine removal. The LaunchServices run verified version `0.1.9`,
the exact packaged binaries, and complete process-tree cleanup. Its untracked
closed evidence SHA-256 is
`a805e560505aab30cbb4ec160e6034828764db8f5bb446f6167aaea9e759c29c`.
Mounted and detached startup evidence SHA-256 values are respectively
`de32eeea6eb5352235fc566e378fdf9606e2ca66565f884bfb6b44569fb03aea`
and
`f711128393219b0ff6be85c240d4a26679880e8230b25a63dfe16136c3eead8e`.

The installed sidecar enumerated `evolab` from the user's normal
`~/.ssh/config`, persisted only the literal alias, and let system
`/usr/bin/ssh` remain the connection authority. It upgraded the retained
Daemon lifecycle from `13` to `14`, established the active Core v2 tunnel,
created a generation-zero project, quit, relaunched, recovered the persisted
profile/project mapping, and reconnected. The closed bootstrap evidence
SHA-256 is
`b01c7bf7e04df07d79c2f74b27a3dd352ea8f189e448b542d64ff0faafb5895b`.

Two real `gpt-5.3-codex-spark`, high-effort Codex Subscription tasks then ran
with explicit transcript capture. `text_memory`, `skill_bundle`, and
`agent_system` were independently enabled using the remote registry. The first
task consumed generation `0`, produced three evolution artifacts, and atomically
activated generation `1`. The second task was admitted against generation `1`,
consumed the exact runtime-context snapshot from that successor, and atomically
activated generation `2` with three evolution artifacts. The untracked closed
two-session evidence SHA-256 is
`0fcaab29bffc92c1b2fbb583e1e02f90aa897ac99251ba9877724099953ab13c`.

That rehearsal also found a retained-store upgrade defect after the initial
Desktop/Daemon connection succeeded. A newly optional evolution metadata field
was serialized as JSON `null` when older materialization rows were read, which
changed their canonical response digest and made startup fail closed with
`persisted materialized context snapshot is inconsistent`. The durable repair
omits that field when it is absent, preserving the canonical identity of
already persisted rows, adds unit and real-store restart regressions, and
advances the Daemon lifecycle floor to `14`. The fixed Daemon started directly
against the retained production Evolution Store without deleting, rewriting, or
quarantining that state before the installed-Desktop rehearsal.

Still required before publication are an immutable GitHub candidate built from
the final reviewed commit, checksum-bound download of that candidate to this
Mac, repetition of the installed-app and two-session flow against those exact
downloaded bytes, the remaining clean-user/Retry/diagnostics acceptance items,
final review, and guarded Preview publication. Public `0.1.8` remains unchanged
and is still the current public release.
