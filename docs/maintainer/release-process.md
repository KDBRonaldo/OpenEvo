# OpenEvo External Beta Release Process

Canonical release requirements are defined in
`docs/maintainer/productization/spec.md`. This guide records the practical
release procedure. It will evolve with the implementation, but it must never
weaken the canonical release gates.

## Current Status

Version `0.1.1` is a public unsigned historical exhibition Preview. Preserve
its exact source commit, tag, release assets, checksums, and packaging records.
It predates and does not satisfy the immutable Preview publication policy
defined below; its GitHub title/body changed when the draft was made public.
Do not withdraw or replace it merely because it is not a gating candidate.

Version `0.1.2` is a public unsigned historical exhibition Preview, published
on July 19, 2026 from the immutable candidate produced by run `29702250883`.
Preserve its exact source commit, tag, release assets, checksums, and complete
publication evidence.

Version `0.1.3` is a public unsigned historical exhibition Preview, published on
July 20, 2026 from source `5e7a203f08dc2fe434979ae8e546c174378d1af5`
and the immutable candidate produced by run `29756612736`. Preserve its exact
source commit, tag, release assets, checksums, and complete publication evidence.
It is a packaging Preview without candidate-bound real Codex science E2E.

Version `0.1.4` is a public unsigned historical exhibition Preview, published on
July 21, 2026 from source `963407c11e8bac3873eb7de73c3fb55fd5547fb7`,
candidate run `29813754427`, and publication run `29816113282`. Its
candidate-bound release-host evidence verifies two remote Codex subscription
sessions, transcript capture, all three text-evolution targets, next-session
context reuse, two built-in samples, and packaged-renderer observability. It is
still a Preview rather than a G1-G12 candidate: the evidence does not execute
the final macOS Tauri process against the remote host, and clean-host matrix,
protected-performance, signing, notarization, and External Beta gates remain
open.

Version `0.1.5` is a public immutable unsigned historical exhibition Preview,
published on July 21, 2026 from source
`cf7d027b3967db2842af3879882af2b2a8cd693c` and candidate run
`29827786454` under tag
`openevo-desktop-v0.1.5-exhibition.29827786454.1`. The exact candidate passed
Daemon Bundle construction, managed `0.1.4` to `0.1.5` Daemon upgrade,
mounted-DMG launch, detached-copy relaunch, packaged-renderer checks, checksum
validation, and clean-directory asset roundtrip. At the product owner's
direction it was made public through an emergency manual publication before a
detached, signed real-science publication record was completed. Several
independent two-session remote Codex runs exercised transcript capture, all
three text-evolution targets, next-session artifact reuse, and successor
Project Heads, but those runs are not detached signed publication evidence for
this immutable release. Therefore `0.1.5` must not be cited as proof that the
signed publication controller or the G1-G12 release gates completed. At the
time, `0.1.4` remained the newest Preview with checked-in signed,
candidate-bound science evidence. The immutable `0.1.5` release notes overstate
that gate; this repository audit records the authoritative deviation because
published notes and assets cannot be replaced.

Version `0.1.6` was published as an immutable unsigned exhibition Preview on
July 21, 2026 from source
`221380f724fcf6dc3b9780bb2b2044a8ababd25a`, candidate run `29850844088`, and
publication run `29853426425` under tag
`openevo-desktop-v0.1.6-v016.29850844088.1`. It replaces the oversized packaged
sidecar with a startup-bounded Desktop runtime, makes Tauri launch and recovery
asynchronous, restores working **Retry** and **Add remote workspace** actions,
and supports a generation-checked automatic upgrade from the `0.1.5` Daemon.
The exact candidate passed mounted-DMG and detached-copy launch checks. Signed,
checked-in candidate-bound evidence also verifies two real remote Codex
subscription sessions, transcript capture, all three text-evolution targets,
next-session artifact reuse, and packaged-renderer observability through the
live Desktop Local API. It remains a Preview rather than a G1-G12 candidate:
the remote-host E2E does not execute the final macOS Tauri process, and the
clean-host matrix, protected-performance gates, signing, notarization, and
External Beta gates remain open.

