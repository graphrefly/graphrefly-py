"""Tests for reactive multi-content block layout engine (roadmap §7.1 — mixed content).

Uses mock adapters with deterministic dimensions (8px per char for text,
fixed sizes for images/SVGs) so tests are environment-independent.
"""

from __future__ import annotations

import pytest

from graphrefly.core.protocol import MessageType
from graphrefly.patterns.reactive_layout.measurement_adapters import (
    ImageSizeAdapter,
    SvgBoundsAdapter,
)
from graphrefly.patterns.reactive_layout.reactive_block_layout import (
    BlockAdapters,
    ImageBlock,
    MeasuredBlock,
    PositionedBlock,
    SvgBlock,
    TextBlock,
    compute_block_flow,
    compute_total_height,
    measure_block,
    measure_blocks,
    reactive_block_layout,
)

# ---------------------------------------------------------------------------
# Mock adapters
# ---------------------------------------------------------------------------

CHAR_WIDTH = 8


class MockTextAdapter:
    def measure_segment(self, text: str, font: str) -> dict[str, float]:
        return {"width": len(text) * CHAR_WIDTH}

    def clear_cache(self) -> None:
        pass


def mock_adapters(
    *,
    svg: dict[str, dict[str, float]] | None = None,
    image: dict[str, dict[str, float]] | None = None,
) -> BlockAdapters:
    return BlockAdapters(
        text=MockTextAdapter(),
        svg=_MockSvgAdapter(svg) if svg else None,
        image=ImageSizeAdapter(image) if image else None,
    )


class _MockSvgAdapter:
    def __init__(self, data: dict[str, dict[str, float]]) -> None:
        self._data = data

    def measure_svg(self, content: str) -> dict[str, float]:
        return self._data.get(content, {"width": 100, "height": 100})


# ---------------------------------------------------------------------------
# SvgBoundsAdapter
# ---------------------------------------------------------------------------


class TestSvgBoundsAdapter:
    adapter = SvgBoundsAdapter()

    def test_viewbox(self) -> None:
        svg = '<svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg"></svg>'
        assert self.adapter.measure_svg(svg) == {"width": 200, "height": 100}

    def test_viewbox_commas(self) -> None:
        svg = '<svg viewBox="0,0,300,150"></svg>'
        assert self.adapter.measure_svg(svg) == {"width": 300, "height": 150}

    def test_width_height_attrs(self) -> None:
        svg = '<svg width="120" height="60"></svg>'
        assert self.adapter.measure_svg(svg) == {"width": 120, "height": 60}

    def test_prefers_viewbox(self) -> None:
        svg = '<svg viewBox="0 0 200 100" width="400" height="200"></svg>'
        assert self.adapter.measure_svg(svg) == {"width": 200, "height": 100}

    def test_no_dimensions_raises(self) -> None:
        with pytest.raises(ValueError, match="cannot determine dimensions"):
            self.adapter.measure_svg("<svg><circle r='10'/></svg>")

    def test_viewbox_not_positive_raises(self) -> None:
        with pytest.raises(ValueError, match="viewBox width/height"):
            self.adapter.measure_svg('<svg viewBox="0 0 0 100"></svg>')

    def test_width_height_attrs_not_positive_raises(self) -> None:
        with pytest.raises(ValueError, match="width/height attributes"):
            self.adapter.measure_svg('<svg width="0" height="60"></svg>')


# ---------------------------------------------------------------------------
# ImageSizeAdapter
# ---------------------------------------------------------------------------


class TestImageSizeAdapter:
    adapter = ImageSizeAdapter(
        {
            "hero.png": {"width": 1200, "height": 630},
            "logo.svg": {"width": 120, "height": 40},
        }
    )

    def test_registered(self) -> None:
        assert self.adapter.measure_image("hero.png") == {"width": 1200, "height": 630}

    def test_unregistered_raises(self) -> None:
        with pytest.raises(KeyError, match="no dimensions registered"):
            self.adapter.measure_image("unknown.png")


# ---------------------------------------------------------------------------
# measure_block
# ---------------------------------------------------------------------------


