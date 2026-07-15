# OpenEvo Desktop Target Workflow

> Pre-release target: no public DMG currently implements this complete flow.

1. Install OpenEvo Desktop from the macOS `.dmg`.
2. Launch the app and add a remote workspace.
3. Enter the remote server host, port, and user, then choose SSH agent, password,
   or private key. Passwords and key passphrases are collected by a macOS secure
   prompt. Private keys are selected with the macOS file picker. Desktop shows
   only whether each required credential slot is configured.
4. Configure proxy, pip index, and Hugging Face mirror settings if needed, then
   connect. Verify the server fingerprint when Desktop asks for host trust.
5. Desktop runs remote checks, provisions the supported Python runtime when
   needed, installs the bundled OpenEvo Core release, and starts the remote
   backend. Project creation remains disabled until the workspace is connected.
6. Create a research project and choose `Prepare evolution`. Desktop saves a
   minimal draft, establishes its remote session, and returns to the same drawer
   with remote methods for text memory, skills, and the agent system. Review the
   defaults, then choose `Save and activate`. Closing or refreshing after the
   first stage resumes the explicit `pending` setup. Memory, skills, and agent
   guidance are independent switches: any one or all three may be disabled.
   The second save records `configured`, so a deliberate zero-target project is
   complete and remains runnable after refresh.
7. Use the default Codex subscription transcript mode. Self-deployed remains
   visible but unavailable in this release; Desktop explains the release reason
   and lets an older saved project switch back to Subscription.
8. Start the science run and monitor its timeline, logs, and artifacts.

The System view reports connection and read-only service status. Diagnostics,
service restart, local repair, and workspace-sync controls are absent because
the current Core release has no corresponding release handlers. Their frozen
Local API routes are reserved for a future implementation and fail closed if
called directly. To update a folder snapshot, select the folder again in project
settings.

Long-running local connection and project activation show `Cancel operation`.
Cancellation closes the exact connecting/bootstrap transport, active SSH
subprocesses, tunnel, and Core client owned by that operation. It returns the
workspace or project to its authoritative retryable state; a late background
completion cannot reactivate the cancelled session.

Project and artifact mode controls use manual tab activation: Left and Right,
or Home and End, move focus without starting an action; Enter or Space activates
the focused choice. When closing a form with unsaved changes, the underlying
form becomes inert and focus remains inside the confirmation until the draft is
kept or discarded. Pointer input outside that confirmation is ignored.

Desktop must show setup-required or typed error states until a real sidecar and
remote Core Backend are reachable. This document becomes a user quickstart only
after the packaged workflow passes its release E2E.
