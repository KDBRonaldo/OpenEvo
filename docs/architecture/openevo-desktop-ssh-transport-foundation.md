# Release-incompatible foundation note

This document preserves pre-External-Beta foundation context. It is not External Beta release behavior.
Direct run commands, dry-run transports,
developer override env vars, legacy token headers, package-relative Core
artifacts, and command-based service facades are superseded by
`docs/maintainer/productization/spec.md`.

# OpenEvo Desktop SSH Transport Foundation

Tracked by #43 and hardened for the provider-owned host-key boundary in #163.

This document describes the first concrete transport behind the OpenEvo Desktop
sidecar executor contract. It lets the local Desktop sidecar run remote
preflight commands and prepare task workspaces on a remote GPU server through
system OpenSSH and rsync.

This is still not the full remote backend. It does not start a long-running
remote daemon, manage Docker Compose, manage vLLM, or perform Desktop UI
orchestration. Remote dependency installation is limited to the bootstrap
layer's user-site Python package checks.

## 0.1.9 System OpenSSH Replacement

The foundation below is frozen 0.1.8 history. The 0.1.9 release replaces its
explicit host/user/port, private known-host store, isolated agent relay, and
authentication selector with the contract in
`desktop-core-contract-v2.md`.

The new connectable profile contains only a user-facing name and a literal
OpenSSH alias. The actual connection is `/usr/bin/ssh <alias>`. System OpenSSH
is authoritative for HostName, User, Port, Include, Match, IdentityFile, agent
and Keychain integration, password/passphrase prompts, ProxyJump, ProxyCommand,
canonicalization, and known-host policy.

The 0.1.9 transport must not use `-F /dev/null`, `-p`, `-l`, `-i`,
`IdentityFile=none`, `IdentitiesOnly`, or an OpenEvo `UserKnownHostsFile`. It may
override only reviewed process ownership, private multiplexing, unrequested
forward/command/TTY suppression, intentional Core tunnel forwarding,
keepalive, and deadlines. The separately inventoried native askpass helper
returns secret input directly to the owning OpenSSH process; OpenEvo does not
persist that input or receive it through React/Local API.

A bounded lexical parser lists literal configured hosts as hints but never
executes config commands. Selection persists the alias, not flattened
effective values. First-host and changed-key behavior uses the user's OpenSSH
trust policy, with explicit changed-key review and only provably unambiguous
`ssh-keygen` repair.

The v0.1.10 release authority further removes the system-OpenSSH rsync follower
entirely. Release installation assets stream through the owned SSH command's
stdin, and legacy rsync-based transfer methods are unavailable to that
authority. References to rsync below remain frozen v0.1.8 foundation history,
not a Desktop release dependency.

## Frozen 0.1.8 Foundation

The remaining sections document the explicit transport retained only for v1
tests and read-only migration context. They are not a 0.1.9 release fallback.

## Transport Contract

Desktop chooses between dry-run and SSH transports through the sidecar API. The
transport contract is covered by focused tests:

```bash
PYTHONPATH=src:. python -m pytest tests/openevo/remote/test_ssh_transport.py -q
```

`dry-run` remains the default. It never opens network connections and is useful
for Desktop integration tests, config validation, and local report rendering.

`ssh` opts into real remote access. It uses the same sidecar execution report
shape as dry-run, including nested `preflight.ready`, `workspace.ready`, and
top-level `ready`.

## Transport Boundary

`SshRemoteExecutorTransport` implements the existing `RemoteExecutorTransport`
protocol:

```python
class RemoteExecutorTransport(Protocol):
    def run(command, *, cwd=None, env=None, timeout_seconds=30.0) -> RemoteCommandResult: ...
    def upload_dir(local_path, remote_path) -> None: ...
```

The implementation is subprocess-backed and uses argv lists, not local shell
strings. This keeps local command construction testable and avoids local shell
injection risks.

Remote commands still execute through the remote user's shell because OpenSSH
receives a remote command string. The transport owns the remote shell quoting for
environment variables, `cwd`, and rsync-created remote paths.

## External Requirements

The local machine running OpenEvo Desktop must have:

