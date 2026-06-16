"""Shared helpers for CLI harnesses that can run with local subscription auth."""

from __future__ import annotations

from polar.agent.capture import TRANSCRIPT_CAPTURE_MODES, transcript_capture_enabled

AUTH_MODE_PROXY = "proxy"
AUTH_MODE_SUBSCRIPTION = "subscription"

SUBSCRIPTION_PROXY_ENV_VARS = (
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_URL",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_GEMINI_BASE_URL",
)


def normalize_auth_mode(
    raw_mode: object,
    *,
    harness: str,
    subscription_aliases: tuple[str, ...] = (),
    capture_mode: object = None,
) -> str:
    """Normalize a harness auth mode setting to proxy or subscription."""

    mode = str(raw_mode or AUTH_MODE_PROXY)
    if mode in subscription_aliases:
        mode = AUTH_MODE_SUBSCRIPTION
    if mode in {AUTH_MODE_PROXY, AUTH_MODE_SUBSCRIPTION}:
        if mode == AUTH_MODE_SUBSCRIPTION and not transcript_capture_enabled(capture_mode):
            accepted_capture_modes = ", ".join(repr(value) for value in TRANSCRIPT_CAPTURE_MODES)
            raise ValueError(
                f"{harness} settings.auth_mode='subscription' requires "
                f"settings.capture_mode to be one of: {accepted_capture_modes}"
            )
        return mode
    accepted = ", ".join(
        repr(value) for value in (AUTH_MODE_PROXY, AUTH_MODE_SUBSCRIPTION, *subscription_aliases)
    )
    raise ValueError(f"{harness} settings.auth_mode must be one of: {accepted}")


def command_with_unset_proxy_env(command: str) -> str:
    """Run a command with Polar proxy environment variables removed."""

    unset_flags = " ".join(f"-u {key}" for key in SUBSCRIPTION_PROXY_ENV_VARS)
    return f"env {unset_flags} {command}"
