# Desktop Remote Home Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the v0.1.10 macOS Desktop release connect, bootstrap, create projects, and run tasks through a literal system-OpenSSH alias when the authenticated Linux account has any supported writable NSS home, without exposing that home through Desktop contracts, persistence, errors, or logs.

**Architecture:** The owned `SystemOpenSshSession` privately probes the effective account after authentication and seals the verified username, UID, NSS home, profile ID, and connection generation into a process-local `RemoteHomeAuthority`. The v2 lifecycle derives its internal workspace root from this authority and passes the same authority through the follower into `SshRemoteExecutorTransport`; rich remote commands and Daemon staging independently revalidate the account/home binding. The legacy explicit-profile transport keeps its conventional fallback, while the ordinary-user v2 path must never use it.

**Tech Stack:** Python 3.11+, Pydantic v2, POSIX `/bin/sh`, system OpenSSH, pytest, React/Vite/Tauri release tooling, PyInstaller Daemon bundle, macOS DMG packaging.

**Design source:** `docs/superpowers/specs/2026-07-31-desktop-remote-home-authority-design.md` (approved 2026-08-01), GitHub #265, related #220.

---

## Task 1: Add the closed remote-home authority primitive

**Files:**

- Create: `src/openevo/deployment/remote_home.py`
- Create: `tests/deployment/test_remote_home.py`

- [ ] **Step 1: Write parser and authority construction tests that fail because the module does not exist**

Add table-driven tests for `/root`, `/home/researcher`, and `/srv/research/alice` using the exact versioned private record:

```python
def _record(*, user: str = "researcher", uid: int = 1001, home: str) -> bytes:
    return (
        "openevo-remote-home-v1\n"
        f"{user}\n{uid}\n{user}\n{uid}\n"
        f"{home}\n{home}\n{uid}\n1\n"
    ).encode("utf-8")


@pytest.mark.parametrize("home", ["/root", "/home/researcher", "/srv/research/alice"])
def test_verified_probe_seals_exact_derived_roots(home: str) -> None:
    user = "root" if home == "/root" else "researcher"
    uid = 0 if home == "/root" else 1001
    authority = parse_remote_home_probe(
        profile_id="profile-1",
        connection_generation=7,
        return_code=0,
        stdout=_record(user=user, uid=uid, home=home),
        stderr=b"",
    )
    assert authority.workspace_root == f"{home}/.openevo/workspaces"
    assert authority.daemon_bundle_root == f"{home}/.openevo/daemon-bundles"
```

Cover every parser rejection named by the design: nonzero result; non-empty stderr; missing/extra line; no final newline; invalid UTF-8; NUL/control bytes; aggregate output over 8 KiB; unsafe username; non-canonical or out-of-range UID; `id`/NSS user or UID mismatch; relative/empty/over-4096-byte home; empty, `.`, `..`, unsafe, repeated, or trailing path components; physical-home mismatch; owner mismatch; and writable flag other than exact `1`.

Assert the class cannot be constructed without its private seal, is immutable and generation-bound, and neither its `repr` nor any validation exception contains `/srv/research/alice`. Assert probe/guard command builders never read `$HOME`, require `getent passwd "$uid"`, validate one seven-field record plus physical path/owner/writability, quote every bound value, reject NUL/control/over-budget commands, and execute the trusted command only after all guards.

- [ ] **Step 2: Run the new tests and record RED**

```bash
uv run pytest -q tests/deployment/test_remote_home.py
```

Expected: collection fails with `ModuleNotFoundError: openevo.deployment.remote_home`.

- [ ] **Step 3: Implement the sealed authority, strict parser, fixed probe, and guard builder**

Implement `RemoteHomeAuthorityError(ValueError)` and a
`@dataclass(frozen=True, slots=True, repr=False)` named
`RemoteHomeAuthority`. Its fields are `profile_id`, `connection_generation`,
`remote_user`, `uid`, private/repr-hidden `_home`, and private/repr-hidden
`_seal`. It exposes read-only `workspace_root` and `daemon_bundle_root`
properties, `matches(*, profile_id: str, connection_generation: int,
remote_user: str) -> bool`, `require_binding(*, profile_id: str,
connection_generation: int, remote_user: str, workspace_root: str) -> None`,
and the constant representation `RemoteHomeAuthority(<sealed>)`.