Version `0.1.7` is the previous public immutable unsigned exhibition Preview,
published on July 22, 2026 from source
`61190cdb3066377b3c511b35a0df420d70b7c665`, candidate run `29894444050`, and
publication run `29907242726` under tag
`openevo-desktop-v0.1.7-v017-startup-final.29894444050.1`. It isolates both the
Desktop Local API and native retry journal from older Preview state instead of
importing it. The candidate's mounted-DMG and copied-app checks seed corrupt
legacy state and an owner-controlled `0755` application-data parent, then
require the real app bundle to reach provider and renderer readiness without
modifying the legacy files. Signed candidate-bound evidence verifies two real
remote Codex subscription sessions, all three text-evolution targets,
next-session artifact reuse, and packaged-renderer observability. It retains
the same non-gating Preview boundary as `0.1.6` and is not a G1-G12 candidate.

Version `0.1.8` is the previous public immutable unsigned exhibition Preview,
published on July 22, 2026 from source
`dde71c6a940d7e17bbfdb7c41ae7f7ee098618b9`, candidate run `29947490201`, and
publication run `29949667800` under tag
`openevo-desktop-v0.1.8-v018-startup-logs.29947490201.1`. It executes the exact
sidecar embedded in the macOS application bundle instead of copying it through
a temporary pre-Python launch path, and adds bounded, redacted native startup
logs with in-app viewing, Finder reveal, and diagnostics export. The exact
candidate passed mounted-DMG and copied-app startup checks. Signed
candidate-bound evidence verifies two real remote Codex subscription sessions,
all three text-evolution targets, next-session artifact reuse, and packaged
renderer observability. It retains the non-gating Preview boundary and is not a
G1-G12 candidate.

Version `0.1.9` is the latest public immutable unsigned exhibition Preview,
published on July 27, 2026 from source
`54650e477a76dd07b0a511ad5450c3b8ea615556`, candidate run `30212086910`, and
publication run `30214520279` under tag
`openevo-desktop-v0.1.9-v019-system-ssh-final.30212086910.1`. Its DMG SHA-256
is `48ecc88bea4afd5805082a9660d3abc3641172f697b868d1f8cc22498f822cde`.
It repairs the macOS Tahoe packaged-sidecar failure without disabling library
validation, makes literal aliases from the user's normal `~/.ssh/config` the
default remote-workspace selector, and delegates routing, identity, prompts,
and trust to system OpenSSH. The exact candidate passed mounted-DMG,
detached-copy, installed-app, native-sidecar, askpass, packaged-renderer, Linux
Daemon, managed-runtime, and asset-roundtrip checks. Checked-in signed evidence
with SHA-256
`205ed88ce3912f216b4fe32ee5bf511bec889ac68278e1d7c15263788afe8dd9`
verifies two real remote Codex subscription Tasks through one System OpenSSH
alias, three independently selected text-evolution targets, adjacent successor
Project Heads, exact Task-2 Runtime Context reuse, and live Desktop v2 renderer
observability. It remains unsigned, unnotarized, non-gating, and not a G1-G12
candidate; the clean-host matrix, both execution modes, protected performance,
and the remaining External Beta gates are still open.

Version `0.1.10` is the active immutable Preview release target. It keeps the
`0.1.9` System OpenSSH alias authority and repairs the project-create timeout by
reserving a durable lifecycle operation before remote work begins. Exact retry,
renderer reload, SSE reconnect, and sidecar relaunch must preserve the same
action and operation identities and reconcile to one Core project, one mapping,
and one applied mutation. The shared progress surface covers all implemented
long-running work: Desktop-owned profile/host-key/Daemon/native-workspace/project
lifecycles, native startup, and Core-owned Tasks, successor transitions,
services, diagnostics, and maintenance operations. Actual sanitized SSH and
Daemon stdout/stderr may be shown; commands, environment values, credentials,
tokens, Core endpoints, and absolute host paths may not. Existing duplicate
projects created by ambiguous `0.1.9` retries are preserved and may be manually
ignored; migration never deletes them. This paragraph is release intent until
the guarded publisher completes and immutable identities are recorded.

Maintainers can manually dispatch `OpenEvo Desktop unsigned draft prerelease`
from one reviewed `stable` commit. The workflow builds only its macOS runner
architecture, mounts the exact candidate DMG, launches its real Tauri app,
copies that app to a temporary installation location, detaches the image, and
launches the copied app again. It verifies the Tauri executable and sidecar
Mach-O slices in both launches with `file` and `lipo`, verifies the
self-contained Linux x86-64 Daemon Bundle and managed runtime, and creates an
unsigned draft prerelease. It uploads all assets, downloads them into a clean
directory, and validates the exact closed manifest before leaving the draft for
review.

