# Remote Server Setup

OpenEvo Desktop connects to a remote Linux server and installs OpenEvo Daemon
under the selected SSH account. The packaged managed science runtime is Linux
amd64. The `0.1.2` Preview has no general clean-host matrix; the requirements
below describe the exhibition profile, not general Linux support.

## Server Requirements

Before opening Desktop, confirm that:

- the server is reachable over SSH from the Mac;
- your remote account can write to its home directory;
- your Mac can authenticate to that account through its SSH agent;
- Docker Engine is installed and exposes the Docker API to that account using
  the exhibition user-container profile;
- the server has enough home-directory and container storage for the project,
  Daemon, runtime, transcripts, and artifacts;
- required outbound HTTPS access works directly or through the configured
  remote proxy;
- Codex CLI is installed and on `PATH` for that account;
- `codex login status` succeeds for a Codex subscription as that account.

OpenEvo does not automatically install or sign in to Codex in this Preview.
Codex authentication belongs to the remote SSH user. Complete the normal Codex
installation and login on the server before project activation.

If SSH agent authentication is not already working, load the required identity
into the Mac's agent. For example, `ssh-add -l` lists available identities and
`ssh-add ~/.ssh/<key>` adds one. OpenEvo never receives the private-key bytes.

## What Desktop Installs

Desktop transfers release-matched, integrity-checked assets and prepares:

- the OpenEvo Daemon and its isolated user-level Python environment;
- the verified method registry used by that Daemon;
- the controlled science runtime used for Preview sessions;
- the private SSH tunnel between Desktop and the Daemon.

You do not clone the repository, run `pip install`, choose a remote OpenEvo
path, or upload a runtime image. Desktop may provision its isolated Python
runtime in user space when the supported automatic path is available. Failure
is reported as a typed action rather than silently using another version.

## What Desktop Does Not Change

Desktop does not promise to prepare an arbitrary clean server. It does not
modify system packages, Docker daemon policy, `systemd`, drivers, firewalls,
global shell profiles, SSH server configuration, or SSH private keys. Required
administrator work remains the server owner's responsibility.

## Confirm The Host Key

The first connection pauses at **Confirm server identity**. Compare the
displayed algorithm and `SHA256:` fingerprint with a fingerprint supplied by
the server administrator through a separate trusted channel. Choose **Trust and
continue** only on a match.

A new fingerprint for an already trusted address can indicate a legitimate
server rebuild or an attack. Do not accept it until the administrator confirms
the change. OpenEvo will not silently replace the stored host identity.

## Codex And Research Data

Subscription sessions send the task input and required context through the
Codex subscription service. OpenEvo captures the resulting transcript on the
remote server for cross-session textual evolution. Remote project data,
transcripts, and artifacts remain until explicitly removed; uninstalling the
Mac application does not remove them.
