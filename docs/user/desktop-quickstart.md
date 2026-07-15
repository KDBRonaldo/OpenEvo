# OpenEvo Desktop Target Workflow

> Pre-release target: no public DMG currently implements this complete flow.

1. Install OpenEvo Desktop from the macOS `.dmg`.
2. Launch the app and add a remote workspace.
3. Enter the remote server host, port, and user. This release uses the macOS SSH
   agent; password and private-key entry are not exposed until the native
   credential broker is implemented.
4. Configure proxy, pip index, and Hugging Face mirror settings if needed, then
   connect. Verify the server fingerprint when Desktop asks for host trust.
5. Desktop runs remote checks, provisions the supported Python runtime when
   needed, installs the bundled OpenEvo Core release, and starts the remote
   backend. Project creation remains disabled until the workspace is connected.
6. Create and activate a research project. An existing project that loses its
   tunnel remains visible, but activation and runs stay disabled until its
   assigned workspace reconnects.
7. Use the default Codex subscription transcript mode. Self-deployed remains
   visible but unavailable in this release; Desktop explains the release reason
   and lets an older saved project switch back to Subscription.
8. Start the science run and monitor its timeline, logs, and artifacts.

The System view distinguishes passing checks from completed checks that contain
warnings or require attention. A warning state never appears as "All checks
passed"; follow the visible repair or reconnect action before starting a run.

Project and artifact mode controls use manual tab activation: Left and Right,
or Home and End, move focus without starting an action; Enter or Space activates
the focused choice. When closing a form with unsaved changes, the underlying
form becomes inert and focus remains inside the confirmation until the draft is
kept or discarded. Pointer input outside that confirmation is ignored.

Desktop must show setup-required or typed error states until a real sidecar and
remote Core Backend are reachable. This document becomes a user quickstart only
after the packaged workflow passes its release E2E.
