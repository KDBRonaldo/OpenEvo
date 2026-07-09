"""Shared agent capture mode helpers."""

from __future__ import annotations

TRANSCRIPT_CAPTURE_MODES = ("transcript", "agent_transcript", "pure_text")


def transcript_capture_enabled(raw_mode: object) -> bool:
    """Return whether a settings.capture_mode value enables text transcript capture."""

    return str(raw_mode or "").lower() in TRANSCRIPT_CAPTURE_MODES