Implement the exact module functions
`build_remote_home_probe_command() -> str`,
`parse_remote_home_probe(*, profile_id: str, connection_generation: int,
return_code: int, stdout: bytes, stderr: bytes) -> RemoteHomeAuthority`, and
`build_remote_home_guarded_command(authority: RemoteHomeAuthority,
remote_command: str) -> str`.

Use a module-private seal sentinel. Validate encoded bytes before retaining strings. Use safe components `[A-Za-z0-9._@%+=,-]+`, exact normalized lexical form, 4,096 home bytes, 8,192 aggregate probe bytes, and a bounded unsigned UID. Exceptions are constant and input-free.

The fixed `/bin/sh` probe uses `set -eu`, `set -f`, `LC_ALL=C`, `id -un`, `id -u`, one `getent passwd "$uid"` result, exactly seven colon fields, `test -d`, `test -w`, `stat -c %u`, and `CDPATH= cd -P "$home" && pwd -P`. The guard repeats these checks against shell-quoted sealed values and ends with `exec /bin/sh -c "$remote_command"` only after admission.

- [ ] **Step 4: Run GREEN verification**

```bash
uv run pytest -q tests/deployment/test_remote_home.py
uv run python -c 'from openevo.deployment.remote_home import RemoteHomeAuthority; print(RemoteHomeAuthority.__name__)'
```

- [ ] **Step 5: Commit and push**

```bash
git add src/openevo/deployment/remote_home.py tests/deployment/test_remote_home.py
git commit -m "feat(deployment): add remote home authority"
git push origin HEAD:stable
```

## Task 2: Make system-OpenSSH account discovery private and bounded

**Files:**

- Modify: `desktop/sidecar/system_ssh_session.py`
- Modify: `tests/openevo/sidecar/test_system_ssh_session.py`

- [ ] **Step 1: Write failing tests for unobserved private discovery**

Extend the injected test runner to return controlled byte stdout/stderr/status. Prove that `discover_remote_home_authority` sends exactly the fixed probe through the owned master, binds snapshot profile/generation, and never forwards private stdout or stderr to the lifecycle observer on either injected or production runner paths. Prove the production runner receives `output_observer=None` plus an exact 8 KiB aggregate cap, while ordinary `run()` and follower output stay observed.

Malformed, over-budget, nonzero, and timed-out discovery must raise only:

```python
SystemOpenSshSessionError(
    "ssh_remote_account_unavailable",
    "The remote SSH account could not be verified.",
)
```

The exception representation and chain must contain no raw probe data.

- [ ] **Step 2: Observe RED**

```bash
uv run pytest -q tests/openevo/sidecar/test_system_ssh_session.py -k 'remote_home or private_discovery or output_observer'
```

- [ ] **Step 3: Add per-call bounded capture and discovery**

Change `_run_bounded_subprocess`, `_run_verified_bounded_subprocess`, and `_collect_bounded_process` to accept `max_capture_bytes` with the existing 4 MiB default. Change `_run_session_subprocess` to accept keyword-only `observe_output: bool = True` and `max_capture_bytes`; skip notification on the private call and enforce the limit on injected-runner return bytes too.

Add `SystemOpenSshSession.discover_remote_home_authority(timeout_seconds=30.0)`. It snapshots first, runs `build_remote_home_probe_command()` with observation disabled and the 8 KiB cap, then calls `parse_remote_home_probe` with snapshot profile/generation. Catch parser, timeout, bounded-output, and runner-shape errors and replace them with the exact sanitized session error using `from None`. Keep master startup diagnostics and normal commands on the observed path.

- [ ] **Step 4: Run the full session test file**

```bash
uv run pytest -q tests/openevo/sidecar/test_system_ssh_session.py
```

- [ ] **Step 5: Commit and push**

```bash
git add desktop/sidecar/system_ssh_session.py tests/openevo/sidecar/test_system_ssh_session.py
git commit -m "feat(desktop): privately discover remote account home"
git push origin HEAD:stable
```

## Task 3: Thread one generation-bound authority through lifecycle and follower

**Files:**

