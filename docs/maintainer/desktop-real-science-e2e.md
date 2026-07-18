# Release Desktop Real-Science E2E

This maintainer runner is the process-boundary rehearsal for the release
Desktop sidecar against a real remote science host. It is part of #163. It does
not add a CLI product surface and does not call Core Control directly.

## Required environment

Run on a supported macOS release-builder host with:

- an existing `SSH_AUTH_SOCK` whose agent can authenticate the requested remote
  user without an interactive prompt;
- a separately reviewed SSH host-key algorithm and SHA-256 fingerprint;
- a Linux x86-64 remote host that satisfies the packaged Core preflight and can
  run the managed Docker runtime;
- a working Codex subscription login for the remote user;
- the exact managed subscription Science runtime archive for the release;
- the exact Linux Daemon bundle and canonical Daemon manifest for the release;
- either an exact packaged sidecar/Core wheel/framework lock triplet or the
  complete local sidecar build toolchain.

Do not pass an unreviewed fingerprint copied from the runner. The runner
requires an exact expected fingerprint and accepts only the candidate returned
by Desktop Local API after the sidecar performs its normal untrusted probe.

## Run with exact release assets

```bash
uv run python scripts/e2e/desktop_real_science_e2e.py \
  --host <remote-host> \
  --port 22 \
  --user <remote-user> \
  --expected-host-key-fingerprint 'SHA256:<reviewed-fingerprint>' \
  --sidecar <packaged-sidecar> \
  --core-wheel <exact-core-wheel> \
  --framework-lock <exact-framework-lock> \
  --managed-runtime-archive <exact-managed-runtime-archive> \
  --daemon-bundle <exact-openevo-daemon-linux-x86_64> \
  --daemon-manifest <exact-openevo-daemon-bundle.json> \
  --output desktop-real-science-e2e-evidence.json
```

The sidecar/Core wheel/framework lock arguments are required together. The
Daemon pair and runtime archive are required in every real run. Before launch, the
runner checks the closed framework-lock schema, binds it to the wheel bytes,
and uses the release builder's PyInstaller inspection to prove that the
packaged sidecar embeds that exact wheel, lock, runtime archive, Daemon bundle,
and Daemon manifest. Held file descriptors bind validation, evidence, and the
executed sidecar snapshot to the same source inodes.

Omit the sidecar/Core wheel/framework lock triplet to build a fresh packaged sidecar and export the
exact embedded wheel/lock pair. That mode uses
`desktop/packaging/build_sidecar.py`; it is not a substitute for candidate
signing, DMG copy smoke, or clean-machine rehearsal.

```bash
uv run python scripts/e2e/desktop_real_science_e2e.py \
  --host <remote-host> \
  --port 22 \
  --user <remote-user> \
  --expected-host-key-fingerprint 'SHA256:<reviewed-fingerprint>' \
  --managed-runtime-archive <exact-managed-runtime-archive> \
  --daemon-bundle <exact-openevo-daemon-linux-x86_64> \
  --daemon-manifest <exact-openevo-daemon-bundle.json> \
  --output desktop-real-science-e2e-evidence.json
```

The local mode invokes the builder with the managed runtime and Daemon pair plus
`--release-build`, so a development sidecar without the complete controlled
release payload cannot be used as release E2E evidence.

The runner always enables `text_memory`, `skill_bundle`, and `agent_system`.
There is no target or method override. `agent_system` must expose the Core-owned
`method=auto` resolver, and every concrete method reachable from that resolver
must match a supported accepted-method identity. This preserves Core's recorded
requested/resolved method decision. For the other targets, the remote effective
default is used only when it is supported; fallback remains within visible
supported methods and prefers stable methods. Remote default config is
preserved. Missing support fails closed before the first run.

## Product-boundary flow

The runner performs these actions only through Desktop Local API v1:

1. Start the real packaged sidecar with an inherited loopback listener,
   inherited executable descriptor, and one native credential frame on stdin.
   Negotiate the exact checked-in release-contract schema, frozen Desktop OpenAPI
   digest, provider kind, and feature flags against a strict closed `/version`
   response; require the legacy shell route to return 404; and require authenticated
   and unauthenticated native session probes to return 204 and 403 respectively.
