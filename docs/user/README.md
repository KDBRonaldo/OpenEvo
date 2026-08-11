# OpenEvo Desktop User Guide

OpenEvo Preview has two applications:

- **OpenEvo Desktop Client** runs on macOS.
- **OpenEvo Daemon** runs as an internal service under the selected SSH account
  on a remote Linux server. Ordinary users manage it through Desktop and do not
  operate it directly.

Version `0.1.10` is the immutable Preview described by these guides. Its exact
DMG and `SHA256SUMS` are available from the immutable
[OpenEvo Desktop 0.1.10 Preview release](https://github.com/CompLifeLab-ZJU/OpenEvo/releases/tag/openevo-desktop-v0.1.10-v0110.31465722294.2).
The Preview DMG is unsigned and not notarized. Open it only after checking the
exact checksum in the quickstart. Use the current release unless a maintainer
has asked you to reproduce an older version.

## Start Here

1. [Check host prerequisites](remote-server-setup.md).
2. [Install Desktop and run two Tasks](desktop-quickstart.md).
3. [Configure a restricted network](proxy-and-network.md), if needed.
4. [Resolve typed errors](troubleshooting.md).

## Preview Scope

This Preview packages:

- one Apple Silicon macOS 12+ asset and matching Linux x86-64 Daemon Bundle;
- two built-in, read-only science project tours, each showing three
  Tasks and the three textual evolution targets without contacting a
  server;
- system OpenSSH aliases, including configured identity, agent/Keychain,
  proxy-jump/command, prompt, and known-host policy;
- a host whose remote Codex CLI is already installed and signed in for the
  selected SSH user;
- the intended Codex subscription transcript path and textual memory, skill
  bundle, and agent-system targets.

It does not support Self-Deployed execution, parameter or adapter evolution,
other agent harnesses, a public CLI or PyPI installation, in-session evolution,
or automatic Codex login. It supports only the documented exhibition host
profile and should not be treated as a general Linux deployment or a
production-critical research service.

Desktop uploads and installs the version-matched OpenEvo Daemon and managed
science runtime, starts the remote services, and maintains the private tunnel.
You do not install an `openevo` Python package, upload a runtime image, or
operate the remote Daemon manually.