The checked-in evidence for `0.1.4`, `0.1.6`, `0.1.7`, `0.1.8`, and `0.1.9` is bound to
each exact candidate manifest and signed by the release host. It proves the
stated Preview paths, but it does not by itself satisfy G2, G3, G4, G7, G12, or
the full ordinary-user qualification matrix.

Final External Beta publication remains disabled while productization work
tracked by #131/#163 is in progress. PyPI is not part of either the Preview or
External Beta. The ordinary-user artifact is the macOS Desktop DMG; Desktop
installs the manifest-matched Daemon Bundle on the remote server.
The current packaging workflow still emits a Core wheel and
`core-install-artifact.json` as maintainer verification evidence. They are not
ordinary-user install assets, not a third product surface, and not a substitute
for the self-contained Daemon Bundle.

## Preview Publication Policy

A reviewed packaging draft may be published as a Preview after its real
mounted-DMG and detached-copy smoke, Daemon Bundle verification, checksum
validation, and clean-directory asset roundtrip pass. Publication changes only
draft visibility: source commit, tag target, assets, checksums, manifest, and
notes remain byte-for-byte the validated set.

Preview notes must state that the release is unsigned, non-gating, and limited
to the demonstrated exhibition path. They must enumerate missing gates and must
not claim G2, G3, G12, full External Beta readiness, signing, notarization, or
unsupported execution modes. Keep the exact source, tag, assets, checksums, and
validation record after publication; corrections use a new version rather than
replacing published bytes.

A Preview created under this policy is not a G1-G12 release candidate. It
cannot later be relabeled, edited, or reused as the G12 candidate. The final candidate must start again
with the immutable draft, downloaded attestation, detached evidence index, and
publication-controller procedure below. Internal tooling may call the Preview
packaging inventory a candidate; that implementation name does not confer gate
status.

## External Beta Candidate Outputs

- one Apple Silicon OpenEvo Desktop DMG for the exact architecture declared by
  the canonical release manifest;
- DMG SHA256 checksum;
- exact Linux x86-64 Daemon Bundle and SHA256 checksum;
- release manifest containing Desktop/Daemon/protocol versions, compatibility,
  source commit, exact artifact identities, closed environment matrix, and gate
  profile/evidence identities;
- candidate manifest and canonical checksum inventory binding the DMG, exact
  Daemon Bundle, release manifest, source commit, architecture, native smoke
  evidence, and supply-chain reports;
- source tag or source archive bound to the candidate commit;
- release notes;
- supported-environment and known-limitation statements;
- dependency lock, practical vulnerability, and license results for shipped
  Python, npm, Rust, Codex CLI, managed runtime/vLLM images, and validated model
  profile dependencies;
- complete per-task records and benchmark summaries for textual memory,
  trajectory-to-skill, and agent-system gates;
- complete G1-G11 case records plus the closed prepublication evidence bundle
  and index;
- detached G12 attestation and detached final candidate evidence index retained
  as protected publication records outside the draft asset set they attest.

## Candidate Preparation

1. Select one candidate commit from `stable` and freeze its release policy,
   closed profiles, and evidence schemas.
2. Build and freeze the self-contained Daemon Bundle, Desktop DMG, release
   manifest, checksums, notes, and managed-component identities.
3. Run G1-G11 against those exact frozen bytes, including protected
   source/behavior checks, both-mode integration, all clean-host profiles,
   three independent Terminal Bench gates, security, privacy, recovery, and
   product-quality cases.
4. Create the closed G1-G11 prepublication evidence bundle and index, including
   the exact expected G12 procedure and case IDs.
5. Upload the immutable release payload and prepublication evidence to a
   non-public draft with no replacement.
6. Download the complete declared asset set into a clean environment and emit
   the detached G12 attestation.
7. Generate and verify the detached final candidate evidence index binding
   G1-G12.
8. Let the publication controller publish only by changing visibility. Any tag,
   manifest, note, payload, report, or profile change creates a new candidate at
   step 1.

The remainder of this section documents the current packaging rehearsal where
it differs from the target process. Its exact Core wheel export runs only on a
GitHub-hosted ephemeral runner or an equivalently controlled one-shot build
account. Its requested output path must not exist. The builder verifies the
generated wheel and canonical
`framework-lock.json`, writes the exact pair into a private random sibling
staging directory, fsyncs and revalidates both files, and atomically publishes
the complete directory with no-replace semantics. It never adopts, overwrites,
or automatically deletes stale output. A failed job publishes no manifest or
artifact; a local maintainer must use a new output path and inspect any
non-authoritative staging residue before removing it.

