"""Exception types shared across the package.

These live in their own module so that both :mod:`mcp_server.config` (which
decides whether a write is permitted) and :mod:`mcp_server.mfl_api` (which
performs it) can raise and catch the same class without importing each other.
"""


class MFLFantasyError(RuntimeError):
    """Base class for everything this package raises."""


class MFLError(MFLFantasyError):
    """An error returned by the MFL API, or a transport failure talking to it."""


class WritesDisabledError(MFLFantasyError):
    """A write was attempted while the write switches were not satisfied.

    This is deliberately *not* a subclass of :class:`MFLError`: a refused write
    is a safety decision, not an API problem, and callers should not retry it.
    """
