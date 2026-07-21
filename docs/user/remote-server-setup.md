# Remote Server Setup

OpenEvo Desktop is the only application an ordinary user operates. It connects
to a remote Linux server and installs, starts, updates, repairs, and attaches
the OpenEvo Daemon under the selected SSH account. The Daemon is an internal
remote service; ordinary users do not SSH to it or run its commands.

The packaged managed science runtime is Linux amd64. Version `0.1.4` supports
only the exhibition profile below; other Linux architectures, container
policies, and host layouts are not supported by this Preview.

## Host Prerequisites

These are capabilities that the server administrator or hosting environment
must provide for the Preview:

- the server is reachable over SSH from the Mac;
- the selected remote account can write to its home directory;
- the account has Docker user-container access using the exhibition profile;
- the server has enough home-directory and container storage for projects,
  Daemon state, the managed runtime, transcripts, and artifacts;
- required outbound HTTPS access works directly or through the configured
  remote proxy;
- a supported Codex CLI is already installed and signed in for the selected
  remote account.

The ordinary user does not need to perform these checks in a remote shell.
Desktop runs the supported preflight and shows a typed result. If a host
prerequisite is missing in this Preview, ask the server administrator to
prepare the host or choose another supported host, then use **Retry** in
Desktop.

The Mac must have an SSH identity available to its configured SSH agent because
this Preview does not accept private-key bytes in the app. This is a local
credential prerequisite, not a request to log in to the remote server or
operate the Daemon manually.

## What Desktop Does Automatically

Desktop transfers release-matched, integrity-checked assets and manages:

- the OpenEvo Daemon and its isolated user-level Python environment;
- the verified method registry used by that Daemon;
- the controlled science runtime used for Preview sessions;
- the private SSH tunnel between Desktop and the Daemon;
- readiness checks, service startup, reconnection, upgrade, and supported
  repair actions.

You do not clone the repository, install an OpenEvo package, choose a remote
OpenEvo path, upload a runtime image, start or stop the Daemon, or copy remote
commands from this guide. Desktop reports failures as typed actions rather than
silently using another version.

## Host-Level Limitations

Desktop does not modify system packages, Docker daemon policy, `systemd`,
drivers, firewalls, global shell profiles, SSH server configuration, or SSH
private keys. Those host-level responsibilities belong to the server owner.
OpenEvo-owned installation and lifecycle work remains fully controlled by
Desktop.

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