- the platform-fixed `/usr/bin/ssh` binary;
- the platform-fixed `/usr/bin/ssh-keyscan` binary for the controlled host-key
  probe;
- the platform-fixed `/usr/bin/rsync` binary for local-folder workspace uploads;
- network access from the local machine to the remote SSH server.

Release execution never searches `PATH`. Each binary must be a root-owned,
link-count-one regular executable with no group/world write bit and an unchanged
held pathname/vnode identity.

Remote `host` values support DNS names, IPv4 addresses, and IPv6 literals. IPv6
rsync destinations use the required bracketed form. Host values that can be
interpreted as local option or destination injection are rejected before any
subprocess starts.

Desktop does not read or modify the user's `~/.ssh/known_hosts`. It also disables
global known-host lookup for every SSH subprocess.

## Provider-Owned Host-Key Trust

`ProviderKnownHostStore` owns enrollment and persistence. Its root must be a
direct child of a sidecar-owned secure ancestor. The ancestor must be an
owner-controlled, non-symlink directory that is not group/world writable; the
store root must be an owner-controlled, non-symlink directory with mode `0700`.
The store holds no-follow ancestor/root descriptors. Its owner-only root also
contains the link-count-one `0600` cross-process lock file, so independent store
instances use one verifiable lock namespace rather than per-instance lock paths.
Every operation rechecks root and lock pathname-to-descriptor identity and uses
a shared/exclusive `flock`. Both shared and exclusive acquisition use monotonic,
nonblocking retry with the same short bounded timeout and return typed
`host_key_in_use` instead of waiting indefinitely. Each profile
is mapped to an opaque SHA-256 filename, and each known-host file must be a
link-count-one, owner-controlled regular file with mode `0600`. Reads are
descriptor-relative and no-follow. Publication writes and fsyncs a private
temporary file, publishes it with an atomic no-replace hard link, removes the
temporary name, and fsyncs the directory. Existing records are immutable unless
an explicit compare-and-swap rotation succeeds. Store paths reject whitespace,
quoting, backslashes, and OpenSSH `%` token expansion.

Each operation lock is owned by an explicit, single-use authority rather than a
generator frame. The authority records the root and lock inode bindings, lock
state, and exact operation FD in a bounded process registry. Unlock, close, and
the `fstat` used to prove close are independently retryable; the registry entry
is removed only after that FD is proven closed. The permanent lock-inode anchor
uses a separate open-file description, so closing an operation FD still releases
its `flock` if an explicit unlock reports failure. A retained shared-lock
authority continues to block rotation after repeated cleanup faults, but a later
operation retries it and allows rotation to proceed once cleanup recovers.

The threat model is an owner-only trust root plus advisory locking among
cooperating Desktop processes. This prevents non-owner modification and avoids
accidental or concurrent mutation by cooperating processes. It does not claim
that pathname or inode checks can stop a malicious process running under the
same UID. Such a process can inspect process memory, replace owner files, or
interfere with subprocesses; that means the Desktop account is already
compromised and is outside this boundary.

OpenSSH accepts a pathname rather than an already-verified file descriptor. For
each command, rsync, or tunnel spawn, Core therefore copies the validated record
to a random `0700` lease directory directly under the held secure ancestor. The
lease file is `0600`, is independent of the mutable trust-store root, and is
created while the shared store lock is held. Synchronous command/rsync leases
remain present until complete process-group cleanup and leader reap; tunnel
leases remain present until the tunnel closes. Lease construction and
synchronous runner exit handle every `BaseException`. Before directory creation,
the lease reserves one of 64 process-local recovery slots. Its random directory
name, pinned directory FD/inode, and held shared store lock remain in that slot
until no-follow pathname revalidation, file unlink, directory and parent fsync,
`rmdir`, and FD close all complete. A partial publication or transient
`fstat`/unlink/`rmdir` failure is retried synchronously by later lease creation;
capacity exhaustion rejects a new lease before filesystem publication. The
caller retains lease cleanup
responsibility through runner entry and transfers it only after the process
authority exists. Secret prepare/finalize/discard and runtime-preflight commands
use the same handoff as ordinary commands and rsync. Replacing or renaming the
trust-store root after validation cannot redirect that in-flight pathname to a
different key.

