# EvoLab self-hosted WebUI

This branch provides a repository-free launcher candidate. It does not restore
the old DMG or packaged Tauri Desktop application.

## Install

Requirements on the user's computer:

- native Windows PowerShell, macOS, Linux, or WSL;
- Python 3.11 or newer;
- OpenSSH on the local OS, or OpenSSH in WSL on Windows;
- a literal host alias in the native or WSL `~/.ssh/config`.

Download `evolab-launcher.zip` from the matching GitHub Release. The same
package is tested on Windows, macOS, and Linux. Do not
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

For native Windows, expand the ZIP and run this from PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\evolab-launcher\install.ps1
evolab webui
```

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
On native Windows it searches both the Windows OpenSSH config and WSL configs.
If an alias belongs to WSL, the full SSH operation stays inside WSL so Linux
key paths, agents, `ProxyJump`, and `known_hosts` continue to work. Ubuntu is
preferred automatically; another distribution can be selected with
`--ssh-client wsl --wsl-distribution <name>`.
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

For the complete distinction between hard server prerequisites and components
that the launcher installs automatically, see
[`self-deployed-server-requirements.md`](self-deployed-server-requirements.md).

- a reachable Linux SSH account;
- root or passwordless `sudo` with `apt-get`, `dnf`, `yum`, or `apk`;
- outbound HTTPS access to download Codex and complete ChatGPT authentication.

On first run, EvoLab automatically installs missing Python, curl, certificates,
core utilities, and the official Codex CLI. If Codex is not logged in, the
terminal prints a one-time device code and EvoLab opens the ChatGPT verification
page on the user's computer. Enter that code in the browser; the login is stored
on the server for the exact SSH user EvoLab invokes. There is no need to open a
separate server shell or prepare packages manually.

For diagnostics only, the equivalent manual checks are:

```bash
command -v codex
codex login --device-auth
codex login status
```

Logging in as another server user does not authenticate the account EvoLab
will invoke. Device-code login must be enabled in personal ChatGPT security
settings or by the ChatGPT workspace administrator.

Public Hugging Face models can be registered from a Project's **Execution
model** settings. Self-deployed execution requires an NVIDIA GPU and enough
disk/VRAM for the selected vLLM-compatible safetensors model. On Ubuntu GPU
servers, the launcher automatically installs a missing recommended NVIDIA
compute driver, Docker, and NVIDIA Container Toolkit and configures access for
the SSH user. If a new driver requires a reboot, reboot once and rerun the same
`evolab webui` command; preparation then resumes automatically. CPU-only
servers skip the GPU container stack. On container-style GPU rentals, the
launcher does not change GPU drivers, Docker configuration, services, or user
permissions. These rentals must already expose a user-accessible Docker daemon
with the NVIDIA runtime; otherwise use a Docker-enabled container or a full
Ubuntu GPU virtual machine. GPU setup needs access to Docker Hub and
NVIDIA's signed package repository, and model download needs access to Hugging
Face (or an administrator-configured `HF_ENDPOINT` mirror). Model files remain
in the daemon's private state root; the browser receives only model identity
and progress records. If a model download receives no new data for 90 seconds,
EvoLab marks it as stalled and offers **Resume download** in Project setup.
Matching partial files are preserved and reused by the retry.

Use `evolab webui --no-gpu` when the server has visible NVIDIA hardware but
this EvoLab deployment must remain CPU/Codex-subscription only. The launcher
then avoids NVIDIA probes, Docker setup, and local-model runtime changes.

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