The generated target-triple sidecar uses a separate same-directory atomic
replacement. The builder verifies and fsyncs a random staging file before
replacing the Tauri externalBin target. A failure before replacement keeps the
previous target intact; any retained staging file is non-authoritative and must
be inspected before removal.

The pull-request release smoke keeps platform responsibilities separate. Its
macOS packaging job builds the wheel/lock pair once, verifies a two-member
SHA-256 manifest, and uploads the exact inputs under an artifact name bound to
the source commit, workflow run, and run attempt. The Linux Core job depends on
that producer, verifies the downloaded manifest against the digest passed
through the job output, rechecks both member digests, and only then installs the
wheel and runs `openevo-core-service`. GitHub artifact transfer does not preserve
Unix file modes, so the pull-request consumer restores its release-input
directory to `0700` and the wheel, framework lock, and checksum inventory to
`0600` before verification. The candidate consumer likewise restores the
transferred wheel and framework lock to `0600`. Consumers must not weaken the
Core supervisor's owner-only framework-lock requirement. The Linux job must not
rebuild the wheel or lock, and it rechecks those final candidate bytes after the
service smoke. Conversely, the macOS packaging job must not run the Linux-only
Core service lifecycle. Framework wheel verification also has explicit platform
scopes: macOS uses `--mode installed-registry` to verify the installed wheel,
exact lock, distribution inventory, entry points, target handlers, and frozen
registry; Linux uses `--mode linux-context-projection` to repeat those checks
and then exercise the `O_PATH`-dependent artifact migration and
context-projection path. The full Linux scope must remain on the exact
downloaded candidate bytes and cannot be replaced by the cross-platform
registry scope. The stronger hostile-install bootstrap threat model is tracked
in GitHub issue #193 and is not a claim of this unsigned packaging-only
candidate.

On macOS, the native host directly executes the verified
`Contents/MacOS/openevo-desktop-sidecar` bundle member. It retains the verified
file descriptor and the relevant bundle-directory descriptors, then compares
the executable path, device, inode, size, mode, owner, link count, timestamps,
and digest before and after spawn. The PyInstaller archive is opened through
retained FD 4 via `/dev/fd/4`; unlike the `0.1.7` implementation, no private
macOS copy is created. Linux retains its separate private anonymous-copy and
`/proc/self/fd/4` execution design.

If the packaged sidecar exits before readiness, inspect only the bounded
`OPENEVO_STARTUP_V1` stage/code emitted by the bootloader or Python entry
point. A candidate with a missing, malformed, unknown, or non-allowlisted
startup diagnostic remains failed. Maintainers must fix the identified
descriptor/executable/startup stage and rerun from a new commit; they must not
publish arbitrary process output, disclose paths or credentials, or replace an
FD-bound archive read with a pathname fallback. `python_launcher/*_failed`
codes identify the last closed release-composition phase, such as Core assets,
provider/workspace storage, SSH lifecycle, Core bridge/runtime, Local API, or
static app mounting. They do not authorize printing the underlying exception.
`python_launcher/shutdown_failed` means the packaged listener or release
provider could not be closed after an otherwise normal server return. When a
startup or server failure is already active, cleanup remains best effort and
does not replace that earlier fixed phase code.
The packaged launcher must pass `close_on_shutdown=False` to the Local API app
factory and close the provider itself after `Server.run()` returns. Re-enabling
the ASGI close hook in this path is invalid because Uvicorn records shutdown
handler failures internally and can return without propagating them.
The launcher must also retain its packaged signal-replay guard around both
`Server.run()` and explicit cleanup. Without it, Uvicorn restores and replays
Tauri's `SIGTERM`, terminating Python before launcher-owned cleanup runs.

The manual packaging draft uses the same producer/consumer rule for the complete
release inventory. `release-candidate.json` and `core-install-artifact.json`
bind the exact Core wheel and framework lock; the former also binds the
self-contained Daemon Bundle and manifest, managed runtime identity, DMG,
`SHA256SUMS`, commit, runner architecture, mounted-DMG/detached-copy native
evidence, and Python, npm, and Cargo dependency/license/security summaries.
Those summaries are checked against the candidate checkout's four lock/license
files. The Python export uses `uv export --frozen --no-emit-project` with
SHA-256 hashes. The collector requires every requirement block to be one exact
pin with at least one valid hash, requires the `pip-audit` package/version set
to equal every applicable requirement, rejects OpenEvo itself, and records the
requirements digest and audited package count. The clean-wheel smokes install
that closure with `pip --require-hashes` before installing the exact OpenEvo
wheel with `--no-deps`; they never upgrade pip or resolve the wheel's lower
bounds online. `cargo-audit` is pinned to `0.22.2` so the current RustSec CVSS
4.0 data is parseable. `pip-audit==2.9.0` and its tool closure are installed
from the frozen repository `uv.lock`, not resolved by `uvx` during the release
run. Malformed or incomplete audit JSON still fails in the collector and no
advisory is ignored. The final draft job alone receives `contents: write` and
runs behind the protected `openevo-preview-publication` environment; build and
Linux verification retain read-only permissions.