A stored record contains both canonical metadata and an OpenSSH known-host line.
The metadata binds all of:

- remote profile ID;
- exact host and port;
- selected host-key algorithm;
- full public key, including key type and encoded key blob;
- independently computed `SHA256:` fingerprint.

The fingerprint is not a substitute for the public key. Loading a binding
recomputes the fingerprint from the stored key and requires the metadata and
OpenSSH line to be byte-canonical.

Enrollment is explicitly two phase:

1. `probe(profile)` runs `ssh-keyscan` with an argv list and a closed request set
   of Ed25519, NIST P-256 ECDSA, and RSA keys. It accepts only exact host/port
   lines and rejects markers, hashed hosts, host lists, duplicate algorithms,
   comments, extra fields, malformed key blobs, and unsupported algorithms.
2. The caller independently verifies and submits an exact algorithm and
   fingerprint from the pending result. `confirm(...)` requires the current
   profile ID/host/port to match, repeats the probe, and requires the complete
   candidate set to be unchanged before it persists the selected full key.

The second `ssh-keyscan` only detects a change between the two observations. It
does not authenticate the server, because an active attacker can answer both
probes consistently. A TOFU enrollment must display the fingerprint for
independent verification through an administrator, provider console, or another
authenticated channel before confirmation.

RSA known-host records use the OpenSSH public-key type `ssh-rsa`; the transport
pins `rsa-sha2-512` as the corresponding host-key signature algorithm. Probe
results reject RSA moduli smaller than 2048 bits and never mutate trust state,
so first contact is not silent TOFU.

Persisted trust is loaded only with an expected fingerprint retained by the
profile owner. Recreating the same profile ID/host/port without that expected
fingerprint does not inherit the old binding. `revoke(profile,
expected_fingerprint=...)` is an atomic compare-and-swap operation.
`rotate_from_pending(...)` is the only rotation entry: the store verifies its
private pending seal and canonical digest, exact profile/store identity and
candidate, repeats the complete probe, then checks the old fingerprint under the
exclusive lock before replacement. There is no caller-constructible confirmed
pending capability. Rotation fully rereads and canonical-validates its private
temporary record before `os.replace`; successful return from `os.replace` is the
irreversible commit point. Any directory fsync, authoritative reread, or
canonical-validation failure after that point, including shared context-manager
unlock or lock-fd close failure, returns sanitized typed
`host_key_rotation_indeterminate`. Lock cleanup always makes a best-effort
unlock and fd close. The typed error identifies the confirmed candidate
fingerprint as the authoritative reload key; callers must reload with that
fingerprint and must not assume that old trust remains installed. Cleanup
failure before `os.replace` remains an ordinary fail-closed operation failure.

Before revoke or rotation attempts the exclusive lock, the store requests closure
of matching tunnels registered in the current process. `SshTunnel` is a context
manager with idempotent bounded `close()` and a daemon exit monitor that releases
the trust lease when SSH exits independently. A tunnel in another process must be
closed by that owner; otherwise mutation fails with `host_key_in_use` after the
bounded lock timeout. Synchronous commands intentionally keep their shared lease
until completion. Revoking the store record is not a general remote-session
termination mechanism. Registration and exit-monitor startup are one constructor
transaction. Before either the general forward or a Core connection child can
call `Popen`, it owns a bounded registry slot, birth-record FD, and independent
session/process group. A failure after process creation performs bounded
whole-group TERM/observe/KILL cleanup, but unregisters the closer and releases
the trust lease only after every group member is dead or zombie, the leader is
reaped, and a second bounded process-table scan proves that the PGID has
disappeared.
If cleanup cannot confirm exit, the same process authority and tunnel quarantine
retain the registration and lease while a daemon monitor retries. Matching
trust mutation also retries quarantined cleanup when constructor registration
succeeded; later
tunnel creation retries every quarantined entry. Finalization is idempotent, so
concurrent recovery paths cannot unregister or close the lease twice. Until one
path proves exit, revoke and rotation remain blocked by the shared trust lease
instead of leaving an unmanaged child behind.

