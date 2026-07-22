# OpenEvo Desktop Quickstart

This guide describes the published `0.1.8` exhibition Preview and its supported
Apple Silicon Mac plus Linux Docker-host profile.

## Before You Start

You operate OpenEvo through Desktop. A server administrator or hosting
environment must provide:

- an Apple Silicon Mac running macOS 12 or later;
- a remote Linux x86-64 server reachable over SSH and matching the exhibition
  Docker user-container assumptions;
- the release DMG and its published checksum;
- an SSH key available through the macOS SSH agent;
- Docker Engine configured so your remote SSH user can run user containers;
- Codex CLI installed and signed in to a subscription as that same remote user.

See [Remote server setup](remote-server-setup.md) for the host prerequisite
boundary. You do not need to SSH to the server, install the Daemon, or prepare
a runtime image manually.

## Install The Unsigned DMG

1. Open the immutable
   [OpenEvo Desktop 0.1.8 Preview release](https://github.com/CompLifeLab-ZJU/OpenEvo/releases/tag/openevo-desktop-v0.1.8-v018-startup-logs.29947490201.1),
   confirm the title **OpenEvo Desktop 0.1.8 Preview**, then download
   `OpenEvo-Desktop-0.1.8-aarch64.dmg` and `SHA256SUMS`.
2. Verify the exact DMG checksum recorded by that release:

   ```bash
   grep '  OpenEvo-Desktop-0.1.8-aarch64.dmg$' SHA256SUMS \
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

## Startup Diagnostics

If Desktop opens but startup is still in progress, open **Diagnostics** and use
**View logs** to inspect local startup events. After the attempt succeeds or
fails, **Reveal in Finder** opens the log directory and **Export diagnostics**
creates the support artifact. When contacting support, provide the exported
JSON together with the displayed error code and Desktop version; do not send
raw terminal output, SSH material, credentials, transcripts, or research data.

Version `0.1.7` introduced the current local Preview-state namespace and stopped
importing projects or retry records created by older Preview builds. Version
`0.1.8` keeps that namespace, so workspaces saved by `0.1.7` remain available.
Removing an older application does not remove pre-`0.1.7` files, but those
files are not read by the current release and cannot block its startup. Remote
project and Daemon data are not deleted by this local isolation.

## Explore The Built-In Projects

Before a server is configured, Desktop exposes two read-only examples in the
project selector: **Enzyme Kinetics Model Review** and **Protein Stability
Evidence Review**. Each contains three demonstration task sessions,
de-identified reasoning and tool summaries, and a progression from an
unsupported result to a bounded, validated conclusion. Use **Research** to
inspect task execution and **Evolution** to inspect textual-memory,
trajectory-to-skill, and agent-system histories plus their readable
`memory.md`, `SKILL.md`, and `AGENTS.md` outputs.

Both examples are built in and read-only. They do not
represent completed remote runs and perform no SSH, Daemon, Core, or network
action. They remain available in the project selector after real projects are
created.

## Connect The Server

1. Choose **Add remote workspace** and enter a display name, server address,
   SSH port, and remote user name.
2. Add HTTP, HTTPS, or bypass-proxy settings if the remote server needs them.
3. Choose **Save workspace**, then **Connect**.
4. When **Confirm server identity** appears, compare the displayed SHA-256
   fingerprint with a value obtained from the server administrator through a
   trusted channel.
5. Choose **Trust and continue** only after the fingerprint matches.

Desktop uses the SSH agent already running on the Mac. This Preview does not
accept an SSH password, a private-key file, or a passphrase in the app.

During connection and activation, Desktop runs preflight, transfers and
verifies the version-matched self-contained OpenEvo Daemon Bundle, prepares the
managed science runtime, starts or attaches the Daemon, and establishes the
private tunnel. The current Preview cannot add Docker host access or complete a
Codex installation/login on an arbitrary clean host. If either host
prerequisite is missing, Desktop identifies the blocked prerequisite; ask the
server administrator to prepare it or choose another supported host, then
retry in Desktop. **Cancel operation** stops that connection or preparation
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
Engine, managed science runtime, and Daemon services. OpenEvo-owned components
are repaired or restarted through Desktop. For a host-level Docker or Codex
prerequisite that this Preview cannot change, follow the typed administrator
action shown by Desktop and then choose **Retry**; do not open a remote shell
to operate OpenEvo.

## Try The Exhibition Workflow

1. Start the first session and follow its timeline and transcript.
2. Wait until the session and its reported evolution work finish.
3. Open **Evolution** to inspect the selected memory, skill, and agent-system
   artifacts and their changes.
4. Start a second session in the same project.

Evolution is designed to affect only later sessions. Wait until Desktop shows
that the current session and its evolution work have completed before starting
the next session. Treat successor state in this Preview as experimental and do
not use it for production-critical work.

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
when only the application is removed. Current releases store that data under
`~/Library/Application Support/org.openevo.desktop`. The older Preview directory
`~/.openevo/desktop` is preserved but is not read by v0.1.7 or later. To clear
all local OpenEvo Desktop state after quitting the app, remove both directories.
This does not delete remote data.

The first Preview does not expose a complete in-app OpenEvo Daemon uninstall or
remote project-erasure workflow. Do not manually edit or delete OpenEvo remote
state while a run or transition may be active. Retain the remote state, or
arrange deliberate cleanup with the server owner after preserving any required
research data.
