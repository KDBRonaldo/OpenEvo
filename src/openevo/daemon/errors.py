"""Shared errors surfaced by the self-hosted daemon application boundary."""

from __future__ import annotations


class RequestError(ValueError):
    """An authenticated daemon request is malformed or exceeds a bound."""


class AgentRunError(RuntimeError):
    """An admitted agent operation produced invalid or unsafe output."""


class StateConflictError(RuntimeError):
    """A valid request conflicts with current durable daemon authority."""