The same rule covers the general `-L` forward readiness probe. Timeout,
cancellation, and every other `BaseException` close the forward before returning
or re-raising. A terminate/reap or lease-release failure keeps the exact tunnel
in quarantine for later close/recovery rather than losing forwarding or trust
authority.

Authenticated Core Control traffic uses parent-created connection endpoints
rather than the general developer TCP forward or an OpenSSH-created streamlocal
pathname. For each HTTP connection the Desktop process creates an anonymous
`AF_UNIX`/`SOCK_STREAM` socketpair and validates the declared and kernel socket
type, effective UID, empty local/peer names, and the initial identity of both
held FDs. It revalidates both identities after child creation and the parent
identity again before returning the HTTP endpoint. It retains the HTTP side and
transfers only the peer FD plus the private birth-record FD through the exact
`pass_fds` set to a dedicated
`ssh -W 127.0.0.1:<remote-port>` child as stdin/stdout. The child uses the same
pinned known-host lease and explicit auth argv as command execution.

Anonymous socketpair endpoints have no filesystem pathname contract. In
particular, the transport does not call `fchmod` and does not treat permission
bits or link count as an authority boundary; Darwin may reject `fchmod` on these
FDs with `EINVAL`, and those filesystem fields do not establish socket
ownership. A changed FD identity, socket type, owner, or anonymous address fails
the connection and triggers bounded child cleanup. An exception from the child
`poll()` authority probe is also a connection failure; it can never be treated
as proof that the child is running.

There is no `-L` listener, `-S` control socket, control master, filesystem
pathname, hard-link pre-pin window, or temporary TCP-port reservation in this
Core path. Every forwarding connection therefore has one explicit SSH child
authority that is checked before returning the local socket and again after
bearer-authenticated response reads. A nonzero child exit is a service failure;
an SSH operation timeout remains a retryable deadline failure across the Core
bootstrap layer.

Construction transfers the trust lease to the tunnel owner before registration
or child creation can fail. `BaseException`, including cancellation, closes
untransferred socket FDs and is re-raised unchanged. Close performs bounded
terminate/wait/kill for every owned child. If exit cannot be proven, a
process-local quarantine retains the child and trust lease for retry, so trust
rotation cannot leave an unowned forwarding authority. Any connection setup or
authority failure permanently marks that Core endpoint closing and registers
quarantine ownership while still holding its state lock. Only then are the two
socket endpoints closed independently and bounded child cleanup attempted, so an
`EBADF` or another close failure cannot hide the original typed failure or skip
process cleanup. A concurrent open observes the poison before cleanup completes
and cannot create another child generation. The endpoint finalizes immediately
when every owned process group is confirmed terminated and its leader reaped;
otherwise quarantine retains it for later retry. A production child authority
is inserted into the endpoint's single pending-child ownership slot before
`Popen`; injected test starters are wrapped in that state immediately on return.
Cancellation and registry insertion failure therefore poison the endpoint while
retaining the exact child authority. Close
deduplicates pending and registered references by object identity, and neither
unregisters the closer nor releases the trust lease until bounded cleanup proves
that every such child exited.

`SshRemoteExecutorTransport` requires a `TrustedKnownHostsBinding` and revalidates
it before building every command. A release call without a binding fails closed.
This slice deliberately does not add a UI, native host route, contract DTO, or
Core API for confirmation; those callers must be wired separately before release
SSH actions can proceed.

## Supported Auth Modes

Supported:

- `ssh_agent`: uses only the explicitly isolated OpenSSH agent path without
  loading user SSH configuration or default identity files.
- `private_key`: passes `-i <private_key_path>` as a subprocess argv element,
  clears default identities with `IdentityFile=none`, sets `IdentitiesOnly=yes`,
  and disables agent use with `IdentityAgent=none`.

`password_ref` and `passphrase_ref` fail closed. The Desktop release never
constructs the generic `private_key` pathname mode: it accepts only `ssh_agent`.
Native password/private-key/passphrase modes, askpass, private agents, and
`ssh-add` are absent from the packaged composition. The generic pathname mode
remains an internal non-Desktop transport contract and is not advertised as a
release capability.

