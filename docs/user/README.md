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
state is authoritative on the remote host; the browser is only a client. The
default command uploads the current committed source over SSH only when the
server has a different commit. It does not require `git push`.

## Requirements

- local WSL with Git, Node/npm, Python, `uv`, and OpenSSH;
- a reachable Linux SSH account;
- Codex CLI installed and authenticated for that remote account.

Commit local changes before delivery. The remote host does not fetch OpenEvo
from GitHub. A first installation or changed dependency lock may still need
access to the configured Python package sources if the required tools and
packages are not cached. Network-sensitive launcher phases have finite
timeouts instead of waiting indefinitely.

Use `--source-action install` for the first source and runtime installation,
`--source-action update` to deliver a commit and prepare its runtime, and
`--source-action start` to start an already matching, prepared commit. Install
and update do not start services. Omitting the flag keeps the one-command
automatic flow.

For development and diagnostics, see the repository `README.md` and
`docs/maintainer/testing.md`.
