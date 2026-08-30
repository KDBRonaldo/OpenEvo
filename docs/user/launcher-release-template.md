# EvoLab {{VERSION}} for macOS and WSL

> **Supported local platforms:** macOS and WSL. Native Windows PowerShell is not supported in this pre-release. Both supported environments use the same ZIP package.

## Download this file

Under **Assets**, download exactly:

**`evolab-launcher.zip`**

Do not download GitHub's automatically generated **Source code (zip)** or **Source code (tar.gz)** files. Those are repository snapshots, not the EvoLab launcher package.

## 1. Prepare your Mac or WSL environment

Your local environment needs:

- Python 3.11 or newer: `python3 --version`
- the system OpenSSH client: `ssh -V`
- `unzip`

You do **not** need Git, uv, Node.js, npm, or an OpenEvo/EvoLab source checkout.

## 2. Prepare the remote Linux server

The server must be reachable over SSH and have Python 3 plus standard POSIX tools. Install the Codex CLI on the server if it is not already available:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

Then log in as the **exact same SSH user that EvoLab will use** and authenticate Codex:

```bash
ssh evolab-server
command -v codex
codex login --device-auth
codex login status
exit
```

EvoLab checks Codex authentication for this same remote user; logging in as `root` does not authenticate `ubuntu`, and vice versa.

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

After opening a new terminal, the formal command is normally available directly:

```bash
evolab webui
```

`openevo webui` remains only as a compatibility command. The launcher discovers the SSH alias, delivers the matching server package, creates a local SSH tunnel, and opens EvoLab in the browser. The server does not fetch EvoLab product source from GitHub.

## Common problems

- **No server is listed:** confirm `Host evolab-server` exists in `~/.ssh/config` and is not a wildcard-only entry.
- **SSH connection fails:** run `ssh evolab-server` and fix the host, port, user, key, or server firewall first.
- **Remote Codex is missing:** SSH to the server and check `command -v codex`.
- **Codex is not logged in:** run `codex login --device-auth` and `codex login status` as the same SSH user configured above.
- **Python is too old:** confirm Python 3.11+ locally with `python3 --version`.

## Changes in this release

{{CHANGELOG}}
