"""MeasurementAdapter implementations (roadmap §7.1 — pluggable backends).

All adapters satisfy the :class:`~graphrefly.extra.reactive_layout.MeasurementAdapter`
protocol.  Sync constructors, sync ``measure_segment()`` — no async, no polling.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Shared: East Asian Width detection for CLI adapter
# ---------------------------------------------------------------------------


def _cell_width(ch: str) -> int:
    """Return display-cell width of a single character in a monospace terminal.

    Combining marks (category M) → 0 cells; fullwidth / wide CJK → 2 cells;
    everything else → 1 cell.  Uses :func:`unicodedata.east_asian_width` and
    :func:`unicodedata.category`.

    Does not handle ZWJ emoji sequences (multi-codepoint clusters that
    render as a single glyph) — terminal support for these varies widely.
    """
    cat = unicodedata.category(ch)
    # Combining marks (Mn, Mc, Me) + ZWJ → 0 cells
    if cat.startswith("M") or ch == "\u200d":
        return 0
    eaw = unicodedata.east_asian_width(ch)
    # W = Wide, F = Fullwidth
    if eaw in ("W", "F"):
        return 2
    return 1


def _count_cells(text: str) -> int:
    """Count total display cells for *text* in a monospace terminal."""
    return sum(_cell_width(ch) for ch in text)


# ---------------------------------------------------------------------------
# CliMeasureAdapter
# ---------------------------------------------------------------------------


class CliMeasureAdapter:
    """Monospace terminal measurement adapter.

    Width = cell count × ``cell_px``.  CJK / fullwidth characters count as 2 cells.
    No external dependencies.  Works in any Python environment.

    Parameters
    ----------
    cell_px:
        Pixel width per terminal cell (default: 8).
    """

    __slots__ = ("_cell_px",)

    def __init__(self, *, cell_px: float = 8) -> None:
        self._cell_px = cell_px

    def measure_segment(self, text: str, font: str) -> dict[str, float]:
        """Return ``{"width": <px>}`` for *text* using monospace cell counting."""
        return {"width": _count_cells(text) * self._cell_px}

    def clear_cache(self) -> None:
        """CliMeasureAdapter is stateless; this is a no-op hook."""


# ---------------------------------------------------------------------------
# PrecomputedAdapter
# ---------------------------------------------------------------------------


class PrecomputedAdapter:
    """Pre-computed measurement adapter for SSR / snapshot replay.

    Reads from a static metrics dict — zero measurement at runtime.
    Ideal for server-side rendering or replaying snapshotted layouts.

    Parameters
    ----------
    metrics:
        ``{font: {segment: width_px}}``.  Outer key is the CSS font string;
        inner key is the text segment.
    fallback:
        What to do when a segment is not found:
        ``"per-char"`` (default) — sum individual character widths.
        ``"error"`` — raise :class:`KeyError`.
    """

    __slots__ = ("_metrics", "_fallback")

    def __init__(
        self,
        metrics: dict[str, dict[str, float]],
        *,
        fallback: str = "per-char",
    ) -> None:
        if fallback not in ("per-char", "error"):
            raise ValueError(f"fallback must be 'per-char' or 'error', got {fallback!r}")
        self._metrics = metrics
        self._fallback = fallback

    def measure_segment(self, text: str, font: str) -> dict[str, float]:
        """Return ``{"width": <px>}`` from pre-computed metrics."""
        font_map = self._metrics.get(font)
        if font_map is not None:
            w = font_map.get(text)
            if w is not None:
                return {"width": w}

        if self._fallback == "error":
            raise KeyError(
                f"PrecomputedAdapter: no metrics for segment {text!r} in font {font!r}"
            )

        # per-char fallback: sum individual character widths
        total = 0.0
        if font_map is not None:
            for ch in text:
                cw = font_map.get(ch)
                if cw is not None:
                    total += cw
        return {"width": total}

    def clear_cache(self) -> None:
        """PrecomputedAdapter is stateless; this is a no-op hook."""


# ---------------------------------------------------------------------------
# PillowMeasureAdapter
# ---------------------------------------------------------------------------


class PillowMeasureAdapter:
    """Server-side measurement adapter using Pillow ``ImageFont.getlength()``.

    Requires ``Pillow`` as an optional dependency.  Font objects are cached
    by ``(font_path, size)`` tuple.

    Parameters
    ----------
    font_map:
        ``{css_font_string: (font_path, size)}`` mapping CSS font strings to
        Pillow font constructor args.  Example::

            {"16px serif": ("/usr/share/fonts/serif.ttf", 16)}

    fallback_font:
        ``(font_path, size)`` used when a CSS font string is not in *font_map*.
        If ``None`` (default), Pillow's default font is used.
    """

    __slots__ = ("_font_map", "_fallback_font", "_cache")

    def __init__(
        self,
        font_map: dict[str, tuple[str, int]] | None = None,
        *,
        fallback_font: tuple[str, int] | None = None,
    ) -> None:
        self._font_map = font_map or {}
        self._fallback_font = fallback_font
        self._cache: dict[tuple[str, int] | None, Any] = {}

    def _get_font(self, font: str) -> Any:
        from PIL import ImageFont  # type: ignore[import-untyped]

        spec = self._font_map.get(font, self._fallback_font)
        if spec in self._cache:
            return self._cache[spec]

        if spec is None:
            pil_font = ImageFont.load_default()
        else:
            pil_font = ImageFont.truetype(spec[0], spec[1])

        self._cache[spec] = pil_font
        return pil_font

    def measure_segment(self, text: str, font: str) -> dict[str, float]:
        """Return ``{"width": <px>}`` via Pillow ``getlength()``."""
        pil_font = self._get_font(font)
        return {"width": float(pil_font.getlength(text))}

    def clear_cache(self) -> None:
        """Discard cached Pillow font objects."""
        self._cache.clear()
