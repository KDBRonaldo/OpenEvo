"""Run the formal Desktop Sidecar as a localhost browser host.

The browser never receives SSH credentials and never opens a remote socket.
It talks to this loopback process, which owns system OpenSSH and the formal
remote Daemon lifecycle.
"""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import threading
import webbrowser
from pathlib import Path

import uvicorn

from desktop.server.launcher import _NativeLauncherFrame, create_app


def _is_wsl() -> bool:
    """Avoid handing the URL to a terminal browser inside WSL."""

    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text(
            encoding="utf-8"
        ).lower()
    except OSError:
        return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open OpenEvo in the system browser.")
    parser.add_argument("--static-root", type=Path, required=True)
    parser.add_argument("--desktop-config-root", type=Path, default=None)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--build-channel", choices=("development", "test"), default="development")
    parser.add_argument("--release-assets-root", type=Path, required=True)
    parser.add_argument("--ssh-askpass-helper-path", type=Path, required=True)
    parser.add_argument("--ssh-askpass-helper-sha256", required=True)
    parser.add_argument("--ssh-askpass-helper-byte-size", type=int, required=True)
    parser.add_argument("--no-open", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config_root = (
        args.desktop_config_root.expanduser().resolve()
        if args.desktop_config_root is not None
        else Path("~/.openevo/desktop-browser").expanduser().resolve()
    )
    config_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    bootstrap_token = secrets.token_hex(32)
    frame = _NativeLauncherFrame(
        instance_id=secrets.token_hex(16),
        readiness_key=secrets.token_bytes(32),
        session_token=secrets.token_hex(32),
        handoff_token=secrets.token_hex(32),
    )
    try:
        app = create_app(
            static_root=args.static_root,
            desktop_config_root=config_root,
            native_frame=frame,
            source_commit=args.source_commit,
            build_channel=args.build_channel,
            release_assets_root=args.release_assets_root,
            packaged_askpass_helper_path=args.ssh_askpass_helper_path,
            packaged_askpass_helper_sha256=args.ssh_askpass_helper_sha256,
            packaged_askpass_helper_byte_size=args.ssh_askpass_helper_byte_size,
            openssh_home=config_root / "managed-openssh-home",
            browser_endpoint=endpoint,
            browser_bootstrap_token=bootstrap_token,
        )
    except BaseException:
        listener.close()
        raise
    url = f"{endpoint}/openevo#browser-bootstrap={bootstrap_token}"
    print("OpenEvo browser host is ready.", flush=True)
    print(f"Open this complete URL in Edge, Chrome, or Safari:\n{url}", flush=True)
    print(
        "SSH and Daemon operations remain owned by the local Sidecar.",
        flush=True,
    )
    if not args.no_open and not _is_wsl():
        opener = threading.Timer(0.5, lambda: webbrowser.open(url, new=1, autoraise=True))
        opener.daemon = True
        opener.start()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0))
    try:
        server.run(sockets=[listener])
    finally:
        listener.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
