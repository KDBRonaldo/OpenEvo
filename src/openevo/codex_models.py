"""Shared Codex model normalization and validation."""

from __future__ import annotations

from typing import Final


CODEX_PROVIDER_PREFIXES: Final[tuple[str, ...]] = (
    "gcp/google/",
    "openai/",
    "anthropic/",
    "google/",
)
UNSUPPORTED_BARE_CODEX_MODELS: Final[frozenset[str]] = frozenset({"gpt-5"})
MAX_CODEX_CLI_MODEL_BYTES: Final[int] = 128


def codex_cli_model_name(model_name: str) -> str:
    """Return the model identifier that Codex CLI will receive."""

    for prefix in CODEX_PROVIDER_PREFIXES:
        if model_name.startswith(prefix):
            return model_name[len(prefix) :]
    return model_name


def validate_codex_model_ref(
    model_name: str,
    *,
    field_name: str = "Codex model",
    max_length: int = 256,
) -> str:
    """Validate a persisted model reference against its final Codex CLI value."""

    model = model_name.strip()
    if (
        not model
        or len(model) > max_length
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in model)
    ):
        raise ValueError(f"{field_name} is invalid")
    cli_model = codex_cli_model_name(model)
    if (
        not cli_model
        or len(cli_model.encode("ascii", errors="ignore")) != len(cli_model)
        or len(cli_model) > MAX_CODEX_CLI_MODEL_BYTES
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in cli_model)
    ):
        raise ValueError(f"{field_name} has an invalid final Codex CLI model value")
    if cli_model in UNSUPPORTED_BARE_CODEX_MODELS:
        raise ValueError(
            f"{field_name} must name a valid Codex model; "
            f"bare {cli_model} is unsupported after provider normalization"
        )
    return model
