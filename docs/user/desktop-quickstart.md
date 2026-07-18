# OpenEvo Desktop Quickstart

This guide runs one science session, lets OpenEvo produce cross-session text
artifacts, and then runs a second session with the committed result.

## Before You Start

You need:

- a Mac and remote Linux server listed as supported in the release notes;
- the release DMG and its published checksum;
- an SSH key available through the macOS SSH agent;
- Docker Engine available to your remote SSH user;
- Codex CLI installed and signed in to a subscription as that same remote user.

See [Remote server setup](remote-server-setup.md) before connecting.

## Install The Unsigned DMG

1. Download the Preview DMG and checksum from
   [GitHub Releases](https://github.com/CompLifeLab-ZJU/OpenEvo/releases).
2. Verify that the DMG checksum matches the release checksum.
3. Open the DMG and move **OpenEvo Desktop** to **Applications**.
4. Try to open OpenEvo Desktop. macOS will warn that it cannot verify the
   developer because this Preview is unsigned and not notarized.
5. Open **System Settings > Privacy & Security**, find the blocked OpenEvo
   message, choose **Open Anyway**, and confirm only if the release identity is
   the one you verified.

The wording and location of **Open Anyway** can vary by macOS version.

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
version-matched OpenEvo Daemon and managed science runtime. Do not install the
Daemon with `pip` and do not upload a runtime image. A normal first preparation
can take time. **Cancel operation** stops that connection or preparation
attempt and leaves it retryable.

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

## Run Two Sessions

1. Start the first session and follow its timeline and transcript.
2. Wait until the session finishes and the successor revision is active.
3. Open **Evolution** to inspect the selected memory, skill, and agent-system
   artifacts and their changes.
4. Start a second session in the same project.

Evolution never changes the session that produced it. Session N captures a
transcript and produces the selected artifacts only after it finishes. Session
N+1 uses the complete committed successor revision. If that successor is still
being prepared, Desktop waits or blocks submission instead of running with a
partial or stale revision.

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
