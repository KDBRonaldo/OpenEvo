# OpenEvo self-hosted WebUI

This branch is a source-development preview. It does not provide the old DMG or
packaged Desktop application.

## Start

Prepare SSH access to a Linux server, then run from WSL:

```bash
cd /mnt/c/Users/18083/Desktop/OpenEvo/desktop
npm run dev:agent:webui:remote -- \
  --host <host> \
  --user <user> \
  --ssh-port <port>
```

The command prints the loopback URL to open in your browser. Project and session
state is authoritative on the remote host; the browser is only a client.

## Requirements

- local WSL with Git, Node/npm, Python, `uv`, and OpenSSH;
- a reachable Linux SSH account;
- GitHub access from the remote host for the configured OpenEvo repository and
  branch;
- Codex CLI installed and authenticated for that remote account.

If the remote deploy reports `couldn't find remote ref`, push the current branch
to the configured repository first. If the browser shows state from an older
build, stop the launcher, restart it, and hard-refresh the browser.

For development and diagnostics, see the repository `README.md` and
`docs/maintainer/testing.md`.