- Modify: `desktop/sidecar/remote_lifecycle.py`
- Modify: `desktop/sidecar/system_ssh_session.py`
- Modify: `src/openevo/deployment/profile.py`
- Modify: `tests/openevo/sidecar/test_remote_lifecycle.py`
- Modify: `tests/openevo/sidecar/test_system_ssh_session.py`
- Modify: `tests/openevo/sidecar/test_sidecar_models.py`

- [ ] **Step 1: Write failing lifecycle/follower tests**

Replace the fake session's `id -un` result with `discover_remote_home_authority`. Prove one discovery per generation; exact literal alias remains `config.host`; sealed user and exact custom-home workspace are used; the factory receives `(config, session, authority)`; reconnect gets a fresh authority; mismatch in profile ID/generation/user/workspace rejects construction; representations and errors hide the home; malformed discovery disconnects before transport construction.

- [ ] **Step 2: Observe RED**

```bash
uv run pytest -q tests/openevo/sidecar/test_remote_lifecycle.py \
  tests/openevo/sidecar/test_system_ssh_session.py -k 'home or authority or discovered_remote'
```

- [ ] **Step 3: Replace the free-form user path**

Use:

```python
SystemTransportFactoryV2 = Callable[
    [RemoteProfileConfig, object, RemoteHomeAuthority], _RemoteTransport
]
```

In `_connect_locked`, call private discovery, construct the internal profile with `user=authority.remote_user` and `workspace_root=authority.workspace_root`, and pass the authority. Delete `_SYSTEM_REMOTE_USER` and `_remote_user` from v2.

Make `SystemOpenSshFollowerTransportAuthority` accept the exact authority and require its profile/generation to match `session.snapshot()`. Carry it as a sealed, process-only `remote_home_authority`; `command_argv` wraps trusted commands with `build_remote_home_guarded_command`, while `core_tunnel_argv` remains raw `ssh -W`. Keep initial discovery unguarded/private. Set `RemoteProfileConfig.workspace_root = Field(default=None, repr=False)`.

- [ ] **Step 4: Run complete lifecycle/session/profile regressions**

```bash
uv run pytest -q tests/openevo/sidecar/test_remote_lifecycle.py \
  tests/openevo/sidecar/test_system_ssh_session.py
uv run pytest -q tests/openevo/sidecar/test_sidecar_models.py
```

- [ ] **Step 5: Commit and push**

```bash
git add desktop/sidecar/remote_lifecycle.py desktop/sidecar/system_ssh_session.py \
  src/openevo/deployment/profile.py tests/openevo/sidecar/test_remote_lifecycle.py \
  tests/openevo/sidecar/test_system_ssh_session.py \
  tests/openevo/sidecar/test_sidecar_models.py
git commit -m "feat(desktop): bind remote lifecycle to verified home"
git push origin HEAD:stable
```

## Task 4: Derive transport and Daemon staging roots from the authority

**Files:**

- Modify: `src/openevo/deployment/ssh.py`
- Modify: `src/openevo/deployment/daemon_bundle_transport.py`
- Modify: `tests/openevo/remote/test_ssh_transport.py`
- Modify: `tests/deployment/test_daemon_bundle_transport.py`

- [ ] **Step 1: Write failing rich-transport tests**

Extend the fake follower with a sealed authority. Test root, conventional, and `/srv/research/alice` roots. Reject mismatched profile ID, generation, user, workspace, or follower binding. Prove the legacy non-system transport alone retains `daemon_bundle_service_root_for_user` and cannot become a fallback after system-authority failure. Prove rich commands are guarded and Core tunnel argv remains exact non-shell `ssh -W`.

- [ ] **Step 2: Write failing Daemon-stage tests**

Give the shell harness a controlled `getent`. Execute the real stage script against a controlled custom home and prove first publish plus idempotent reuse. Add root/conventional/custom cases; wrong root suffix; NSS name/UID/home drift; zero/multiple records; physical mismatch; wrong owner; non-writable home; root symlink and inode replacement. Retain every existing size/digest/link-count/mode/cancellation/lock/cleanup test. Require `getent` in the host profile and keep Python/rsync/scp/sudo/package managers absent.

- [ ] **Step 3: Observe RED**

```bash
uv run pytest -q tests/openevo/remote/test_ssh_transport.py \
  tests/deployment/test_daemon_bundle_transport.py \
  -k 'system_openssh or custom_home or nss or service_root or declared_tools'
```

