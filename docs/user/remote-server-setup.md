# Remote Server Setup Target

> Pre-release target: the complete fresh-server workflow is still under
> implementation and is not currently a supported user procedure.

Before the Daemon is installed, the Desktop native host/sidecar connects over
SSH, prepares OpenEvo-owned directories, uploads and verifies the exact
manifest-matched Daemon Bundle, installs it in a user-level environment, starts
the Daemon, and opens the local tunnel. After the Daemon is healthy, it owns
doctor/repair, upgrades, services, and runs.

The supported setup should configure process-level proxy variables, install
supported user-space dependencies, verify or install Codex CLI, pull managed
runtime assets, download the pinned self-deployed reference model, and start
user-owned services.

OpenEvo must not modify system packages, Docker daemon configuration, systemd,
global shell profiles, drivers, firewall policy, or SSH private keys. It reports
those requirements as explicit user actions and preserves completed setup work
for retry.
