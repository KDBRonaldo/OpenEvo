from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import socket
import sys
import threading

from openevo import __version__
from openevo.backend.contracts.v2 import models as core_v2_models
from openevo.backend.contracts.v2.app import create_core_control_v2_contract_app
from openevo.backend.contracts.v2.provider import CoreControlProviderV2
from openevo.backend.contracts.v2.store import CoreControlStoreV2
from openevo.backend.project_authority_v2 import ProjectAuthorityV2
from openevo.backend.run_admission import install_core_run_admission_endpoint
from openevo.backend.runtime_identity import (
    HostServiceRoot,
    canonical_json_bytes,
    compute_release_identity,
    load_or_create_core_bearer_token,
    release_runtime_contract_sha256,
    require_host_global_service_root,
)
from openevo.backend.science_execution_v2 import ScienceAttemptExecutorV2
from openevo.backend.science_run_owner import CoreScienceTaskOwnerV2
from openevo.backend.science_successor_preparer_v2 import (
    ProductionScienceSuccessorPreparerV2,
)
from openevo.backend.service import claim_core_service_spawn
from openevo.backend.service_supervisor import CoreServiceSupervisor, ServiceLaunchMode
from openevo.backend.workspace_handoff_v2 import WorkspaceHandoffStoreV2
from openevo.backend.workspace_store_v2 import WorkspaceStoreV2
from openevo.evolution.framework import (
    EvolutionExecutionProfile,
    load_verified_framework_registry,
)
from openevo.evolution.framework.builtins import VerifiedExecutableRegistry
from openevo.evolution.parametric.trainer_service import sd_lora_trainer_available
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


@dataclass(slots=True)
class _ReleaseDaemonV2Composition:
    app: object
    provider: CoreControlProviderV2
    catalog: CoreControlStoreV2
    workspaces: WorkspaceStoreV2
    workspace_handoffs: WorkspaceHandoffStoreV2
    task_owner: CoreScienceTaskOwnerV2
    project_authority: ProjectAuthorityV2
    attempt_executor: ScienceAttemptExecutorV2
    successor_preparer: ProductionScienceSuccessorPreparerV2
    service_supervisor: object
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _provider_closed: bool = False
    _handoffs_closed: bool = False
    _supervisor_closed: bool = False

    def close(self) -> None:
        with self._lock:
            if not self._provider_closed:
                self.provider.close()
                self._provider_closed = True
            if not self._handoffs_closed:
                self.workspace_handoffs.close()
                self._handoffs_closed = True
            if not self._supervisor_closed:
                self.service_supervisor.close()
                self._supervisor_closed = True

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)


def _build_release_daemon_v2_composition(
    *,
    state_root: str | Path,
    bearer_token: str,
    source_commit: str,
    executable_registry: VerifiedExecutableRegistry,
    service_supervisor: object,
    runtime_contract_sha256: str,
) -> _ReleaseDaemonV2Composition:
    """Build the only mutation authority shipped by the release Daemon."""

    root = Path(state_root).expanduser().absolute()
    catalog: CoreControlStoreV2 | None = None
    workspaces: WorkspaceStoreV2 | None = None
    handoffs: WorkspaceHandoffStoreV2 | None = None
    task_owner: CoreScienceTaskOwnerV2 | None = None
    project_authority: ProjectAuthorityV2 | None = None
    provider: CoreControlProviderV2 | None = None
    executor_holder: dict[str, ScienceAttemptExecutorV2] = {}
    preparer_holder: dict[str, ProductionScienceSuccessorPreparerV2] = {}
    try:
        catalog = CoreControlStoreV2(root / "core-control-v2")
        workspaces = WorkspaceStoreV2(root / "workspaces-v2")
        handoff_root = getattr(service_supervisor, "workspace_handoff_root")
        handoffs = WorkspaceHandoffStoreV2(handoff_root)

        def build_executor(ledger) -> ScienceAttemptExecutorV2:
            executor = ScienceAttemptExecutorV2(
                catalog=catalog,
                workspaces=workspaces,
                workspace_handoffs=handoffs,
                ledger=ledger,
                services=service_supervisor,
                executable_registry=executable_registry,
                prior_dataset_artifact_ids=(
                    lambda head: ledger.prior_dataset_artifact_ids_for_head(head.project_head_id)
                ),
            )
            executor_holder["value"] = executor
            return executor

        def build_preparer(ledger) -> ProductionScienceSuccessorPreparerV2:
            preparer = ProductionScienceSuccessorPreparerV2(
                catalog=catalog,
                ledger=ledger,
                workspaces=workspaces,
                workspace_handoffs=handoffs,
                services=service_supervisor,
                executable_registry=executable_registry,
            )
            preparer_holder["value"] = preparer
            return preparer

        task_owner = CoreScienceTaskOwnerV2(
            state_root=root,
            attempt_executor_factory=build_executor,
            successor_preparer_factory=build_preparer,
        )
        if not task_owner.production_ready:
            raise RuntimeError("release v2 Task owner did not complete recovery")
        project_authority = ProjectAuthorityV2(
            catalog_store=catalog,
            workspace_store=workspaces,
            task_owner=task_owner,
            executable_registry=executable_registry,
            service_binding_provider=service_supervisor,
            runtime_contract_sha256=runtime_contract_sha256,
        )
        provider = CoreControlProviderV2(
            catalog,
            task_owner=task_owner,
            executable_registry=executable_registry,
            project_authority=project_authority,
            service_authority=service_supervisor,
            bearer_token=bearer_token,
            release_version=__version__,
            source_commit=source_commit,
            build_channel="release",
            runtime_contract_sha256=runtime_contract_sha256,
        )
        app = create_core_control_v2_contract_app(provider)
        install_core_run_admission_endpoint(app, service_supervisor, task_owner)
        composition = _ReleaseDaemonV2Composition(
            app=app,
            provider=provider,
            catalog=catalog,
            workspaces=workspaces,
            workspace_handoffs=handoffs,
            task_owner=task_owner,
            project_authority=project_authority,
            attempt_executor=executor_holder["value"],
            successor_preparer=preparer_holder["value"],
            service_supervisor=service_supervisor,
        )
        app.state.core_control_provider = provider
        app.state.release_daemon_v2_composition = composition
        app.router.add_event_handler("shutdown", composition.aclose)
        return composition
    except BaseException:
        if provider is not None:
            try:
                provider.close()
            except BaseException:
                pass
        else:
            if task_owner is not None:
                try:
                    task_owner.close()
                except BaseException:
                    pass
            if project_authority is not None:
                try:
                    project_authority.close()
                except BaseException:
                    pass
            elif workspaces is not None:
                workspaces.close()
            if catalog is not None:
                catalog.close()
        if handoffs is not None:
            handoffs.close()
        try:
            service_supervisor.close()
        except BaseException:
            pass
        raise


