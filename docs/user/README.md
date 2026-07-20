# OpenEvo Preview User Documentation

OpenEvo Preview has two applications:

- **OpenEvo Desktop Client** runs on macOS.
- **OpenEvo Daemon** runs as an internal service under the selected SSH account
  on a remote Linux server. Ordinary users manage it through Desktop and do not
  operate it directly.

Version `0.1.3` is the current immutable Preview described by these guides. Its
exact DMG and `SHA256SUMS` are available in the
[OpenEvo Desktop 0.1.3 Preview release](https://github.com/CompLifeLab-ZJU/OpenEvo/releases/tag/openevo-desktop-v0.1.3-exhibition.29756612736.1).
The Preview DMG is unsigned and not notarized. Open it only after checking the
exact checksum in the quickstart. Earlier Preview releases are retained only as
historical evidence.

## Start Here

1. [Check host prerequisites](remote-server-setup.md).
2. [Install Desktop and run two sessions](desktop-quickstart.md).
3. [Configure a restricted network](proxy-and-network.md), if needed.
4. [Resolve typed errors](troubleshooting.md).

## Preview Scope

This Preview packages:

- one Apple Silicon macOS 12+ asset and matching Linux x86-64 Daemon Bundle;
- two built-in, read-only synthetic science project tours, each showing three
  task sessions and the three textual evolution targets without contacting a
  server;
- SSH agent authentication;
- a host whose remote Codex CLI is already installed and signed in for the
  selected SSH user;
- the intended Codex subscription transcript path and textual memory, skill
  bundle, and agent-system targets.

It does not support Self-Deployed execution, parameter or adapter evolution,
other agent harnesses, a public CLI or PyPI installation, in-session evolution,
or automatic Codex login. Its publication workflow verifies the exact Desktop
and Daemon package composition, but it has no canonical two-session science gate
or general clean-host support evidence. Use it only for the documented
exhibition profile.

Desktop uploads and installs the version-matched OpenEvo Daemon and managed
science runtime, starts the remote services, and maintains the private tunnel.
You do not install an `openevo` Python package, upload a runtime image, or
operate the remote Daemon manually.
