"""Shared agent capture mode helpers."""

from __future__ import annotations

TRANSCRIPT_CAPTURE_MODES = ("transcript", "agent_transcript", "pure_text")


def canonicalize_capture_mode(settings: dict[str, object]) -> object:
    """Write the canonical transcript mode back into one settings mapping."""

    raw_mode = settings.get("capture_mode")
    if transcript_capture_enabled(raw_mode):
        settings["capture_mode"] = "transcript"
        return "transcript"
    return raw_mode


def transcript_capture_enabled(raw_mode: object) -> bool:
    """Return whether a settings.capture_mode value enables text transcript capture."""

    return str(raw_mode or "").lower() in TRANSCRIPT_CAPTURE_MODES
