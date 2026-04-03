"""Tests for reactive text layout engine (roadmap §7.1 — Pretext parity).

Uses a MockMeasureAdapter with deterministic widths (8px per character)
so tests are environment-independent (no Canvas/DOM).
"""

from __future__ import annotations

from graphrefly.core.protocol import MessageType, batch
from graphrefly.patterns.reactive_layout.reactive_layout import (
    CharPosition,
    LayoutLine,
    LineBreaksResult,
    MeasurementAdapter,
    PreparedSegment,
    SegmentBreakKind,
    _normalize_whitespace,
    analyze_and_measure,
    compute_char_positions,
    compute_line_breaks,
    reactive_layout,
)

# ---------------------------------------------------------------------------
# Mock adapter: 8px per character (deterministic, no Canvas)
# ---------------------------------------------------------------------------

CHAR_WIDTH = 8


class MockMeasureAdapter:
    """Deterministic measurement: 8px per character."""

    def measure_segment(self, text: str, font: str) -> dict[str, float]:
        return {"width": len(text) * CHAR_WIDTH}


def mock_adapter() -> MockMeasureAdapter:
    return MockMeasureAdapter()


# ---------------------------------------------------------------------------
# Text analysis
# ---------------------------------------------------------------------------


class TestAnalyzeAndMeasure:
    def test_segments_simple_text_into_words_and_spaces(self) -> None:
        segs = analyze_and_measure("hello world", "16px mono", mock_adapter(), {})
        texts = [s.text for s in segs]
        assert "hello" in texts
        assert "world" in texts
        space_segs = [s for s in segs if s.kind == SegmentBreakKind.SPACE]
        assert len(space_segs) > 0

    def test_returns_empty_for_empty_text(self) -> None:
        assert analyze_and_measure("", "16px mono", mock_adapter(), {}) == []

    def test_returns_empty_for_whitespace_only(self) -> None:
        assert analyze_and_measure("   ", "16px mono", mock_adapter(), {}) == []

    def test_normalizes_collapsible_whitespace(self) -> None:
        segs = analyze_and_measure("hello   world", "16px mono", mock_adapter(), {})
        space_segs = [s for s in segs if s.kind == SegmentBreakKind.SPACE]
        for s in space_segs:
            assert s.text == " "

    def test_merges_left_sticky_punctuation(self) -> None:
        segs = analyze_and_measure("hello, world", "16px mono", mock_adapter(), {})
        hello_seg = next((s for s in segs if s.text.startswith("hello")), None)
        assert hello_seg is not None
        assert hello_seg.text == "hello,"

    def test_measures_segment_widths(self) -> None:
        segs = analyze_and_measure("abc", "16px mono", mock_adapter(), {})
        text_seg = next((s for s in segs if s.kind == SegmentBreakKind.TEXT), None)
        assert text_seg is not None
        assert text_seg.width == 3 * CHAR_WIDTH

    def test_caches_measurements_per_font(self) -> None:
        cache: dict[str, dict[str, float]] = {}
        analyze_and_measure("abc", "16px mono", mock_adapter(), cache)
        assert "16px mono" in cache
        analyze_and_measure("abc def", "16px mono", mock_adapter(), cache)
        assert "abc" in cache["16px mono"]

    def test_splits_cjk_into_per_grapheme_segments(self) -> None:
        segs = analyze_and_measure("你好世界", "16px mono", mock_adapter(), {})
        assert len(segs) >= 2
        for seg in segs:
            if seg.kind == SegmentBreakKind.TEXT:
                assert len(seg.text) <= 2

    def test_precomputes_grapheme_widths_for_long_words(self) -> None:
        segs = analyze_and_measure("superlongword", "16px mono", mock_adapter(), {})
        word_seg = next(
            (s for s in segs if s.kind == SegmentBreakKind.TEXT and s.text == "superlongword"),
            None,
        )
        assert word_seg is not None
        assert word_seg.grapheme_widths is not None
        assert len(word_seg.grapheme_widths) == 13

    def test_handles_soft_hyphens(self) -> None:
        segs = analyze_and_measure("auto\u00admatic", "16px mono", mock_adapter(), {})
        kinds = [s.kind for s in segs]
        assert SegmentBreakKind.SOFT_HYPHEN in kinds

    def test_measure_stats_hits_and_misses(self) -> None:
        cache: dict[str, dict[str, float]] = {}
        stats: dict[str, int] = {"hits": 0, "misses": 0}
        analyze_and_measure("aa", "16px mono", mock_adapter(), cache, stats)
        assert stats["misses"] > 0
        m0, h0 = stats["misses"], stats["hits"]
        analyze_and_measure("aa", "16px mono", mock_adapter(), cache, stats)
        assert stats["hits"] > h0
        assert stats["misses"] == m0


