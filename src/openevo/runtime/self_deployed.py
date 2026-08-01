"""Release-bound identities for the supported Self-Deployed execution profile.

The public project contract selects a stable profile ID.  Model repositories,
revisions, serving arguments, hardware floors, and image identities are owned by
the release and cannot be supplied by a project or renderer.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
from types import MappingProxyType
from typing import Final, Literal, Mapping


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SHA1_RE = re.compile(r"[0-9a-f]{40}\Z")
_MODEL_REVISION_RE = re.compile(r"[0-9a-f]{40}\Z")
_PROFILE_ID_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}\Z")
_IMAGE_RE = re.compile(r"[a-z0-9./_-]+@sha256:[0-9a-f]{64}\Z")


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class SelfDeployedModelFile:
    path: str
    byte_size: int
    digest_algorithm: Literal["git_blob_sha1", "sha256"]
    digest: str

    def __post_init__(self) -> None:
        path = PurePosixPath(self.path)
        if (
            not self.path
            or path.is_absolute()
            or str(path) != self.path
            or any(part in {"", ".", ".."} for part in path.parts)
            or type(self.byte_size) is not int
            or self.byte_size <= 0
        ):
            raise ValueError("Self-Deployed model file identity is invalid")
        pattern = _SHA1_RE if self.digest_algorithm == "git_blob_sha1" else _SHA256_RE
        if pattern.fullmatch(self.digest) is None:
            raise ValueError("Self-Deployed model file digest is invalid")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "byte_size": self.byte_size,
            "digest_algorithm": self.digest_algorithm,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class SelfDeployedModelProfile:
    profile_id: str
    display_name: str
    model_id: str
    model_revision: str
    license_spdx: str
    architecture: str
    maximum_context_tokens: int
    minimum_free_vram_bytes: int
    minimum_system_memory_bytes: int
    minimum_free_disk_bytes: int
    tensor_parallel_size: int
    gpu_memory_utilization_milli: int
    vllm_image: str
    vllm_image_config_digest: str
    vllm_version: str
    serving_arguments: tuple[str, ...]
    required_files: tuple[SelfDeployedModelFile, ...]
    model_snapshot_manifest_sha256: str
    profile_sha256: str

    def __post_init__(self) -> None:
        if (
            _PROFILE_ID_RE.fullmatch(self.profile_id) is None
            or not 1 <= len(self.display_name) <= 128
            or self.display_name != self.display_name.strip()
            or not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", self.model_id)
            or _MODEL_REVISION_RE.fullmatch(self.model_revision) is None
            or self.license_spdx != "Apache-2.0"
            or self.architecture != "Qwen3ForCausalLM"
            or type(self.maximum_context_tokens) is not int
            or not 1 <= self.maximum_context_tokens <= 1_000_000
            or type(self.minimum_free_vram_bytes) is not int
            or self.minimum_free_vram_bytes < 1024**3
            or type(self.minimum_system_memory_bytes) is not int
            or self.minimum_system_memory_bytes < 1024**3
            or type(self.minimum_free_disk_bytes) is not int
            or self.minimum_free_disk_bytes < 1024**3
            or self.tensor_parallel_size != 1
            or not 1 <= self.gpu_memory_utilization_milli <= 999
            or _IMAGE_RE.fullmatch(self.vllm_image) is None
            or not self.vllm_image.startswith("docker.io/vllm/vllm-openai@sha256:")
            or not self.vllm_image_config_digest.startswith("sha256:")
            or _SHA256_RE.fullmatch(
                self.vllm_image_config_digest.removeprefix("sha256:")
            )
            is None
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", self.vllm_version)
            or not self.serving_arguments
            or any(
                not isinstance(argument, str)
                or not argument
                or len(argument.encode("utf-8")) > 256
                or any(ord(character) < 0x20 for character in argument)
                for argument in self.serving_arguments
            )
            or not self.required_files
            or tuple(item.path for item in self.required_files)
            != tuple(sorted(item.path for item in self.required_files))
            or len({item.path for item in self.required_files}) != len(self.required_files)
        ):
            raise ValueError("Self-Deployed model profile is invalid")
        if self.model_snapshot_manifest_sha256 != self.computed_snapshot_manifest_sha256:
            raise ValueError("Self-Deployed model snapshot manifest digest is invalid")
        if self.profile_sha256 != self.computed_profile_sha256:
            raise ValueError("Self-Deployed model profile digest is invalid")

    @property
    def computed_snapshot_manifest_sha256(self) -> str:
        return _digest(
            {
                "model_snapshot_contract_version": "1",
                "model_id": self.model_id,
                "model_revision": self.model_revision,
                "required_files": [item.canonical_payload() for item in self.required_files],
            }
        )

    @property
    def computed_profile_sha256(self) -> str:
        return _digest(self.canonical_profile_payload())

    def canonical_profile_payload(self) -> dict[str, object]:
        return {
            "profile_contract_version": "1",
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "model_id": self.model_id,
            "model_revision": self.model_revision,
            "license_spdx": self.license_spdx,
            "architecture": self.architecture,
            "maximum_context_tokens": self.maximum_context_tokens,
            "minimum_free_vram_bytes": self.minimum_free_vram_bytes,
            "minimum_system_memory_bytes": self.minimum_system_memory_bytes,
            "minimum_free_disk_bytes": self.minimum_free_disk_bytes,
            "tensor_parallel_size": self.tensor_parallel_size,
            "gpu_memory_utilization_milli": self.gpu_memory_utilization_milli,
            "vllm_image": self.vllm_image,
            "vllm_image_config_digest": self.vllm_image_config_digest,
            "vllm_version": self.vllm_version,
            "model_snapshot_manifest_sha256": self.model_snapshot_manifest_sha256,
            "serving_arguments": list(self.serving_arguments),
        }


_QWEN3_06B_FILES: Final[tuple[SelfDeployedModelFile, ...]] = (
    SelfDeployedModelFile(
        path=".gitattributes",
        byte_size=1_570,
        digest_algorithm="git_blob_sha1",
        digest="52373fe24473b1aa44333d318f578ae6bf04b49b",
    ),
    SelfDeployedModelFile(
        path="LICENSE",
        byte_size=11_343,
        digest_algorithm="git_blob_sha1",
        digest="6634c8cc3133b3848ec74b9f275acaaa1ea618ab",
    ),
    SelfDeployedModelFile(
        path="README.md",
        byte_size=13_965,
        digest_algorithm="git_blob_sha1",
        digest="a50b19e76f5274f9ec99f5a5d99873dca5bff25e",
    ),
    SelfDeployedModelFile(
        path="config.json",
        byte_size=726,
        digest_algorithm="git_blob_sha1",
        digest="f5c3703b78ae2a478ae15b247e9f855e0ce2107b",
    ),
    SelfDeployedModelFile(
        path="generation_config.json",
        byte_size=239,
        digest_algorithm="git_blob_sha1",
        digest="20a8a9156fc8c3f25295ca067f61fdf120d517c5",
    ),
    SelfDeployedModelFile(
        path="merges.txt",
        byte_size=1_671_853,
        digest_algorithm="git_blob_sha1",
        digest="31349551d90c7606f325fe0f11bbb8bd5fa0d7c7",
    ),
    SelfDeployedModelFile(
        path="model.safetensors",
        byte_size=1_503_300_328,
        digest_algorithm="sha256",
        digest="f47f71177f32bcd101b7573ec9171e6a57f4f4d31148d38e382306f42996874b",
    ),
    SelfDeployedModelFile(
        path="tokenizer.json",
        byte_size=11_422_654,
        digest_algorithm="sha256",
        digest="aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4",
    ),
    SelfDeployedModelFile(
        path="tokenizer_config.json",
        byte_size=9_732,
        digest_algorithm="git_blob_sha1",
        digest="417d038a63fa3de29cfde265caedae14d1a58d92",
    ),
    SelfDeployedModelFile(
        path="vocab.json",
        byte_size=2_776_833,
        digest_algorithm="git_blob_sha1",
        digest="4783fe10ac3adce15ac8f358ef5462739852c569",
    ),
)

_QWEN3_06B_PROFILE = SelfDeployedModelProfile(
    profile_id="qwen3-0.6b-v1",
    display_name="Qwen3 0.6B (OpenEvo validated)",
    model_id="Qwen/Qwen3-0.6B",
    model_revision="c1899de289a04d12100db370d81485cdf75e47ca",
    license_spdx="Apache-2.0",
    architecture="Qwen3ForCausalLM",
    maximum_context_tokens=8_192,
    minimum_free_vram_bytes=8 * 1024**3,
    minimum_system_memory_bytes=16 * 1024**3,
    minimum_free_disk_bytes=30 * 1024**3,
    tensor_parallel_size=1,
    gpu_memory_utilization_milli=200,
    vllm_image=(
        "docker.io/vllm/vllm-openai@"
        "sha256:df2607b26bdda2875de4832f4d08da0055b4b6e3570347f3a849bcc652771dd6"
    ),
    vllm_image_config_digest=(
        "sha256:5791f8642d1b71d45d832e232250aeca6a9aeb99b89698dff3c1ca5eea7a9655"
    ),
    vllm_version="0.10.2",
    serving_arguments=(
        "--dtype",
        "bfloat16",
        "--max-model-len",
        "8192",
        "--gpu-memory-utilization",
        "0.20",
        "--tensor-parallel-size",
        "1",
        "--disable-log-stats",
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        "hermes",
    ),
    required_files=_QWEN3_06B_FILES,
    model_snapshot_manifest_sha256=(
        "25e6685d8f97d7221452a7fe0d58df5efa9f67045cc7fb54cec7c270c8f12383"
    ),
    profile_sha256="c9458b77eef092be684ac41265478a2a9a0264fbabaf5a66aa47c968aa3827c0",
)

RELEASE_SELF_DEPLOYED_MODEL_PROFILES: Final[
    Mapping[str, SelfDeployedModelProfile]
] = MappingProxyType({_QWEN3_06B_PROFILE.profile_id: _QWEN3_06B_PROFILE})


def require_release_self_deployed_model_profile(
    profile_id: str,
) -> SelfDeployedModelProfile:
    if not isinstance(profile_id, str):
        raise TypeError("Self-Deployed model profile ID must be text")
    try:
        profile = RELEASE_SELF_DEPLOYED_MODEL_PROFILES[profile_id]
    except KeyError as exc:
        raise ValueError(
            f"Self-Deployed model profile {profile_id!r} is not supported by this release"
        ) from exc
    profile.__post_init__()
    return profile


__all__ = [
    "RELEASE_SELF_DEPLOYED_MODEL_PROFILES",
    "SelfDeployedModelFile",
    "SelfDeployedModelProfile",
    "require_release_self_deployed_model_profile",
]
