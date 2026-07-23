"""Typed control-flow errors for the Desktop SSH host catalog provider."""


class SshCatalogGenerationChangedError(Exception):
    """A catalog mutation named a stale semantic generation."""


class SshCatalogIdempotencyConflictError(Exception):
    """A catalog action key was reused for another request identity."""


class SshCatalogActionCapacityError(Exception):
    """The bounded in-memory catalog action ledger is full."""


__all__ = (
    "SshCatalogActionCapacityError",
    "SshCatalogGenerationChangedError",
    "SshCatalogIdempotencyConflictError",
)
