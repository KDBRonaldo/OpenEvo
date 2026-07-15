# Runtime Backends

`openevo.runtime` gives each rollout session its own **sandbox** — one container
(Docker or Apptainer) that lives for the whole session. The gateway uses it to
run the prepare recipe, execute the agent and evaluator commands, move files in
and out, then tear it down.

## Mental model

- **One `RuntimeSpec` → one container**, shared across the init → run → eval
  stages of a session.
- The host session directory is **bind-mounted** to a fixed in-container path,
  `/openevo/session` (`RUNTIME_SESSION_DIR`). Uploads/downloads under that path
  use descriptor-relative host-side copies. Docker and Apptainer retain their
  normal `docker cp` or tar fallback for paths outside that bind.
- Commands run in a login shell (`bash -lc`) with working directory
  `cwd or spec.workdir or /openevo/session`.
- The factory verifies the chosen backend actually supports what the spec asks
  for (GPUs, CPU/memory limits, internet-off) before building it.

## Main files

- `models.py`: `RuntimeSpec`, `PrepareAction`, `ExecInput`, `ExecResult`.
- `base.py`: the `BaseRuntime` contract, the `/openevo/session` path constants, and
  the bind-mount copy helpers.
- `docker.py`: `DockerRuntime` — the default backend.
- `apptainer.py`: `ApptainerRuntime` — daemonless, for clusters.
- `factory.py`: backend lookup + capability validation; also loads a custom
  backend via `RuntimeSpec.import_path`.

## The contract

A backend implements `start`, `stop`, `exec`, `upload_file`, `upload_dir`,
`download_file(remote_path, local_path) -> None`, and
`download_dir(remote_path, local_path) -> None` (plus `cancel`), hiding
container details from harnesses and evaluators. The two download methods remain
abstract backend APIs, and `RuntimeSpec.import_path` plugins may implement or
override them. Well-known in-container paths (from `base.py`) are
`/openevo/session` and, under it, `artifacts/`, `logs/`, `logs/agent/`,
`logs/eval/`, and `eval_artifacts/`. Those container-visible log paths are
agent/runtime workspace paths. Gateway-owned step stdout/stderr is persisted in
a separate unmounted Core log authority and is not written through this bind.

## Prepare recipe

`RuntimeSpec.prepare` and `RuntimeSpec.eval_prepare` are ordered lists of
`PrepareAction` steps:

- `upload_file`: copy one host file in.
- `upload_dir`: copy one host directory in.
- `exec`: run a command inside the container.

`prepare` runs before the agent. `eval_prepare` runs before evaluation — and if
it's omitted, the eval runtime simply replays `prepare`.

Upload targets must be canonical absolute descendants of `/openevo/session`.
Gateway admission checks every main/evaluator prepare target before runtime
construction. Bind copies then pin the absolute session root, open every source
and target component relative to held directory descriptors with `O_NOFOLLOW`,
reject links and special-file leaves, and recheck root, ancestor, leaf, owner,
link count, and inode bindings after transfer. A `..` component, symlink, or
concurrent directory replacement fails closed without falling back to a
pathname copy.

## Private session-bind receipt readback

The public download API is not the receipt authority. For the product's
canonical `/openevo/session/evolution` target on a supported Linux Core runtime,
Gateway may call a separate module-private session-bind primitive that bypasses
backend download hooks. That primitive shares a non-refundable closed budget
across the evolution tree and agent-system target inventory: at most 4096
files, 64 MiB of streamed file bytes, and 16384 source-node enumeration
attempts. The node limit allows two complete ordered enumerations of a
maximally nested 4096-file tree; failed enumeration, open, copy, hash, and
remote target attempts retain the resources already consumed. Limits may be
reduced for tests or narrower consumers but cannot be raised above this
contract.

The source path must resolve below the held `/openevo/session` bind. Core pins
every ancestor and traverses directories relative to no-follow FDs. Each source
directory is bound to device/inode, ownership, mode, link count, size, mtime,
ctime, and a Linux inotify mutation generation. Core records sorted entries,
streams each single-link regular file in bounded chunks while hashing and
charging bytes, then re-enumerates every directory and compares the complete
ordered entry identities. Late additions, removals, rename or replace/restore
ABA, in-place writes, growth, links, and special files fail the whole readback.
A sparse or declared-large file is rejected against remaining capacity before
any host payload file is created.

Host output is built below a random `0700` staging directory with `0600` files,
fsynced, rechecked by held descriptors, and published with Linux
`renameat2(RENAME_NOREPLACE)`. Normal failure removes the identity-bound partial
staging tree; a raced destination replacement is never overwritten or removed.
The synchronous FD walk runs in a controlled worker thread. Timeout or task
cancellation signals that worker and waits for bounded cleanup before returning
to the event loop.