def _release_daemon_v2_ready_payload(
    provider: CoreControlProviderV2,
    *,
    generation: str,
    release_identity: str,
) -> dict[str, object]:
    version = provider.invoke("discoverCoreContractVersionV2", {})
    if type(version) is not core_v2_models.VersionResponseV2:
        raise RuntimeError("release v2 discovery authority is unavailable")
    offers = [offer for offer in version.contracts if offer.api_major == 2]
    if len(offers) != 1 or version.mutation_major != 2:
        raise RuntimeError("release v2 mutation contract is unavailable")
    offer = offers[0]
    return {
        "schema_version": 2,
        "generation": generation,
        "release_identity": release_identity,
        "api_major": 2,
        "openapi_sha256": offer.openapi_sha256,
        "event_schema_sha256": offer.event_schema_sha256,
        "release_version": version.release_version,
        "build_id": version.build_id,
        "source_commit": version.source_commit,
        "provider_kind": version.provider_kind,
        "feature_set_sha256": version.feature_set_sha256,
        "registry_digest": version.registry_sha256,
        "runtime_contract_sha256": version.runtime_contract_sha256,
    }


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
        or not _is_inherited_listener(inherited_socket)
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
    composition = _build_release_daemon_v2_composition(
        state_root=state_root,
        bearer_token=bearer_token,
        source_commit=args.source_commit,
        executable_registry=registry,
        service_supervisor=service_supervisor,
        runtime_contract_sha256=release_runtime_contract_sha256(),
    )
    app = composition.app
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
                ready_payload=_release_daemon_v2_ready_payload(
                    composition.provider,
                    generation=args.generation,
                    release_identity=release.digest,
                ),
            )
        )
    finally:
        try:
            composition.close()
        finally:
            inherited_socket.close()


def _is_inherited_listener(value: socket.socket) -> bool:
    if value.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
        return False
    try:
        return value.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
    except OSError as exc:
        # Darwin declares SO_ACCEPTCONN but returns ENOPROTOOPT when it is read.
        # The release Daemon runs only on Linux; this branch keeps Mac-side
        # packaging tests non-mutating while preserving the Linux listen proof.
        if sys.platform == "darwin" and exc.errno in {42, 102}:
            return True
        raise


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
    runtime_capabilities: tuple[str, ...] = ()
    if not subscription:
        runtime_capabilities = ("adapter_serving",)
        if sd_lora_trainer_available():
            runtime_capabilities += ("gpu", "sd_lora_continual_trainer")
    return EvolutionExecutionProfile(
        execution_mode="subscription" if subscription else "self_deployed",
        capture_mode=capture_mode,
        harness_id=config.agent.preset,
        runtime_capabilities=runtime_capabilities,
    )


if __name__ == "__main__":
    raise SystemExit(main())
