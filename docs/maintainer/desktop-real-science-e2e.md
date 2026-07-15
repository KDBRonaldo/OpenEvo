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
  --output desktop-real-science-e2e-evidence.json
```

All three release-asset arguments are required together. Before launch, the
runner checks the closed framework-lock schema, binds it to the wheel bytes,
and uses the release builder's PyInstaller inspection to prove that the
packaged sidecar embeds that exact wheel and lock.

Omit all three asset arguments to build a fresh packaged sidecar and export the
exact embedded wheel/lock pair. That mode uses
`desktop/packaging/build_sidecar.py`; it is not a substitute for candidate
signing, DMG copy smoke, or clean-machine rehearsal.

The defaults select `text_memory` with
`text_memory_expel_reflector`. A maintainer may pass another non-parametric
text target and method exposed by the exact Core release. The runner still
requires remote capabilities to report the selected method as supported and
requires Core project validation to pass before the first run.

## Product-boundary flow

The runner performs these actions only through Desktop Local API v1:

1. Start the real packaged sidecar with an inherited loopback listener,
   inherited executable descriptor, and one native credential frame on stdin.
2. Create an SSH-agent profile, connect, compare the candidate host key with
   the expected identity, and confirm it.
3. Create a scratch project with
   `codex_subscription_transcript`, explicit transcript capture, no token-level
   metrics, and one enabled text-evolution target.
4. Activate the project, require compatible remote capabilities, and run Core
   project validation.
5. Run session 1 to `succeeded`; inspect its timeline, logs, context, artifact
   summary, and bounded artifact content endpoint.
6. Run session 2 to `succeeded`; prove that its exact pinned revision is the
   generation-adjacent revision produced by session 1 and that a session-1
   artifact appears in session 2's pinned context.
7. Request profile disconnect, then terminate and wait for the sidecar process
   group. Sidecar shutdown owns tunnel and Core attachment release.

Any missing product state fails the run. The script does not read a remote DB,
invoke SSH commands directly after startup, call Core Control directly, or
manufacture a successor/artifact observation.

## Evidence policy

The output is canonical JSON, mode `0600`, and at most 128 KiB. It contains
release digests, build identity, redacted remote identity digests, state/count
inventories, revision generations/manifests, artifact metadata digests, reuse
booleans, and cleanup results.

It does not contain:

- the Desktop session credential or native handoff/readiness values;
- a Core bearer, `SSH_AUTH_SOCK`, Codex authentication, or another raw secret;
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

`--structural-check` verifies only the release provider policy and native
launcher shape. It does not build assets, contact a remote host, launch the
sidecar, or write evidence. Its success text explicitly states that E2E was not
run. Unit tests use synthetic revision documents only to test fail-closed
assertions and never emit a passing E2E artifact.
