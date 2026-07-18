# OpenEvo Preview User Documentation

OpenEvo Preview has two applications:

- **OpenEvo Desktop Client** runs on macOS.
- **OpenEvo Daemon** runs under your SSH account on a remote Linux server.

Install Desktop from the DMG on the project's
[GitHub Releases](https://github.com/CompLifeLab-ZJU/OpenEvo/releases) page.
The Preview DMG is unsigned and not notarized. Open it only after checking that
the release and checksum are the ones you intended to install.

## Start Here

1. [Prepare the remote server](remote-server-setup.md).
2. [Install Desktop and run two sessions](desktop-quickstart.md).
3. [Configure a restricted network](proxy-and-network.md), if needed.
4. [Resolve typed errors](troubleshooting.md).

## Preview Scope

This first Preview supports:

- a release-listed macOS build and remote Linux x86-64 host;
- SSH agent authentication;
- a remote Codex CLI that is already installed and signed in for the SSH user;
- Codex subscription execution with transcript capture;
- cross-session textual memory, skill bundle, and agent-system evolution.

It does not support Self-Deployed execution, parameter or adapter evolution,
other agent harnesses, a public CLI or PyPI installation, in-session evolution,
or automatic Codex login. Exact supported operating-system versions and asset
checksums are listed with each release.

Desktop uploads and installs the version-matched OpenEvo Daemon and managed
science runtime. You do not install an `openevo` Python package or upload a
runtime image.