Release process launches use fixed `/usr/bin/ssh`, `/usr/bin/ssh-keyscan`, and
`/usr/bin/rsync` identities after root-owner, mode, type, ancestor, and pathname
binding verification. Linux invokes the exact main executable through its held
FD. macOS does not support the same Mach-O `/dev/fd` execution contract, so the
isolated birth child for top-level SSH/rsync revalidates the held FD against the
fixed root-owned, non-writable ancestor/path binding immediately before
executing that system path. The parent repeats the binding check after process
birth. On macOS, ssh-keyscan instead has the same path/FD binding verified before
launch and after completion. The ordinary environment is empty. In agent mode
the original host
`SSH_AUTH_SOCK` pathname never enters child env or argv. Before each command,
rsync, or tunnel spawn, the parent holds every upstream directory identity,
connects one upstream FD, and revalidates the socket pathname/inode after
connect. It then creates a fresh owner-private one-shot relay and gives OpenSSH
only that private relay path. `PATH`, shell configuration, askpass variables,
and ambient proxy values are not inherited.

Linux monitors the exact socket and ancestor bindings with inotify. Darwin
cannot open a Unix-domain socket pathname with `O_EVTONLY`, so it registers
kqueue vnode events only on the already-held ancestor directory FDs. Ancestor
rename, revoke, attribute, delete, or kqueue error events fail closed. Immediate
parent namespace writes force another exact socket identity check. Darwin can
coalesce `NOTE_ATTRIB` with `NOTE_WRITE` when a child directory is created or
removed because the parent's link count changes; that combined parent event is
treated as namespace churn and receives the same exact revalidation, while a
standalone attribute event or any rename/delete/revoke event still fails closed.
The socket identity includes ctime, so target rename-and-restore is rejected
while unrelated sibling churn can continue. The connected Darwin peer is
independently bound with libc
`getpeereid`, `LOCAL_PEERPID`, and the complete 32-byte `LOCAL_PEERTOKEN` audit
token. The token's effective UID, GID, and PID must agree with the other two
kernel results, and every later connection must match the complete baseline
token, including its process-generation value. Missing or partial credentials
are not downgraded to PID-only authority.

The relay accepts only a peer whose kernel-reported PID belongs to the exact new
session/process group owned for that spawn and whose executable vnode is the
held `/usr/bin/ssh` identity. This rejects a same-UID connector that reaches the
socket first. After the first authorized connection, the listener pathname is
removed and bounded-buffer forwarding uses the already connected upstream FD.
Timeout, cancellation, process exit, and spawn failure close both streams and
the listener. Socket/root removal is relative to held directory FDs and exact
inode identities; uncertain cleanup is retained in bounded retry ownership and
never deletes a replacement pathname.

For rsync, the remote-shell string names the verified SSH authority as
`/dev/fd/<ssh-fd>` on Linux. On macOS it names the same fixed `/usr/bin/ssh`
path whose root-owned, non-writable chain and held FD identity were verified;
Darwin cannot execute that Mach-O image through `/dev/fd`. The SSH FD remains
in the inherited descriptor set on both platforms, and both executable
identities are verified immediately before and after rsync process birth.
The Darwin nested SSH exec itself is path-based; concurrent privileged
replacement of the root-owned `/usr/bin` chain is outside this unsigned Desktop
threat boundary.

## Command Execution

`run(command, env=..., cwd=...)` builds:

```text
ssh -F /dev/null -p <port> \
  [-o IdentityFile=none -i <key> \
   -o IdentitiesOnly=yes -o IdentityAgent=none] \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=<private-lease-file> \
  -o GlobalKnownHostsFile=/dev/null \
  -o UpdateHostKeys=no \
  -o CheckHostIP=no \
  -o VerifyHostKeyDNS=no \
  -o KnownHostsCommand=none \
  -o HashKnownHosts=no \
  -o HostKeyAlgorithms=<confirmed-algorithm> \
  -o BatchMode=yes -l <user> -- <host> <remote-command>
```