2. Create an SSH-agent profile, connect, compare the candidate host key with
   the expected identity, and confirm it.
3. Create a scratch project with
   `codex_subscription_transcript`, explicit transcript capture, no token-level
   metrics, and initially disabled drafts for all three required evolution
   targets. Core compiles it to the exact `managed_science` runtime profile with
   `container_user=host`; the existing experiment model remains fail closed for
   any other subscription runtime shape.
4. Activate the project, fetch capabilities through its active tunnel, select
   all three remote stable methods, reactivate, and run Core project validation.
5. Run session 1 to `succeeded`; inspect its timeline, logs, context, artifact
   summary, and bounded artifact content endpoint.
6. Prove session 1's pinned context contains neither its own output artifact IDs
   nor their successor revision. Then run session 2 to `succeeded` and prove that
   its exact pinned revision is the generation-adjacent revision produced by
   session 1. Require all three session-1 artifacts in session 2's pinned context,
   require each session-2 artifact lineage to reference its matching predecessor,
   and verify the Core runtime-context receipt digest. Both successful sessions
   must contain the real harness execution phase. Codex skill installation is
   fail-closed; its setup no longer ignores copy failure. Gateway creates the v3
   receipt only after harness setup, run, postprocess, and a final runtime
   download/readback. It binds the pinned revision/context, effective instruction
   SHA-256, complete runtime file inventory/tree, authoritative source content,
   canonical memory/agent-system files, every skill file, and every agent-system
   target. Core independently rebuilds the expected rendering from the persisted
   context before success. Together, the successful execution phase, strict skill
   installation, effective-instruction binding, and exact post-run receipt prove
   the Codex harness path consumed the three context surfaces instead of merely
   accepting a staging receipt. Runner-returned metadata cannot supply or mutate
   the receipt. No Codex transcript or artifact content is retained in evidence.
7. If timeout, interruption, or another failure leaves a nonterminal Desktop run,
   request cancellation and wait a bounded interval for terminal `cancelled`.
   Record only boolean cleanup outcome fields; cancellation failure remains a
   failure. Then request profile disconnect and terminate/wait for the sidecar
   process group. Sidecar shutdown owns tunnel and Core attachment release.

All polling, activation, run, HTTP, build, and shutdown waits are finite and
positive. The build timeout is configurable with `--build-timeout-seconds`.
Sidecar and local-build cleanup retain the unreaped process-group leader as PGID
authority until descendants have been signalled and the leader can be reaped,
including the race where the leader exits before its descendants.

Any missing product state fails the run. The script does not read a remote DB,
invoke SSH commands directly after startup, call Core Control directly, or
manufacture a successor/artifact observation.

## Evidence policy

The output is canonical JSON, mode `0600`, and at most 128 KiB. It contains
release digests and sizes including the validated managed runtime archive,
build identity, redacted remote identity digests, state/count inventories,
revision generations/manifests, artifact metadata digests, the runtime-context
receipt digest, session-1 exclusion and session-2 consumption booleans, and
cleanup results. A closed field allowlist rejects every unrecognized evidence
key.

It does not contain:

- the Desktop session credential or native handoff/readiness values;
- a mutation token, Core bearer, password, passphrase, private key,
  `SSH_AUTH_SOCK`, Codex authentication, or another raw secret;
- host filesystem paths, remote host/user text, opaque resource IDs, log or
  timeline messages, or artifact document text.

Failure evidence uses only a bounded stage, typed code, optional HTTP status,
and partial redacted observations. Sidecar stdout/stderr remains in an unnamed
temporary file and is never copied into evidence. The runner writes evidence
only after cleanup has completed; incomplete cleanup changes an otherwise
passing result to failure.

## Structural verification

```bash
uv run python scripts/e2e/desktop_real_science_e2e.py --structural-check
uv run pytest -q tests/ci/test_desktop_real_science_e2e.py
```

`--structural-check` verifies only the frozen release contract shape and native
launcher shape. It does not build assets, contact a remote host, launch the
sidecar, require the managed runtime archive or release-asset triplet, or write
evidence. Its success text explicitly states that E2E was not run. Unit tests
use synthetic capability/revision/session documents and real local child
processes only to test fail-closed assertions and cleanup; they never emit a
passing E2E artifact or claim a real Codex/SSH session ran.
