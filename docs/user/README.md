# OpenEvo Preview User Documentation

OpenEvo Preview has two applications:

- **OpenEvo Desktop Client** runs on macOS.
- **OpenEvo Daemon** runs under your SSH account on a remote Linux server.

Version `0.1.2` is the Preview candidate described by these guides. Install it
only after its exact DMG and `SHA256SUMS` appear together in the immutable
[GitHub Release](https://github.com/CompLifeLab-ZJU/OpenEvo/releases); until
then, `0.1.1` remains the current public historical Preview. The Preview DMG is
unsigned and not notarized. Open it only after checking the exact checksum in
the quickstart.

## Start Here

1. [Prepare the remote server](remote-server-setup.md).
2. [Install Desktop and run two sessions](desktop-quickstart.md).
3. [Configure a restricted network](proxy-and-network.md), if needed.
4. [Resolve typed errors](troubleshooting.md).

## Preview Scope

This Preview packages:

- one Apple Silicon macOS 12+ asset and matching Linux x86-64 Daemon Bundle;
- a built-in, read-only synthetic science project tour showing three task
  sessions and the three textual evolution targets without contacting a server;
- SSH agent authentication;
- a remote Codex CLI that is already installed and signed in for the SSH user;
- the intended Codex subscription transcript path and textual memory, skill
  bundle, and agent-system targets.

It does not support Self-Deployed execution, parameter or adapter evolution,
other agent harnesses, a public CLI or PyPI installation, in-session evolution,
or automatic Codex login. Its publication workflow verifies the exact Desktop
and Daemon package composition, but it has no canonical two-session science gate
or general clean-host support evidence. Use it only for the documented
exhibition profile.

Desktop uploads and installs the version-matched OpenEvo Daemon and managed
science runtime. You do not install an `openevo` Python package or upload a
runtime image.