`-F /dev/null` prevents `IdentityFile`, `Match`, or `Include` entries in user SSH
configuration from extending private-key authentication. The same base argv is
used for remote commands, the rsync remote shell, and SSH tunnels. Neither
keyscan nor transport subprocesses use `shell=True`.

Remote commands run inside a small shell wrapper that appends a random controlled
completion marker and the remote exit status to stderr. The transport removes
that marker before returning output. A verified marker preserves a legitimate
remote exit `255` as `RemoteCommandResult(return_code=255)`; exit `255` without a
valid marker is a typed connection failure. Host-key-specific failures may only
come from a separately verified transport setup/protocol signal, never by parsing
arbitrary stderr text.

Configuration, host-key, timeout, process-start, connection, and rsync failures
are renderer-safe closed typed errors. Translation discards the original
exception object and chain, so exception strings and formatted tracebacks cannot
expose subprocess argv, stdout/stderr, local or remote paths, lease tokens, or
credentials. The deployment logger records only the typed code and a random
opaque diagnostic ID. It never records raw SSH/rsync
stdout/stderr, exception text, local paths, or usernames. Command output remains
available only through the existing restricted result/diagnostic handling path;
known trust paths are redacted from returned remote-command stderr as defense in
depth. Synchronous SSH and rsync subprocesses incrementally drain both streams
under one 4 MiB aggregate byte cap. A timeout or cap overflow terminates and
reaps the process before the existing typed error translation runs. Every such
subprocess first creates one authority, inserts it in the 32-entry ownership
registry, and transfers the entered known-host lease to it. Only that
pre-published owner may call `Popen`. The child starts a new session, writes its
PID, PGID, and SID to an anonymous owner-held birth-record FD, fsyncs it, and
then execs the requested SSH or rsync argv. If `Popen` succeeds but its Python
return is interrupted, the already registered authority reconstructs a
non-reaping wait handle from the birth record; if birth cannot yet be proved, it
retains the same bounded slot and lease fail closed. There is no semaphore to
registry handoff and no portable waiter thread that can call `wait()` early.
Process-group validation, `waitid`, Darwin `kqueue`, Linux
`/proc/<pid>/stat` fallback setup, and capture all execute under that authority.
Darwin may return `ESRCH` from `getpgid` after a very short-lived leader has
already become an unreaped zombie. That race is accepted only while the owning
`Popen` still has no return code and a bounded `ps` snapshot contains the exact
PID with `PGID == PID` and state `X` or `Z`; every other missing, live,
wrong-group, non-Darwin, or non-`ESRCH` result remains a hard failure.
After Darwin registers the one-shot kqueue event, it takes a bounded, non-reaping
`ps` snapshot of the pinned PID/PGID. That snapshot detects a leader that became
a zombie before registration; when kqueue registration is unavailable, the same
strict snapshot remains the bounded polling fallback.
Production tunnel readiness, exit monitoring, Core connection verification, and
close use only this non-reaping observer. Both `_NonReapingPopen.poll()` and the
birth-record-recovered wait handle reject a live unreaped child, preventing an
accidental `waitpid(WNOHANG)` from consuming the leader status before group
cleanup.
If no non-reaping observer is available, closed capture pipes or the operation
deadline initiate cleanup conservatively. Error and cancellation cleanup keeps
the direct child unreaped while that PID fixes the group identity. It enumerates
the exact PGID before each signal, sends `SIGTERM` only while a live member
remains, and then escalates to `SIGKILL`. A Darwin `EPERM` signal result is
re-observed: it is accepted only if every member is now dead or zombie;
otherwise cleanup remains failed and owned. Successful signals do not mark
group cleanup confirmed. A bounded observer enumerates process state through
Linux `/proc` or portable `ps`, requires the pinned leader to remain observable,
and requires every member of that PGID to be dead or a zombie. Only then may the
owner wait/reap the direct child; it subsequently requires the exact PGID to be
absent before closing its birth-record FD, removing the registry entry,
releasing capacity, and closing the known-host lease. Any group signal,
observation, reap, record removal, or lease cleanup failure retains the complete
authority. Later command, tunnel, close, or recovery calls retry up to four retained
entries synchronously. Full ownership capacity rejects a new command before
`Popen`, so it cannot create an unrecorded process. All waits remain bounded.

