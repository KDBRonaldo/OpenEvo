from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import unquote, urlsplit
from urllib.request import Request

import pytest

from openevo.runtime.self_deployed import (
    RELEASE_SELF_DEPLOYED_MODEL_PROFILES,
    SelfDeployedModelFile,
    SelfDeployedModelProfile,
    require_release_self_deployed_model_profile,
)
from openevo.runtime import self_deployed_cache as cache_module
from openevo.runtime.self_deployed_cache import (
    SelfDeployedModelCacheError,
    prepare_release_model_snapshot,
    verify_release_model_file,
    verify_release_model_snapshot,
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
    assert profile.serving_arguments[-3:] == (
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        "hermes",
    )
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


def test_model_cache_file_verifier_binds_mode_size_digest_and_path(
    tmp_path: Path,
) -> None:
    payload = b"closed model bytes"
    path = tmp_path / "weights.bin"
    path.write_bytes(payload)
    path.chmod(0o600)
    identity = SelfDeployedModelFile(
        path="weights.bin",
        byte_size=len(payload),
        digest_algorithm="sha256",
        digest=hashlib.sha256(payload).hexdigest(),
    )

    verify_release_model_file(path, identity)
    path.chmod(0o644)
    with pytest.raises(SelfDeployedModelCacheError, match="metadata"):
        verify_release_model_file(path, identity)


def test_model_cache_file_verifier_uses_git_blob_identity_and_rejects_symlink(
    tmp_path: Path,
) -> None:
    payload = b"git managed config\n"
    target = tmp_path / "config.json"
    target.write_bytes(payload)
    target.chmod(0o600)
    digest = hashlib.sha1(f"blob {len(payload)}\0".encode() + payload).hexdigest()
    identity = SelfDeployedModelFile(
        path="config.json",
        byte_size=len(payload),
        digest_algorithm="git_blob_sha1",
        digest=digest,
    )
    verify_release_model_file(target, identity)

    link = tmp_path / "linked.json"
    link.symlink_to(target)
    with pytest.raises(SelfDeployedModelCacheError, match="opened safely"):
        verify_release_model_file(link, identity)


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _tiny_profile(files: tuple[SelfDeployedModelFile, ...]) -> SelfDeployedModelProfile:
    release = require_release_self_deployed_model_profile("qwen3-0.6b-v1")
    snapshot_digest = _canonical_digest(
        {
            "model_snapshot_contract_version": "1",
            "model_id": release.model_id,
            "model_revision": release.model_revision,
            "required_files": [item.canonical_payload() for item in files],
        }
    )
    profile_payload = {
        **release.canonical_profile_payload(),
        "model_snapshot_manifest_sha256": snapshot_digest,
    }
    profile_payload.pop("profile_contract_version")
    return SelfDeployedModelProfile(
        profile_id=release.profile_id,
        display_name=release.display_name,
        model_id=release.model_id,
        model_revision=release.model_revision,
        license_spdx=release.license_spdx,
        architecture=release.architecture,
        maximum_context_tokens=release.maximum_context_tokens,
        minimum_free_vram_bytes=release.minimum_free_vram_bytes,
        minimum_system_memory_bytes=release.minimum_system_memory_bytes,
        minimum_free_disk_bytes=release.minimum_free_disk_bytes,
        tensor_parallel_size=release.tensor_parallel_size,
        gpu_memory_utilization_milli=release.gpu_memory_utilization_milli,
        vllm_image=release.vllm_image,
        vllm_image_config_digest=release.vllm_image_config_digest,
        vllm_version=release.vllm_version,
        serving_arguments=release.serving_arguments,
        required_files=files,
        model_snapshot_manifest_sha256=snapshot_digest,
        profile_sha256=_canonical_digest({"profile_contract_version": "1", **profile_payload}),
    )


class _BytesResponse:
    def __init__(self, payload: bytes) -> None:
        self.headers: dict[str, str] = {"Content-Length": str(len(payload))}
        self._payload = payload
        self._offset = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._payload) - self._offset
        result = self._payload[self._offset : self._offset + size]
        self._offset += len(result)
        return result

    def close(self) -> None:
        self.closed = True


