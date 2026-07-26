# Remote Server Setup

OpenEvo Desktop is the only application an ordinary user operates. It connects
through one literal alias from the Mac user's OpenSSH configuration and manages
the internal OpenEvo Daemon under that remote account.

The packaged v0.1.9 managed Science runtime is Linux amd64. Other Linux
architectures, container engines/policies, and arbitrary host layouts are not
supported by this Preview.

## Server Prerequisites

The server administrator or hosting environment must provide:

- a Linux x86-64 server reachable from the Mac through system OpenSSH;
- a selected remote account with a writable home directory (an approved root
  user inside a private user container is supported);
- Docker Engine access for the supported user-container profile;
- enough home-directory and container storage for Daemon state, the managed
  runtime, projects, transcripts, revisions, and artifacts;
- required outbound HTTPS access, directly or through server-side policy; and
- a supported Codex CLI installed and signed in to a subscription for that
  exact account.

Desktop performs the supported preflight and reports typed failures. It cannot
install system packages, grant Docker permissions, alter the SSH server, or log
Codex into a subscription. Ask the administrator to repair a missing host
prerequisite, then retry from Desktop.

## Mac OpenSSH Prerequisite

The Mac user must have a literal SSH alias in `~/.ssh/config`. System
`/usr/bin/ssh <alias>` is authoritative for:

- `HostName`, `User`, and `Port`;
- `Include`, `Match`, and canonicalization;
- `IdentityFile`, agent, and Keychain integration;
- password and key-passphrase prompts;
- `ProxyJump` or `ProxyCommand`; and
- known-host and changed-key policy.

OpenEvo stores only the chosen alias. It does not flatten these options into a
second configuration and does not ask users to paste private-key bytes. Native
askpass responses go directly to the owning OpenSSH process, not through the
React renderer or Desktop Local API.

Verify the alias before using Desktop:

```bash
/usr/bin/ssh <alias>
```

Confirm that it reaches the intended account, then exit. Do not install or run
OpenEvo commands there.

## What Desktop Manages

After the user selects the alias, Desktop:

- checks server compatibility;
- transfers and verifies the release-matched self-contained Daemon Bundle;
- installs, starts, upgrades, repairs, or reattaches the Daemon;
- verifies the executable evolution registry;
- prepares the controlled Science runtime;
- creates private SSH tunnels to the compatible Core v2 control API; and
- manages connection, Task, successor, service, and recovery actions.

Users do not clone this repository, install an `openevo` Python package, choose
a remote OpenEvo path, upload a runtime image, or start/stop the Daemon in a
shell. After Daemon compatibility is established, business actions use Core v2
through the active project tunnel and never fall back to SSH commands or Core
v1.

## Host Trust

First-use and changed-key behavior follows the user's OpenSSH policy. Verify a
new fingerprint with the server administrator over a trusted channel before
accepting it. Treat an unexpected changed key as a possible attack or server
replacement; reject it until the administrator confirms the change. OpenEvo
does not maintain a competing private known-host database.

## Codex And Research Data

Subscription Tasks send required prompts/context through the Codex subscription
service. OpenEvo captures the remote transcript for pure-text evolution; it
does not claim token-level metrics. Remote project data, transcripts, revisions,
and artifacts remain until explicitly removed. Uninstalling the Mac app does
not remove them.
