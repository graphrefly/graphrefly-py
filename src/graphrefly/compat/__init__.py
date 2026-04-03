"""Framework compatibility layer — asyncio/trio runners and async utilities (roadmap §5.1)."""

from graphrefly.compat.async_utils import (
    first_value_from_async,
    settled,
    to_async_iter,
)
from graphrefly.compat.asyncio_runner import AsyncioRunner

__all__ = [
    "AsyncioRunner",
    "first_value_from_async",
    "settled",
    "to_async_iter",
]

# TrioRunner is available via direct import (optional trio dependency):
#   from graphrefly.compat.trio_runner import TrioRunner
