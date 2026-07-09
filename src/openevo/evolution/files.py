from __future__ import annotations

import json
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