The Linux producer runs the nine contract-simulator preview tests as a separate
non-release gate. They do not emit a candidate blob and are never merged into
public evidence. A second invocation runs only the three packaged release
projects (`release-packaged-1440`, `release-packaged-1024`, and
`release-packaged-760`) against the release sidecar mock contract and the
packaged web composition. Playwright's report merger consumes only that
packaged-release blob and emits one JSON report after the packaged Desktop web
payload is built. The workflow emits
`playwright-candidate-evidence.json`. That closed record binds the source commit,
Actions run ID and attempt, Chromium version, exact test/project list, the
declared viewport for every project, first-attempt pass status, sanitized
`playwright-report.json` digest, and the packaged-web build and manifest
digests. Schema version 2 explicitly records `simulator=false`,
`provider_kind=desktop_sidecar`, and `composition=packaged_web`. The candidate
tool strictly parses the packaged-only raw report and emits a
canonical sanitized report containing only the closed project, viewport, case,
retry, and status fields. Runner paths, web-server commands, stdout, attachments,
and other Playwright metadata remain temporary and are not uploaded. The
sanitized report, copied `packaged-web-manifest.json`, and closed evidence are
uploaded together. The macOS consumer revalidates all three against the same
run identity, requires its independently produced packaged-web manifest to
match, and then adds all three files to `candidate-artifacts`.
`release-candidate.json`, `SHA256SUMS`, Linux verification, and the downloaded
draft roundtrip treat these as required roles; missing, extra, rewritten,
skipped, flaky, retried, or cross-candidate evidence fails closed.

This Playwright result proves only the source-bound packaged Desktop interaction
and viewport contract represented by those browser tests. It is not evidence
of a real Codex Subscription science run. Release notes must describe Codex
Subscription support as packaged and declared while the release remains a
draft. A public v0.1.10 Preview additionally requires candidate-bound evidence
for the exact packaged macOS Desktop sidecar and askpass helper, a
`system_openssh` workspace selected from the user's literal SSH-config aliases,
a supported remote OpenEvo Daemon/Core v2, subscription-authenticated Codex
with transcript capture, two completed immutable Tasks, three typed Evolution
Revision outputs per successor, an adjacent generation-0 -> generation-1 ->
generation-2 Project Head chain, Task-2 Runtime Context reuse, and
packaged-renderer v2 observation. It also requires one project-create lifecycle
whose reservation completes below the renderer deadline, whose terminal work
lasts more than 15 seconds, whose ordered phases and sanitized SSH/Daemon log
sources are rendered, and whose authority survives an SSE reconnect and
sidecar relaunch without a second create request. Post-shutdown evidence must
show one Core project, one mapping, and one applied mutation for the stable
action ID, and a generated secret canary must be absent from lifecycle payloads,
renderer text, screenshot bytes, support output, and the evidence document.
Without that evidence, neither a Preview nor a packaging candidate may say
that a real Codex Subscription Task or the lifecycle timeout repair was
validated.

The exact-candidate run is performed on the macOS release host after the draft
exists. It consumes the draft's exact manifest, Daemon, Core, runtime, and
packaged-web evidence; installs the app copied from the exact DMG; and executes
that app's packaged macOS sidecar and askpass helper. Release acceptance
composes this live remote Task run and packaged-renderer observation with the
candidate workflow's mounted-DMG and copied-app native Tauri smokes. It does
not claim that Playwright itself drives the Tauri webview. A passing canonical
record and its OpenSSH signature are committed at
`release-evidence/<candidate-tag>/desktop-real-science-e2e.json{,.sig}`.
The candidate manifest's `app_bundle_smoke` role binds the exact macOS sidecar
digest observed in the mounted DMG, and the science-run record requires that
same digest from the installed app. It also binds the exact candidate askpass
helper digest, size, mode, relative path, architecture policy, and ad-hoc
signature. Publication is fail-closed until the reviewed publication workflow
validates the record, both caller-supplied SHA256 values, and the signature
against the public key frozen in the candidate source. That key must also match
the SHA-256 trust anchor configured as
`OPENEVO_REAL_SCIENCE_E2E_PUBLIC_KEY_SHA256` in the protected
`openevo-preview-publication` environment. The publication-policy
commit must descend from the candidate commit and its complete Git delta may add
only those two candidate-tag files, so evidence cannot be accompanied by a
post-candidate policy relaxation.

