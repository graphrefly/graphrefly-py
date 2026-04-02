"""Reactive text layout engine (roadmap §7.1 — Pretext parity).

Pure-arithmetic text measurement and line breaking without DOM thrashing.
Inspired by `Pretext <https://github.com/chenglou/pretext>`_, rebuilt as a
GraphReFly graph — inspectable via ``describe()``, snapshotable, debuggable.

Two-tier DX:

- ``reactive_layout(adapter, *, text=..., font=..., ...)`` — convenience factory
- :class:`MeasurementAdapter` — pluggable backends (``measure_segment``; optional ``clear_cache``)
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from graphrefly.core.clock import monotonic_ns
from graphrefly.core.protocol import MessageType, emit_with_batch
from graphrefly.core.sugar import derived, state
from graphrefly.graph.graph import Graph

if TYPE_CHECKING:
    from graphrefly.core.node import NodeImpl

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@runtime_checkable
class MeasurementAdapter(Protocol):
    """Pluggable measurement backend.

    Implementations may omit ``clear_cache``; the layout engine treats it as an
    optional no-op hook.
    """

    def measure_segment(self, text: str, font: str) -> dict[str, float]:
        """Return ``{"width": <px>}`` for *text* rendered in *font*."""
        ...

    def clear_cache(self) -> None:
        """Optional hook to clear internal cached measurement state."""
        ...


class SegmentBreakKind(StrEnum):
    """Break kind for each segment (ported from Pretext analysis.ts)."""

    TEXT = "text"
    SPACE = "space"
    ZERO_WIDTH_BREAK = "zero-width-break"
    SOFT_HYPHEN = "soft-hyphen"
    HARD_BREAK = "hard-break"


class PreparedSegment:
    """A measured text segment ready for line breaking."""

    __slots__ = ("text", "width", "kind", "grapheme_widths")

    def __init__(
        self,
        text: str,
        width: float,
        kind: SegmentBreakKind,
        grapheme_widths: list[float] | None = None,
    ) -> None:
        self.text = text
        self.width = width
        self.kind = kind
        self.grapheme_widths = grapheme_widths

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PreparedSegment):
            return NotImplemented
        return (
            self.text == other.text
            and self.width == other.width
            and self.kind == other.kind
            and self.grapheme_widths == other.grapheme_widths
        )

    def __repr__(self) -> str:
        return (
            f"PreparedSegment({self.text!r}, {self.width}, {self.kind!r},"
            f" grapheme_widths={self.grapheme_widths})"
        )


class LayoutLine:
    """A laid-out line with start/end cursors."""

    __slots__ = (
        "text",
        "width",
        "start_segment",
        "start_grapheme",
        "end_segment",
        "end_grapheme",
    )

    def __init__(
        self,
        text: str,
        width: float,
        start_segment: int,
        start_grapheme: int,
        end_segment: int,
        end_grapheme: int,
    ) -> None:
        self.text = text
        self.width = width
        self.start_segment = start_segment
        self.start_grapheme = start_grapheme
        self.end_segment = end_segment
        self.end_grapheme = end_grapheme

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LayoutLine):
            return NotImplemented
        return (
            self.text == other.text
            and self.width == other.width
            and self.start_segment == other.start_segment
            and self.start_grapheme == other.start_grapheme
            and self.end_segment == other.end_segment
            and self.end_grapheme == other.end_grapheme
        )

    def __repr__(self) -> str:
        return (
            f"LayoutLine({self.text!r}, width={self.width},"
            f" [{self.start_segment}:{self.start_grapheme}"
            f" → {self.end_segment}:{self.end_grapheme}])"
        )


class CharPosition:
    """Per-character position for hit testing."""

    __slots__ = ("x", "y", "width", "height", "line")

    def __init__(self, x: float, y: float, width: float, height: float, line: int) -> None:
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.line = line

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CharPosition):
            return NotImplemented
        return (
            self.x == other.x
            and self.y == other.y
            and self.width == other.width
            and self.height == other.height
            and self.line == other.line
        )

    def __repr__(self) -> str:
        return (
            f"CharPosition(x={self.x}, y={self.y},"
            f" w={self.width}, h={self.height}, line={self.line})"
        )


@dataclass
class LineBreaksResult:
    """Full layout result from the line-breaks derived node."""

    lines: list[LayoutLine]
    line_count: int


@dataclass
class ReactiveLayoutBundle:
    """Result of the reactive layout graph factory."""

    graph: Graph
    set_text: Any  # (str) -> None
    set_font: Any  # (str) -> None
    set_line_height: Any  # (float) -> None
    set_max_width: Any  # (float) -> None
    segments: NodeImpl[list[PreparedSegment]]
    line_breaks: NodeImpl[LineBreaksResult]
    height: NodeImpl[float]
    char_positions: NodeImpl[list[CharPosition]]


# ---------------------------------------------------------------------------
# Text analysis (ported from Pretext analysis.ts — core subset)
# ---------------------------------------------------------------------------


def _is_cjk(s: str) -> bool:
    """Return *True* if *s* contains any CJK codepoint."""
    for ch in s:
        c = ord(ch)
        if (
            (0x4E00 <= c <= 0x9FFF)  # CJK Unified Ideographs
            or (0x3400 <= c <= 0x4DBF)  # CJK Extension A
            or (0x3000 <= c <= 0x303F)  # CJK Symbols and Punctuation
            or (0x3040 <= c <= 0x309F)  # Hiragana
            or (0x30A0 <= c <= 0x30FF)  # Katakana
            or (0xAC00 <= c <= 0xD7AF)  # Hangul
            or (0xFF00 <= c <= 0xFFEF)  # Fullwidth Forms
        ):
            return True
    return False


# Kinsoku: characters that cannot start a line (CJK punctuation)
_KINSOKU_START: frozenset[str] = frozenset(
    "\uff0c\uff0e\uff01\uff1a\uff1b\uff1f"
    "\u3001\u3002\u30fb"
    "\uff09\u3015\u3009\u300b\u300d\u300f\u3011"
)

# Left-sticky punctuation (merges into preceding segment)
_LEFT_STICKY_PUNCTUATION: frozenset[str] = frozenset('.,!?:;)]}\u201d\u2019\u00bb\u203a\u2026%"')

# Whitespace normalization regex (CSS white-space: normal)
_WS_RE = re.compile(r"[\t\n\r\f ]+")


def _normalize_whitespace(text: str) -> str:
    """Normalize collapsible whitespace (CSS white-space: normal).

    Matches ``graphrefly-ts`` ``normalizeWhitespace``: collapse ``[\\t\\n\\r\\f ]+``
    to a single ASCII space, then remove at most one leading and one trailing
    ASCII space (not full Unicode :meth:`str.strip`).
    """
    s = _WS_RE.sub(" ", text)
    if s.startswith(" "):
        s = s[1:]
    if s.endswith(" "):
        s = s[:-1]
    return s


def _iter_graphemes(text: str) -> list[str]:
    """Iterate grapheme clusters in *text*.

    Uses a simple approach: iterate codepoints and merge combining marks
    into the preceding base character. Sufficient for CJK, Latin, emoji
    base cases covered by the TS test suite.
    """
    clusters: list[str] = []
    current = ""
    for ch in text:
        cat = unicodedata.category(ch)
        if cat.startswith("M") and current:
            # Combining mark — append to current cluster
            current += ch
        else:
            if current:
                clusters.append(current)
            current = ch
    if current:
        clusters.append(current)
    return clusters


def _segment_text(
    normalized: str,
) -> list[tuple[list[str], list[bool], list[SegmentBreakKind]]]:
    """Segment text using word-boundary splitting and classify break kinds.

    Returns raw segmentation pieces before merging.
    Mirrors TS ``segmentText()`` using ``Intl.Segmenter({granularity: "word"})``.
    """
    # Split into word-like and non-word-like pieces using Unicode word boundaries.
    # Python's re \b{} extended boundary is not available, so we use a simpler
    # approach: split on runs of word characters vs non-word characters.
    pieces: list[tuple[list[str], list[bool], list[SegmentBreakKind]]] = []

    # Use regex to split into alternating word/non-word tokens
    tokens = re.findall(r"\w+|[^\w]+", normalized, re.UNICODE)

    for token in tokens:
        is_word_like = bool(re.match(r"\w", token, re.UNICODE))

        texts: list[str] = []
        word_likes: list[bool] = []
        kinds: list[SegmentBreakKind] = []

        current_text = ""
        current_kind: SegmentBreakKind | None = None

        for ch in token:
            if ch == " ":
                kind = SegmentBreakKind.SPACE
            elif ch == "\u200b":
                kind = SegmentBreakKind.ZERO_WIDTH_BREAK
            elif ch == "\u00ad":
                kind = SegmentBreakKind.SOFT_HYPHEN
            elif ch == "\n":
                kind = SegmentBreakKind.HARD_BREAK
            else:
                kind = SegmentBreakKind.TEXT

            if current_kind is not None and kind == current_kind:
                current_text += ch
            else:
                if current_kind is not None:
                    texts.append(current_text)
                    word_likes.append(current_kind == SegmentBreakKind.TEXT and is_word_like)
                    kinds.append(current_kind)
                current_text = ch
                current_kind = kind

        if current_kind is not None:
            texts.append(current_text)
            word_likes.append(current_kind == SegmentBreakKind.TEXT and is_word_like)
            kinds.append(current_kind)

        pieces.append((texts, word_likes, kinds))

    return pieces


def analyze_and_measure(
    text: str,
    font: str,
    adapter: MeasurementAdapter,
    cache: dict[str, dict[str, float]],
    measure_stats: dict[str, int] | None = None,
) -> list[PreparedSegment]:
    """Analyze text, merge segments, measure widths, return prepared segments.

    Port of TS ``analyzeAndMeasure()``.

    Args:
        text: Raw input text.
        font: CSS font string (passed to adapter).
        adapter: Measurement backend.
        cache: Shared ``{font: {segment: width}}`` measurement cache.
        measure_stats: If provided, must be a dict with ``hits`` and ``misses`` keys
            (integers); incremented for each cache hit/miss in ``measure_segment``.

    Returns:
        List of :class:`PreparedSegment` ready for line breaking.
    """
    normalized = _normalize_whitespace(text)
    if not normalized:
        return []

    pieces = _segment_text(normalized)

    # Flatten pieces into a single segment list
    raw_texts: list[str] = []
    raw_kinds: list[SegmentBreakKind] = []
    raw_word_like: list[bool] = []

    for texts, word_likes, kinds in pieces:
        for i in range(len(texts)):
            raw_texts.append(texts[i])
            raw_kinds.append(kinds[i])
            raw_word_like.append(word_likes[i])

    # Merge: left-sticky punctuation and kinsoku-start into preceding text segment
    merged_texts: list[str] = []
    merged_kinds: list[SegmentBreakKind] = []
    merged_word_like: list[bool] = []

    for i in range(len(raw_texts)):
        t = raw_texts[i]
        k = raw_kinds[i]
        wl = raw_word_like[i]

        # Merge left-sticky punctuation into preceding text
        if (
            k == SegmentBreakKind.TEXT
            and not wl
            and merged_texts
            and merged_kinds[-1] == SegmentBreakKind.TEXT
        ):
            is_sticky = len(t) == 1 and (t in _LEFT_STICKY_PUNCTUATION or t in _KINSOKU_START)
            if is_sticky:
                merged_texts[-1] += t
                continue

        # Merge hyphen after word into preceding text ("well-known" stays together)
        if (
            t == "-"
            and merged_texts
            and merged_kinds[-1] == SegmentBreakKind.TEXT
            and merged_word_like[-1]
        ):
            merged_texts[-1] += t
            continue

        merged_texts.append(t)
        merged_kinds.append(k)
        merged_word_like.append(wl)

    # Get or create font-specific cache
    font_cache = cache.get(font)
    if font_cache is None:
        font_cache = {}
        cache[font] = font_cache

    def measure_cached(seg: str) -> float:
        w = font_cache.get(seg)
        if w is None:
            if measure_stats is not None:
                measure_stats["misses"] += 1
            w = adapter.measure_segment(seg, font)["width"]
            font_cache[seg] = w
        elif measure_stats is not None:
            measure_stats["hits"] += 1
        return w

    # Build final prepared segments, splitting CJK into per-grapheme
    segments: list[PreparedSegment] = []

    for i in range(len(merged_texts)):
        t = merged_texts[i]
        k = merged_kinds[i]

        if k != SegmentBreakKind.TEXT:
            # Non-text segments: space, hard-break, soft-hyphen, zero-width-break
            width = measure_cached(" ") * len(t) if k == SegmentBreakKind.SPACE else 0.0
            segments.append(PreparedSegment(t, width, k, None))
            continue

        # CJK text: split into per-grapheme segments for line breaking
        if _is_cjk(t):
            graphemes = _iter_graphemes(t)
            unit_text = ""
            for grapheme in graphemes:
                # Kinsoku: line-start-prohibited chars stick to preceding unit
                if unit_text and grapheme in _KINSOKU_START:
                    unit_text += grapheme
                    continue

                if unit_text:
                    w = measure_cached(unit_text)
                    segments.append(PreparedSegment(unit_text, w, SegmentBreakKind.TEXT, None))
                unit_text = grapheme

            if unit_text:
                w = measure_cached(unit_text)
                segments.append(PreparedSegment(unit_text, w, SegmentBreakKind.TEXT, None))
            continue

        # Non-CJK text: measure whole segment, pre-compute grapheme widths for break-word
        w = measure_cached(t)
        grapheme_widths: list[float] | None = None

        if merged_word_like[i] and len(t) > 1:
            graphemes = _iter_graphemes(t)
            if len(graphemes) > 1:
                grapheme_widths = [measure_cached(g) for g in graphemes]

        segments.append(PreparedSegment(t, w, SegmentBreakKind.TEXT, grapheme_widths))

    return segments


# ---------------------------------------------------------------------------
# Line breaking (greedy, ported from Pretext line-break.ts — core subset)
# ---------------------------------------------------------------------------


def compute_line_breaks(
    segments: list[PreparedSegment],
    max_width: float,
    adapter: MeasurementAdapter,
    font: str,
    cache: dict[str, dict[str, float]],
) -> LineBreaksResult:
    """Greedy line-breaking algorithm.

    Walks segments left to right, accumulating width.  Breaks when a segment
    would overflow *max_width*.  Supports:

    - Trailing space hang (spaces don't trigger breaks)
    - ``overflow-wrap: break-word`` via grapheme widths
    - Soft hyphens (break opportunity, adds visible hyphen width)
    - Hard breaks (forced newline)
    """
    if not segments:
        return LineBreaksResult(lines=[], line_count=0)

    lines: list[LayoutLine] = []
    line_w = 0.0
    has_content = False
    line_start_seg = 0
    line_start_grapheme = 0
    line_end_seg = 0
    line_end_grapheme = 0
    pending_break_seg = -1
    pending_break_width = 0.0

    # Measure hyphen for soft-hyphen support
    font_cache = cache.get(font)
    if font_cache is None:
        font_cache = {}
        cache[font] = font_cache
    hyphen_width = font_cache.get("-")
    if hyphen_width is None:
        hyphen_width = adapter.measure_segment("-", font)["width"]
        font_cache["-"] = hyphen_width

    # --- Nested helpers (closures over mutable state) ---

    def emit_line(
        end_seg: int = -1,
        end_grapheme: int = -1,
        width: float = -1.0,
    ) -> None:
        nonlocal line_w, has_content, pending_break_seg, pending_break_width
        nonlocal line_start_seg, line_start_grapheme

        es = end_seg if end_seg >= 0 else line_end_seg
        eg = end_grapheme if end_grapheme >= 0 else line_end_grapheme
        w = width if width >= 0 else line_w

        # Build line text
        text_parts: list[str] = []
        for si in range(line_start_seg, es):
            seg = segments[si]
            if seg.kind in (SegmentBreakKind.SOFT_HYPHEN, SegmentBreakKind.HARD_BREAK):
                continue
            if si == line_start_seg and line_start_grapheme > 0 and seg.grapheme_widths:
                graphemes = _iter_graphemes(seg.text)
                text_parts.append("".join(graphemes[line_start_grapheme:]))
            else:
                text_parts.append(seg.text)

        # Handle partial end segment
        if eg > 0 and es < len(segments):
            seg = segments[es]
            graphemes = _iter_graphemes(seg.text)
            start_g = line_start_grapheme if line_start_seg == es else 0
            text_parts.append("".join(graphemes[start_g:eg]))

        # Add visible hyphen if line ends at soft-hyphen
        if (
            es > 0
            and segments[es - 1].kind == SegmentBreakKind.SOFT_HYPHEN
            and not (line_start_seg == es and line_start_grapheme > 0)
        ):
            text_parts.append("-")

        line_text = "".join(text_parts)
        lines.append(LayoutLine(line_text, w, line_start_seg, line_start_grapheme, es, eg))
        line_w = 0.0
        has_content = False
        pending_break_seg = -1
        pending_break_width = 0.0

    def can_break_after(kind: SegmentBreakKind) -> bool:
        return kind in (
            SegmentBreakKind.SPACE,
            SegmentBreakKind.ZERO_WIDTH_BREAK,
            SegmentBreakKind.SOFT_HYPHEN,
        )

    def start_line(seg_idx: int, grapheme_idx: int, width: float) -> None:
        nonlocal has_content, line_start_seg, line_start_grapheme
        nonlocal line_end_seg, line_end_grapheme, line_w
        has_content = True
        line_start_seg = seg_idx
        line_start_grapheme = grapheme_idx
        line_end_seg = seg_idx + 1
        line_end_grapheme = 0
        line_w = width

    def start_line_at_grapheme(seg_idx: int, grapheme_idx: int, width: float) -> None:
        nonlocal has_content, line_start_seg, line_start_grapheme
        nonlocal line_end_seg, line_end_grapheme, line_w
        has_content = True
        line_start_seg = seg_idx
        line_start_grapheme = grapheme_idx
        line_end_seg = seg_idx
        line_end_grapheme = grapheme_idx + 1
        line_w = width

    def append_breakable_segment(seg_idx: int, start_g: int, g_widths: list[float]) -> None:
        nonlocal has_content, line_w, line_end_seg, line_end_grapheme
        for g in range(start_g, len(g_widths)):
            gw = g_widths[g]
            if not has_content:
                start_line_at_grapheme(seg_idx, g, gw)
                continue
            if line_w + gw > max_width + 0.005:
                emit_line()
                start_line_at_grapheme(seg_idx, g, gw)
            else:
                line_w += gw
                line_end_seg = seg_idx
                line_end_grapheme = g + 1
        # If we consumed the whole segment, advance end past it
        if has_content and line_end_seg == seg_idx and line_end_grapheme == len(g_widths):
            line_end_seg = seg_idx + 1
            line_end_grapheme = 0

    # --- Main loop ---
    i = 0
    while i < len(segments):
        seg = segments[i]

        # Hard break: emit current line, start fresh
        if seg.kind == SegmentBreakKind.HARD_BREAK:
            if has_content:
                emit_line()
            else:
                lines.append(LayoutLine("", 0.0, i, 0, i, 0))
            line_start_seg = i + 1
            line_start_grapheme = 0
            i += 1
            continue

        w = seg.width

        if not has_content:
            # First content on a new line
            if w > max_width and seg.grapheme_widths:
                append_breakable_segment(i, 0, seg.grapheme_widths)
            else:
                start_line(i, 0, w)
            if can_break_after(seg.kind):
                pending_break_seg = i + 1
                pending_break_width = line_w - w if seg.kind == SegmentBreakKind.SPACE else line_w
            i += 1
            continue

        new_w = line_w + w

        if new_w > max_width + 0.005:
            # Overflow
            if can_break_after(seg.kind):
                # Trailing space: hang past edge, then break
                line_w += w
                line_end_seg = i + 1
                line_end_grapheme = 0
                emit_line(
                    i + 1,
                    0,
                    line_w - w if seg.kind == SegmentBreakKind.SPACE else line_w,
                )
                i += 1
                continue

            if pending_break_seg >= 0:
                # Break at last break opportunity
                emit_line(pending_break_seg, 0, pending_break_width)
                # Don't advance i — re-process current segment on new line
                continue

            if w > max_width and seg.grapheme_widths:
                # Break-word: split at grapheme level
                emit_line()
                append_breakable_segment(i, 0, seg.grapheme_widths)
                i += 1
                continue

            # No break opportunity: force break before this segment
            emit_line()
            continue

        # Fits on current line
        line_w = new_w
        line_end_seg = i + 1
        line_end_grapheme = 0

        if can_break_after(seg.kind):
            pending_break_seg = i + 1
            pending_break_width = line_w - w if seg.kind == SegmentBreakKind.SPACE else line_w

        i += 1

    if has_content:
        emit_line()

    return LineBreaksResult(lines=lines, line_count=len(lines))


# ---------------------------------------------------------------------------
# Character positions
# ---------------------------------------------------------------------------


def compute_char_positions(
    line_breaks: LineBreaksResult,
    segments: list[PreparedSegment],
    line_height: float,
) -> list[CharPosition]:
    """Compute per-character x,y positions from line breaks and segments."""
    positions: list[CharPosition] = []

    for line_idx, line in enumerate(line_breaks.lines):
        y = line_idx * line_height
        x = 0.0

        for si in range(line.start_segment, len(segments)):
            seg = segments[si]
            if seg.kind in (SegmentBreakKind.SOFT_HYPHEN, SegmentBreakKind.HARD_BREAK):
                if si >= line.end_segment and line.end_grapheme == 0:
                    break
                continue

            graphemes = _iter_graphemes(seg.text)
            if not graphemes:
                continue  # empty TEXT segment — skip (QA P4: avoid ZeroDivisionError)
            start_g = line.start_grapheme if si == line.start_segment else 0

            # Determine how many graphemes of this segment belong to this line
            if si < line.end_segment:
                end_g = len(graphemes)
            elif si == line.end_segment and line.end_grapheme > 0:
                end_g = line.end_grapheme
            else:
                break

            for g in range(start_g, end_g):
                g_width = (
                    seg.grapheme_widths[g] if seg.grapheme_widths else seg.width / len(graphemes)
                )
                positions.append(CharPosition(x, y, g_width, line_height, line_idx))
                x += g_width

    return positions


# ---------------------------------------------------------------------------
# Reactive graph factory
# ---------------------------------------------------------------------------


def reactive_layout(
    adapter: MeasurementAdapter,
    *,
    name: str = "reactive-layout",
    text: str = "",
    font: str = "16px sans-serif",
    line_height: float = 20.0,
    max_width: float = 800.0,
) -> ReactiveLayoutBundle:
    """Create a reactive text layout graph.

    .. code-block:: text

        Graph("reactive-layout")
        ├── state("text")
        ├── state("font")
        ├── state("line-height")
        ├── state("max-width")
        ├── derived("segments")        — text + font → PreparedSegment[]
        ├── derived("line-breaks")     — segments + max-width → LineBreaksResult
        ├── derived("height")          — line-breaks → number
        └── derived("char-positions")  — line-breaks + segments → CharPosition[]

    Args:
        adapter: Measurement backend (required).
        name: Graph name (default ``"reactive-layout"``).
        text: Initial text content.
        font: Initial CSS font string.
        line_height: Initial line height in px.
        max_width: Initial max width constraint in px.

    Returns:
        :class:`ReactiveLayoutBundle` with graph, setters, and node references.
    """
    g = Graph(name)

    # Shared measurement cache: {font: {segment: width}}
    measure_cache: dict[str, dict[str, float]] = {}

    def _invalidate_measure_cache(
        msg: Any,
        cache: dict[str, dict[str, float]],
        adapter: MeasurementAdapter,
    ) -> bool:
        """Clear layout measurement cache on INVALIDATE (spec §1.2)."""
        if msg[0] == MessageType.INVALIDATE:
            cache.clear()
            clear_fn = getattr(adapter, "clear_cache", None)
            if callable(clear_fn):
                clear_fn()
        return False

    # --- State nodes ---
    text_node: NodeImpl[str] = state(text, name="text")
    font_node: NodeImpl[str] = state(font, name="font")
    line_height_node: NodeImpl[float] = state(line_height, name="line-height")
    max_width_node: NodeImpl[float] = state(max_width, name="max-width")

    # --- Derived: segments (text + font → PreparedSegment[]) ---
    def _compute_segments(deps: list[Any], _actions: Any) -> list[PreparedSegment]:
        text_val: str = deps[0]
        font_val: str = deps[1]

        t0 = monotonic_ns()
        measure_stats: dict[str, int] = {"hits": 0, "misses": 0}
        result = analyze_and_measure(
            text_val, font_val, adapter, measure_cache, measure_stats
        )
        elapsed = monotonic_ns() - t0

        lookups = measure_stats["hits"] + measure_stats["misses"]
        hit_rate = 1.0 if lookups == 0 else measure_stats["hits"] / lookups

        # Phase-3 deferral: meta companion values arrive after parent's own
        # DATA has propagated through phase-2 (parity with TS emitWithBatchPhase3).
        meta = segments_node.meta
        if meta:
            cr = meta.get("cache-hit-rate")
            if cr is not None:
                emit_with_batch(cr.down, [(MessageType.DATA, hit_rate)], phase=3)
            sc = meta.get("segment-count")
            if sc is not None:
                emit_with_batch(sc.down, [(MessageType.DATA, len(result))], phase=3)
            lt = meta.get("layout-time-ns")
            if lt is not None:
                emit_with_batch(lt.down, [(MessageType.DATA, elapsed)], phase=3)

        return result

    def _segments_equals(a: list[PreparedSegment] | None, b: list[PreparedSegment] | None) -> bool:
        if a is None or b is None:
            return a is b
        if len(a) != len(b):
            return False
        return all(
            sa.text == sb.text and sa.width == sb.width and sa.grapheme_widths == sb.grapheme_widths
            for sa, sb in zip(a, b, strict=True)
        )

    segments_node: NodeImpl[list[PreparedSegment]] = derived(
        [text_node, font_node],
        _compute_segments,
        name="segments",
        meta={"cache-hit-rate": 0, "segment-count": 0, "layout-time-ns": 0},
        equals=_segments_equals,
        on_message=lambda msg, _dep_index, _actions: _invalidate_measure_cache(
            msg, measure_cache, adapter
        ),
    )

    # --- Derived: line-breaks (segments + max-width + font → LineBreaksResult) ---
    def _compute_line_breaks(deps: list[Any], _actions: Any) -> LineBreaksResult:
        segs: list[PreparedSegment] = deps[0]
        mw: float = deps[1]
        f: str = deps[2]
        return compute_line_breaks(segs, mw, adapter, f, measure_cache)

    def _line_breaks_equals(a: LineBreaksResult | None, b: LineBreaksResult | None) -> bool:
        if a is None or b is None:
            return a is b
        if a.line_count != b.line_count:
            return False
        return all(
            la.text == lb.text
            and la.width == lb.width
            and la.start_segment == lb.start_segment
            and la.start_grapheme == lb.start_grapheme
            and la.end_segment == lb.end_segment
            and la.end_grapheme == lb.end_grapheme
            for la, lb in zip(a.lines, b.lines, strict=True)
        )

    line_breaks_node: NodeImpl[LineBreaksResult] = derived(
        [segments_node, max_width_node, font_node],
        _compute_line_breaks,
        name="line-breaks",
        equals=_line_breaks_equals,
    )

    # --- Derived: height ---
    height_node: NodeImpl[float] = derived(
        [line_breaks_node, line_height_node],
        lambda deps, _a: deps[0].line_count * deps[1],
        name="height",
    )

    # --- Derived: char-positions ---
    def _compute_char_positions(deps: list[Any], _actions: Any) -> list[CharPosition]:
        lb: LineBreaksResult = deps[0]
        segs: list[PreparedSegment] = deps[1]
        lh: float = deps[2]
        return compute_char_positions(lb, segs, lh)

    def _char_positions_equals(a: list[CharPosition] | None, b: list[CharPosition] | None) -> bool:
        if a is None or b is None:
            return a is b
        if len(a) != len(b):
            return False
        return all(
            ca.x == cb.x and ca.y == cb.y and ca.width == cb.width
            for ca, cb in zip(a, b, strict=True)
        )

    char_positions_node: NodeImpl[list[CharPosition]] = derived(
        [line_breaks_node, segments_node, line_height_node],
        _compute_char_positions,
        name="char-positions",
        equals=_char_positions_equals,
    )

    # --- Register in graph ---
    g.add("text", text_node)
    g.add("font", font_node)
    g.add("line-height", line_height_node)
    g.add("max-width", max_width_node)
    g.add("segments", segments_node)
    g.add("line-breaks", line_breaks_node)
    g.add("height", height_node)
    g.add("char-positions", char_positions_node)

    # --- Edges (for describe() visibility) ---
    g.connect("text", "segments")
    g.connect("font", "segments")
    g.connect("segments", "line-breaks")
    g.connect("max-width", "line-breaks")
    g.connect("font", "line-breaks")
    g.connect("line-breaks", "height")
    g.connect("line-height", "height")
    g.connect("line-breaks", "char-positions")
    g.connect("segments", "char-positions")
    g.connect("line-height", "char-positions")

    return ReactiveLayoutBundle(
        graph=g,
        set_text=lambda t: g.set("text", t),
        set_font=lambda f: g.set("font", f),
        set_line_height=lambda lh: g.set("line-height", lh),
        set_max_width=lambda mw: g.set("max-width", mw),
        segments=segments_node,
        line_breaks=line_breaks_node,
        height=height_node,
        char_positions=char_positions_node,
    )