class TestNormalizeWhitespace:
    def test_collapses_and_strips_ascii_spaces_like_ts(self) -> None:
        assert _normalize_whitespace("  hello  ") == "hello"

    def test_does_not_strip_nbsp(self) -> None:
        # Match TS: only single ASCII space trimmed from each end
        s = "\u00a0x\u00a0"
        assert _normalize_whitespace(s) == s


class AdapterNoClearCache:
    def measure_segment(self, text: str, font: str) -> dict[str, float]:
        return {"width": len(text) * CHAR_WIDTH}

    def clear_cache(self) -> None:
        # Optional hook: no-op for this test adapter.
        return


def test_measurement_adapter_with_noop_clear_cache() -> None:
    a = AdapterNoClearCache()
    assert isinstance(a, MeasurementAdapter)
    segs = analyze_and_measure("hi", "16px mono", a, {})
    assert len(segs) >= 1


# ---------------------------------------------------------------------------
# Line breaking
# ---------------------------------------------------------------------------


class TestComputeLineBreaks:
    def _break_lines(self, text: str, max_width: float) -> LineBreaksResult:
        cache: dict[str, dict[str, float]] = {}
        adapter = mock_adapter()
        font = "16px mono"
        segs = analyze_and_measure(text, font, adapter, cache)
        return compute_line_breaks(segs, max_width, adapter, font, cache)

    def test_single_line_when_text_fits(self) -> None:
        result = self._break_lines("hello", 100)
        assert result.line_count == 1
        assert result.lines[0].text == "hello"
        assert result.lines[0].width == 5 * CHAR_WIDTH

    def test_wraps_at_word_boundary(self) -> None:
        # "hello world" = 5*8 + 8 + 5*8 = 88px; maxWidth=60 forces wrap
        result = self._break_lines("hello world", 60)
        assert result.line_count == 2
        assert result.lines[0].text == "hello "
        assert result.lines[1].text == "world"

    def test_multiple_words_wrapping(self) -> None:
        # "aa bb cc" → each word=16px, space=8px; maxWidth=30
        result = self._break_lines("aa bb cc", 30)
        assert result.line_count == 3

    def test_empty_text_produces_no_lines(self) -> None:
        result = self._break_lines("", 100)
        assert result.line_count == 0
        assert result.lines == []

    def test_break_word_at_grapheme_boundaries(self) -> None:
        # "abcdefghij" = 80px; maxWidth=40 → 2 lines
        result = self._break_lines("abcdefghij", 40)
        assert result.line_count == 2
        assert len(result.lines[0].text) == 5
        assert len(result.lines[1].text) == 5

    def test_trailing_space_hangs_past_line_edge(self) -> None:
        # "hello world" maxWidth=40: "hello" fits (40px), space hangs
        result = self._break_lines("hello world", 40)
        assert result.line_count == 2
        assert result.lines[0].text == "hello "
        assert result.lines[1].text == "world"

    def test_hard_breaks(self) -> None:
        # Use raw compute_line_breaks with manual segments
        segs = [
            PreparedSegment("line1", 40, SegmentBreakKind.TEXT, None),
            PreparedSegment("\n", 0, SegmentBreakKind.HARD_BREAK, None),
            PreparedSegment("line2", 40, SegmentBreakKind.TEXT, None),
        ]
        cache: dict[str, dict[str, float]] = {}
        result = compute_line_breaks(segs, 200, mock_adapter(), "16px mono", cache)
        assert result.line_count == 2
        assert result.lines[0].text == "line1"
        assert result.lines[1].text == "line2"

    def test_soft_hyphen_creates_break_with_visible_hyphen(self) -> None:
        segs = [
            PreparedSegment("auto", 32, SegmentBreakKind.TEXT, None),
            PreparedSegment("\u00ad", 0, SegmentBreakKind.SOFT_HYPHEN, None),
            PreparedSegment("matic", 40, SegmentBreakKind.TEXT, None),
        ]
        cache: dict[str, dict[str, float]] = {}
        result = compute_line_breaks(segs, 40, mock_adapter(), "16px mono", cache)
        assert result.line_count == 2
        assert result.lines[0].text == "auto-"
        assert result.lines[1].text == "matic"


