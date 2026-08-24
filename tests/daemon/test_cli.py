from __future__ import annotations

from openevo.daemon.cli import build_parser


def test_lifecycle_commands_accept_instance_state_root() -> None:
    parser = build_parser()

    start = parser.parse_args(["start", "--state-root", "/tmp/oev", "--port", "9000"])
    status = parser.parse_args(["status", "--state-root", "/tmp/oev"])

    assert start.command == "start"
    assert start.port == 9000
    assert str(start.state_root) == "/tmp/oev"
    assert status.command == "status"