def _portable_noreplace(root: Path, source: str, destination: str) -> None:
    if (root / destination).exists():
        raise FileExistsError(destination)
    os.rename(root / source, root / destination)


def test_model_cache_preparation_is_content_verified_idempotent_and_observable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = b'{"model_type":"qwen3"}\n'
    weights = b"tiny deterministic weights"
    files = (
        SelfDeployedModelFile(
            path="config.json",
            byte_size=len(config),
            digest_algorithm="git_blob_sha1",
            digest=hashlib.sha1(f"blob {len(config)}\0".encode("ascii") + config).hexdigest(),
        ),
        SelfDeployedModelFile(
            path="model.safetensors",
            byte_size=len(weights),
            digest_algorithm="sha256",
            digest=hashlib.sha256(weights).hexdigest(),
        ),
    )
    profile = _tiny_profile(files)
    cache_root = tmp_path / "models"
    cache_root.mkdir(mode=0o700)
    responses: list[_BytesResponse] = []
    requested: list[str] = []

    def open_model(request: Request, timeout: float) -> _BytesResponse:
        assert 1 <= timeout <= 30
        requested.append(request.full_url)
        relative = unquote(urlsplit(request.full_url).path).split("/resolve/", 1)[1]
        revision, relative = relative.split("/", 1)
        assert revision == profile.model_revision
        assert urlsplit(request.full_url).query == "download=true"
        payload = {"config.json": config, "model.safetensors": weights}[relative]
        response = _BytesResponse(payload)
        responses.append(response)
        return response

    monkeypatch.setattr(cache_module, "_rename_noreplace", _portable_noreplace)
    progress: list[str] = []
    prepared = prepare_release_model_snapshot(
        cache_root=cache_root,
        profile=profile,
        deadline=time.monotonic() + 10,
        opener=open_model,
        progress=progress.append,
    )

    verify_release_model_snapshot(prepared, profile)
    assert len(requested) == 2
    assert all(response.closed for response in responses)
    assert progress[-1] == "Model snapshot qwen3-0.6b-v1 is ready and verified."
    assert any(message.startswith("Model download progress: 100%") for message in progress)

    replay_progress: list[str] = []
    replay = prepare_release_model_snapshot(
        cache_root=cache_root,
        profile=profile,
        deadline=time.monotonic() + 10,
        opener=lambda *_args: pytest.fail("verified replay must not download"),
        progress=replay_progress.append,
    )
    assert replay == prepared
    assert replay_progress == ["Model snapshot qwen3-0.6b-v1 is already verified."]


def test_model_cache_preparation_cleans_failed_and_cancelled_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"expected"
    identity = SelfDeployedModelFile(
        path="model.safetensors",
        byte_size=len(payload),
        digest_algorithm="sha256",
        digest=hashlib.sha256(payload).hexdigest(),
    )
    profile = _tiny_profile((identity,))
    cache_root = tmp_path / "models"
    cache_root.mkdir(mode=0o700)
    monkeypatch.setattr(cache_module, "_rename_noreplace", _portable_noreplace)

    with pytest.raises(SelfDeployedModelCacheError, match="digest"):
        prepare_release_model_snapshot(
            cache_root=cache_root,
            profile=profile,
            deadline=time.monotonic() + 10,
            opener=lambda *_args: _BytesResponse(b"tampered"),
        )
    assert list(cache_root.iterdir()) == []

    cancellation = threading.Event()
    cancellation.set()
    with pytest.raises(SelfDeployedModelCacheError, match="cancelled"):
        prepare_release_model_snapshot(
            cache_root=cache_root,
            profile=profile,
            deadline=time.monotonic() + 10,
            cancellation=cancellation,
            opener=lambda *_args: pytest.fail("cancelled preparation must not download"),
        )
    assert list(cache_root.iterdir()) == []
