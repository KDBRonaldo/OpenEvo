# OpenEvo Desktop Quickstart

This guide describes the `0.1.2` exhibition candidate. Follow it only after the
`0.1.2` GitHub Release is public. The Preview publication workflow verifies the
exact packaged applications and assets, but it does not establish the canonical
two-session science gate or general host support.

## Before You Start

You need:

- an Apple Silicon Mac running macOS 12 or later;
- a remote Linux x86-64 server reachable over SSH and matching the exhibition
  Docker user-container assumptions;
- the release DMG and its published checksum;
- an SSH key available through the macOS SSH agent;
- Docker Engine configured so your remote SSH user can run user containers;
- Codex CLI installed and signed in to a subscription as that same remote user.

See [Remote server setup](remote-server-setup.md) before connecting.

## Install The Unsigned DMG

1. Confirm that the public release title is **OpenEvo Desktop 0.1.2 Preview**,
   then download `OpenEvo-Desktop-0.1.2-aarch64.dmg` and `SHA256SUMS` from that
   same immutable
   [GitHub Release](https://github.com/CompLifeLab-ZJU/OpenEvo/releases).
2. Verify the exact DMG checksum recorded by that release:

   ```bash
   grep '  OpenEvo-Desktop-0.1.2-aarch64.dmg$' SHA256SUMS \
     | shasum -a 256 -c -
   ```

3. Open the DMG and move **OpenEvo Desktop** to **Applications**.
4. Try to open OpenEvo Desktop. macOS will warn that it cannot verify the
   developer because this Preview is unsigned and not notarized.
5. After verifying the checksum, quit the app and clear quarantine from only the
   installed application:

   ```bash
   xattr -dr com.apple.quarantine "/Applications/OpenEvo Desktop.app"
   ```

6. Reopen **OpenEvo Desktop**. If command-line recovery is unavailable, use
   **System Settings > Privacy & Security > Open Anyway** for this verified app.

The wording and location of **Open Anyway** can vary by macOS version.
Do not clear quarantine from a parent directory or from an unverified download.

## Explore The Built-In Project

Before a server is configured, Desktop opens the read-only **酶动力学模型复核**
example. Use **Research** to inspect three synthetic task sessions, their
de-identified reasoning and tool summaries, and the progression from baseline
failure to a validated result. Use **Evolution** to inspect textual-memory,
trajectory-to-skill, and agent-system histories plus their readable
`memory.md`, `SKILL.md`, and `AGENTS.md` outputs.

The example is always labelled **内置示例 · 只读**. It is synthetic Desktop
content, does not represent a completed remote run, and performs no SSH,
Daemon, Core, or network action. It remains available in the project selector
after real projects are created.

## Connect The Server

1. Choose **Add workspace** and enter a display name, server address, SSH port,
   and remote user name.
2. Add HTTP, HTTPS, or bypass-proxy settings if the remote server needs them.
3. Choose **Save workspace**, then **Connect**.
4. When **Confirm server identity** appears, compare the displayed SHA-256
   fingerprint with a value obtained from the server administrator through a
   trusted channel.
5. Choose **Trust and continue** only after the fingerprint matches.

Desktop uses the SSH agent already running on the Mac. This Preview does not
accept an SSH password, a private-key file, or a passphrase in the app.

During connection and activation, Desktop transfers and verifies the
version-matched self-contained OpenEvo Daemon Bundle and managed science
runtime. The current Preview does not prepare Docker or Codex: the remote SSH
user must already be able to run Docker user containers, and Codex must already
be installed and signed in for that user. Do not install the Daemon with `pip`
and do not upload a runtime image. A normal first preparation can take time.
**Cancel operation** stops that connection or preparation attempt and leaves it
retryable.

## Create A Project

1. Choose **Create project**.
2. Enter a project name, task title, and objective.
3. Start with an empty managed workspace or select a local folder snapshot.
4. Keep **Codex Subscription** as the model mode. Self-Deployed is unavailable
   in this Preview.
5. Choose which evolution carriers to enable:

   - **Textual Memory** stores concise learned context.
   - **Skills** stores reusable procedural guidance as a skill bundle.
   - **Agent System** updates the instructions presented to the agent.

6. Choose **Prepare evolution**. Desktop establishes the project session and
   loads methods from the connected Daemon.
7. Review the enabled targets and methods, then choose **Save and activate**.

Activation checks the remote Codex installation, subscription login, Docker
Engine, managed science runtime, and Daemon services. It does not install or
sign in to Codex. Follow any typed remediation shown by Desktop, then retry.

## Try The Exhibition Workflow

1. Start the first session and follow its timeline and transcript.
2. Wait until the session and its reported evolution work finish.
3. Open **Evolution** to inspect the selected memory, skill, and agent-system
   artifacts and their changes.
4. Start a second session in the same project.

The canonical product requires evolution to affect only later sessions and
requires Desktop to block stale or partial successor state. The `0.1.2` v1
Preview does not yet prove that complete authority contract. Do not use it for
work that depends on atomic cross-session guarantees.

Transcript mode does not provide token IDs, loss masks, or token-level
log-probability metrics.

## Cancel, Close, And Reconnect

- **Cancel operation** applies to local connection, preparation, or activation.
- **Cancel session** requests cancellation of the remote session. Wait for the
  terminal **Cancelled** state; a cancelled session does not activate a
  successful successor revision.
- Closing Desktop does not stop a remote session. Reopen Desktop and choose
  **Reconnect** to recover authoritative state and missed timeline events.
- If the Mac was restarted, make the SSH identity available to the SSH agent
  again before reconnecting.
- Do not repeatedly retry an action while Desktop says its outcome is being
  confirmed. The same action identity is reconciled before another attempt is
  admitted.

## Uninstall And Retention

To remove Desktop, quit it and move **OpenEvo Desktop** from **Applications** to
the Trash. This does not delete remote projects, transcripts, artifacts, the
managed runtime, or the OpenEvo Daemon.

Local workspace profiles, accepted host keys, and recovery state are also kept
when only the application is removed. To clear this Preview's local state after
quitting Desktop, remove `~/.openevo/desktop` and, if present,
`~/Library/Application Support/org.openevo.desktop` on the Mac. This does not
delete remote data.

The first Preview does not expose a complete in-app OpenEvo Daemon uninstall or
remote project-erasure workflow. Do not manually delete remote OpenEvo
directories while a run or transition may be active. Retain the remote state,
or arrange deliberate cleanup with the server owner after preserving any
required research data.
