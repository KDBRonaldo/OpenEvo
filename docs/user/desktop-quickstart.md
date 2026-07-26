# OpenEvo Desktop Quickstart

This guide describes the unsigned `0.1.9` exhibition Preview for Apple Silicon
Macs and a compatible Linux Docker server.

## Before You Start

You operate OpenEvo through Desktop. You need:

- an Apple Silicon Mac running macOS 12 or later;
- the `0.1.9` release DMG and its published checksum;
- a remote Linux x86-64 server matching the supported Docker user-container
  profile;
- a literal host alias in your Mac user's `~/.ssh/config` that can connect to
  that server with `/usr/bin/ssh <alias>`; and
- Codex CLI installed and signed in to a subscription for the remote account.

Your SSH config remains authoritative for the real hostname, user, port,
identity, agent/Keychain, proxy jump or command, and host-trust policy. OpenEvo
stores the selected alias and does not ask you to re-enter those connection
fields.

See [Remote server setup](remote-server-setup.md) for the complete host boundary.
You do not install or operate the OpenEvo Daemon manually.

## Install The Unsigned DMG

1. Open the immutable
   [OpenEvo Desktop 0.1.9 Preview release](https://github.com/CompLifeLab-ZJU/OpenEvo/releases/tag/openevo-desktop-v0.1.9-v019-system-ssh-final.30212086910.1), and download
   `OpenEvo-Desktop-0.1.9-aarch64.dmg` plus `SHA256SUMS`.
2. Verify the exact DMG checksum from that release:

   ```bash
   grep '  OpenEvo-Desktop-0.1.9-aarch64.dmg$' SHA256SUMS \
     | shasum -a 256 -c -
   ```

3. Open the DMG and move **OpenEvo Desktop** to **Applications**.
4. Try to open it. macOS warns that this unsigned, unnotarized Preview cannot be
   verified.
5. After checking the checksum, quit the app and clear quarantine from only the
   installed application:

   ```bash
   xattr -dr com.apple.quarantine "/Applications/OpenEvo Desktop.app"
   ```

6. Reopen **OpenEvo Desktop**. If command-line recovery is unavailable, use
   **System Settings > Privacy & Security > Open Anyway** for this verified app.

The wording and position of **Open Anyway** varies by macOS version. Never clear
quarantine from a parent directory or an unverified download.

## Startup Diagnostics

If startup is still in progress, open **Diagnostics** and use **View logs**.
After an attempt succeeds or fails, **Reveal in Finder** opens the log directory
and **Export diagnostics** creates a bounded support artifact. Send support the
exported JSON and displayed error code/version—not raw terminal output, SSH
material, credentials, transcripts, or research data.

Version `0.1.9` repairs the Tahoe packaged-sidecar startup failure seen in
`0.1.8`. It preserves the current Preview-state namespace and treats old v1
remote profiles as read-only migration input: rebind such a profile to one
configured SSH alias before connecting. Older files cannot become v2 mutation
authority.

## Explore The Built-In Projects

Before connecting a server, Desktop shows two read-only examples: **Enzyme
Kinetics Model Review** and **Protein Stability Evidence Review**. They
demonstrate Tasks, Project Heads, Runtime Context, and the three textual
evolution targets without performing SSH, Daemon, Core, or network operations.
They are tours, not completed remote runs.

## Configure System OpenSSH

If you do not already have a literal alias, add one to `~/.ssh/config`. For
example:

```sshconfig
Host my-openevo-server
    HostName server.example.org
    User research-user
    IdentityFile ~/.ssh/id_ed25519
```

This is only an example; normal OpenSSH options such as `Include`, `Match`,
`Port`, `ProxyJump`, `ProxyCommand`, agent, and Keychain integration remain
supported. Confirm in Terminal that the alias reaches the intended account:

```bash
/usr/bin/ssh my-openevo-server
```

Then exit that shell. You do not need to install or start OpenEvo there.

## Connect The Server

1. Choose **Add remote workspace**.
2. Enter a workspace name and select an SSH alias discovered from your normal
   OpenSSH configuration. If an alias from `Include` or `Match` cannot be listed
   lexically, choose **Use another SSH alias** and enter that literal alias—not
   an IP/user/port combination.
3. Choose **Save and connect**.
4. Respond to any native password, key-passphrase, or first-host trust prompt.
   Secret input goes directly to the owning OpenSSH process and is never exposed
   to the renderer or stored by OpenEvo.
5. If a host key changed, stop and verify the rebuild/change with the server
   administrator before choosing the offered repair action.

Desktop uses the equivalent of `/usr/bin/ssh <alias>`. During connection it
preflights the server, transfers and verifies the exact matching Daemon Bundle,
prepares the managed runtime, starts or attaches the Daemon, negotiates Core v2,
and establishes a private project tunnel. It cannot grant Docker access or
complete a Codex installation/login on an arbitrary host; follow the typed
administrator action and retry if those prerequisites are missing.

## Create A Project

1. Choose **Create project**.
2. Enter a project name, Task title, and objective.
3. Use a new scratch workspace or choose a local folder snapshot.
4. Confirm the fixed Preview execution profile: **Codex Subscription**,
   transcript capture, `gpt-5.3-codex-spark`, and high effort.
5. Choose **Create project**. Core creates immutable generation-zero Project
   Head, Workspace Snapshot, Evolution Revision, Runtime Context Snapshot, and
   Effective Execution Snapshot authority.
6. Open **Evolution**. Enable any desired subset of **Textual Memory**,
   **Skills**, and **Agent System** and select only methods offered by the
   verified remote registry. **Agent System / auto** is a Core-owned supported
   resolver in this release.
7. Choose **Save evolution configuration**.

An enabled target without a currently accepted remote method remains visible
but blocks Task admission until corrected or disabled. Desktop never falls back
to a bundled method table.

## Run Two Tasks

1. Open **Research** and choose **Validate and run task**. Desktop re-fetches
   remote capabilities, validates the exact registry/config, and admits one
   immutable Task.
2. Wait for its authoritative Attempt and successor transition to complete.
   The active Project Head advances only after the complete successor is
   committed atomically.
3. Review the Task Admission, predecessor Project Head, Attempt, transition,
   active Project Head generation, and Evolution Revision output count.
4. Run the second Task only when Desktop says the project is ready.

Evolution affects only later Tasks. The second Task pins the first Task's
successor Project Head and Runtime Context; it never consumes a stale or partial
successor. v0.1.9 shows the committed Evolution Revision output count but does
not yet expose Task artifact-content collection in the ordinary renderer.

Transcript capture does not provide token IDs, loss masks, logprobs, or other
token-level training metrics.

## Cancel, Retry, And Reconnect

- **Cancel Task** requests cancellation of an active Task.
- A failed or cancelled Task can append a new infrastructure **Attempt** under
  the same immutable Task Admission.
- A failed successor transition offers explicit retry or abandonment; Desktop
  does not silently use stale evolution state.
- Closing Desktop does not rewrite remote authority. Reopen it, reconnect the
  same SSH alias, and refresh the Project Head/Task state.
- Do not repeatedly submit while an outcome is being confirmed. Idempotency and
  generation/ETag checks reconcile the original action first.

## Uninstall And Retention

Quit Desktop and move **OpenEvo Desktop** from **Applications** to the Trash.
This does not delete remote projects, transcripts, revisions, the managed
runtime, or the Daemon.

Local profiles and recovery state are also retained. Current releases store
them under `~/Library/Application Support/org.openevo.desktop`; the historical
`~/.openevo/desktop` directory is preserved but is not v2 mutation input. The
Preview does not yet provide a complete in-app remote project-erasure or Daemon
uninstall workflow. Do not manually delete OpenEvo remote state while a Task or
successor transition may be active.
