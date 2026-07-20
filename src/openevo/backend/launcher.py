from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket

from openevo import __version__
from openevo.backend.contracts.v1.provider import create_core_control_app
from openevo.backend.runtime_identity import (
    HostServiceRoot,
    canonical_json_bytes,
    compute_release_identity,
    load_or_create_core_bearer_token,
    require_host_global_service_root,
)
from openevo.backend.science_run_owner import CoreScienceRunOwner
from openevo.backend.service import claim_core_service_spawn
from openevo.backend.service_supervisor import CoreServiceSupervisor, ServiceLaunchMode
from openevo.evolution.framework import (
    EvolutionExecutionProfile,
    load_verified_framework_registry,
)
from openevo.harness.capture import transcript_capture_enabled
from openevo.runtime.managed import require_managed_runtime_binding


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openevo-backend")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="Start supervised Core Control.")
    serve.add_argument("--service-root", type=Path, required=True)
    serve.add_argument("--framework-lock", type=Path, required=True)
    serve.add_argument("--source-commit", required=True)
    serve.add_argument("--socket-fd", type=int, required=True)
    serve.add_argument("--ready-fd", type=int, required=True)
    serve.add_argument("--spawn-lock-fd", type=int, required=True)
    serve.add_argument("--expected-release-identity", required=True)
    serve.add_argument("--generation", required=True)
    run = subparsers.add_parser("run", help="Run an OpenEvo experiment snapshot.")
    run.add_argument("config", type=Path)
    run.add_argument("--output-dir", type=Path)
    run.add_argument("--artifact-root", type=Path)
    run.add_argument("--rounds", type=int, dest="rounds_override")
    run.add_argument("--task-id", action="append", default=[])
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--json", action="store_true")
    run.add_argument("--framework-lock", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return _serve_core_control(args)
    if args.command == "run":
        return _run_experiment_command(args)
    raise ValueError(args.command)


def _serve_core_control(args: argparse.Namespace) -> int:
    descriptors = {args.socket_fd, args.ready_fd, args.spawn_lock_fd}
    if min(descriptors) < 3 or len(descriptors) != 3:
        raise RuntimeError("Core supervisor descriptors are invalid")
    require_host_global_service_root(args.service_root)
    inherited_socket = socket.socket(fileno=args.socket_fd)
    inherited_socket.set_inheritable(False)
    host, port = inherited_socket.getsockname()[:2]
    if (
        inherited_socket.family != socket.AF_INET
        or host != "127.0.0.1"
        or not 0 < port <= 65535
        or inherited_socket.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1
    ):
        inherited_socket.close()
        raise RuntimeError("Core supervisor socket is not a listening IPv4 loopback socket")
    claim_core_service_spawn(
        service_root=args.service_root,
        spawn_lock_fd=args.spawn_lock_fd,
        release_identity=args.expected_release_identity,
        port=port,
        generation=args.generation,
    )
    registry = load_verified_framework_registry(args.framework_lock)
    release = compute_release_identity(
        framework_lock=args.framework_lock,
        registry=registry,
        source_commit=args.source_commit,
    )
    if release.digest != args.expected_release_identity:
        raise RuntimeError("Core release identity does not match the supervisor")
    with HostServiceRoot(args.service_root, create=False) as root:
        state_root = root.ensure_directory("state")
        bearer_token = load_or_create_core_bearer_token(root)
    service_supervisor = CoreServiceSupervisor(
        launch_mode=ServiceLaunchMode.RELEASE,
        service_root=args.service_root / "managed-services",
        framework_lock=args.framework_lock,
        verified_registry=registry,
        run_admission_url=(
            f"http://127.0.0.1:{port}/internal/v1/run-admissions/verify"
        ),
    )
    try:
        app = create_core_control_app(
            state_root=state_root,
            bearer_token=bearer_token,
            source_commit=args.source_commit,
            build_channel="release",
            enable_maintenance_owner=True,
            evolution_registry=registry,
            service_supervisor=service_supervisor,
            run_control_factory=lambda store: CoreScienceRunOwner(
                state_root=state_root,
                project_store=store,
                service_supervisor=service_supervisor,
                executable_registry=registry,
            ),
        )
    except BaseException:
        service_supervisor.close()
        raise
    _bind_host_service_identity(
        app,
        generation=args.generation,
        release_identity=release.digest,
    )
    try:
        return asyncio.run(
            _run_supervised_server(
                app,
                inherited_socket=inherited_socket,
                ready_fd=args.ready_fd,
                ready_payload={
                    "schema_version": 1,
                    "generation": args.generation,
                    "release_identity": release.digest,
                    "registry_digest": release.registry_digest,
                },
            )
        )
    finally:
        inherited_socket.close()


def _bind_host_service_identity(
    app: object,
    *,
    generation: str,
    release_identity: str,
) -> None:
    @app.middleware("http")
    async def add_host_service_identity(request, call_next):
        response = await call_next(request)
        response.headers["X-OpenEvo-Core-Generation"] = generation
        response.headers["X-OpenEvo-Core-Release-Identity"] = release_identity
        return response


async def _run_supervised_server(
    app: object,
    *,
    inherited_socket: socket.socket,
    ready_fd: int,
    ready_payload: dict[str, object],
) -> int:
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=None,
            port=None,
            access_log=False,
            lifespan="on",
            log_config=None,
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[inherited_socket]))
    signalled = False
    try:
        while not server.started:
            if server_task.done():
                await server_task
                raise RuntimeError("Core server exited before ASGI readiness")
            await asyncio.sleep(0.01)
        payload = canonical_json_bytes(ready_payload) + b"\n"
        written = os.write(ready_fd, payload)
        if written != len(payload):
            raise RuntimeError("Core readiness signal was incomplete")
        signalled = True
        return_code = await server_task
        del return_code
        return 0
    finally:
        os.close(ready_fd)
        if not signalled and not server_task.done():
            server.should_exit = True
        if not server_task.done():
            await server_task