The current rehearsal's dependency and security summaries and Core descriptor
use schema version 2 for those closed contracts. The release candidate manifest
uses version 10, with explicit Developer ID, ad-hoc bundle signature,
notarization, quarantine-removal, Rust-toolchain, and closed unsigned macOS
code-signing-policy fields. For the unsigned Preview, the policy requires plain
ad-hoc signing with hardened runtime disabled and rejects the
disable-library-validation entitlement. Candidate
Playwright evidence uses version 2. Native smoke evidence uses version 3 so
every report declares its
`mounted_dmg` or `detached_copy`
launch origin and binds the source DMG and both packaged binaries by SHA256. The
unchanged license inventory remains version 1; `framework-lock.json` retains its
independent string-valued version 1 contract.

Each native smoke records closed `mach_o` evidence for the Tauri executable and
packaged sidecar: bounded `file -b` output plus the sorted `lipo -archs` slice
list. Candidate creation requires the mounted-DMG and detached-copy
observations, binary SHA256 values, and source-DMG SHA256 values to match the
candidate bytes, and requires both binaries to contain exactly the slice
represented by the runner architecture. Old untyped app evidence is rejected.
The workflow deliberately does not inspect Tauri's intermediate `bundle/macos`
path because the DMG bundler may remove that directory after packaging.
`release-candidate.json` repeats those normalized slice lists under
`macos.native_architectures`, while the file inventory binds both complete smoke
reports by size and SHA256.
Before launch, the smoke rejects symbolic links in the app root, `Info.plist`
path, Tauri executable path, sidecar path, and their in-bundle ancestors. It
also parses the final merged `Info.plist` and requires only the exact
`127.0.0.1` ATS exception; local-network, broad, or additional transport
exceptions fail the candidate. It
captures each binary's pre-launch device/inode/size identity and digest. The
Rust native host emits a credential-free V2 marker binding its verified
private executable FD to that sidecar digest plus its private device, inode,
and size. The smoke binds the marker PID to the current app descendant,
process group, session, and live Darwin birth identity; requires FD 3 to be an
IPv4 loopback listener; and requires FD 4 in the same process to match the
declared regular-file device, inode, and size. It also requires the V2
renderer-ready marker, which the Tauri host emits only for the invoking `main`
WebView with non-zero inner dimensions after an authoritative Local API snapshot
has rendered and the product shell has committed. Logical window visibility is
a closed diagnostic, not a direct-binary release condition, because GitHub's
macOS runner does not provide an interactive Aqua visibility contract. It
does not reopen the private executable pathname: the pathname remains in the
documented same-UID trust boundary until native cleanup, while the marker and
live FD identity are the observer's authority. Both packaged paths are
rechecked after process cleanup.
Native stderr uses a nonblocking bounded pipe, parsing remains byte/line bounded,
and timeout output uses only closed readiness and renderer stage names. Unknown
renderer stages fail closed and never become success evidence. The renderer can
report only the fixed `bootstrap_context_{validated,failed}`,
`local_api_version_{verified,failed}`, `retry_recovery_{ready,failed}`,
`provider_adapter_{ready,failed}`, `provider_{created,create_failed}`,
`initial_snapshot_failed`, and `product_committed` stages through its typed
native command. These reports diagnose bootstrap progress without serializing
errors or runtime values and cannot replace the V2 renderer-ready marker. A
probe deadline or
transient shallower probe result cannot overwrite the deepest product readiness
stage reached. Probe subprocesses
share one readiness deadline and run in private sessions. Timeout cleanup takes
a bounded ancestry snapshot while the direct leader is unreaped, kills observed
escaped descendant groups before the root group, and then reaps the direct
leader. The app process group is signalled only while the unreaped
child reserves its leader PID; after `poll` or `wait`, that numeric group is
observation-only. Native cleanup and the parent-liveness watchdog own sidecar
termination, while both success and failure paths verify every observed group
ceases to exist within a bounded cleanup period; a zombie-only group is still a
cleanup failure.
The release host gives a cold packaged sidecar up to 60 seconds to complete its
bounded PyInstaller onefile startup. Each mounted-DMG or detached-copy smoke has
a separate 120-second readiness deadline so renderer and FD probes cannot
expire merely because the inner cold-start budget was consumed. WebView identity
and dimensions are checked in-process by Tauri rather than by repeatedly
compiling an external CoreGraphics probe. Cleanup
then retains its independent 5-second termination and 15-second group-
disappearance maximum bounds.
Candidate JSON parsing
rejects duplicate keys at every nesting level, so a last-key-wins parser cannot
reinterpret the closed evidence contract.
The preceding tool probe locates an executable `lipo` through
`xcrun --find lipo`; it does not use the unsupported `lipo -version` flag.
Availability alone is not release evidence: both real app launches still run
`lipo -archs` against the Tauri executable and packaged sidecar.

