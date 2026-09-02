# EvoLab self-hosted WebUI

This branch provides a repository-free launcher candidate. It does not restore
the old DMG or packaged Tauri Desktop application.

## Install

Requirements on the user's computer:

- macOS or WSL (native Windows PowerShell is not supported by this pre-release);
- Python 3.11 or newer;
- POSIX `sh` and `tar`;
- system OpenSSH (`ssh`);
- a literal host alias in `~/.ssh/config`.

Download `evolab-launcher.zip` from the matching GitHub Release. The same
package is tested on macOS and the WSL-compatible Linux path. Do not
download GitHub's `Source code (zip)` or `Source code (tar.gz)` links; those are
repository snapshots rather than the installer. Install the archive with:

```bash
unzip evolab-launcher.zip
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
  IdentityFile ~/.ssh/id_ed25519
```

Verify the alias first, then run EvoLab:

```bash
ssh openevo-lab
exit
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
- Codex CLI installed and authenticated for that exact remote account.

For a headless server, connect as the same SSH user configured above and run:

```bash
command -v codex
codex login --device-auth
codex login status
```

Logging in as another server user does not authenticate the account EvoLab
will invoke.

Public Hugging Face models can be registered from a Project's **Execution
model** settings. Self-deployed execution additionally requires Docker, an
NVIDIA GPU, enough disk/VRAM for the selected vLLM-compatible safetensors
model, and server access to Hugging Face (or an administrator-configured
`HF_ENDPOINT` mirror). Model files remain in the daemon's private state root;
the browser receives only model identity and progress records.

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
