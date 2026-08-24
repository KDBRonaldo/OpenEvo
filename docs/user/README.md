# OpenEvo self-hosted WebUI

This branch is a source-development preview. It does not provide the old DMG or
packaged Desktop application.

## Start

Add a literal host alias to `~/.ssh/config`, for example:

```sshconfig
Host openevo-lab
  HostName server.example.com
  User researcher
  Port 22
```

Then run from the repository checkout:

```bash
uv run openevo webui
```

The launcher discovers concrete aliases from the SSH config (including common
`Include` files), asks which workspace to use, remembers the last selection,
and opens the WebUI. The previous explicit development form remains valid:

```bash
cd /mnt/c/Users/18083/Desktop/OpenEvo/desktop
npm run dev:agent:webui:remote -- \
  --host <host> \
  --user <user> \
  --ssh-port <port>
```

The command opens and prints the loopback URL. Project and session
state is authoritative on the remote host; the browser is only a client. The
default command uploads the current committed source over SSH only when the
server has a different commit. It does not require `git push`.

## Release candidate

Maintainers can provide a versioned `.oevobundle` instead of asking a user to
run from the OpenEvo source tree. The current release-candidate invocation is:

```bash
uv run openevo webui \
  --release-bundle /path/to/openevo-self-hosted.oevobundle
```

OpenEvo verifies the complete release locally and remotely, installs it under
an immutable release ID, keeps project/session data outside the release
directory, and then opens the same loopback WebUI. Docker is not required.

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