- [ ] **Step 4: Bind `SshRemoteExecutorTransport` to the authority**

Extend the system follower protocol with read-only authority/binding access. For system OpenSSH, validate profile ID, follower session generation, user, and exact explicit workspace against that authority, then use `authority.daemon_bundle_root`. Convert any mismatch to `SshTransportError(INVALID_REQUEST)` without a sensitive chain. Only the explicit legacy branch calls `daemon_bundle_service_root_for_user(profile.user)`.

- [ ] **Step 5: Generalize and harden `_STAGE_SCRIPT`**

Widen `_valid_bundle_root` only to safe absolute homes plus `/.openevo/daemon-bundles`; add `getent` to `DOCKER_USER_CONTAINER_V1.required_commands`. Replace the root-versus-`/home` admission with exact effective user/UID/NSS record, seven-field/single-record, safe lexical home, physical home, owner, writability, and fixed-suffix checks. Create `.openevo`/service root owner-private without following symlinks; require physical equality, owner, `0700`, and stable device/inode around lock, stream, hash, and publication. Preserve hardlink no-overwrite publication and canonical receipt.

- [ ] **Step 6: Run full suites GREEN**

```bash
uv run pytest -q tests/openevo/remote/test_ssh_transport.py
uv run pytest -q tests/deployment/test_daemon_bundle_transport.py
```

- [ ] **Step 7: Commit and push**

```bash
git add src/openevo/deployment/ssh.py src/openevo/deployment/daemon_bundle_transport.py \
  tests/openevo/remote/test_ssh_transport.py tests/deployment/test_daemon_bundle_transport.py
git commit -m "feat(deployment): stage daemon under verified remote home"
git push origin HEAD:stable
```

## Task 5: Project the stable sanitized failure and prove privacy

**Files:**

- Modify: `desktop/sidecar/release_provider_v2.py`
- Modify: `tests/openevo/sidecar/test_release_local_api_v2.py`
- Modify: `tests/openevo/sidecar/test_lifecycle_logs_v2.py`

- [ ] **Step 1: Write failing API and log privacy tests**

Inject the remote-account session error with a message containing user, UID, NSS line, home, and command. Await the persisted operation and assert exactly:

```python
assert operation.failure.code == "ssh_remote_account_unavailable"
assert operation.failure.message == (
    "Desktop could not verify a supported writable remote account home."
)
assert operation.failure.retryable is True
assert operation.failure.recovery == "administrator_action"
```

Assert operation/events/provider persistence/HTTP JSON contain none of the injected data. Feed `/srv/research/alice/.openevo/private-stage` through SSH and Daemon log sources and require `[REDACTED_HOST_PATH]`. Inspect v2 model fields and prove no home/workspace/Daemon path field was added.

- [ ] **Step 2: Observe RED**

```bash
uv run pytest -q tests/openevo/sidecar/test_release_local_api_v2.py \
  tests/openevo/sidecar/test_lifecycle_logs_v2.py \
  -k 'remote_account or custom_home or public_model'
```

- [ ] **Step 3: Add only the closed failure mapping**

```python
"ssh_remote_account_unavailable": (
    "ssh_remote_account_unavailable",
    "Desktop could not verify a supported writable remote account home.",
    True,
    "administrator_action",
),
```

Never project the original exception/message.

- [ ] **Step 4: Run full provider/log suites GREEN**

```bash
uv run pytest -q tests/openevo/sidecar/test_release_local_api_v2.py
uv run pytest -q tests/openevo/sidecar/test_lifecycle_logs_v2.py
```

- [ ] **Step 5: Commit and push**

```bash
git add desktop/sidecar/release_provider_v2.py \
  tests/openevo/sidecar/test_release_local_api_v2.py \
  tests/openevo/sidecar/test_lifecycle_logs_v2.py
git commit -m "fix(desktop): sanitize remote account admission failures"
git push origin HEAD:stable
```

## Task 6: Update architecture, release, and handoff docs

**Files:**

- Modify: `docs/superpowers/specs/2026-07-31-desktop-remote-home-authority-design.md`
- Modify: `docs/architecture/desktop-core-contract-v2.md`
- Modify: `desktop/sidecar/README.md`
- Modify: `docs/architecture/openevo-desktop-release.md`
- Modify: `docs/maintainer/macos-desktop-development-handoff.md`

