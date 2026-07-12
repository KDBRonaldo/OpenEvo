# OpenEvo Desktop Target Workflow

> Pre-release target: no public DMG currently implements this complete flow.

1. Install OpenEvo Desktop from the macOS `.dmg`.
2. Launch the app and create a project.
3. Enter the remote server host, port, user, and SSH auth reference.
4. Configure proxy, pip index, and Hugging Face mirror settings if needed.
5. Run remote doctor/bootstrap from Desktop.
6. Start the remote OpenEvo Core Backend.
7. Choose Codex subscription transcript or self-deployed reference mode.
8. Start the science run and monitor its timeline, logs, and artifacts.

Desktop must show setup-required or typed error states until a real sidecar and
remote Core Backend are reachable. This document becomes a user quickstart only
after the packaged workflow passes its release E2E.