The portable app-smoke unit fixture uses an emitted evidence record because its
executable is a test script, not Mach-O. It never substitutes for candidate
evidence: macOS candidate runs always inspect and launch the real Tauri binary
and packaged sidecar with `file`, `lipo`, host-bound renderer/product readiness,
and FD checks.
Any product or benchmark failure creates a new candidate after the fix.
Infrastructure-only retries must be recorded and may not be used to select the
best stochastic result.

## Preview Draft Validation

The current manual workflow creates a uniquely tagged GitHub draft prerelease
only after its macOS and Linux rehearsal jobs succeed. Cross-job Actions
artifact names bind source commit, workflow run, and run attempt so a full
rerun cannot collide with immutable v4 artifacts from an earlier attempt. It
uploads the required outputs, downloads every asset into a clean directory, and
verifies:

- asset names and architectures are expected;
- SHA256 files match downloaded bytes;
- the Core descriptor references the uploaded Core wheel;
- the Daemon Bundle, its manifest, the DMG-embedded copy, and managed runtime
  identity match the source commit and candidate inventory;
- the DMG version and bundled/fetched descriptor match the source commit;
- release notes exactly match the release-tool-owned canonical packaging
  document. The GitHub body adds only a release-tool-generated, 128-bit random
  ownership marker used by failure cleanup. The document states
  unsigned/not-notarized status, available and unavailable execution
  modes, known limitations, `0 of 3` benchmark gates with all three rescue
  counts `pending`, and that Codex Subscription is packaged while public
  publication additionally requires separately signed exact-candidate real
  science E2E evidence. It also states privacy/security behavior and
  install/upgrade/uninstall retention for the current
  `~/Library/Application Support/org.openevo.desktop` state, preserved legacy
  Preview data under `~/.openevo/desktop`, and remote data;
- the GitHub draft title, tag, target commit, body, draft state, and prerelease
  state match the candidate at the discrete API read immediately after asset
  redownload. Its repository-bound API URL supplies the immutable numeric
  release ID; cleanup persists that authority once with mode `0600`. This is not
  an atomic assertion about later workflow completion;
- no unclassified development, secret, benchmark-private, or source-checkout
  files are present.

The retained canonical Preview snapshot additionally binds the immutable
numeric release ID, exact body, asset IDs, names, sizes, API SHA256 digests,
candidate-manifest digest, and candidate workflow run ID/attempt. It is the
publisher's reviewed baseline, not permission to edit the draft.

Before creation, the workflow validates the exact candidate tag name as a Git
ref and uses the authenticated, paginated release inventory so private drafts
are included when requiring both a same-name release and remote Git tag to be
absent. If creation, upload, redownload, metadata validation, or
verification-record upload fails or is cancelled, an `always()` cleanup deletes
only a draft whose complete metadata and random ownership marker still match
this workflow attempt. Cleanup deletes the validated immutable release ID, not a
second lookup by mutable tag name. It does
not delete Git tags, and it fails unless both the owned draft and any same-name
remote Git tag are absent afterward. A successful run leaves the draft for
review but proves that no real Git tag exists; the draft's `tagName` is release
metadata only. It does not publish the release.

A GitHub draft remains administratively mutable after the workflow. The
workflow provides point-in-time verification, not an immutable GitHub object.
Any later edit, asset replacement, or tag movement invalidates the validation;
delete it and run a new packaging draft. An unchanged, revalidated draft may be
published only under the Preview policy above.

## Preview Publisher