Capture polls the unreaped leader at a bounded interval through `waitid`,
gap-closed Darwin `kqueue` observation, Linux proc status, or the Darwin `ps`
fallback instead of reaping it. Once the leader exits,
inherited descendant pipes receive a 100 ms drain grace; capture keeps bytes
already delivered, kills the still-pinned process group, closes any remaining
pipes, and returns the leader's actual exit code. This ordering prevents
PID/PGID reuse from redirecting cleanup, never treats `killpg` success or
`ESRCH` alone as group-termination proof, never signals the Desktop process
group, and keeps a descendant from writing after an error return or trust-lease
release.

This ownership change is limited to the parent-side Python deployment SSH and
provider known-host modules. It does not alter remote bootstrap incoming-marker
or crash-recovery behavior. If the separate Python bootstrap branch lands first,
these parent-side authority changes must be merged manually afterward, preserving
that branch's marker/recovery implementation while retaining the non-reaping
tunnel and retryable lock cleanup contracts above.

`env` is injected into the remote command as POSIX assignments:

```text
env HTTPS_PROXY=http://127.0.0.1:7890 HF_ENDPOINT=https://hf-mirror.com <command>
```

This environment is for the remote task command. It is not an SSH connection
proxy. Users who need SSH jump hosts or SSH-level proxying should configure
OpenSSH separately.

If `cwd` is provided, it must be an absolute remote path using only the SSH
transport safe path characters (`/`, letters, digits, `.`, `_`, `-`, `@`, `%`,
`+`, `=`, `,`). It is emitted as:

```text
cd <quoted-cwd> && <command>
```

Non-zero remote exit codes are returned as `RemoteCommandResult` values. They do
not raise, including a marker-verified remote `255`. Timeouts raise
`TimeoutError`, which the executor converts into structured failure reports
during preflight or workspace execution.

## Workspace Upload

`upload_dir(local_path, remote_path)`:

1. verifies the local path exists and is a directory;
2. verifies the remote path is absolute and uses only the SSH transport safe
   path characters;
3. creates the remote target directory with `mkdir -p` through the trusted SSH
   binding;
4. runs `rsync -az --delete -e "ssh ... <provider trust options> -l <user>"`
   from `local_path/` to
   `<host>:remote_path/`.

The trailing slash semantics intentionally upload the contents of the local
folder into the prepared remote workspace path.

Each Core bootstrap transfer receives an unpredictable 128-bit transfer ID and
a unique owner-only `incoming-<bundle>-<transfer>` directory. Before prepare
returns, that inode is no-follow pinned and contains a fsynced, inode-revalidated
`0600` transfer marker. Prepare, discard,
and finalize validate that closed authority under the same publication lock;
concurrent or exact retries never reuse an incoming pathname or inode. Staging
admits at most 16 live incoming attempts and scans at most 32 staging entries.
The transport independently publishes one of 16 local ownership tokens before
remote prepare. That same token remains the sole capacity owner while its state
moves from pending receipt to exact prepared authority, active upload, finalize
reconciliation, and publication or confirmed discard. There is no
pending-to-cleanup slot transfer: an interruption immediately before or after
the first authority update leaves the token inactive with the exact transfer
identity, so the next same-process retry reclaims it. Full ownership capacity
fails before another incoming directory can be created; repeated malformed
finalize receipts therefore retain recoverable exact authority but cannot grow
local or remote work without bound. Upload failure uses a separate bounded
10-second cleanup deadline instead of an exhausted staging deadline; failed
cleanup remains retryable and runs before the next staging prepare. Once
finalize starts, a timeout, authenticated failure, cancellation, or malformed
receipt is an unknown outcome. Cleanup first repeats the exact idempotent finalize transaction
and validates its receipt. It discards only after that reconciliation completes
with a definite non-publication result, so cleanup cannot remove an exact bundle
that was published before the first response was lost. The upload-to-finalize
state update, remote finalize, and receipt publication retain one active owner,
which concurrent cleanup must skip. A `finally` retires that active state on
every `BaseException`: an interruption before the finalize state update leaves
recoverable discard authority, while an interruption after it leaves exact
finalize-reconciliation authority. Repeated interrupted handoffs therefore
reuse bounded cleanup capacity instead of permanently consuming active slots.