class TestMeasureBlock:
    adapters = mock_adapters()
    cache: dict[str, dict[str, float]] = {}
    font = "16px mono"
    lh = 20.0

    def test_text_block(self) -> None:
        block = TextBlock(text="hello world")
        m = measure_block(block, 800, self.adapters, {}, self.font, self.lh, 0)
        assert m.type == "text"
        assert m.index == 0
        assert m.height > 0
        assert m.text_segments is not None
        assert m.text_line_breaks is not None
        assert m.text_char_positions is not None

    def test_text_block_wraps(self) -> None:
        block = TextBlock(text="hello world")
        m = measure_block(block, 50, self.adapters, {}, self.font, self.lh, 0)
        assert m.text_line_breaks is not None
        assert m.text_line_breaks.line_count > 1
        assert m.height == m.text_line_breaks.line_count * self.lh

    def test_image_explicit_dims(self) -> None:
        block = ImageBlock(src="pic.png", natural_width=400, natural_height=300)
        m = measure_block(block, 800, self.adapters, {}, self.font, self.lh, 1)
        assert m.type == "image"
        assert m.index == 1
        assert m.width == 400
        assert m.height == 300

    def test_image_scales(self) -> None:
        block = ImageBlock(src="wide.png", natural_width=1600, natural_height=900)
        m = measure_block(block, 800, self.adapters, {}, self.font, self.lh, 0)
        assert m.width == 800
        assert m.height == 450  # 900 * (800/1600)

    def test_image_via_adapter(self) -> None:
        a = mock_adapters(image={"hero.png": {"width": 1200, "height": 630}})
        block = ImageBlock(src="hero.png")
        m = measure_block(block, 800, a, {}, self.font, self.lh, 0)
        assert m.width == 800
        assert m.height == 420  # 630 * (800/1200)

    def test_image_no_dims_raises(self) -> None:
        block = ImageBlock(src="no-dims.png")
        with pytest.raises(ValueError, match="no natural_width"):
            measure_block(block, 800, self.adapters, {}, self.font, self.lh, 0)

    def test_svg_with_viewbox(self) -> None:
        block = SvgBlock(content="<svg></svg>", view_box=(200, 100))
        m = measure_block(block, 800, self.adapters, {}, self.font, self.lh, 2)
        assert m.type == "svg"
        assert m.width == 200
        assert m.height == 100

    def test_svg_scales(self) -> None:
        block = SvgBlock(content="<svg></svg>", view_box=(1000, 500))
        m = measure_block(block, 800, self.adapters, {}, self.font, self.lh, 0)
        assert m.width == 800
        assert m.height == 400  # 500 * (800/1000)

    def test_svg_no_viewbox_raises(self) -> None:
        block = SvgBlock(content="<svg><rect/></svg>")
        with pytest.raises(ValueError, match="no view_box"):
            measure_block(block, 800, self.adapters, {}, self.font, self.lh, 0)


# ---------------------------------------------------------------------------
# measure_blocks
# ---------------------------------------------------------------------------


class TestMeasureBlocks:
    def test_mixed_content(self) -> None:
        adapters = mock_adapters()
        blocks = [
            TextBlock(text="hello"),
            ImageBlock(src="pic.png", natural_width=200, natural_height=100),
            TextBlock(text="world"),
        ]
        result = measure_blocks(blocks, 800, adapters, {}, "16px mono", 20)
        assert len(result) == 3
        assert result[0].type == "text"
        assert result[1].type == "image"
        assert result[2].type == "text"
        assert result[0].index == 0
        assert result[1].index == 1
        assert result[2].index == 2

    def test_empty(self) -> None:
        assert measure_blocks([], 800, mock_adapters(), {}, "16px mono", 20) == []


# ---------------------------------------------------------------------------
# compute_block_flow
# ---------------------------------------------------------------------------


class TestComputeBlockFlow:
    blocks = [
        MeasuredBlock(index=0, type="text", width=200, height=40),
        MeasuredBlock(index=1, type="image", width=300, height=150),
        MeasuredBlock(index=2, type="text", width=200, height=60),
    ]

    def test_vertical_stack_no_gap(self) -> None:
        flow = compute_block_flow(self.blocks, 0)
        assert len(flow) == 3
        assert flow[0].x == 0 and flow[0].y == 0 and flow[0].height == 40
        assert flow[1].x == 0 and flow[1].y == 40 and flow[1].height == 150
        assert flow[2].x == 0 and flow[2].y == 190 and flow[2].height == 60

    def test_vertical_stack_with_gap(self) -> None:
        flow = compute_block_flow(self.blocks, 10)
        assert flow[0].y == 0
        assert flow[1].y == 50  # 40 + 10
        assert flow[2].y == 210  # 50 + 150 + 10

    def test_empty(self) -> None:
        assert compute_block_flow([], 0) == []


