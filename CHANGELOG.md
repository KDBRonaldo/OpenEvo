# Changelog

- Extracted durable project catalog and active-project SQLite ownership into
  `openevo.daemon` while preserving the working remote WebUI command, existing
  database layout, event behavior, and multi-project visibility.

## Unreleased — self-hosted rebuild

- Retain the working remote agent WebUI path driven by
  `npm run dev:agent:webui:remote`.
- Keep remote project and session state visible across project creation.
- Remove the former packaged Tauri Desktop, release sidecar, Core Control
  service, managed deployment stack, release workflows, and their tests.
- Reset product documentation and CI around the self-hosted WebUI architecture.
- Start the replacement loopback daemon with authenticated health/status APIs,
  private atomic lifecycle state, PID identity checks, and lifecycle commands.

Earlier packaged Desktop preview history remains available in Git history and
is not a compatibility contract for this branch.
