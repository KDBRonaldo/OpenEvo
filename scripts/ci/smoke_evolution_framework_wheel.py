#!/usr/bin/env python3
"""Verify the installed OpenEvo wheel and load its frozen built-in catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
from importlib import metadata
from pathlib import Path

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


def smoke(wheel_path: Path) -> dict[str, object]:
    wheel = wheel_path.resolve(strict=True)
    repository_src = Path(__file__).resolve().parents[2] / "src"
    import_path = Path(openevo.__file__).resolve(strict=True)
    if import_path.is_relative_to(repository_src):
        raise RuntimeError("wheel smoke imported OpenEvo from the source checkout")

    version = metadata.version("openevo")
    verified = verify_distribution_install(
        DistributionArtifactExpectation(
            distribution="openevo",
            distribution_version=version,
            distribution_digest=_sha256(wheel),
        ),
        wheel,
    )

    from openevo.evolution.framework.builtins import load_verified_builtin_registry

    loaded = load_verified_builtin_registry(verified)
    if set(loaded.method_handles) != EXPECTED_METHOD_IDS:
        raise RuntimeError("installed built-in method handles are incomplete")
    if set(loaded.snapshot.targets) != EXPECTED_TARGET_IDS:
        raise RuntimeError("installed built-in targets do not match the release contract")
    if set(loaded.snapshot.target_handlers) != EXPECTED_HANDLER_IDS:
        raise RuntimeError("installed target handlers do not match the release contract")
    expected_anchor_keys = {
        *(f"target:{target_id}" for target_id in EXPECTED_TARGET_IDS),
        *(f"target_handler:{handler_id}" for handler_id in EXPECTED_HANDLER_IDS),
    }
    if set(loaded.descriptor_anchors) != expected_anchor_keys:
        raise RuntimeError("installed target/handler anchors are incomplete")
    return {
        "distribution": "openevo",
        "version": version,
        "wheel_sha256": verified.expectation.distribution_digest,
        "inventory_digest": verified.inventory_digest,
        "registry_digest": loaded.snapshot.registry_digest,
        "method_count": len(loaded.method_handles),
        "anchor_count": len(loaded.descriptor_anchors),
        "target_count": len(loaded.snapshot.targets),
        "handler_count": len(loaded.snapshot.target_handlers),
        "import_path": str(import_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(smoke(args.wheel), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