- [ ] **Step 1: Mark the design approved**

Set its status to `approved 2026-08-01`; do not claim implementation/release completion there.

- [ ] **Step 2: Document the implemented boundary**

Cover literal alias/system OpenSSH final authority, private generation-bound NSS discovery, safe writable custom homes and fixed suffixes, no public/persisted/logged path, guarded rich commands, independent Daemon-stage validation, raw `ssh -W` tunnel distinction, no v2 username fallback, and the stable failure.

- [ ] **Step 3: Update release/handoff evidence conservatively**

Record the reproduced real-E2E defect, fix commit, exact verification commands/results, and mandatory full source-bound rebuild. Keep GitHub publication/payment state separate; never describe an unproven candidate as released.

- [ ] **Step 4: Check docs for drift**

```bash
rg -n '/home/<user>|id -un|workspace_root|remote home|ssh_remote_account_unavailable' \
  docs/architecture desktop/sidecar/README.md docs/maintainer/macos-desktop-development-handoff.md
rg -n 'OPENEVOLVE_|/openevolve/|openevolve\.' \
  docs/architecture desktop/sidecar/README.md docs/maintainer/macos-desktop-development-handoff.md
```

- [ ] **Step 5: Commit and push**

```bash
git add -f docs/superpowers/specs/2026-07-31-desktop-remote-home-authority-design.md
git add docs/architecture/desktop-core-contract-v2.md desktop/sidecar/README.md \
  docs/architecture/openevo-desktop-release.md \
  docs/maintainer/macos-desktop-development-handoff.md
git commit -m "docs: document verified remote home authority"
git push origin HEAD:stable
```

## Task 7: Verify and review the complete source change

**Files:** Review all files changed since `5700618498fe00e97614643270ba038a4ec001ee`.

- [ ] **Step 1: Run repository-prescribed formatting/lint/type checks**

Discover canonical commands from `pyproject.toml`, `desktop/package.json`, and workflows. At minimum run relevant Ruff checks without rewriting unrelated files.

- [ ] **Step 2: Run focused suites together**

```bash
uv run pytest -q tests/deployment/test_remote_home.py \
  tests/deployment/test_daemon_bundle_transport.py \
  tests/openevo/remote/test_ssh_transport.py \
  tests/openevo/sidecar/test_system_ssh_session.py \
  tests/openevo/sidecar/test_remote_lifecycle.py \
  tests/openevo/sidecar/test_release_local_api_v2.py \
  tests/openevo/sidecar/test_lifecycle_logs_v2.py
```

- [ ] **Step 3: Run full relevant regressions**

Run all of `tests/deployment`, `tests/openevo/sidecar`, and `tests/openevo/remote`, then the release-selected full Python suite from the canonical release document/CI.

- [ ] **Step 4: Review authority/privacy diff**

```bash
git diff --check 5700618498fe00e97614643270ba038a4ec001ee..HEAD
git diff --stat 5700618498fe00e97614643270ba038a4ec001ee..HEAD
git diff 5700618498fe00e97614643270ba038a4ec001ee..HEAD -- \
  src/openevo/deployment desktop/sidecar tests docs
```

Verify no v2 username-home derivation; authority is neither directly constructed nor serialized; discovery is unobserved on both runner paths; rich commands are guarded; Core tunnel is still `ssh -W`; legacy fallback is isolated; exceptions/reprs are path-free; shell values are quoted; budgets precede decoding.

- [ ] **Step 5: Sync and prove stable identity**

```bash
git status --short
git push origin HEAD:stable
git fetch origin stable
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/stable)"
```

## Task 8: Rebuild and validate every source-bound v0.1.10 artifact

**Files:** Generated outside tracked source per `docs/architecture/openevo-desktop-release.md`; evidence in `docs/maintainer/macos-desktop-development-handoff.md`.

- [ ] **Step 1: Build from the exact final source commit**

Use existing release scripts to rebuild in order: wheel, verified framework lock/inventory, Linux x86_64 PyInstaller Daemon bundle, managed-runtime composition, Desktop resources, app, candidate manifest, and DMG. Use a fresh commit-keyed output directory; never reuse `v0.1.10-215a7403` as evidence.

