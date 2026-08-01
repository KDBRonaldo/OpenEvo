from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any


ARTIFACT_TYPE_DIRECTORIES = {
    "text_memory": "text_memory",
    "skill_bundle": "skills",
    "skills": "skills",
    "agent_system": "agent_system",
    "parametric_memory": "parametric_memory",
    "dataset": "datasets",
    "report": "reports",
    "reports": "reports",
    "context_snapshot": "contexts",
}

_MANAGED_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)


def _managed_identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _MANAGED_IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a stable managed identifier")
    return value


class ArtifactFileStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def initialize(self) -> None:
        for relative in (
            "events",
            "datasets",
            "artifacts/text_memory",
            "artifacts/skills",
            "artifacts/agent_system",
            "artifacts/parametric_memory",
            "artifacts/datasets",
            "artifacts/reports",
            "artifacts/contexts",
            "contexts",
        ):
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        root_descriptor = os.open(
            self.root,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
        )
        dataset_descriptor: int | None = None
        contexts_descriptor: int | None = None
        materialization_descriptor: int | None = None
        try:
            dataset_descriptor = os.open(
                "datasets",
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
                dir_fd=root_descriptor,
            )
            dataset_opened = os.fstat(dataset_descriptor)
            if dataset_opened.st_uid != os.geteuid() or not stat.S_ISDIR(
                dataset_opened.st_mode
            ):
                raise ValueError("dataset materialization root must be an owned directory")
            os.fchmod(dataset_descriptor, 0o700)
            if stat.S_IMODE(os.fstat(dataset_descriptor).st_mode) != 0o700:
                raise ValueError("dataset materialization root must have mode 0700")
            contexts_descriptor = os.open(
                "contexts",
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
                dir_fd=root_descriptor,
            )
            contexts_opened = os.fstat(contexts_descriptor)
            if contexts_opened.st_uid != os.geteuid() or not stat.S_ISDIR(contexts_opened.st_mode):
                raise ValueError("context snapshot root must be an owned directory")
            os.fchmod(contexts_descriptor, 0o700)
            if stat.S_IMODE(os.fstat(contexts_descriptor).st_mode) != 0o700:
                raise ValueError("context snapshot root must have mode 0700")
            try:
                os.mkdir("context_materializations", mode=0o700, dir_fd=root_descriptor)
            except FileExistsError:
                pass
            materialization_descriptor = os.open(
                "context_materializations",
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
                dir_fd=root_descriptor,
            )
            opened = os.fstat(materialization_descriptor)
            if opened.st_uid != os.geteuid() or not stat.S_ISDIR(opened.st_mode):
                raise ValueError("context materialization root must be an owned directory")
            os.fchmod(materialization_descriptor, 0o700)
            if stat.S_IMODE(os.fstat(materialization_descriptor).st_mode) != 0o700:
                raise ValueError("context materialization root must have mode 0700")
            os.fsync(dataset_descriptor)
            os.fsync(contexts_descriptor)
            os.fsync(materialization_descriptor)
            os.fsync(root_descriptor)
        except OSError as exc:
            raise ValueError(
                "context materialization root could not be initialized safely"
            ) from exc
        finally:
            if materialization_descriptor is not None:
                os.close(materialization_descriptor)
            if contexts_descriptor is not None:
                os.close(contexts_descriptor)
            if dataset_descriptor is not None:
                os.close(dataset_descriptor)
            os.close(root_descriptor)

    def safe_path(self, *parts: str) -> Path:
        path = (self.root / Path(*parts)).resolve()
        if self.root != path and self.root not in path.parents:
            raise ValueError(f"path escapes artifact root: {path}")
        return path

    def write_json(self, path: Path, payload: dict[str, Any]) -> Path:
        path = path.resolve()
        if self.root != path and self.root not in path.parents:
            raise ValueError(f"path escapes artifact root: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def event_payload_path(self, event_id: str) -> Path:
        return self.safe_path("events", f"{event_id}.json")

    def dataset_manifest_path(self, dataset_id: str) -> Path:
        return self.safe_path("datasets", dataset_id, "manifest.json")

    def artifact_manifest_path(self, artifact_type: str, artifact_id: str) -> Path:
        artifact_directory = ARTIFACT_TYPE_DIRECTORIES.get(artifact_type.strip())
        if artifact_directory is None:
            raise ValueError(f"unknown artifact type: {artifact_type}")
        return self.safe_path("artifacts", artifact_directory, artifact_id, "manifest.json")

    def context_snapshot_path(self, context_id: str) -> Path:
        return self.safe_path("contexts", f"{context_id}.json")

    def context_materialization_dir(self, context_id: str) -> Path:
        return self.safe_path(
            "context_materializations",
            _managed_identifier(context_id, "context ID"),
        )