def _run_experiment_command(args: argparse.Namespace) -> int:
    from openevo.experiments import dry_run_experiment, load_experiment_config, run_experiment

    config = load_experiment_config(args.config)
    registry = load_verified_framework_registry(args.framework_lock)
    execution_profile = _execution_profile_for_config(config)
    task_ids = args.task_id or None
    if args.dry_run:
        result = dry_run_experiment(
            config,
            task_ids=task_ids,
            rounds_override=args.rounds_override,
            registry_snapshot=registry.snapshot,
            execution_profile=execution_profile,
        )
    else:
        result = run_experiment(
            config,
            task_ids=task_ids,
            rounds_override=args.rounds_override,
            output_dir=args.output_dir,
            artifact_root=args.artifact_root,
            executable_registry=registry,
            execution_profile=execution_profile,
        )
    output = json.dumps(result, indent=None if args.json else 2, sort_keys=True)
    print(output)
    return 0


def _execution_profile_for_config(config) -> EvolutionExecutionProfile:
    require_managed_runtime_binding(
        profile=config.runtime.profile,
        image=config.runtime.image,
        backend=config.runtime.kind,
        container_user=config.runtime.container_user,
    )
    subscription = config.agent.auth in {"subscription", "chatgpt_subscription"}
    configured_capture = config.agent.settings.get("capture_mode")
    capture_mode = (
        "transcript"
        if subscription or transcript_capture_enabled(configured_capture)
        else "token_level"
    )
    return EvolutionExecutionProfile(
        execution_mode="subscription" if subscription else "self_deployed",
        capture_mode=capture_mode,
        harness_id=config.agent.preset,
        runtime_capabilities=("adapter_serving",) if not subscription else (),
    )


if __name__ == "__main__":
    raise SystemExit(main())
