"""Core-owned runtime identities for managed Science execution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal, TypeAlias


ManagedRuntimeProfile: TypeAlias = Literal["managed_science", "python_research"]

MANAGED_RUNTIME_IMAGES: Final[dict[ManagedRuntimeProfile, str]] = {
    "managed_science": "openevo/science-runtime:0.1.0",
    "python_research": "openevo/python-research-runtime:0.1.0",
}
MANAGED_HOME: Final[str] = "/openevo/session/home"
MANAGED_CODEX_HOME: Final[str] = "/openevo/credentials/codex"
MANAGED_CODEX_BINARY: Final[str] = "/home/openevo/.local/bin/codex"
MANAGED_PATH: Final[str] = (
    "/home/openevo/.local/bin:/usr/local/sbin:/usr/local/bin:"
    "/usr/sbin:/usr/bin:/sbin:/bin"
)
MANAGED_SUBSCRIPTION_ENV: Final[dict[str, str]] = {
    "HOME": MANAGED_HOME,
    "PATH": MANAGED_PATH,
    "CODEX_HOME": MANAGED_CODEX_HOME,
}
MANAGED_SUBSCRIPTION_ENV_KEYS: Final[frozenset[str]] = frozenset(
    MANAGED_SUBSCRIPTION_ENV
)


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
            raise ValueError(
                f"subscription {owner} {name} is Core-owned and must be omitted"
            )


def require_exact_managed_runtime_binding(
    *,
    profile: str | None,
    image: str | None,
) -> bool:
    """Return true only for a closed managed profile and its exact image."""

    if profile is None:
        return False
    if profile not in MANAGED_RUNTIME_IMAGES:
        raise ValueError(
            f"runtime profile/image binding is not Core-managed: {profile!r}"
        )
    expected_image = MANAGED_RUNTIME_IMAGES[profile]
    if image != expected_image:
        raise ValueError(
            "runtime profile/image binding does not match the exact Core-managed "
            f"image {expected_image!r} for profile {profile!r}"
        )
    return True


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
    try:
        require_exact_managed_runtime_binding(profile=profile, image=image)
    except ValueError as exc:
        raise ValueError(
            "subscription execution requires the exact managed runtime image "
            f"{MANAGED_RUNTIME_IMAGES[profile]!r} for profile {profile!r}"
        ) from exc
    if backend != "docker":
        raise ValueError("subscription execution requires the managed Docker runtime")
    if container_user != "host":
        raise ValueError(
            "subscription credentials require runtime.container_user='host'"
        )


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
    require_exact_managed_runtime_binding(profile=profile, image=image)
    if backend != "docker":
        raise ValueError("Core-managed runtime profiles require the Docker runtime")
    if container_user != "host":
        raise ValueError(
            "Core-managed runtime profiles require runtime.container_user='host'"
        )
    return True


__all__ = [
    "MANAGED_CODEX_HOME",
    "MANAGED_CODEX_BINARY",
    "MANAGED_HOME",
    "MANAGED_PATH",
    "MANAGED_RUNTIME_IMAGES",
    "MANAGED_SUBSCRIPTION_ENV",
    "MANAGED_SUBSCRIPTION_ENV_KEYS",
    "ManagedRuntimeProfile",
    "reject_managed_subscription_env",
    "require_exact_managed_runtime_binding",
    "require_managed_runtime_binding",
    "require_managed_subscription_runtime",
]
