"""Core-owned runtime identities for managed Science execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Final, Literal, TypeAlias


ManagedRuntimeProfile: TypeAlias = Literal["managed_science", "python_research"]

MANAGED_RUNTIME_IMAGES: Final[dict[ManagedRuntimeProfile, str]] = {
    "managed_science": "openevo/science-runtime:0.1.0",
    "python_research": "openevo/python-research-runtime:0.1.0",
}
_SHA256_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ManagedRuntimeImageRelease:
    """Core-owned immutable image identity shipped by a release."""

    image: str
    trusted_digest: str

    def __post_init__(self) -> None:
        if "@" in self.image or not self.image.strip():
            raise ValueError("managed runtime image alias must be a non-empty tag")
        if _SHA256_DIGEST_RE.fullmatch(self.trusted_digest) is None:
            raise ValueError("managed runtime release digest must be a full sha256")

    @property
    def repository(self) -> str:
        final_slash = self.image.rfind("/")
        final_colon = self.image.rfind(":")
        if final_colon <= final_slash:
            raise ValueError("managed runtime image alias must include an explicit tag")
        return self.image[:final_colon]

    @property
    def immutable_reference(self) -> str:
        return f"{self.repository}@{self.trusted_digest}"


@dataclass(frozen=True, slots=True)
class ManagedCredentialMount:
    """Identity-bound host authority adopted by a managed runtime."""

    root: Path
    root_identity: tuple[int, int, int]
    auth_identity: tuple[int, int, int, int, int, int, int, int]


# The profile aliases remain internal compiler/runtime wiring. Trust comes only
# from these full release digests; a tag is never accepted as image identity.
MANAGED_RUNTIME_RELEASES: Final[dict[ManagedRuntimeProfile, ManagedRuntimeImageRelease]] = {
    profile: ManagedRuntimeImageRelease(
        image=image,
        trusted_digest=("sha256:16837a0db8af654383ea9af8af4f81a1175fbb0add74b98d7692cbaa87f44a5c"),
    )
    for profile, image in MANAGED_RUNTIME_IMAGES.items()
}
MANAGED_HOME: Final[str] = "/openevo/session/home"
MANAGED_CODEX_HOME: Final[str] = "/openevo/credentials/codex"
MANAGED_CODEX_BINARY: Final[str] = "/home/openevo/.local/bin/codex"
MANAGED_PATH: Final[str] = (
    "/home/openevo/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)
MANAGED_SUBSCRIPTION_ENV: Final[dict[str, str]] = {
    "HOME": MANAGED_HOME,
    "PATH": MANAGED_PATH,
    "CODEX_HOME": MANAGED_CODEX_HOME,
}
MANAGED_SUBSCRIPTION_ENV_KEYS: Final[frozenset[str]] = frozenset(MANAGED_SUBSCRIPTION_ENV)


def reject_managed_subscription_env(
    env: Mapping[str, object] | None,
    *,
    owner: str,
    allow_exact: bool = False,
) -> None:
    """Reject caller-controlled values for the managed subscription environment."""

    if not env:
        return
    for name in MANAGED_SUBSCRIPTION_ENV:
        if name in env:
            if allow_exact and env[name] == MANAGED_SUBSCRIPTION_ENV[name]:
                continue
            raise ValueError(f"subscription {owner} {name} is Core-owned and must be omitted")


def require_exact_managed_runtime_binding(
    *,
    profile: str | None,
    image: str | None,
) -> bool:
    """Return true only for a closed managed profile and its exact image."""

    if profile is None:
        return False
    if profile not in MANAGED_RUNTIME_IMAGES:
        raise ValueError(f"runtime profile/image binding is not Core-managed: {profile!r}")
    expected_image = MANAGED_RUNTIME_IMAGES[profile]
    if image != expected_image:
        raise ValueError(
            "runtime profile/image binding does not match the exact Core-managed "
            f"image {expected_image!r} for profile {profile!r}"
        )
    return True


def managed_runtime_image_release(
    *,
    profile: str | None,
    image: str | None,
) -> ManagedRuntimeImageRelease | None:
    """Return the release for its alias or one exact immutable authority."""

    if profile is None:
        return None
    if profile not in MANAGED_RUNTIME_RELEASES:
        raise ValueError(f"runtime profile/image binding is not Core-managed: {profile!r}")
    release = MANAGED_RUNTIME_RELEASES[profile]
    if image not in {release.image, release.trusted_digest, release.immutable_reference}:
        raise ValueError(
            "runtime profile/image binding does not match the exact Core-managed image release "
            f"for profile {profile!r}"
        )
    return release


def require_immutable_managed_runtime_image(
    *,
    profile: str | None,
    image: str | None,
) -> ManagedRuntimeImageRelease:
    """Require one exact immutable digest/reference for a managed release."""

    release = managed_runtime_image_release(profile=profile, image=image)
    if release is None or image not in {release.trusted_digest, release.immutable_reference}:
        raise ValueError("managed runtime execution requires an immutable image authority")
    return release


def verified_managed_runtime_image_reference(
    *,
    profile: str | None,
    image: str | None,
    image_id: object,
    repo_digests: object,
    labels: object,
) -> str:
    """Validate inspected image evidence and return an immutable Docker reference."""

    release = managed_runtime_image_release(profile=profile, image=image)
    if release is None:
        raise ValueError("managed runtime image release is unavailable")
    if (
        not isinstance(image_id, str)
        or _SHA256_DIGEST_RE.fullmatch(image_id) is None
        or not isinstance(labels, dict)
        or labels.get("io.openevo.managed-runtime") != "true"
    ):
        raise ValueError("managed runtime image identity is invalid")
    if repo_digests is not None and (
        not isinstance(repo_digests, list)
        or any(not isinstance(item, str) for item in repo_digests)
    ):
        raise ValueError("managed runtime repository digests are invalid")
    if image_id == release.trusted_digest:
        return release.trusted_digest
    if isinstance(repo_digests, list) and release.immutable_reference in repo_digests:
        return release.immutable_reference
    raise ValueError("managed runtime image digest mismatch")


def require_managed_subscription_runtime(
    *,
    profile: str | None,
    image: str | None,
    backend: str,
    container_user: str,
) -> None:
    """Require the exact Core-managed Docker runtime used for subscription auth."""

    if profile not in MANAGED_RUNTIME_IMAGES:
        raise ValueError("subscription execution requires a managed runtime profile")
    try:
        require_immutable_managed_runtime_image(profile=profile, image=image)
    except ValueError as exc:
        raise ValueError(
            "subscription execution requires the exact immutable managed runtime "
            f"image for profile {profile!r}"
        ) from exc
    if backend != "docker":
        raise ValueError("subscription execution requires the managed Docker runtime")
    if container_user != "host":
        raise ValueError("subscription credentials require runtime.container_user='host'")


def require_managed_runtime_binding(
    *,
    profile: str | None,
    image: str | None,
    backend: str,
    container_user: str,
) -> bool:
    """Validate any explicitly selected Core-managed runtime profile."""

    if profile is None:
        return False
    managed_runtime_image_release(profile=profile, image=image)
    if backend != "docker":
        raise ValueError("Core-managed runtime profiles require the Docker runtime")
    if container_user != "host":
        raise ValueError("Core-managed runtime profiles require runtime.container_user='host'")
    return True


__all__ = [
    "MANAGED_CODEX_HOME",
    "MANAGED_CODEX_BINARY",
    "MANAGED_HOME",
    "MANAGED_PATH",
    "MANAGED_RUNTIME_IMAGES",
    "MANAGED_RUNTIME_RELEASES",
    "MANAGED_SUBSCRIPTION_ENV",
    "MANAGED_SUBSCRIPTION_ENV_KEYS",
    "ManagedRuntimeProfile",
    "ManagedRuntimeImageRelease",
    "ManagedCredentialMount",
    "managed_runtime_image_release",
    "require_immutable_managed_runtime_image",
    "verified_managed_runtime_image_reference",
    "reject_managed_subscription_env",
    "require_exact_managed_runtime_binding",
    "require_managed_runtime_binding",
    "require_managed_subscription_runtime",
]
