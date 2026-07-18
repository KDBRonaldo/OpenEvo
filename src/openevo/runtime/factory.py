"""Runtime factory with built-in backend map and import_path support."""

from __future__ import annotations

from pathlib import Path

from openevo._imports import import_subclass
from openevo.runtime.apptainer import ApptainerRuntime
from openevo.runtime.base import BaseRuntime
from openevo.runtime.bubblewrap import BubblewrapRuntime
from openevo.runtime.docker import DockerRuntime
from openevo.runtime.docker_host import DockerHostPathSpec
from openevo.runtime.managed import ManagedCredentialMount
from openevo.runtime.models import RuntimeSpec

_BUILTIN_BACKENDS: dict[str, type[BaseRuntime]] = {
    "docker": DockerRuntime,
    "apptainer": ApptainerRuntime,
    "bubblewrap": BubblewrapRuntime,
}


def create_runtime(
    spec: RuntimeSpec,
    session_id: str,
    session_dir: Path,
    *,
    credential_mount: ManagedCredentialMount | None = None,
    docker_ownership_root: Path | None = None,
    docker_host_path: DockerHostPathSpec | None = None,
) -> BaseRuntime:
    """Instantiate a runtime from a RuntimeSpec.

    Uses the built-in backend map for ``docker``, ``apptainer``, and
    ``bubblewrap``.
    Falls back to ``spec.import_path`` for plugin runtimes.
    """
    if spec.import_path:
        if docker_host_path is not None:
            raise ValueError("Docker host paths cannot be passed to a plugin runtime")
        if credential_mount is not None:
            raise ValueError("managed credentials cannot be passed to a plugin runtime")
        cls = _import_runtime_class(spec.import_path)
        runtime = cls(spec, session_id, session_dir)
        _validate_runtime_capabilities(runtime)
        return runtime
    cls = _BUILTIN_BACKENDS.get(spec.backend)
    if cls is None:
        raise ValueError(f"Unsupported runtime backend: {spec.backend}")
    if cls is DockerRuntime:
        runtime = DockerRuntime(
            spec,
            session_id,
            session_dir,
            credential_mount=credential_mount,
            ownership_root=docker_ownership_root,
            docker_host_path=docker_host_path,
        )
    else:
        if docker_host_path is not None:
            raise ValueError("Docker host paths cannot be passed to a non-Docker runtime")
        if credential_mount is not None:
            raise ValueError("managed credentials require the Docker runtime")
        runtime = cls(spec, session_id, session_dir)
    _validate_runtime_capabilities(runtime)
    return runtime


def _import_runtime_class(import_path: str) -> type[BaseRuntime]:
    return import_subclass(import_path, BaseRuntime, kind="runtime import path")


def _validate_runtime_capabilities(runtime: BaseRuntime) -> None:
    spec = runtime.spec
    backend = spec.backend
    if spec.gpus > 0 and not runtime.supports_gpus:
        raise ValueError(f"runtime backend {backend!r} does not support GPUs")
    if spec.cpus is not None and not runtime.supports_cpu_limits:
        raise ValueError(f"runtime backend {backend!r} does not support CPU limits")
    if spec.memory_mb is not None and not runtime.supports_memory_limits:
        raise ValueError(f"runtime backend {backend!r} does not support memory limits")
    if spec.storage_mb is not None and not runtime.supports_storage_limits:
        raise ValueError(f"runtime backend {backend!r} does not support storage limits")
    if not spec.allow_internet:
        if not runtime.can_disable_internet:
            raise ValueError(f"runtime backend {backend!r} cannot disable internet access")
        if spec.network not in (None, "", "host", "none"):
            raise ValueError(
                "runtime.network must be unset, 'host', or 'none' when allow_internet=false"
            )
