from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

from openevo.evolution.framework import load_verified_framework_registry
from openevo.evolution.framework.execution import MethodExecutionServices
from openevo.evolution.harness_service import (
    CodexSubscriptionHarnessService,
    CoreGatewayHarnessService,
    core_gateway_base_url_from_environment,
)
from openevo.evolution.parametric.trainer_service import (
    SubprocessSdLoraTrainerService,
)
from openevo.evolution.methods import METHOD_REGISTRY
from openevo.evolution.server import create_app
from openevo.evolution.worker import EvolutionWorkerClient, run_once
from openevo.internal_auth import (
    inherited_listen_fd,
    read_internal_service_identity,
    verified_private_file_sha256,
)


_MAX_FRAMEWORK_LOCK_BYTES = 4 * 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m openevo.evolution.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Start the Evolution Backend.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8200)
    serve.add_argument("--db", default=".openevo/evolution/evolution.db")
    serve.add_argument("--artifact-root", default=".openevo/evolution")
    serve.add_argument("--framework-lock", type=Path, required=True)

    worker = subparsers.add_parser("worker", help="Run an Evolution reference worker.")
    worker.add_argument("--base-url", default="http://127.0.0.1:8200")
    worker.add_argument("--worker-id", default="reference-worker")
    worker.add_argument("--capability", action="append", default=[])
    worker.add_argument("--artifact-root", default=".openevo/evolution")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--sleep-seconds", type=float, default=5.0)
    worker.add_argument("--lease-seconds", type=int, default=600)
    worker.add_argument("--framework-lock", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "serve":
        import uvicorn

        registry = load_verified_framework_registry(args.framework_lock)
        internal_identity = read_internal_service_identity(
            required=False,
            expected_service_id="evolution-backend",
        )
        if (
            internal_identity is not None
            and (
                internal_identity.registry_digest != registry.snapshot.registry_digest
                or internal_identity.framework_lock_digest
                != verified_private_file_sha256(
                    args.framework_lock,
                    max_bytes=_MAX_FRAMEWORK_LOCK_BYTES,
                )
            )
        ):
            raise RuntimeError("loaded framework lock/registry does not match the service generation")
        app_kwargs = {
            "db_path": Path(args.db),
            "artifact_root": Path(args.artifact_root),
            "executable_registry": registry,
        }
        if internal_identity is not None:
            app_kwargs["internal_identity"] = internal_identity
        app = create_app(**app_kwargs)
        listen_fd = inherited_listen_fd()
        if internal_identity is not None and listen_fd is None:
            raise RuntimeError("release-owned evolution backend requires an inherited listener")
        if listen_fd is None:
            uvicorn.run(app, host=args.host, port=args.port)
        else:
            uvicorn.run(app, fd=listen_fd)
        return 0

    artifact_root = Path(args.artifact_root)
    artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    registry = (
        load_verified_framework_registry(args.framework_lock)
        if args.framework_lock is not None
        else None
    )
    capabilities = _parse_capabilities(
        args.capability,
        defaults=(
            tuple(registry.snapshot.methods) if registry is not None else tuple(METHOD_REGISTRY)
        ),
    )
    internal_identity = read_internal_service_identity(
        required=False,
        expected_service_id="evolution-worker",
        actual_registry_digest=(
            registry.snapshot.registry_digest if registry is not None else None
        ),
    )
    if internal_identity is not None:
        if args.framework_lock is None:
            raise RuntimeError("release-owned worker requires a framework lock")
        if (
            internal_identity.framework_lock_digest
            != verified_private_file_sha256(
                args.framework_lock,
                max_bytes=_MAX_FRAMEWORK_LOCK_BYTES,
            )
        ):
            raise RuntimeError("worker framework lock does not match the service generation")
    proxy_base_url = core_gateway_base_url_from_environment()
    harness_service = (
        CodexSubscriptionHarnessService()
        if proxy_base_url is None
        else CoreGatewayHarnessService(proxy_base_url=proxy_base_url)
    )
    with EvolutionWorkerClient(
        args.base_url,
        headers=(internal_identity.request_headers() if internal_identity is not None else None),
    ) as client, SubprocessSdLoraTrainerService(artifact_root) as parametric_trainer:
        method_services = MethodExecutionServices(
            harness=harness_service,
            parametric_trainer=parametric_trainer,
        )
        if internal_identity is not None:
            client.register_internal_worker(
                worker_id=args.worker_id,
                framework_lock_digest=internal_identity.framework_lock_digest,
                generation_digest=internal_identity.generation_digest,
                registry_digest=internal_identity.registry_digest,
            )
        while True:
            claimed = run_once(
                client,
                worker_id=args.worker_id,
                capabilities=capabilities,
                artifact_root=artifact_root,
                lease_seconds=args.lease_seconds,
                executable_registry=registry,
                method_services=method_services,
            )
            if args.once:
                return 0
            if not claimed:
                time.sleep(args.sleep_seconds)


def _parse_capabilities(
    values: list[str],
    *,
    defaults: tuple[str, ...] | None = None,
) -> list[str]:
    capabilities: list[str] = []
    for value in values:
        capabilities.extend(part.strip() for part in value.split(",") if part.strip())
    return capabilities or list(defaults or METHOD_REGISTRY)


if __name__ == "__main__":
    raise SystemExit(main())
