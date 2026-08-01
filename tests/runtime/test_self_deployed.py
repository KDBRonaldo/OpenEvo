from __future__ import annotations

from dataclasses import replace

import pytest

from openevo.runtime.self_deployed import (
    RELEASE_SELF_DEPLOYED_MODEL_PROFILES,
    require_release_self_deployed_model_profile,
)


def test_release_self_deployed_profile_is_exact_and_content_addressed() -> None:
    profile = require_release_self_deployed_model_profile("qwen3-0.6b-v1")

    assert profile is RELEASE_SELF_DEPLOYED_MODEL_PROFILES["qwen3-0.6b-v1"]
    assert profile.model_id == "Qwen/Qwen3-0.6B"
    assert profile.model_revision == "c1899de289a04d12100db370d81485cdf75e47ca"
    assert profile.vllm_image == (
        "docker.io/vllm/vllm-openai@"
        "sha256:df2607b26bdda2875de4832f4d08da0055b4b6e3570347f3a849bcc652771dd6"
    )
    assert profile.vllm_image_config_digest == (
        "sha256:5791f8642d1b71d45d832e232250aeca6a9aeb99b89698dff3c1ca5eea7a9655"
    )
    assert profile.model_snapshot_manifest_sha256 == profile.computed_snapshot_manifest_sha256
    assert profile.profile_sha256 == profile.computed_profile_sha256
    assert profile.maximum_context_tokens == 8192
    assert profile.minimum_free_vram_bytes >= 8 * 1024**3
    assert profile.required_files[-2].path == "tokenizer_config.json"
    assert tuple(item.path for item in profile.required_files) == tuple(
        sorted(item.path for item in profile.required_files)
    )


def test_release_self_deployed_profile_rejects_unknown_or_mutated_identity() -> None:
    with pytest.raises(ValueError, match="not supported"):
        require_release_self_deployed_model_profile("Qwen/Qwen3-0.6B")

    original = require_release_self_deployed_model_profile("qwen3-0.6b-v1")
    with pytest.raises(ValueError, match="profile digest"):
        replace(
            original,
            minimum_free_vram_bytes=original.minimum_free_vram_bytes + 1,
        )