- [ ] **Step 2: Validate exact identity binding**

Run the canonical candidate validator and independently hash every input. Prove source commit equality, Daemon/framework/wheel binding, and byte-identical embedded/external candidate manifests.

- [ ] **Step 3: Run all local release gates**

Run renderer tests/build, Tauri/Rust tests, packaged Playwright (all expected pass), DMG attach/copy/LaunchServices/local-API/quit/detach smokes, and direct exact-app launch. Capture sanitized outcomes and digests.

- [ ] **Step 4: Install the exact validated app**

Quit the old app, replace `/Applications/OpenEvo Desktop.app` using a recoverable temporary copy/rename, register and launch it, and prove its executable plus embedded manifest match the candidate. This replacement is user-authorized.

- [ ] **Step 5: Commit evidence without breaking source binding**

If evidence changes the source commit bound by the release, rebuild from that commit. Do not call an earlier artifact final unless a documented cryptographic evidence-only policy permits it.

## Task 9: Complete the real custom-home macOS-to-Ubuntu E2E

**Files:** Temporary isolated SSH/account resources; sanitized evidence in `docs/maintainer/macos-desktop-development-handoff.md`.

- [ ] **Step 1: Read-only host baseline**

Via the literal existing alias verify Linux x86_64, Docker, capacity, `getent`, required tools, and a safe custom-home prefix. Do not modify the original root OpenEvo service (historically PID 2886736).

- [ ] **Step 2: Provision one isolated temporary account**

Create a unique no-sudo test account with NSS home outside `/home`, e.g. a fresh `/EvoLab/<opaque-id>`. Install only scoped authorized key material. Add a literal local alias through a dedicated private SSH config include.

- [ ] **Step 3: Prove system OpenSSH authority, then connect through Desktop**

Verify effective user/UID/NSS and physical home/owner/writability with `/usr/bin/ssh <alias>`. Select that alias from Desktop's `~/.ssh/config` host list; never enter IP/user/path in Desktop.

- [ ] **Step 4: Complete the product flow**

Connect/bootstrap/negotiate; create a project with an idempotency key while observing progress/logs; run one real science task to terminal success; verify outputs/evolution/Project Head semantics and no duplicate mutation; quit/relaunch/reconnect and prove rediscovery plus state resume. After negotiation, business operations must use tunneled `/v2/*`, never SSH fallback.

- [ ] **Step 5: Prove placement and privacy**

Verify workspace/Daemon roots only under exact NSS home with private modes. Inspect provider persistence/events/logs/crash/evidence for raw home/user/UID: none may remain. Confirm original root service is unchanged.

- [ ] **Step 6: Clean up exact isolated resources**

Remove only the temporary alias/include, credential copy, account, custom home, and its service after resolving exact targets. Verify cleanup and original service health.

- [ ] **Step 7: Record sanitized evidence and maintain source binding**

Commit source/candidate/app/DMG identities, test counts, E2E and cleanup outcomes; rebuild again if the release contract binds the evidence commit.

## Task 10: Publish v0.1.10 without weakening release gates

- [ ] **Step 1: Run final clean-tree/candidate verification**

Re-run decisive candidate, packaged, DMG, source-equality, diff, and clean-status checks. Require `origin/stable` to equal the candidate source.

- [ ] **Step 2: Tag and release using the canonical CLI-first path**

Upload only exact validated artifacts/manifests/checksums. Do not alter repository visibility or bypass provenance/digest/release-mode gates. If GitHub Actions remains unavailable due account spending limits, use the authorized local release path only when the repository contract accepts it; otherwise record that single external publication blocker after local correctness/evidence are complete.

- [ ] **Step 3: Verify release state through `git` and `gh`**

Verify tag target, version, draft/prerelease flags, asset names/sizes/digests, candidate source, downloadable DMG checksum, and final `stable` identity.

- [ ] **Step 4: Close #265 only on proof**

Post sanitized evidence and close #265 after acceptance. Update #220 with proven facts and leave it open for any broader lifecycle coverage still unresolved.

- [ ] **Step 5: Apply verification-before-completion**

Rerun decisive commands immediately before marking the active goal complete. Report final source commit, release/tag URL, DMG digest, installed-app identity, tests, real E2E, and any genuinely unavoidable external publication blocker.
