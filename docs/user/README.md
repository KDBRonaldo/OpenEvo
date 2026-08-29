# EvoLab self-hosted WebUI

This branch provides a repository-free launcher candidate. It does not restore
the old DMG or packaged Tauri Desktop application.

## Install

Requirements on the user's computer:

- Python 3.11 or newer;
- POSIX `sh` and `tar`, or Windows PowerShell;
- system OpenSSH (`ssh`);
- a literal host alias in `~/.ssh/config`.

Install the latest published version:

```bash
curl -fsSL https://github.com/KDBRonaldo/OpenEvo/releases/download/v0.2.0/install.sh \
  | sh -s -- --version v0.2.0
~/.local/bin/evolab webui
```

The online installer verifies the archive against the separately published
SHA-256 file before installation. On Windows, run:

```powershell
irm https://github.com/KDBRonaldo/OpenEvo/releases/download/v0.2.0/install.ps1 | iex
evolab webui
```

For an offline installation, download `evolab-launcher.tar.gz` and its
`.sha256` file from the same GitHub Release, verify it, then run:

```bash
sha256sum --check evolab-launcher.tar.gz.sha256
tar -xzf evolab-launcher.tar.gz
sh evolab-launcher/install.sh
~/.local/bin/evolab webui
```

Add `~/.local/bin` to `PATH` to use `evolab webui` directly. The archive
already contains the matching server Release Bundle. Git, uv, Node/npm, and an
OpenEvo checkout are not required on the user's computer.

## SSH configuration

Add a literal host alias to `~/.ssh/config`, for example:

```sshconfig
Host openevo-lab
  HostName server.example.com
  User researcher
  Port 22
```

Then run:

```bash
evolab webui
```

The launcher discovers concrete aliases from the SSH config (including common
`Include` files), asks which workspace to use, remembers the last selection,
uploads its verified server Release Bundle when needed, and opens the WebUI.
The explicit source-development form remains valid:

```bash
cd /mnt/c/Users/18083/Desktop/OpenEvo/desktop
npm run dev:agent:webui:remote -- \
  --host <host> \
  --user <user> \
  --ssh-port <port>
```

The command opens and prints the loopback URL. Project and session
state is authoritative on the remote host; the browser is only a client. The
installed command uploads its embedded server Release Bundle only when the
server has another release. The source-development command instead uploads the
current committed tree and does not require `git push`.

## Server requirements

- a reachable Linux SSH account;
- Python 3 and standard POSIX utilities on the server;
- Codex CLI installed and authenticated for that remote account.

The remote host does not fetch OpenEvo from GitHub. A first installation may
still need access to the configured Python package sources if uv, Python 3.11,
or required packages are not cached. Network-sensitive launcher phases have
finite timeouts instead of waiting indefinitely.

Use `--source-action install` for the first release and runtime installation,
`--source-action update` to deliver a newer release and prepare its runtime,
and `--source-action start` to start an already matching, prepared release.
Install and update do not start services. Omitting the flag keeps the
one-command automatic flow.

For development and diagnostics, see the repository `README.md` and
`docs/maintainer/testing.md`.
