"""The closed message set and the checks on node output.

A message is a tuple whose first element is its type (R-messages): ``(DATA, v)``,
``(ERROR, e)`` or a bare ``(TYPE,)``. An array is the sequence of messages one
``ctx.down`` call or one host input carries (R-wave). The kernel keeps the order
an array was written in and never sorts it by class.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from graphrefly._sentinel import SENTINEL

START: Final = "START"
PENDING: Final = "PENDING"
DATA: Final = "DATA"
RESOLVED: Final = "RESOLVED"
INVALIDATE: Final = "INVALIDATE"
COMPLETE: Final = "COMPLETE"
ERROR: Final = "ERROR"

#: The closed set (R-messages). TEARDOWN, PAUSE, RESUME and PULL are not messages.
MESSAGE_TYPES: Final = frozenset({START, PENDING, DATA, RESOLVED, INVALIDATE, COMPLETE, ERROR})

#: Sent only by the kernel; rejected in node output (R-wave@2, R-subscription@3 and
#: R-failure@3; START by graphrefly:D801).
KERNEL_ONLY: Final = frozenset({START, PENDING, RESOLVED})

#: ``(TYPE,)`` or ``(TYPE, payload)``; the shape is checked at runtime (check_message).
type Message = tuple[Any, ...]
type Array = list[Message]

PENDING_MESSAGE: Final[Message] = (PENDING,)
RESOLVED_MESSAGE: Final[Message] = (RESOLVED,)
INVALIDATE_MESSAGE: Final[Message] = (INVALIDATE,)
COMPLETE_MESSAGE: Final[Message] = (COMPLETE,)
START_MESSAGE: Final[Message] = (START,)


class KernelError(Exception):
    """A request or a node output that the kernel refuses."""


def is_terminal(message: Message) -> bool:
    """Return whether ``message`` is COMPLETE or ERROR."""

    kind: str = message[0]
    return kind in (COMPLETE, ERROR)


def is_value(message: Message) -> bool:
    """Return whether ``message`` is DATA or RESOLVED."""

    kind: str = message[0]
    return kind in (DATA, RESOLVED)


def marks(message: Message) -> bool:
    """Return whether an array holding ``message`` marks its sender's cone pending.

    DATA, RESOLVED, INVALIDATE, COMPLETE and ERROR mark and settle (R-wave); START
    and PENDING do neither.
    """

    kind: str = message[0]
    return kind not in (START, PENDING)


def check_message(message: object) -> Message:
    """Check one message's shape and payload; return it typed, or raise KernelError."""

    if not isinstance(message, tuple) or not message:
        raise KernelError(f"a message is a non-empty tuple, not {message!r}")
    kind = message[0]
    if not isinstance(kind, str) or kind not in MESSAGE_TYPES:
        raise KernelError(f"unknown message type {kind!r}")
    if kind in (DATA, ERROR):
        if len(message) != 2:
            raise KernelError(f"{kind} carries exactly one payload")
        payload = message[1]
        if payload is SENTINEL:
            raise KernelError(f"{kind} cannot carry SENTINEL")
        if kind == ERROR and isinstance(payload, bool):
            raise KernelError("an ERROR diagnostic cannot be a bool")
        return (kind, payload)
    if len(message) != 1:
        raise KernelError(f"{kind} carries no payload")
    return (kind,)


def split(array: Sequence[Message]) -> list[Array]:
    """Split a checked array at its last INVALIDATE (R-wave).

    What came before the last INVALIDATE is dropped and the INVALIDATE goes out as
    an array of its own: ``[1, INVALIDATE, 2, 3]`` becomes ``[INVALIDATE]`` then
    ``[2, 3]``. A terminal must be the last message of the array.
    """

    last = len(array) - 1
    cut = -1
    for index, message in enumerate(array):
        kind = message[0]
        if (kind in (COMPLETE, ERROR)) and index != last:
            raise KernelError("a terminal must be the last message of its array")
        if kind == INVALIDATE:
            cut = index
    if cut < 0:
        return [list(array)] if array else []
    rest = list(array[cut + 1 :])
    return [[INVALIDATE_MESSAGE], rest] if rest else [[INVALIDATE_MESSAGE]]


def prepare_output(array: Sequence[object]) -> list[Array]:
    """Check one ``ctx.down`` array and return the arrays it publishes.

    Rejects, before anything is applied, a message that is malformed or of an
    unknown type, START, PENDING or RESOLVED (the kernel's alone), DATA or ERROR carrying
    SENTINEL, an ERROR whose diagnostic is a bool, and a terminal that is not last
    (R-failure).
    """

    if type(array) is list and len(array) == 1:
        only = array[0]
        if type(only) is tuple and len(only) == 2 and only[0] == DATA and only[1] is not SENTINEL:
            return [[only]]  # the common case: one DATA
    if isinstance(array, tuple | str | bytes) or not isinstance(array, Sequence):
        raise KernelError("ctx.down takes a list of message tuples")
    checked: list[Message] = []
    for item in array:
        message = check_message(item)
        if message[0] in KERNEL_ONLY:
            raise KernelError(f"{message[0]} is sent by the kernel, not by node output")
        checked.append(message)
    return split(checked)