Custom target directories, non-Linux Core hosts, and third-party runtimes keep
the ordinary backend download behavior. Gateway ignores any backend return
metadata and applies Core's bounded `ArtifactPayloadService` verification to the
downloaded tree before constructing a receipt. This compatibility path does not
claim the stronger source-tree mutation evidence of the private Linux bind walk.

## Docker vs Apptainer

Docker is the default for local examples and supports `--cpus` / `--memory`
limits. Apptainer is daemonless (good for clusters that forbid the Docker
socket), uses a host-backed overlay, and exposes GPUs with `--nv`. Both
bind-mount the session directory and run commands via `bash -lc`, so harnesses
and evaluators behave the same on either.

## Container user policy

`RuntimeSpec.container_user` is a closed `image | host` choice. `image` keeps
the image's declared user and is the default for benchmark automation, custom
images, and existing experiment configs. Docker `host` starts the container
with the Core process UID/GID and therefore keeps the bind-mounted session
writable without recursively widening host file permissions. Because `host` is
a material runtime override, experiment config rejects it when no explicit
runtime image is present; it is never dropped while falling back to the rollout
node default runtime.

OpenEvo-managed Science profiles use `host`; user-supplied custom images keep
`image`. Every `managed_science` runtime, including subscription and
self-deployed execution, is bound to Docker and the profile's exact
Core-managed image alias at experiment config, `RuntimeSpec`, launcher, and
Gateway admission boundaries. The release contract separately binds that alias
to a full trusted `sha256` digest; the tag is never an identity proof. Bootstrap
pulls the immutable `repository@digest` reference and verifies Docker
`RepoDigests`/image ID. Immediately before `docker create`, DockerRuntime repeats
that inspection and creates from the matched immutable reference, before adding
any subscription credential volume. A missing digest, tag drift, empty
`RepoDigests` with a different image ID, or malformed inspect response fails
closed. Custom runtime loaders/options are forbidden for that profile. It is not
a general compatibility
promise that arbitrary images can run under a replaced user identity. A custom
image, loader, option/volume, entrypoint, image-user runtime, or non-literal
transcript capture mode is rejected before credential bytes are staged.

Managed Science fixes `HOME=/openevo/session/home` and a closed `PATH` beginning
with `/home/openevo/.local/bin`, where the managed image installs the pinned
Codex binary. Subscription execution invokes
`/home/openevo/.local/bin/codex` directly, so a workspace executable cannot
shadow it. Core also fixes `CODEX_HOME=/openevo/credentials/codex`, a separate
private bind mount outside `/openevo/session`. Caller `agent.env`, `runtime.env`,
and prepare-action env cannot override any of these three values.

Host-user startup, upload, stop, and failed-start cleanup never invoke the
legacy recursive `a+rwX` compatibility path. Gateway teardown instead pins the
session root device/inode/owner at dispatch, restores only owner directory
permissions through stable descriptors, and removes a bounded no-follow tree.
An owner or identity mismatch fails closed rather than acting on a replacement
path.

Local Docker/Apptainer command capture drains stdout and stderr concurrently
into fixed-size buffers. Timeout termination preserves bytes already captured
instead of returning empty output, while still bounding final pipe drain time.

Docker gives every create attempt a node-scoped Core-private authority root that
is outside both the agent session bind and its nested evaluator bind. Each
runtime has a unique `0600` lock/cidfile pair; the lock is created exclusively
and held with a process-exclusive lock. Docker may initially create the cidfile
with a non-executable `0666 & ~umask` mode. Core opens that inode no-follow
relative to the held authority root, verifies its owner, link count, type, and
identity, tightens it through the open descriptor to `0600`, and revalidates
the descriptor and directory entry before reading any bytes. Unexpected mode
bits, a replacement, or a final mode other than exact `0600` fail closed.
Normal return, cancellation, timeout, and command exceptions all reconcile the
private cidfile after the Docker CLI has stopped. An explicit create failure
with no cidfile establishes no ownership, so stop and cancel perform no
operation against the diagnostic container name. If a full 64-character
container ID was written, Core retains it as a candidate and requires
`docker container inspect --format {{.Id}} <id>` to agree before any operation.
Unreadable or unverified creates retain their ownership files without using
stdout, a name, or a partial ID as fallback.

Release bootstrap never builds a managed image on the target host. The explicit
development mode may fall back to the Core-owned Dockerfile, but both base
images are digest-pinned and the built image must still match the release
contract digest before use. Ordinary Desktop users select a managed profile and
never supply either the image alias or digest.

