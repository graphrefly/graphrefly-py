"""The protocol SENTINEL, shared by the kernel and the public facade."""

from __future__ import annotations

from typing import ClassVar, Final


class Sentinel:
    """Python representation of the protocol SENTINEL (absence of DATA).

    It is never a legal DATA payload or ERROR diagnostic (R-messages,
    R-sentinel); a dependency without a value is reported by presence, not
    by this object.
    """

    __slots__ = ()
    _instance: ClassVar[Sentinel | None] = None

    def __new__(cls) -> Sentinel:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "graphrefly.SENTINEL"


SENTINEL: Final = Sentinel()
