# EvoLab {{VERSION}} for Windows, macOS, Linux, and WSL

> **Supported local platforms:** native Windows PowerShell, macOS, Linux, and WSL. Every platform uses the same ZIP package.

## Download this file

Under **Assets**, download exactly:

**`evolab-launcher.zip`**

Do not download GitHub's automatically generated **Source code (zip)** or **Source code (tar.gz)** files. Those are repository snapshots, not the EvoLab launcher package.

## 1. Prepare your client environment

Your local environment needs:

- Python 3.11 or newer: `python3 --version`
- OpenSSH on this OS, or OpenSSH in WSL on Windows
- an archive extractor (`Expand-Archive` is built into PowerShell)

You do **not** need Git, uv, Node.js, npm, or an OpenEvo/EvoLab source checkout.

## 2. Prepare the remote Linux server

The server must be reachable over SSH, provide `apt-get`, `dnf`, `yum`, or `apk`, and allow the selected account to use root or passwordless `sudo`. It also needs outbound HTTPS access to install dependencies, download Codex, and complete ChatGPT authentication. EvoLab automatically installs missing Python, curl, certificates, and core utilities.

On full Ubuntu virtual machines with an NVIDIA GPU, EvoLab also automatically installs a missing recommended NVIDIA compute driver, Docker, and NVIDIA Container Toolkit, configures Docker GPU access, and grants the SSH user access. A newly installed driver can require one reboot; reboot the server once and run the same `evolab webui` command again. CPU-only servers skip these GPU packages. On container-style GPU rentals, EvoLab does not change GPU drivers, Docker configuration, services, or user permissions; these rentals must already expose a user-accessible Docker daemon with the NVIDIA runtime. GPU model serving requires enough disk and VRAM plus outbound access to Docker Hub, NVIDIA's signed package repository, and Hugging Face.

Pass `--no-gpu` for a CPU/Codex-subscription-only deployment on a host whose
NVIDIA devices belong to other workloads. EvoLab then disables self-deployed
model management and does not probe or modify the NVIDIA and Docker runtime.
Existing model files and unrelated containers are left untouched.

You do not need to install or log in to Codex on the server manually. On the first `evolab webui` run, EvoLab installs the official Codex CLI for the **exact same SSH user that EvoLab will use**. It then prints a one-time device code in the terminal and opens the ChatGPT verification page on this computer. Enter the displayed code in the browser; EvoLab continues automatically after login succeeds.

Device-code login must be enabled in your personal ChatGPT security settings or by your ChatGPT workspace administrator. Authentication belongs to the selected server user: logging in as `root` does not authenticate `ubuntu`, and vice versa.

## 3. Configure SSH locally

Add a concrete alias to `~/.ssh/config`:

```sshconfig
Host evolab-server
  HostName server.example.com
  User researcher
  Port 22
  IdentityFile ~/.ssh/id_ed25519
```

Verify it before starting EvoLab:

```bash
ssh evolab-server
exit
```

## 4. Install and run EvoLab

From the directory where the ZIP was downloaded:

```bash
unzip evolab-launcher.zip
sh evolab-launcher/install.sh
~/.local/bin/evolab webui
```

On native Windows PowerShell instead run:

```powershell
Expand-Archive .\evolab-launcher.zip -DestinationPath . -Force
powershell -ExecutionPolicy Bypass -File .\evolab-launcher\install.ps1
evolab webui
```

Windows automatically discovers aliases from both its native SSH config and
installed WSL distributions. If the alias is in WSL, EvoLab keeps SSH keys,
agents, ProxyJump rules, and known-host records inside WSL. It prefers Ubuntu;
override this with `--ssh-client wsl --wsl-distribution <name>`.

After opening a new terminal, the formal command is normally available directly:

```bash
evolab webui
```

`openevo webui` remains only as a compatibility command. The launcher discovers the SSH alias, delivers the matching server package, creates a local SSH tunnel, and opens EvoLab in the browser. The server does not fetch EvoLab product source from GitHub.

## Common problems

- **No server is listed:** confirm `Host evolab-server` exists in `~/.ssh/config` and is not a wildcard-only entry.
- **SSH connection fails:** run `ssh evolab-server` and fix the host, port, user, key, or server firewall first.
- **Automatic preparation fails:** confirm the SSH account has root/passwordless `sudo`, a supported package manager, and outbound HTTPS access, then retry `evolab webui`.
- **A GPU driver was installed:** reboot the GPU server once, reconnect if its public IP changed, and rerun the same `evolab webui` command. Docker and NVIDIA runtime setup will continue automatically.
- **The device code is rejected:** enable device-code login in ChatGPT security/workspace settings and retry. Use `command -v codex`, `codex login --device-auth`, and `codex login status` as the same SSH user only for diagnostics.
- **Python is too old:** confirm Python 3.11+ locally with `python3 --version`.

## Changes in this release

{{CHANGELOG}}
