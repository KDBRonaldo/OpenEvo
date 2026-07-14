"""Core-owned runtime identities for managed Science execution."""

from __future__ import annotations

from typing import Final, Literal, TypeAlias


ManagedRuntimeProfile: TypeAlias = Literal["managed_science", "python_research"]

MANAGED_RUNTIME_IMAGES: Final[dict[ManagedRuntimeProfile, str]] = {
    "managed_science": "openevo/science-runtime:0.1.0",
    "python_research": "openevo/python-research-runtime:0.1.0",
}
MANAGED_CODEX_HOME: Final[str] = "/openevo/credentials/codex"


def require_managed_subscription_runtime(
    *,
    profile: str | None,
    image: str | None,
    backend: str,
    container_user: str,
) -> None:
    """Require the exact Core-managed Docker runtime used for subscription auth."""

    if profile not in MANAGED_RUNTIME_IMAGES:
        raise ValueError(
            "subscription execution requires a managed runtime profile"
        )
    expected_image = MANAGED_RUNTIME_IMAGES[profile]
    if image != expected_image:
        raise ValueError(
            "subscription execution requires the exact managed runtime image "
            f"{expected_image!r} for profile {profile!r}"
        )
    if backend != "docker":
        raise ValueError("subscription execution requires the managed Docker runtime")
    if container_user != "host":
        raise ValueError(
            "subscription credentials require runtime.container_user='host'"
        )


__all__ = [
    "MANAGED_CODEX_HOME",
    "MANAGED_RUNTIME_IMAGES",
    "ManagedRuntimeProfile",
    "require_managed_subscription_runtime",
]
