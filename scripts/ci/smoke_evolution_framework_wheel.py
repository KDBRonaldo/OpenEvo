#!/usr/bin/env python3
"""Verify the installed OpenEvo wheel and load its frozen built-in catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from importlib import metadata
from pathlib import Path
from typing import Any

import openevo

from openevo.evolution.framework.loading import (
    DistributionArtifactExpectation,
    verify_distribution_install,
)


EXPECTED_METHOD_IDS = frozenset(
    {
        "agent_system",
        "agent_system_gepa_reflector",
        "agent_system_history_reflector",
        "agent_system_pareto_reflector",
        "agent_system_reflector",
        "parametric_memory_lora_sft",
        "parametric_memory_register",
        "skill_bundle",
        "skill_bundle_reflector",
        "text_memory",
        "text_memory_expel_reflector",
        "text_memory_memevolve",
        "text_memory_reflector",
    }
)
EXPECTED_TARGET_IDS = frozenset(
    {"agent_system", "parametric_memory", "skill_bundle", "text_memory"}
)
EXPECTED_HANDLER_IDS = frozenset(
    {f"{target_id}_handler" for target_id in EXPECTED_TARGET_IDS}
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_installed_registry(
    wheel_path: Path,
    framework_lock_path: Path,
) -> tuple[dict[str, object], Any]:
    wheel = wheel_path.resolve(strict=True)
    framework_lock = framework_lock_path.resolve(strict=True)
    repository_src = Path(__file__).resolve().parents[2] / "src"
    import_path = Path(openevo.__file__).resolve(strict=True)
    if import_path.is_relative_to(repository_src):
        raise RuntimeError("wheel smoke imported OpenEvo from the source checkout")

    version = metadata.version("openevo")
    wheel_sha256 = _sha256(wheel)
    verified = verify_distribution_install(
        DistributionArtifactExpectation(
            distribution="openevo",
            distribution_version=version,
            distribution_digest=wheel_sha256,
        ),
        wheel,
    )

    from openevo.evolution.framework import (
        load_framework_distribution_lock,
        load_verified_framework_registry,
    )

    locked_identity, locked_wheel = load_framework_distribution_lock(framework_lock)
    if (
        locked_wheel.resolve(strict=True) != wheel
        or locked_identity.distribution_version != version
        or locked_identity.distribution_digest != wheel_sha256
        or locked_identity.wheel_filename != wheel.name
    ):
        raise RuntimeError("packaged framework lock does not bind the exact Core wheel")
    loaded = load_verified_framework_registry(framework_lock)
    if set(loaded.method_handles) != EXPECTED_METHOD_IDS:
        raise RuntimeError("installed built-in method handles are incomplete")
    if set(loaded.snapshot.targets) != EXPECTED_TARGET_IDS:
        raise RuntimeError("installed built-in targets do not match the release contract")
    if set(loaded.snapshot.target_handlers) != EXPECTED_HANDLER_IDS:
        raise RuntimeError("installed target handlers do not match the release contract")
    if set(loaded.handler_handles) != EXPECTED_HANDLER_IDS:
        raise RuntimeError("installed target handler callables are incomplete")
    expected_anchor_keys = {
        *(f"target:{target_id}" for target_id in EXPECTED_TARGET_IDS),
    }
    if set(loaded.descriptor_anchors) != expected_anchor_keys:
        raise RuntimeError("installed target anchors are incomplete")

    return (
        {
            "distribution": "openevo",
            "framework_lock_sha256": _sha256(framework_lock),
            "version": version,
            "wheel_sha256": verified.expectation.distribution_digest,
            "inventory_digest": verified.inventory_digest,
            "registry_digest": loaded.snapshot.registry_digest,
            "method_count": len(loaded.method_handles),
            "handler_handle_count": len(loaded.handler_handles),
            "anchor_count": len(loaded.descriptor_anchors),
            "target_count": len(loaded.snapshot.targets),
            "handler_count": len(loaded.snapshot.target_handlers),
            "import_path": str(import_path),
        },
        loaded,
    )


def smoke(
    wheel_path: Path,
    framework_lock_path: Path,
    *,
    mode: str,
) -> dict[str, object]:
    if mode not in {"installed-registry", "linux-context-projection"}:
        raise ValueError("framework wheel smoke mode is invalid")
    if mode == "linux-context-projection" and sys.platform != "linux":
        raise RuntimeError("linux-context-projection framework smoke requires Linux")

    evidence, loaded = _verify_installed_registry(wheel_path, framework_lock_path)
    if mode == "linux-context-projection":
        _smoke_linux_context_projection(loaded)
    evidence["linux_context_projection"] = (
        "passed" if mode == "linux-context-projection" else "not-run"
    )
    evidence["verification_mode"] = mode
    return evidence


def _smoke_linux_context_projection(loaded: Any) -> None:
    from openevo.evolution.context_projection import ContextProjectionResolveRequest
    from openevo.evolution.framework import (
        EvolutionExecutionProfile,
        RuntimeDestinationRoots,
    )
    from openevo.evolution.models import ArtifactRegisterRequest, ArtifactType
    from openevo.evolution.store import EvolutionStore

    with tempfile.TemporaryDirectory(prefix="openevo-migration-projection-smoke-") as temp_dir:
        state_root = Path(temp_dir)
        db_path = state_root / "evolution.db"
        artifact_root = state_root / "artifacts"
        legacy_store = EvolutionStore(db_path=db_path, artifact_root=artifact_root)
        legacy_store.initialize()
        legacy_payload = artifact_root / "payloads" / "legacy.md"
        legacy_payload.parent.mkdir()
        legacy_payload.write_text("legacy memory", encoding="utf-8")
        legacy_artifact = legacy_store.register_artifact(
            ArtifactRegisterRequest(
                type=ArtifactType.TEXT_MEMORY,
                name="legacy memory",
                uri=legacy_payload.as_uri(),
                manifest={"content_path": legacy_payload.name},
                promoted=True,
            )
        )
        with legacy_store.connect() as connection:
            connection.execute("ALTER TABLE artifacts DROP COLUMN manifest_json")
            connection.commit()

        migrated_store = EvolutionStore(
            db_path=db_path,
            artifact_root=artifact_root,
            executable_registry=loaded,
        )
        migrated_store.initialize()
        current_payload = artifact_root / "payloads" / "current.md"
        current_payload.write_text("current memory", encoding="utf-8")
        current_artifact = migrated_store.register_artifact(
            ArtifactRegisterRequest(
                type=ArtifactType.TEXT_MEMORY,
                name="current memory",
                uri=current_payload.as_uri(),
                manifest={"content_path": current_payload.name},
                promoted=True,
            )
        )
        projection = migrated_store.resolve_context_projections(
            ContextProjectionResolveRequest(
                task_id="installed-wheel-migration-smoke",
                instruction="Continue.",
                agent={"harness": "codex"},
                execution_profile=EvolutionExecutionProfile(
                    execution_mode="self_deployed",
                    capture_mode="transcript",
                    harness_id="codex",
                ),
                destination_roots=RuntimeDestinationRoots(
                    target_data="/openevo/session/evolution",
                    harness_skills="/openevo/session/evolution/skills",
                    harness_instruction="/workspace/repository",
                ),
            )
        )
        if projection.selection.artifact_ids != (current_artifact.artifact_id,):
            raise RuntimeError("installed migrated store did not project current artifact")
        if projection.selection.skipped_artifact_ids != (legacy_artifact.artifact_id,):
            raise RuntimeError("installed migrated store did not quarantine legacy artifact")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--framework-lock", required=True, type=Path)
    parser.add_argument(
        "--mode",
        choices=("installed-registry", "linux-context-projection"),
        required=True,
    )
    args = parser.parse_args()
    print(json.dumps(smoke(args.wheel, args.framework_lock, mode=args.mode), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