# ---------------------------------------------------------------------------
# compute_total_height
# ---------------------------------------------------------------------------


class TestComputeTotalHeight:
    def test_empty(self) -> None:
        assert compute_total_height([]) == 0

    def test_last_block(self) -> None:
        flow = [
            PositionedBlock(index=0, type="text", width=200, height=40, x=0, y=0),
            PositionedBlock(index=1, type="image", width=300, height=150, x=0, y=50),
        ]
        assert compute_total_height(flow) == 200  # 50 + 150


# ---------------------------------------------------------------------------
# reactive_block_layout — factory
# ---------------------------------------------------------------------------


class TestReactiveBlockLayout:
    def test_graph_nodes(self) -> None:
        bundle = reactive_block_layout(mock_adapters())
        desc = bundle.graph.describe()
        for n in ("blocks", "max-width", "gap", "measured-blocks", "block-flow", "total-height"):
            assert n in desc["nodes"]

    def test_edges(self) -> None:
        bundle = reactive_block_layout(mock_adapters())
        desc = bundle.graph.describe()
        edge_strs = {f"{e['from']}->{e['to']}" for e in desc["edges"]}
        assert "blocks->measured-blocks" in edge_strs
        assert "max-width->measured-blocks" in edge_strs
        assert "measured-blocks->block-flow" in edge_strs
        assert "gap->block-flow" in edge_strs
        assert "block-flow->total-height" in edge_strs

    def test_text_block_height(self) -> None:
        bundle = reactive_block_layout(
            mock_adapters(),
            blocks=[TextBlock(text="hello world")],
            max_width=800,
        )
        unsub = bundle.total_height.subscribe(lambda _msgs: None)
        assert bundle.total_height.get() > 0
        unsub()

    def test_mixed_blocks(self) -> None:
        bundle = reactive_block_layout(
            mock_adapters(),
            blocks=[
                TextBlock(text="hello"),
                ImageBlock(src="pic.png", natural_width=200, natural_height=100),
            ],
            max_width=800,
        )
        unsub = bundle.total_height.subscribe(lambda _msgs: None)
        flow = bundle.block_flow.get()
        assert len(flow) == 2
        assert flow[0].type == "text"
        assert flow[1].type == "image"
        assert flow[1].width == 200
        assert flow[1].height == 100
        total = bundle.total_height.get()
        assert total == flow[0].height + flow[1].height
        unsub()

    def test_recomputes_on_set_blocks(self) -> None:
        bundle = reactive_block_layout(
            mock_adapters(),
            blocks=[TextBlock(text="hello")],
            max_width=800,
        )
        unsub = bundle.total_height.subscribe(lambda _msgs: None)
        h1 = bundle.total_height.get()
        bundle.set_blocks(
            [
                TextBlock(text="hello"),
                ImageBlock(src="x.png", natural_width=100, natural_height=50),
            ]
        )
        h2 = bundle.total_height.get()
        assert h2 > h1
        unsub()

    def test_recomputes_on_set_max_width(self) -> None:
        bundle = reactive_block_layout(
            mock_adapters(),
            blocks=[ImageBlock(src="wide.png", natural_width=1600, natural_height=900)],
            max_width=800,
        )
        unsub = bundle.total_height.subscribe(lambda _msgs: None)
        m1 = bundle.measured_blocks.get()
        assert m1[0].width == 800
        assert m1[0].height == 450

        bundle.set_max_width(400)
        m2 = bundle.measured_blocks.get()
        assert m2[0].width == 400
        assert m2[0].height == 225
        unsub()

    def test_gap(self) -> None:
        bundle = reactive_block_layout(
            mock_adapters(),
            blocks=[
                ImageBlock(src="a.png", natural_width=100, natural_height=50),
                ImageBlock(src="b.png", natural_width=100, natural_height=50),
            ],
            max_width=800,
            gap=10,
        )
        unsub = bundle.total_height.subscribe(lambda _msgs: None)
        flow = bundle.block_flow.get()
        assert flow[0].y == 0
        assert flow[1].y == 60  # 50 + 10
        assert bundle.total_height.get() == 110
        unsub()

    def test_set_gap(self) -> None:
        bundle = reactive_block_layout(
            mock_adapters(),
            blocks=[
                ImageBlock(src="a.png", natural_width=100, natural_height=50),
                ImageBlock(src="b.png", natural_width=100, natural_height=50),
            ],
            max_width=800,
            gap=0,
        )
        unsub = bundle.total_height.subscribe(lambda _msgs: None)
        assert bundle.total_height.get() == 100
        bundle.set_gap(20)
        assert bundle.total_height.get() == 120
        unsub()

    def test_invalidate_clears_cache(self) -> None:
        clear_count = 0

        class TrackingAdapter:
            def measure_segment(self, text: str, font: str) -> dict[str, float]:
                return {"width": len(text) * CHAR_WIDTH}

            def clear_cache(self) -> None:
                nonlocal clear_count
                clear_count += 1

        bundle = reactive_block_layout(
            BlockAdapters(text=TrackingAdapter()),
            blocks=[TextBlock(text="hello")],
            max_width=800,
        )
        unsub = bundle.measured_blocks.subscribe(lambda _msgs: None)
        bundle.measured_blocks.get()
        # signal broadcasts to all nodes, so clearCache may be called
        # more than once (once per INVALIDATE delivery)
        bundle.graph.signal([(MessageType.INVALIDATE,)])
        assert clear_count >= 1
        unsub()

    def test_snapshotable(self) -> None:
        bundle = reactive_block_layout(
            mock_adapters(),
            blocks=[TextBlock(text="hello")],
            max_width=800,
        )
        snap = bundle.graph.snapshot()
        assert snap is not None
        assert "blocks" in snap["nodes"]
        assert "total-height" in snap["nodes"]

    def test_meta_block_count(self) -> None:
        bundle = reactive_block_layout(
            mock_adapters(),
            blocks=[
                TextBlock(text="hello"),
                ImageBlock(src="a.png", natural_width=100, natural_height=50),
            ],
            max_width=800,
        )
        unsub = bundle.measured_blocks.subscribe(lambda _msgs: None)
        bundle.measured_blocks.get()
        mb_node = bundle.graph.node("measured-blocks")
        assert mb_node.meta["block-count"].get() == 2
        assert isinstance(mb_node.meta["layout-time-ns"].get(), int)
        unsub()

    def test_invalidate_preserves_meta(self) -> None:
        """Spec §2.3: meta nodes survive INVALIDATE via graph.signal()."""
        bundle = reactive_block_layout(
            mock_adapters(),
            blocks=[
                TextBlock(text="hello"),
                ImageBlock(src="a.png", natural_width=100, natural_height=50),
            ],
            max_width=800,
        )
        unsub = bundle.measured_blocks.subscribe(lambda _msgs: None)
        bundle.measured_blocks.get()

        mb_node = bundle.graph.node("measured-blocks")
        count_before = mb_node.meta["block-count"].get()
        assert count_before == 2

        bundle.graph.signal([(MessageType.INVALIDATE,)])
        assert mb_node.meta["block-count"].get() == count_before
        unsub()

    def test_negative_gap(self) -> None:
        """Negative gap produces overlapping blocks."""
        bundle = reactive_block_layout(
            mock_adapters(),
            blocks=[
                ImageBlock(src="a.png", natural_width=100, natural_height=50),
                ImageBlock(src="b.png", natural_width=100, natural_height=50),
            ],
            max_width=800,
            gap=-10,
        )
        unsub = bundle.total_height.subscribe(lambda _msgs: None)
        flow = bundle.block_flow.get()
        assert flow[0].y == 0
        assert flow[1].y == 40  # 50 + (-10)
        assert bundle.total_height.get() == 90  # 40 + 50
        unsub()

    def test_zero_max_width(self) -> None:
        """Zero maxWidth: images scale to 0."""
        bundle = reactive_block_layout(
            mock_adapters(),
            blocks=[ImageBlock(src="a.png", natural_width=100, natural_height=50)],
            max_width=0,
        )
        unsub = bundle.total_height.subscribe(lambda _msgs: None)
        m = bundle.measured_blocks.get()
        assert m[0].width == 0
        assert m[0].height == 0
        unsub()

    def test_negative_max_width_clamped(self) -> None:
        bundle = reactive_block_layout(mock_adapters(), blocks=[], max_width=-100)
        assert bundle.graph.get("max-width") == 0
        bundle.set_max_width(-5)
        assert bundle.graph.get("max-width") == 0
