from __future__ import annotations

import uvicorn

from openevo.evolution import cli as evolution_cli
from openevo.evolution.cli import build_parser


def test_evolution_cli_defaults_use_openevo_state_root() -> None:
    parser = build_parser()

    cases = [
        ["serve", "--framework-lock", "/tmp/framework-lock.json"],
        ["worker"],
    ]

    for argv in cases:
        args = parser.parse_args(argv)
        if hasattr(args, "db"):
            assert args.db == ".openevo/evolution/evolution.db"
        if hasattr(args, "artifact_root"):
            assert args.artifact_root == ".openevo/evolution"


def test_serve_passes_full_executable_registry_to_backend(
    tmp_path,
    monkeypatch,
) -> None:
    registry = object()
    app = object()
    observed: dict[str, object] = {}

    def fake_load_registry(lock_path):
        observed["lock_path"] = lock_path
        return registry

    monkeypatch.setattr(
        evolution_cli,
        "load_verified_framework_registry",
        fake_load_registry,
    )

    def fake_create_app(**kwargs):
        observed["create_app"] = kwargs
        return app

    def fake_uvicorn_run(candidate, *, host, port):
        observed["uvicorn"] = (candidate, host, port)

    monkeypatch.setattr(evolution_cli, "create_app", fake_create_app)
    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
    lock_path = tmp_path / "framework-lock.json"
    db_path = tmp_path / "evolution.db"
    artifact_root = tmp_path / "artifacts"

    result = evolution_cli.main(
        [
            "serve",
            "--framework-lock",
            str(lock_path),
            "--db",
            str(db_path),
            "--artifact-root",
            str(artifact_root),
            "--host",
            "127.0.0.2",
            "--port",
            "8300",
        ]
    )

    assert result == 0
    assert observed["lock_path"] == lock_path
    assert observed["create_app"] == {
        "db_path": db_path,
        "artifact_root": artifact_root,
        "executable_registry": registry,
    }
    assert observed["uvicorn"] == (app, "127.0.0.2", 8300)
