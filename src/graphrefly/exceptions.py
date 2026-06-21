"""Public exception classes for the v0 Python facade."""


class GraphReflyError(Exception):
    """Base class for public Python facade errors."""


class GraphReflyRuntimeError(GraphReflyError, RuntimeError):
    """Runtime boundary failure from the native graph engine or host facade."""


class GraphReflyValueError(GraphReflyError, ValueError):
    """Invalid public API value for this facade slice."""


class CallbackError(GraphReflyError):
    """A Python callback failed and is mapped into graph ERROR."""