# ---------------------------------------------------------------------------
# Character positions
# ---------------------------------------------------------------------------


class TestComputeCharPositions:
    def test_single_line_char_positions(self) -> None:
        segs = [
            PreparedSegment("abc", 24, SegmentBreakKind.TEXT, [8.0, 8.0, 8.0]),
        ]
        lb = LineBreaksResult(
            line_count=1,
            lines=[LayoutLine_("abc", 24, 0, 0, 1, 0)],
        )
        positions = compute_char_positions(lb, segs, 20)
        assert len(positions) == 3
        assert positions[0] == CharPosition(0, 0, 8, 20, 0)
        assert positions[1] == CharPosition(8, 0, 8, 20, 0)
        assert positions[2] == CharPosition(16, 0, 8, 20, 0)

    def test_multi_line_y_offset(self) -> None:
        segs = [
            PreparedSegment("ab", 16, SegmentBreakKind.TEXT, [8.0, 8.0]),
            PreparedSegment(" ", 8, SegmentBreakKind.SPACE, None),
            PreparedSegment("cd", 16, SegmentBreakKind.TEXT, [8.0, 8.0]),
        ]
        lb = LineBreaksResult(
            line_count=2,
            lines=[
                LayoutLine_("ab", 16, 0, 0, 1, 0),
                LayoutLine_("cd", 16, 2, 0, 3, 0),
            ],
        )
        positions = compute_char_positions(lb, segs, 20)
        line2_pos = [p for p in positions if p.line == 1]
        assert len(line2_pos) == 2
        assert line2_pos[0].y == 20


def LayoutLine_(
    text: str,
    width: float,
    start_seg: int,
    start_g: int,
    end_seg: int,
    end_g: int,
) -> LayoutLine:
    """Helper to construct LayoutLine."""
    return LayoutLine(text, width, start_seg, start_g, end_seg, end_g)


# ---------------------------------------------------------------------------
# Reactive graph factory
# ---------------------------------------------------------------------------