Gateway startup scans only complete private lock/cidfile pairs, acquires the
abandoned lock, and performs bounded recovery against the exact ID. A lock held
by another live Core process or an incomplete/tampered record fails closed.
Every stop attempt revalidates the ID and, after `rm -f`, marks the runtime
destroyed only when a final inspect proves that exact ID absent. Gateway does
not remove either bind root until that proof succeeds; cleanup ownership remains
in the private authority root and durable retry journal otherwise. Subscription
post-run uses bounded immediate retries and then periodic reconciliation. Its
private v7 journal also carries a monotonic revision, the exact staged auth identity, an explicit recovery phase, redacted
finalization state, and a canonical result digest with monotonic export/callback
success proofs. The terminal agent result is journaled before its in-memory
terminal transition. Cancellation uses the same rule: its authority is fsynced
before `runtime.cancel()` or any stop side effect, and a persistence failure
leaves the runtime running and fails the request closed. Required export authority includes the normalized backend
URL, timeout, fail-open policy, and canonical identity digest; recovery requires
the current config/client to match it exactly. Evolution export is idempotent by
stable source event identity; callback retries carry a stable result-derived
idempotency key. A restarted Gateway skips a durably successful phase, retries
an unknown/failed phase, and removes storage and owned roots only after both
required phases are fsynced successful. Missing phase/finalization authority or
export config drift retains the journal and all transcript/session roots. An
incomplete journal update also retains a durable pending marker and blocks
restart cleanup until the exact transition is successfully retried. Live terminal
status/error authority changes only after that durable transition succeeds.
Every record read, pending/candidate/rollback write, replace, unlink, and
directory fsync in a transition is relative to one no-follow held journal-root
descriptor and runs while holding a bounded cross-process `flock` on a persistent
owner-only, link-count-one lock inode in that root. The lock inode and root
binding are revalidated before a transition can return success. A root pathname rename or replacement therefore fails closed while
retaining the displaced authority and never writes journal bytes into the
replacement.
Inside the lock, writers reread the durable record and require an exact revision
compare-and-swap plus monotonic phase and terminal-delivery proof transitions;
stale candidates fail without replacing the authority.
An immutable marker in the journal's private parent binds its normalized path,
no-follow ancestor identity chain, and root inode. Startup rejects a missing or
replaced bound root and any symlinked ancestor. It also completes row, filename,
metadata, per-file, and aggregate-byte preflight for the directory before reading
the first journal record. The empty root and marker remain after the last record
is cleared so a later session cannot silently establish a different authority.

Subscription auth bytes are validated before `DockerRuntime` is created. Core
uses a random `0700` staging child inside the already journaled private
credential root, verifies the complete source and staged file identities,
digest, bounded JSON, and redactor, then publishes the `0600` inode with Linux
atomic no-replace rename. As soon as the empty staging inode exists, Core durably
journals its device/inode identity before copying any secret bytes; publication
then durably replaces that record with the final full identity. Thus a crash on
either side of rename never leaves an unbound secret-bearing inode.

`ManagedCredentialMount` binds the root and auth-file identities. DockerRuntime
creates and pins a separate empty home-view directory and empty `auth.json`
placeholder under that root. Because a daemon cannot reliably see the client's
PID namespace, Docker receives two daemon-visible absolute sources: the empty
view is mounted read-only at `CODEX_HOME`, and the exact auth file, which is a
sibling rather than a member of that view, is mounted read-only over its
placeholder. This prevents a host rename of the auth source from moving the
container mountpoint and exposing a replacement. Runtime holds and revalidates
root, view, placeholder, and exact-auth descriptors through container absence,
fixes `restart=no`, verifies both create-time mount records, and compares the
adopted view/auth identities inside the stable exact container before any prepare
or agent command.

After terminal delivery, Core performs a separate bounded no-follow recursive
scan before ordinary tree deletion. It locates the journal-bound auth by stable
device/inode even after a nested rename, truncates and fsyncs that inode, and
fails without deleting the root if the inode cannot be located or the scan
budget is exhausted. Historical cleanup authority without an auth identity
scrubs every owned regular file in the dedicated credential root before deletion.
That recovery rule does not make a replacement redaction authority:
credential-bearing v5 terminal finalization still fails closed. A publication
race, symlink, owner/mode/link mismatch, or unavailable rename primitive fails
before agent execution.
Credential redaction writes only to the node-private log authority. Workspace
and artifact files are never redaction write targets. If a private captured log
exceeds a per-file or aggregate scan budget, finalization fails explicitly and
all original bytes remain unchanged; Core does not substitute content in the
workspace or artifact tree.
