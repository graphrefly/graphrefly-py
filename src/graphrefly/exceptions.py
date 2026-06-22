"""Public exception classes for the Python facade."""


class GraphReflyError(Exception):
    """Base class for public Python facade errors."""


class GraphReflyRuntimeError(GraphReflyError, RuntimeError):
    """Runtime boundary failure from the native graph engine or host facade."""


class GraphReflyValueError(GraphReflyError, ValueError):
    """Invalid public API value for this facade slice."""


class GraphReflyNoDataError(GraphReflyError, LookupError):
    """A node has no cached DATA value at the public Python boundary."""


class CallbackError(GraphReflyError):
    """A Python callback failed and is mapped into graph ERROR."""


class GraphCallbackError(CallbackError):
    """Callback failure represented as a graph ERROR payload."""

    def __init__(self, type_name: str, message: str, raw: object | None = None) -> None:
        self.type_name = type_name
        self.message = message
        self.raw = raw
        super().__init__(f"{type_name}: {message}" if type_name else message)


class SubscriberCallbackError(CallbackError):
    """A subscribe/observe callback failed at the Python observer boundary."""

    def __init__(self, original: Exception) -> None:
        self.original = original
        super().__init__(str(original))