class TestReactiveLayout:
    def test_creates_graph_with_all_expected_nodes(self) -> None:
        layout = reactive_layout(mock_adapter(), text="hello world", max_width=200)
        try:
            desc = layout.graph.describe()
            assert "text" in desc["nodes"]
            assert "font" in desc["nodes"]
            assert "line-height" in desc["nodes"]
            assert "max-width" in desc["nodes"]
            assert "segments" in desc["nodes"]
            assert "line-breaks" in desc["nodes"]
            assert "height" in desc["nodes"]
            assert "char-positions" in desc["nodes"]
        finally:
            layout.graph.destroy()

    def test_computes_correct_height_single_line(self) -> None:
        layout = reactive_layout(
            mock_adapter(),
            text="hello",
            font="16px mono",
            line_height=20,
            max_width=200,
        )
        try:
            unsub = layout.height.subscribe(lambda _msgs: None)
            assert layout.height.get() == 20  # 1 line × 20px
            unsub()
        finally:
            layout.graph.destroy()

    def test_recomputes_on_text_change(self) -> None:
        layout = reactive_layout(
            mock_adapter(),
            text="hello",
            font="16px mono",
            line_height=20,
            max_width=48,  # 6 chars fit
        )
        try:
            unsub = layout.height.subscribe(lambda _msgs: None)
            assert layout.height.get() == 20  # 1 line

            layout.set_text("hello world")
            assert layout.height.get() == 40  # 2 lines
            unsub()
        finally:
            layout.graph.destroy()

    def test_recomputes_on_max_width_change(self) -> None:
        layout = reactive_layout(
            mock_adapter(),
            text="hello world",
            font="16px mono",
            line_height=20,
            max_width=200,
        )
        try:
            unsub = layout.height.subscribe(lambda _msgs: None)
            assert layout.height.get() == 20  # 1 line

            layout.set_max_width(40)
            assert layout.height.get() == 40  # 2 lines
            unsub()
        finally:
            layout.graph.destroy()

    def test_negative_max_width_clamped(self) -> None:
        layout = reactive_layout(mock_adapter(), text="hi", max_width=-50)
        try:
            assert layout.graph.get("max-width") == 0
            layout.set_max_width(-20)
            assert layout.graph.get("max-width") == 0
        finally:
            layout.graph.destroy()

    def test_invalidate_clears_measurement_cache(self) -> None:
        state = {"multiplier": 1, "clear_calls": 0}

        class AdapterInvalidateAware:
            def measure_segment(self, text: str, font: str) -> dict[str, float]:
                return {"width": len(text) * CHAR_WIDTH * state["multiplier"]}

            def clear_cache(self) -> None:
                state["clear_calls"] += 1

        layout = reactive_layout(
            AdapterInvalidateAware(),
            text="hello world",
            font="16px mono",
            line_height=20,
            max_width=90,  # 88px fits initially, wraps after multiplier change
        )
        try:
            seen: list[object] = []
            unsub = layout.height.subscribe(lambda msgs: seen.extend(msgs))
            assert layout.height.get() == 20

            state["multiplier"] = 2
            layout.graph.signal([(MessageType.INVALIDATE,)])

            # INVALIDATE clears cached state of all nodes in the graph, including
            # source nodes like `line-height` and `max-width`. Restore required
            # inputs so recomputation yields deterministic, non-None values.
            with batch():
                layout.set_text("hello world")
                layout.set_font("16px mono")
                layout.set_line_height(20)
                layout.set_max_width(90)
            assert any(m[0] is MessageType.DATA and m[1] == 40 for m in seen), seen
            assert layout.height.get() == 40
            assert state["clear_calls"] >= 1

            unsub()
        finally:
            layout.graph.destroy()

    def test_segments_meta_observability(self) -> None:
        layout = reactive_layout(mock_adapter(), text="hello world", max_width=200)
        try:
            desc = layout.graph.describe()
            seg_desc = desc["nodes"].get("segments")
            assert seg_desc is not None
            assert "meta" in seg_desc
            assert "cache-hit-rate" in seg_desc["meta"]
            assert "segment-count" in seg_desc["meta"]
            assert "layout-time-ns" in seg_desc["meta"]
        finally:
            layout.graph.destroy()

    def test_resolved_optimization_unchanged_text(self) -> None:
        layout = reactive_layout(
            mock_adapter(),
            text="hello",
            font="16px mono",
            line_height=20,
            max_width=200,
        )
        try:
            unsub = layout.segments.subscribe(lambda _msgs: None)
            segs1 = layout.segments.get()
            layout.set_text("hello")  # same text → RESOLVED
            segs2 = layout.segments.get()
            assert segs1 == segs2
            unsub()
        finally:
            layout.graph.destroy()

    def test_char_positions_computed_correctly(self) -> None:
        layout = reactive_layout(
            mock_adapter(),
            text="ab",
            font="16px mono",
            line_height=20,
            max_width=200,
        )
        try:
            unsub = layout.char_positions.subscribe(lambda _msgs: None)
            positions = layout.char_positions.get()
            assert len(positions) == 2
            assert positions[0].x == 0
            assert positions[0].y == 0
            assert positions[0].width == CHAR_WIDTH
            assert positions[1].x == CHAR_WIDTH
            unsub()
        finally:
            layout.graph.destroy()

    def test_graph_is_snapshotable(self) -> None:
        layout = reactive_layout(mock_adapter(), text="test", max_width=200)
        try:
            snapshot = layout.graph.snapshot()
            assert snapshot is not None
            assert snapshot["name"] == "reactive-layout"
        finally:
            layout.graph.destroy()

    def test_graph_is_describable_with_edges(self) -> None:
        layout = reactive_layout(mock_adapter(), text="test", max_width=200)
        try:
            desc = layout.graph.describe()
            assert len(desc["edges"]) > 0
        finally:
            layout.graph.destroy()
