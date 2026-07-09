# Remote Server Setup

OpenEvo Desktop prepares as much of a fresh remote GPU server as it can from
user-level permissions.

Desktop may create `~/.openevo`, install the exact Core Backend bundle into a
Python environment or user site, configure process-level proxy variables, pull
managed runtime images, download model snapshots, and start user-owned backend
services.

Desktop must not modify system packages, Docker daemon configuration, systemd,
global shell profiles, or SSH private keys. Codex subscription login remains a
user action on the remote server.
