"""Durable Hugging Face model downloads and one daemon-owned vLLM runtime."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import urlopen

from openevo.daemon.errors import RequestError, StateConflictError
from openevo.daemon.model_proxy import ManagedModelProxy


MODEL_REPOSITORY_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,95})/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,95})\Z"
)
MODEL_REVISION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
MODEL_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
MAX_MODEL_SNAPSHOT_BYTES = 256 * 1024**3
MODEL_METADATA_TIMEOUT_SECONDS = 30
_SAFE_MODEL_SUFFIXES = (
    ".json",
    ".safetensors",
    ".model",
    ".txt",
    ".tiktoken",
    ".jinja",
)
_REQUIRED_MODEL_FILES = frozenset({"config.json"})
VLLM_IMAGE = (
    "docker.io/vllm/vllm-openai@"
    "sha256:c48cf118e1e6e39d7790e174d6014f7af5d06f79c2d29d984d11cbe2e8d414e7"
)
VLLM_IMAGE_PULL_TIMEOUT_SECONDS = 1_800
VLLM_GPU_MEMORY_UTILIZATION = 0.85


class VllmModelRuntime:
    """Own exactly one loopback-only vLLM container for this daemon process."""

    def __init__(
        self,
        *,
        startup_timeout_seconds: int = 600,
        owner_id: str | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        health_probe: Callable[[str], bool] | None = None,
        proxy_factory: Callable[..., ManagedModelProxy] = ManagedModelProxy,
    ) -> None:
        self._startup_timeout_seconds = startup_timeout_seconds
        self._owner_id = owner_id or secrets.token_hex(8)
        self._run = command_runner
        self._health_probe = health_probe or self._probe
        self._proxy_factory = proxy_factory
        self._lock = threading.Lock()
        self._container_id: str | None = None
        self._volume_name: str | None = None
        self._model_resource_id: str | None = None
        self._upstream_base_url: str | None = None
        self._base_url: str | None = None
        self._proxy: ManagedModelProxy | None = None
        self._stale_cleaned = False

    def ensure_running(
        self, *, model_resource_id: str, repository_id: str, snapshot_path: Path
    ) -> dict[str, str]:
        with self._lock:
            if (
                self._model_resource_id == model_resource_id
                and self._upstream_base_url is not None
                and self._base_url is not None
            ):
                if self._health_probe(f"{self._upstream_base_url}/models"):
                    return {"model": repository_id, "base_url": self._base_url}
            self._stop_locked()
            docker = shutil.which("docker")
            if docker is None:
                raise StateConflictError("Docker is required to serve a downloaded model")
            self._ensure_image(docker)
            self._cleanup_stale(docker)
            gpu_index = self._select_gpu(docker)
            import socket

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                port = int(listener.getsockname()[1])
            context_window = self._model_context_window(snapshot_path)
            volume_name = self._prepare_model_volume(docker, snapshot_path)
            self._volume_name = volume_name
            name = f"openevo-vllm-{secrets.token_hex(8)}"
            command = [
                docker, "run", "--detach", "--pull=never", "--name", name,
                "--label", "com.openevo.owner=model-runtime",
                "--label", f"com.openevo.instance={self._owner_id}",
                "--gpus", f"device={gpu_index}",
                "--cap-drop=ALL", "--cap-add=DAC_READ_SEARCH",
                "--security-opt=no-new-privileges:true",
                "--shm-size=4g", "--publish", f"127.0.0.1:{port}:8000",
                "--volume", f"{volume_name}:/model:ro", VLLM_IMAGE,
                "--model", "/model", "--served-model-name", repository_id,
                "--dtype", "auto", "--max-model-len", str(context_window),
                "--gpu-memory-utilization", str(VLLM_GPU_MEMORY_UTILIZATION),
                "--disable-log-stats",
                "--enable-auto-tool-choice", "--tool-call-parser", "hermes",
            ]
            try:
                result = self._run(
                    command, check=False, capture_output=True, text=True, timeout=120
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                self._stop_locked()
                raise StateConflictError(f"could not start the vLLM container: {exc}") from exc
            container_id = result.stdout.strip()
            if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{12,64}", container_id) is None:
                detail = (result.stderr or result.stdout).strip()[-2_000:]
                self._stop_locked()
                raise StateConflictError(f"vLLM container startup failed: {detail}")
            self._container_id = container_id
            self._model_resource_id = model_resource_id
            self._upstream_base_url = f"http://127.0.0.1:{port}/v1"
            deadline = time.monotonic() + self._startup_timeout_seconds
            while time.monotonic() < deadline:
                if self._health_probe(f"{self._upstream_base_url}/models"):
                    try:
                        proxy = self._proxy_factory(
                            upstream_base_url=self._upstream_base_url,
                            model=repository_id,
                            context_window=context_window,
                        )
                        self._base_url = proxy.start()
                        self._proxy = proxy
                    except Exception as exc:
                        self._stop_locked()
                        raise StateConflictError(
                            "could not start the managed model API adapter"
                        ) from exc
                    return {"model": repository_id, "base_url": self._base_url}
                if not self._container_running(docker):
                    logs = self._logs_locked(docker)
                    self._stop_locked()
                    raise StateConflictError(
                        f"vLLM exited before becoming ready: {logs}"
                    )
                time.sleep(2)
            logs = self._logs_locked(docker)
            self._stop_locked()
            raise StateConflictError(f"vLLM did not become ready before timeout: {logs}")

    def _ensure_image(self, docker: str) -> None:
        try:
            inspect = self._run(
                [docker, "image", "inspect", VLLM_IMAGE],
                check=False, capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StateConflictError(f"could not inspect the pinned vLLM image: {exc}") from exc
        if inspect.returncode == 0:
            return
        try:
            pulled = self._run(
                [docker, "pull", VLLM_IMAGE],
                check=False,
                capture_output=True,
                text=True,
                timeout=VLLM_IMAGE_PULL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StateConflictError(f"could not pull the pinned vLLM image: {exc}") from exc
        if pulled.returncode != 0:
            detail = (pulled.stderr or pulled.stdout).strip()[-2_000:]
            raise StateConflictError(f"pinned vLLM image pull failed: {detail}")

    def _select_gpu(self, docker: str) -> int:
        command = [
            docker,
            "run",
            "--rm",
            "--pull=never",
            "--gpus",
            "all",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--entrypoint",
            "nvidia-smi",
            VLLM_IMAGE,
            "--query-gpu=index,memory.total,memory.free",
            "--format=csv,noheader,nounits",
        ]
        try:
            result = self._run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StateConflictError(f"could not inspect available GPUs: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-2_000:]
            raise StateConflictError(f"could not inspect available GPUs: {detail}")

        candidates: list[tuple[int, int, int]] = []
        for line in result.stdout.splitlines():
            fields = [field.strip() for field in line.split(",")]
            if len(fields) != 3 or not all(field.isdigit() for field in fields):
                raise StateConflictError("nvidia-smi returned an invalid GPU inventory")
            index, total_mib, free_mib = (int(field) for field in fields)
            if total_mib <= 0 or free_mib < 0 or free_mib > total_mib:
                raise StateConflictError("nvidia-smi returned an invalid GPU inventory")
            candidates.append((free_mib, total_mib, index))
        if not candidates:
            raise StateConflictError("no NVIDIA GPU is available to serve the selected model")

        free_mib, total_mib, index = max(candidates)
        required_mib = int(total_mib * VLLM_GPU_MEMORY_UTILIZATION)
        if free_mib < required_mib:
            summary = ", ".join(
                f"GPU {candidate_index}: {candidate_free} MiB free"
                for candidate_free, _, candidate_index in sorted(
                    candidates, key=lambda item: item[2]
                )
            )
            raise StateConflictError(
                "no GPU has enough free memory for the managed vLLM runtime "
                f"(requires {required_mib} MiB on GPU {index}; {summary})"
            )
        return index

    def _prepare_model_volume(self, docker: str, snapshot_path: Path) -> str:
        volume_name = f"openevo-model-{secrets.token_hex(8)}"
        helper_name = f"openevo-model-copy-{secrets.token_hex(8)}"
        try:
            created_volume = self._run(
                [
                    docker,
                    "volume",
                    "create",
                    "--label",
                    "com.openevo.owner=model-snapshot",
                    "--label",
                    f"com.openevo.instance={self._owner_id}",
                    volume_name,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if created_volume.returncode != 0 or created_volume.stdout.strip() != volume_name:
                detail = (created_volume.stderr or created_volume.stdout).strip()[-2_000:]
                raise StateConflictError(f"could not create the model volume: {detail}")
            created_helper = self._run(
                [
                    docker,
                    "create",
                    "--name",
                    helper_name,
                    "--label",
                    "com.openevo.owner=model-copy",
                    "--label",
                    f"com.openevo.instance={self._owner_id}",
                    "--volume",
                    f"{volume_name}:/model",
                    "--entrypoint",
                    "/usr/bin/true",
                    VLLM_IMAGE,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            helper_id = created_helper.stdout.strip()
            if (
                created_helper.returncode != 0
                or re.fullmatch(r"[0-9a-f]{12,64}", helper_id) is None
            ):
                detail = (created_helper.stderr or created_helper.stdout).strip()[-2_000:]
                raise StateConflictError(f"could not create the model copy container: {detail}")
            copied = self._run(
                [docker, "cp", f"{snapshot_path}{os.sep}.", f"{helper_id}:/model"],
                check=False,
                capture_output=True,
                text=True,
                timeout=VLLM_IMAGE_PULL_TIMEOUT_SECONDS,
            )
            if copied.returncode != 0:
                detail = (copied.stderr or copied.stdout).strip()[-2_000:]
                raise StateConflictError(f"could not copy the model into Docker: {detail}")
            verified = self._run(
                [
                    docker,
                    "run",
                    "--rm",
                    "--pull=never",
                    "--volume",
                    f"{volume_name}:/model:ro",
                    "--entrypoint",
                    "/usr/bin/test",
                    VLLM_IMAGE,
                    "-f",
                    "/model/config.json",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if verified.returncode != 0:
                detail = (verified.stderr or verified.stdout).strip()[-2_000:]
                raise StateConflictError(
                    f"the copied Docker model snapshot is incomplete: {detail}"
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._remove_model_copy_resources(docker, helper_name, volume_name)
            raise StateConflictError(f"could not prepare the Docker model snapshot: {exc}") from exc
        except StateConflictError:
            self._remove_model_copy_resources(docker, helper_name, volume_name)
            raise
        try:
            self._run(
                [docker, "rm", "--force", helper_name],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return volume_name

    def _remove_model_copy_resources(
        self,
        docker: str,
        helper_name: str,
        volume_name: str,
    ) -> None:
        for command in (
            [docker, "rm", "--force", helper_name],
            [docker, "volume", "rm", "--force", volume_name],
        ):
            try:
                self._run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _cleanup_stale(self, docker: str) -> None:
        if self._stale_cleaned:
            return
        try:
            listed = self._run(
                [
                    docker,
                    "ps",
                    "--all",
                    "--quiet",
                    "--filter",
                    "label=com.openevo.owner=model-runtime",
                    "--filter",
                    f"label=com.openevo.instance={self._owner_id}",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StateConflictError(f"could not inspect stale vLLM containers: {exc}") from exc
        if listed.returncode != 0:
            raise StateConflictError("could not inspect stale vLLM containers")
        for container_id in listed.stdout.splitlines():
            container_id = container_id.strip()
            if re.fullmatch(r"[0-9a-f]{12,64}", container_id) is None:
                raise StateConflictError("Docker returned an invalid stale container identity")
            try:
                removed = self._run(
                    [docker, "rm", "--force", container_id],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise StateConflictError(f"could not remove a stale vLLM container: {exc}") from exc
            if removed.returncode != 0:
                raise StateConflictError("could not remove a stale vLLM container")
        self._stale_cleaned = True

    @staticmethod
    def _probe(url: str) -> bool:
        try:
            with urlopen(url, timeout=2) as response:  # noqa: S310 - fixed loopback URL
                return response.status == 200
        except (OSError, URLError):
            return False

    @staticmethod
    def _model_context_window(snapshot_path: Path) -> int:
        try:
            config = json.loads((snapshot_path / "config.json").read_bytes())
        except (OSError, json.JSONDecodeError):
            return 8_192
        candidates = [config]
        text_config = config.get("text_config") if isinstance(config, dict) else None
        if isinstance(text_config, dict):
            candidates.insert(0, text_config)
        for candidate in candidates:
            for key in ("max_position_embeddings", "n_positions", "seq_length"):
                value = candidate.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 512:
                    return min(value, 32_768)
        return 8_192

    def _logs_locked(self, docker: str) -> str:
        if self._container_id is None:
            return "container did not start"
        try:
            result = self._run(
                [docker, "logs", "--tail", "80", self._container_id],
                check=False, capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "container logs unavailable"
        return (result.stderr or result.stdout).strip()[-4_000:] or "no container logs"

    def _container_running(self, docker: str) -> bool:
        if self._container_id is None:
            return False
        try:
            result = self._run(
                [
                    docker,
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    self._container_id,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    def close(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        proxy = self._proxy
        self._proxy = None
        if proxy is not None:
            proxy.close()
        container_id = self._container_id
        self._container_id = None
        volume_name = self._volume_name
        self._volume_name = None
        self._model_resource_id = None
        self._upstream_base_url = None
        self._base_url = None
        docker = shutil.which("docker")
        if docker is None:
            return
        if container_id is not None:
            try:
                self._run(
                    [docker, "rm", "--force", container_id],
                    check=False, capture_output=True, text=True, timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if volume_name is not None:
            try:
                self._run(
                    [docker, "volume", "rm", "--force", volume_name],
                    check=False, capture_output=True, text=True, timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class HuggingFaceModelManager:
    """Own public model identity, verified snapshots, and download workers."""

    def __init__(
        self,
        *,
        state_path: Path,
        root: Path,
        api_factory: Callable[[], Any] | None = None,
        snapshot_download: Callable[..., str] | None = None,
        runtime: VllmModelRuntime | None = None,
    ) -> None:
        self.state_path = state_path
        self.root = root.resolve(strict=False)
        self.snapshots_root = self.root / "snapshots"
        self.staging_root = self.root / "staging"
        self._api_factory = api_factory
        self._snapshot_download = snapshot_download
        self._runtime = runtime or VllmModelRuntime(
            owner_id=hashlib.sha256(os.fspath(self.state_path.resolve()).encode()).hexdigest()[:16]
        )
        self._lock = threading.RLock()
        self._workers: dict[str, threading.Thread] = {}
        for directory in (self.root, self.snapshots_root, self.staging_root):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise RuntimeError("model cache root must contain only real directories")
            try:
                directory.chmod(0o700)
            except OSError:
                pass
        self._initialize()

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.state_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        self.state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._lock, self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS development_models (
                    model_resource_id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    requested_revision TEXT NOT NULL,
                    resolved_revision TEXT,
                    manifest_sha256 TEXT,
                    state TEXT NOT NULL CHECK (
                        state IN ('queued', 'resolving', 'downloading', 'ready', 'failed')
                    ),
                    downloaded_bytes INTEGER NOT NULL DEFAULT 0 CHECK (downloaded_bytes >= 0),
                    total_bytes INTEGER CHECK (total_bytes IS NULL OR total_bytes >= 0),
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(repository_id, requested_revision)
                );
                CREATE TABLE IF NOT EXISTS development_model_actions (
                    action_id TEXT PRIMARY KEY,
                    model_resource_id TEXT NOT NULL REFERENCES development_models(model_resource_id),
                    action_kind TEXT NOT NULL CHECK (action_kind IN ('register', 'retry')),
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "UPDATE development_models SET state = 'failed', "
                "error = 'Model download was interrupted by a daemon restart.', updated_at = ? "
                "WHERE state IN ('queued', 'resolving', 'downloading')",
                (_utc_now(),),
            )

    @staticmethod
    def validate_repository_id(value: object) -> str:
        if not isinstance(value, str):
            raise RequestError("model repository_id must be text")
        repository_id = value.strip()
        if MODEL_REPOSITORY_PATTERN.fullmatch(repository_id) is None:
            raise RequestError("model repository_id must use the public owner/repository form")
        return repository_id

    @staticmethod
    def validate_revision(value: object) -> str:
        if value is None:
            return "main"
        if not isinstance(value, str):
            raise RequestError("model revision must be text")
        revision = value.strip()
        if MODEL_REVISION_PATTERN.fullmatch(revision) is None:
            raise RequestError("model revision is invalid")
        return revision

    def register(
        self,
        *,
        action_id: str,
        repository_id: object,
        revision: object = None,
    ) -> dict[str, Any]:
        repository = self.validate_repository_id(repository_id)
        requested_revision = self.validate_revision(revision)
        start_worker = False
        with self._lock, self._connection() as connection:
            action = connection.execute(
                "SELECT action.model_resource_id, model.repository_id, "
                "model.requested_revision FROM development_model_actions AS action "
                "JOIN development_models AS model USING(model_resource_id) "
                "WHERE action.action_id = ?",
                (action_id,),
            ).fetchone()
            if action is not None:
                if (
                    action["repository_id"] != repository
                    or action["requested_revision"] != requested_revision
                ):
                    raise StateConflictError("model action_id is already bound to another request")
                model_resource_id = action["model_resource_id"]
            else:
                existing = connection.execute(
                    "SELECT model_resource_id FROM development_models "
                    "WHERE repository_id = ? AND requested_revision = ?",
                    (repository, requested_revision),
                ).fetchone()
                if existing is not None:
                    model_resource_id = existing["model_resource_id"]
                    connection.execute(
                        "INSERT INTO development_model_actions(action_id, model_resource_id, "
                        "action_kind, created_at) VALUES (?, ?, 'register', ?)",
                        (action_id, model_resource_id, _utc_now()),
                    )
                else:
                    model_resource_id = f"model-{secrets.token_hex(8)}"
                    now = _utc_now()
                    connection.execute(
                        "INSERT INTO development_models(model_resource_id, repository_id, "
                        "requested_revision, state, created_at, updated_at) "
                        "VALUES (?, ?, ?, 'queued', ?, ?)",
                        (model_resource_id, repository, requested_revision, now, now),
                    )
                    connection.execute(
                        "INSERT INTO development_model_actions(action_id, model_resource_id, "
                        "action_kind, created_at) VALUES (?, ?, 'register', ?)",
                        (action_id, model_resource_id, now),
                    )
                    start_worker = True
        if start_worker:
            self._start_worker(model_resource_id)
        return self.get(model_resource_id)

    def retry(self, model_resource_id: str, *, action_id: str) -> dict[str, Any]:
        start_worker = False
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT state FROM development_models WHERE model_resource_id = ?",
                (model_resource_id,),
            ).fetchone()
            if row is None:
                raise KeyError(model_resource_id)
            action = connection.execute(
                "SELECT model_resource_id FROM development_model_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if action is not None:
                if action["model_resource_id"] != model_resource_id:
                    raise StateConflictError("model action_id is already bound to another model")
            else:
                if row["state"] not in {"failed", "ready"}:
                    raise StateConflictError("model download is already active")
                now = _utc_now()
                connection.execute(
                    "INSERT INTO development_model_actions(action_id, model_resource_id, "
                    "action_kind, created_at) VALUES (?, ?, 'retry', ?)",
                    (action_id, model_resource_id, now),
                )
                if row["state"] != "ready":
                    connection.execute(
                        "UPDATE development_models SET state = 'queued', downloaded_bytes = 0, "
                        "total_bytes = NULL, error = NULL, updated_at = ? "
                        "WHERE model_resource_id = ?",
                        (now, model_resource_id),
                    )
                    start_worker = True
        if start_worker:
            self._start_worker(model_resource_id)
        return self.get(model_resource_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM development_models ORDER BY created_at, model_resource_id"
            ).fetchall()
        return [self._record(row) for row in rows]

    def get(self, model_resource_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM development_models WHERE model_resource_id = ?",
                (model_resource_id,),
            ).fetchone()
        if row is None:
            raise KeyError(model_resource_id)
        return self._record(row)

    def snapshot_path(self, model_resource_id: str) -> Path:
        model = self.get(model_resource_id)
        if model["state"] != "ready" or model["resolved_revision"] is None:
            raise StateConflictError("the selected model is not ready")
        path = self._final_path(model_resource_id, model["resolved_revision"])
        self._validate_ready_snapshot(path)
        return path

    def prepare_inference(self, model_resource_id: str) -> dict[str, str]:
        model = self.get(model_resource_id)
        if model["state"] != "ready":
            raise StateConflictError("the selected model has not finished downloading")
        return self._runtime.ensure_running(
            model_resource_id=model_resource_id,
            repository_id=model["repository_id"],
            snapshot_path=self.snapshot_path(model_resource_id),
        )

    def close(self) -> None:
        self._runtime.close()

    def _record(self, row: sqlite3.Row) -> dict[str, Any]:
        downloaded = int(row["downloaded_bytes"])
        if row["state"] == "downloading":
            downloaded = max(downloaded, self._tree_size(self._staging_path(row["model_resource_id"])))
        return {
            "model_resource_id": row["model_resource_id"],
            "repository_id": row["repository_id"],
            "requested_revision": row["requested_revision"],
            "resolved_revision": row["resolved_revision"],
            "manifest_sha256": row["manifest_sha256"],
            "state": row["state"],
            "downloaded_bytes": downloaded,
            "total_bytes": row["total_bytes"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _start_worker(self, model_resource_id: str) -> None:
        with self._lock:
            current = self._workers.get(model_resource_id)
            if current is not None and current.is_alive():
                return
            thread = threading.Thread(
                target=self._download,
                args=(model_resource_id,),
                name=f"openevo-model-{model_resource_id}",
                daemon=True,
            )
            self._workers[model_resource_id] = thread
            thread.start()

    def _download(self, model_resource_id: str) -> None:
        staging = self._staging_path(model_resource_id)
        try:
            model = self.get(model_resource_id)
            self._set_state(model_resource_id, "resolving")
            api = self._hugging_face_api()
            info = api.model_info(
                model["repository_id"],
                revision=model["requested_revision"],
                files_metadata=True,
                token=False,
                timeout=MODEL_METADATA_TIMEOUT_SECONDS,
            )
            if bool(getattr(info, "private", False)) or bool(getattr(info, "gated", False)):
                raise RequestError("only public, non-gated Hugging Face models are supported")
            resolved_revision = str(getattr(info, "sha", ""))
            if MODEL_COMMIT_PATTERN.fullmatch(resolved_revision) is None:
                raise RequestError("Hugging Face did not return an immutable model revision")
            files = self._safe_file_manifest(getattr(info, "siblings", None))
            total_bytes = sum(item["byte_size"] for item in files)
            if total_bytes <= 0 or total_bytes > MAX_MODEL_SNAPSHOT_BYTES:
                raise RequestError("model snapshot size is outside the supported bound")
            manifest_sha256 = hashlib.sha256(
                _canonical_json(
                    {
                        "repository_id": model["repository_id"],
                        "resolved_revision": resolved_revision,
                        "files": files,
                    }
                ).encode("utf-8")
            ).hexdigest()
            final = self._final_path(model_resource_id, resolved_revision)
            if final.exists():
                self._validate_ready_snapshot(final)
                self._finish_download(
                    model_resource_id,
                    resolved_revision=resolved_revision,
                    manifest_sha256=manifest_sha256,
                    total_bytes=self._tree_size(final),
                )
                return
            self._discard_staging(staging)
            staging.mkdir(mode=0o700, parents=True)
            self._set_state(
                model_resource_id,
                "downloading",
                resolved_revision=resolved_revision,
                manifest_sha256=manifest_sha256,
                total_bytes=total_bytes,
            )
            download = self._snapshot_download_function()
            downloaded_path = Path(
                download(
                    repo_id=model["repository_id"],
                    revision=resolved_revision,
                    local_dir=staging,
                    token=False,
                    etag_timeout=MODEL_METADATA_TIMEOUT_SECONDS,
                    max_workers=4,
                    allow_patterns=[f"*{suffix}" for suffix in _SAFE_MODEL_SUFFIXES],
                )
            ).resolve(strict=True)
            if downloaded_path != staging.resolve(strict=True):
                raise RequestError("Hugging Face returned an unexpected model snapshot path")
            self._verify_downloaded_files(staging, files)
            self._make_private(staging)
            os.replace(staging, final)
            self._finish_download(
                model_resource_id,
                resolved_revision=resolved_revision,
                manifest_sha256=manifest_sha256,
                total_bytes=self._tree_size(final),
            )
        except Exception as exc:
            self._fail_download(model_resource_id, str(exc))
        finally:
            with self._lock:
                self._workers.pop(model_resource_id, None)

    def _hugging_face_api(self) -> Any:
        if self._api_factory is not None:
            return self._api_factory()
        from huggingface_hub import HfApi

        return HfApi()

    def _snapshot_download_function(self) -> Callable[..., str]:
        if self._snapshot_download is not None:
            return self._snapshot_download
        from huggingface_hub import snapshot_download

        return snapshot_download

    @staticmethod
    def _safe_file_manifest(siblings: object) -> list[dict[str, Any]]:
        if not isinstance(siblings, (list, tuple)):
            raise RequestError("Hugging Face model file metadata is unavailable")
        files: list[dict[str, Any]] = []
        for sibling in siblings:
            name = getattr(sibling, "rfilename", None)
            size = getattr(sibling, "size", None)
            if not isinstance(name, str) or not isinstance(size, int) or size < 0:
                raise RequestError("Hugging Face model file metadata is incomplete")
            path = PurePosixPath(name)
            if (
                path.is_absolute()
                or str(path) != name
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise RequestError("Hugging Face model contains an unsafe path")
            if not name.lower().endswith(_SAFE_MODEL_SUFFIXES):
                continue
            lfs = getattr(sibling, "lfs", None)
            sha256 = None
            if isinstance(lfs, dict):
                candidate = lfs.get("sha256")
                if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{64}", candidate):
                    sha256 = candidate
            else:
                candidate = getattr(lfs, "sha256", None)
                if isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{64}", candidate):
                    sha256 = candidate
            files.append({"path": name, "byte_size": size, "sha256": sha256})
        paths = {item["path"] for item in files}
        if not _REQUIRED_MODEL_FILES.issubset(paths):
            raise RequestError("Hugging Face repository is not a Transformers model")
        weights = [item for item in files if item["path"].endswith(".safetensors")]
        if not weights or any(item["sha256"] is None for item in weights):
            raise RequestError("model must publish content-addressed safetensors weights")
        return sorted(files, key=lambda item: item["path"])

    @staticmethod
    def _verify_downloaded_files(root: Path, files: list[dict[str, Any]]) -> None:
        expected = {item["path"]: item for item in files}
        for relative, item in expected.items():
            path = root.joinpath(*PurePosixPath(relative).parts)
            if path.is_symlink() or not path.is_file() or path.stat().st_size != item["byte_size"]:
                raise RequestError("downloaded model snapshot does not match Hugging Face metadata")
            if item["sha256"] is not None:
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    while chunk := stream.read(4 * 1024 * 1024):
                        digest.update(chunk)
                if digest.hexdigest() != item["sha256"]:
                    raise RequestError("downloaded model weight digest does not match Hugging Face")
        HuggingFaceModelManager._validate_ready_snapshot(root)

    @staticmethod
    def _validate_ready_snapshot(root: Path) -> None:
        if root.is_symlink() or not root.is_dir():
            raise StateConflictError("model snapshot is unavailable")
        config = root / "config.json"
        weights = list(root.glob("*.safetensors"))
        if config.is_symlink() or not config.is_file() or not weights:
            raise StateConflictError("model snapshot is incomplete")
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            if current_path.is_symlink():
                raise StateConflictError("model snapshot contains a symlink")
            for name in [*directories, *files]:
                if (current_path / name).is_symlink():
                    raise StateConflictError("model snapshot contains a symlink")

    @staticmethod
    def _make_private(root: Path) -> None:
        for current, directories, files in os.walk(root, followlinks=False):
            Path(current).chmod(0o700)
            for name in directories:
                (Path(current) / name).chmod(0o700)
            for name in files:
                path = Path(current) / name
                if path.is_symlink() or not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                    raise RequestError("model snapshot contains a non-regular file")
                path.chmod(0o600)

    def _set_state(
        self,
        model_resource_id: str,
        state: str,
        *,
        resolved_revision: str | None = None,
        manifest_sha256: str | None = None,
        total_bytes: int | None = None,
    ) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE development_models SET state = ?, resolved_revision = COALESCE(?, "
                "resolved_revision), manifest_sha256 = COALESCE(?, manifest_sha256), "
                "total_bytes = COALESCE(?, total_bytes), error = NULL, updated_at = ? "
                "WHERE model_resource_id = ?",
                (
                    state,
                    resolved_revision,
                    manifest_sha256,
                    total_bytes,
                    _utc_now(),
                    model_resource_id,
                ),
            )

    def _finish_download(
        self,
        model_resource_id: str,
        *,
        resolved_revision: str,
        manifest_sha256: str,
        total_bytes: int,
    ) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE development_models SET state = 'ready', resolved_revision = ?, "
                "manifest_sha256 = ?, downloaded_bytes = ?, total_bytes = ?, error = NULL, "
                "updated_at = ? WHERE model_resource_id = ?",
                (
                    resolved_revision,
                    manifest_sha256,
                    total_bytes,
                    total_bytes,
                    _utc_now(),
                    model_resource_id,
                ),
            )

    def _fail_download(self, model_resource_id: str, error: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE development_models SET state = 'failed', error = ?, updated_at = ? "
                "WHERE model_resource_id = ?",
                (error[:4_000] or "model download failed", _utc_now(), model_resource_id),
            )

    def _staging_path(self, model_resource_id: str) -> Path:
        return self.staging_root / model_resource_id

    def _final_path(self, model_resource_id: str, revision: str) -> Path:
        if MODEL_COMMIT_PATTERN.fullmatch(revision) is None:
            raise StateConflictError("model revision is not immutable")
        return self.snapshots_root / f"{model_resource_id}-{revision}"

    @staticmethod
    def _tree_size(root: Path) -> int:
        if not root.exists() or root.is_symlink():
            return 0
        total = 0
        for current, directories, files in os.walk(root, followlinks=False):
            directories[:] = [
                name for name in directories
                if not (Path(current) / name).is_symlink()
            ]
            for name in files:
                path = Path(current) / name
                relative = path.relative_to(root)
                in_hugging_face_cache = bool(relative.parts) and relative.parts[0] == ".cache"
                if in_hugging_face_cache and not name.endswith(".incomplete"):
                    continue
                if not path.is_symlink() and path.is_file():
                    total += path.stat().st_size
                    if total > MAX_MODEL_SNAPSHOT_BYTES:
                        return total
        return total

    @staticmethod
    def _discard_staging(path: Path) -> None:
        if not path.exists():
            return
        if path.is_symlink() or path.parent.name != "staging":
            raise RuntimeError("refusing to remove an unsafe model staging path")
        shutil.rmtree(path)


__all__ = [
    "HuggingFaceModelManager",
    "MAX_MODEL_SNAPSHOT_BYTES",
    "MODEL_COMMIT_PATTERN",
    "MODEL_REPOSITORY_PATTERN",
    "VLLM_IMAGE",
    "VllmModelRuntime",
]