Dispatch `Publish validated OpenEvo Desktop Preview` only from `stable` and
through the protected `openevo-preview-publication` environment. Supply the
exact candidate tag, numeric release ID, source SHA, `release-candidate.json`
SHA256, committed two-Task v2 real-science evidence SHA256, its OpenSSH
signature SHA256, candidate workflow run ID and attempt, plus the explicit
confirmation.
The publisher first runs a read-only verification job. That job checks out the
workflow's own `github.workflow_sha`, never the candidate source, and requires
the Actions API to identify that exact run as a successful, completed
`workflow_dispatch` of
`.github/workflows/openevo-desktop-candidate.yml` on `stable` at the expected
source SHA. The candidate run publishes a small immutable Actions artifact
containing only `release-candidate.json` and `app-bundle-smoke.json`; the
read-only job downloads it together with that run's canonical draft snapshot
and binds both files to the snapshot's asset IDs, sizes, and SHA-256 values.
This avoids granting draft-release read/write authority to the verification
job. It also validates the fixed candidate-tag evidence path
with `validate_desktop_real_science_e2e.py`, requiring the exact source and
candidate-manifest identities, the exact candidate native-sidecar smoke,
candidate-source public key and protected trust anchor, trusted evidence
signature, two successful subscription Tasks, the exact adjacent Project Head
chain, three Evolution Revision outputs per successor, Task-2 Runtime Context
reuse, System OpenSSH authority, renderer v2 observability, and complete
ownership cleanup.
It also requires the publication-policy commit to descend from the candidate
source and permits exactly two added paths in that delta: the candidate-tag
evidence and its signature.

The write-authorized job then re-reads the draft by numeric ID and downloads
every draft asset by immutable asset ID before changing visibility. The fixed
publication receipt records the candidate workflow identity,
candidate-manifest digest, publication-policy commit, durable evidence and
signature paths and digests, signer-key digest, release identity, and immutable
publication result. The receipt is retained for 90 days; the canonical evidence
and signature remain durable in the recorded policy commit.

Before publication it downloads every draft asset by immutable asset ID into a
fresh owner-only directory and verifies the manifest, `SHA256SUMS`, title,
body, prerelease/draft state, target commit, source/run identities, API asset
digests, and exact closed inventory. It also proves the real Git tag does not
exist. The protected-environment reviewer must independently verify through
repository administration that immutable releases remain enabled; the normal
workflow token intentionally has no repository-administration permission. Any
failure through this point leaves the draft unpublished.

The protected write job never checks out or executes candidate code. It
downloads the read-only job's validated evidence as data, re-hashes those bytes,
downloads the candidate snapshot as data, repeats the closed identity,
metadata, API digest, asset-byte and tag-absence checks with fixed standard
library code embedded in the reviewed workflow, and only then performs the
single mutation: a REST PATCH to
`repos/<owner>/<repo>/releases/<numeric-id>` with the exact body
`{"draft":false}`. The workflow never uploads, replaces, edits, or deletes an
asset, title, body, target, or tag. A separate read-only job then re-reads the
same numeric release, requires `immutable=true`, downloads all public assets
into another fresh directory, repeats the closed validation, and requires the
new Git tag to point exactly to the source SHA. Post-publication verification
failure is recorded and requires manual incident handling; automation must not
delete the now-public release. If an administrator disables immutable releases
between approval and the visibility PATCH, the fixed write step rejects the
non-immutable response after publication and the run enters that incident path;
no broader token is stored in Actions to eliminate this administrator-level
race.

Two fresh-context `gpt-5.6-terra` high-effort reviews must approve product/spec
compliance and release risk before a candidate reaches `stable`.

## Publication

New Preview publication is allowed only by the policy above. Version `0.1.1`
remains published as an explicitly recorded historical exception; it must not
be cited as policy-compliant publication evidence.

Final External Beta publication remains disabled. After the science, benchmark,
remaining privacy, and final product gates are implemented and pass, create a
new final candidate from a reviewed `stable` commit. The current Preview's
ad-hoc-signature and synthetic quarantine-removal evidence remains
packaging-profile evidence, not a substitute for those missing gates. That
candidate must use the exact Daemon Bundle and canonical release manifest,
generate final release notes and a new checksum/evidence inventory, roundtrip
every asset again, emit the detached G12 records, and publish only through the
canonical publication controller. Preview evidence cannot substitute for any
missing gate.

## Rollback

Before publication, close the failed draft and open a corrective issue. After
publication, mark a broken release clearly, preserve evidence needed to explain
the failure, and publish a corrected version rather than replacing bytes under
the same tag. User-facing rollback is manual installation of the most recent
compatible DMG/Daemon pair within the rollback barrier defined by the canonical
specification; document any irreversible state migration.
