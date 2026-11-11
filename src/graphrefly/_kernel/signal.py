"""The lifetime's abort signal (R-invocation, R-lifetime)."""

from __future__ import annotations

from collections.abc import Callable


class Signal:
    """Aborted when its lifetime closes; not by an INVALIDATE or a rerun.

    Work a function starts for the lifetime (a timer, a request) listens with
    :meth:`on_abort` and stops when it fires, instead of checking :attr:`aborted`
    in a loop.
    """

    __slots__ = ("_aborted", "_listeners")

    def __init__(self) -> None:
        self._aborted = False
        self._listeners: list[Callable[[], object]] | None = []

    @property
    def aborted(self) -> bool:
        """Whether the lifetime has closed."""

        return self._aborted

    def on_abort(self, listener: Callable[[], object]) -> Callable[[], None]:
        """Call ``listener`` once when the lifetime closes; return a function that removes it.

        On a signal already aborted the listener runs at once.
        """

        if self._aborted or self._listeners is None:
            listener()
            return _nothing
        listeners = self._listeners
        listeners.append(listener)

        def remove() -> None:
            if listener in listeners:
                listeners.remove(listener)

        return remove

    def _abort(self, report: Callable[[Exception], None]) -> None:
        if self._aborted:
            return
        self._aborted = True
        listeners, self._listeners = self._listeners or [], None
        for listener in listeners:
            try:
                listener()
            except Exception as error:
                report(error)


def _nothing() -> None:
    return None
