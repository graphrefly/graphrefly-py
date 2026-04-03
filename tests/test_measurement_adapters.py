"""Tests for MeasurementAdapter implementations (roadmap §7.1)."""

import pytest

from graphrefly.patterns.reactive_layout.measurement_adapters import (
    CliMeasureAdapter,
    PrecomputedAdapter,
)
from graphrefly.patterns.reactive_layout.reactive_layout import (
    MeasurementAdapter,
    analyze_and_measure,
    reactive_layout,
)

# ---------------------------------------------------------------------------
# CliMeasureAdapter
# ---------------------------------------------------------------------------


class TestCliMeasureAdapter:
    def test_ascii_text(self) -> None:
        adapter = CliMeasureAdapter()
        assert adapter.measure_segment("hello", "mono")["width"] == 40  # 5 * 8

    def test_cjk_characters(self) -> None:
        adapter = CliMeasureAdapter()
        # 3 CJK chars → 6 cells → 48px
        assert adapter.measure_segment("你好世", "mono")["width"] == 48

    def test_mixed_ascii_cjk(self) -> None:
        adapter = CliMeasureAdapter()
        # "hi你" → 2 ASCII (2 cells) + 1 CJK (2 cells) = 4 cells → 32px
        assert adapter.measure_segment("hi你", "mono")["width"] == 32

    def test_custom_cell_px(self) -> None:
        adapter = CliMeasureAdapter(cell_px=10)
        assert adapter.measure_segment("ab", "mono")["width"] == 20

    def test_empty_string(self) -> None:
        adapter = CliMeasureAdapter()
        assert adapter.measure_segment("", "mono")["width"] == 0

    def test_fullwidth_forms(self) -> None:
        adapter = CliMeasureAdapter()
        # Ａ (U+FF21, fullwidth A) → 2 cells → 16px
        assert adapter.measure_segment("\uff21", "mono")["width"] == 16

    def test_hangul(self) -> None:
        adapter = CliMeasureAdapter()
        # 한 (U+D55C) → 2 cells → 16px
        assert adapter.measure_segment("한", "mono")["width"] == 16

    def test_combining_marks_zero_width(self) -> None:
        adapter = CliMeasureAdapter()
        # "e\u0301" = e + combining acute accent → 1 cell (not 2)
        assert adapter.measure_segment("e\u0301", "mono")["width"] == 8

    def test_ignores_font_parameter(self) -> None:
        adapter = CliMeasureAdapter()
        w1 = adapter.measure_segment("test", "12px serif")
        w2 = adapter.measure_segment("test", "48px monospace")
        assert w1["width"] == w2["width"]

    def test_satisfies_protocol(self) -> None:
        adapter = CliMeasureAdapter()
        assert isinstance(adapter, MeasurementAdapter)

    def test_integrates_with_analyze_and_measure(self) -> None:
        adapter = CliMeasureAdapter(cell_px=8)
        segs = analyze_and_measure("hello world", "mono", adapter, {})
        assert len(segs) > 0
        text_segs = [s for s in segs if s.kind == "text"]
        assert len(text_segs) == 2  # "hello", "world"

    def test_integrates_with_reactive_layout(self) -> None:
        adapter = CliMeasureAdapter(cell_px=8)
        bundle = reactive_layout(adapter, text="hello world", max_width=60)
        # Subscribe to trigger computation
        unsub = bundle.line_breaks.subscribe(lambda msgs: None)
        # "hello" = 40px, " " = 8px, "world" = 40px → total 88px > 60px
        lb = bundle.line_breaks.get()
        assert lb is not None
        assert lb.line_count == 2
        unsub()
        bundle.graph.destroy()


# ---------------------------------------------------------------------------
# PrecomputedAdapter
# ---------------------------------------------------------------------------


class TestPrecomputedAdapter:
    METRICS = {
        "16px mono": {
            "hello": 40,
            "world": 40,
            " ": 8,
            "h": 8,
            "e": 8,
            "l": 8,
            "o": 8,
            "w": 8,
            "r": 8,
            "d": 8,
        },
    }

    def test_exact_segment_lookup(self) -> None:
        adapter = PrecomputedAdapter(self.METRICS)
        assert adapter.measure_segment("hello", "16px mono")["width"] == 40

    def test_per_char_fallback(self) -> None:
        adapter = PrecomputedAdapter(self.METRICS)
        # "held" → h(8) + e(8) + l(8) + d(8) = 32
        assert adapter.measure_segment("held", "16px mono")["width"] == 32

    def test_unknown_font_returns_zero(self) -> None:
        adapter = PrecomputedAdapter(self.METRICS)
        assert adapter.measure_segment("hello", "unknown")["width"] == 0

    def test_error_mode_raises(self) -> None:
        adapter = PrecomputedAdapter(self.METRICS, fallback="error")
        with pytest.raises(KeyError, match="no metrics"):
            adapter.measure_segment("xyz", "16px mono")

    def test_error_mode_exact_match_works(self) -> None:
        adapter = PrecomputedAdapter(self.METRICS, fallback="error")
        assert adapter.measure_segment("hello", "16px mono")["width"] == 40

    def test_invalid_fallback_raises(self) -> None:
        with pytest.raises(ValueError, match="fallback"):
            PrecomputedAdapter(self.METRICS, fallback="bad")

    def test_satisfies_protocol(self) -> None:
        adapter = PrecomputedAdapter(self.METRICS)
        assert isinstance(adapter, MeasurementAdapter)

    def test_integrates_with_analyze_and_measure(self) -> None:
        adapter = PrecomputedAdapter(self.METRICS)
        segs = analyze_and_measure("hello world", "16px mono", adapter, {})
        assert len(segs) > 0
        text_segs = [s for s in segs if s.kind == "text"]
        assert len(text_segs) == 2


# ---------------------------------------------------------------------------
# PillowMeasureAdapter (only if Pillow is installed)
# ---------------------------------------------------------------------------


class TestPillowMeasureAdapter:
    def test_import_and_construct(self) -> None:
        """PillowMeasureAdapter can be imported and constructed."""
        pytest.importorskip("PIL")
        from graphrefly.patterns.reactive_layout.measurement_adapters import PillowMeasureAdapter

        adapter = PillowMeasureAdapter()
        assert isinstance(adapter, MeasurementAdapter)

    def test_default_font_measurement(self) -> None:
        """Measures with Pillow default font (no TTF required)."""
        pytest.importorskip("PIL")
        from graphrefly.patterns.reactive_layout.measurement_adapters import PillowMeasureAdapter

        adapter = PillowMeasureAdapter()
        result = adapter.measure_segment("hello", "default")
        assert result["width"] > 0

    def test_clear_cache(self) -> None:
        pytest.importorskip("PIL")
        from graphrefly.patterns.reactive_layout.measurement_adapters import PillowMeasureAdapter

        adapter = PillowMeasureAdapter()
        adapter.measure_segment("a", "default")
        adapter.clear_cache()
        # Should work fine after cache clear
        result = adapter.measure_segment("b", "default")
        assert result["width"] > 0