Timeout remnants stay private and bounded. Prepare creates a validated `0600`
`.openevo-transfer.lock` in every incoming directory. The rsync server runs
through a closed Python wrapper that holds an exclusive flock on that inode across
`exec`; rsync deletion rules protect the marker. Prepare, discard, and finalize
must acquire a nonblocking exclusive flock before retiring an incoming
directory. A recognized owner-only `0700` incoming directory with no marker is
an interrupted pre-authority prepare and is removed immediately only when its
pinned inode is empty; a nonempty, wrong-mode, symlink, foreign-owner, or
malformed-marker shape fails closed without cleanup. It therefore cannot
permanently consume the 16-attempt capacity. Prepare considers marked
directories only after they are older than 600 seconds, twice the
maximum staging operation lifetime, but age alone never authorizes deletion.
Thus a continuously writing or orphaned cross-process rsync remains protected,
while a later Desktop staging/startup can recover all 16 unlocked abandoned
attempts after a caller crash. Locked reconciliation also cleans retired or
private publish candidates.
A candidate left sealed at `0500` by a pre-rename crash is rebound by inode,
restored privately to `0700`, and then cleared; arbitrary or
identity-mismatched entries still fail closed.

Finalize verifies the exact two-file payload plus the validated internal lease,
then digest-verifies a streaming copy into a random private publish candidate
that was never disclosed to rsync. Candidate members are created as `0400`;
only the publisher's already open `O_WRONLY` FD can populate them, and it closes
every such FD before rename.
Finalize seals the directory to `0500`, verifies and fsyncs through the
still-pinned candidate FD, and atomically renames that inode with no-replace. The same FD
remains open through final pathname/inode verification and issues a receipt for
the bundle and both member inodes. Only then does finalize retire the incoming
pathname and attempt bounded cleanup. An already published exact sealed bundle
remains an idempotent finalize retry.

The returned paths are not standalone authority. The transport retains the
receipt and wraps the bootstrap command that first consumes both paths. Under
the same publication lock, the wrapper reopens the final bundle no-follow,
requires the receipt identities and exact modes/digests, and rewrites both paths
to `/proc/<wrapper-pid>/fd/<pinned-bundle-fd>`. The wrapper stays alive across
the bootstrap, so nested consumers that close inherited FDs (including pip)
still read the pinned directory rather than a same-name replacement. It rechecks
the pinned members and canonical pathname after the consumer exits. Replacement
before handoff fails identity verification; replacement during consumption
cannot redirect reads and makes the wrapper fail closed afterward. A stale rsync
process or held incoming writer can mutate only an unpublishable retired inode.

## Limitations

This slice does not include:

- password or passphrase vault integration;
- OpenSSH config editing;
- UI/native/sidecar contract wiring for host-key confirmation;
- Windows-specific rsync packaging;
- host-wide remote dependency installation or repair beyond bootstrap's
  user-site `openevo` and `huggingface_hub` installs;
- remote sidecar daemon startup;
- Docker daemon, Docker Compose, or vLLM lifecycle management;
- dynamic adapter/model lifecycle;
- Desktop UI wiring.

These should build on top of the transport/report contract rather than changing
the sidecar science plan schema.

## Verification

Focused validation:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_host_keys.py tests/openevo/remote/test_ssh_transport.py -q
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote -q
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/deployment src/openevo/backend/launcher.py tests/openevo/remote
git diff --check openevo/stable...HEAD
```

The host-key and general SSH transport tests use Python, POSIX file APIs,
OpenSSH when present, and `flock`. Core asset consumer handoff is a Linux remote
contract and additionally requires `/proc/<pid>/fd`; startup fails closed when
that pinned path cannot be consumed.
