"""Reactive multi-content block layout engine (roadmap §7.1 — mixed content).

Extends the text-only ``reactive_layout`` with support for image and SVG blocks.
Pure-arithmetic layout over measured child sizes — no DOM, no async.

Graph shape::

    Graph("reactive-block-layout")
    ├── state("blocks")              — ContentBlock list input
    ├── state("max-width")           — container constraint
    ├── state("gap")                 — vertical gap between blocks (px)
    ├── derived("measured-blocks")   — blocks + max-width → MeasuredBlock list
    ├── derived("block-flow")        — measured-blocks + gap → PositionedBlock list
    ├── derived("total-height")      — block-flow → float
    └── meta: { block-count, layout-time-ns }
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from graphrefly.core.clock import monotonic_ns
from graphrefly.core.protocol import MessageType, emit_with_batch
from graphrefly.core.sugar import derived, state
from graphrefly.extra.reactive_layout import (
    CharPosition,
    LineBreaksResult,
    MeasurementAdapter,
    PreparedSegment,
    analyze_and_measure,
    compute_char_positions,
    compute_line_breaks,
)
from graphrefly.graph.graph import Graph

if TYPE_CHECKING:
    from graphrefly.core.node import NodeImpl

# ---------------------------------------------------------------------------
# Adapter protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class SvgMeasurer(Protocol):
    """Pluggable measurement backend for SVG content."""

    def measure_svg(self, content: str) -> dict[str, float]:
        """Return ``{"width": <px>, "height": <px>}``."""
        ...


@runtime_checkable
class ImageMeasurer(Protocol):
    """Pluggable measurement backend for image content."""

    def measure_image(self, src: str) -> dict[str, float]:
        """Return ``{"width": <px>, "height": <px>}``."""
        ...


# ---------------------------------------------------------------------------
# Content block types
# ---------------------------------------------------------------------------


@dataclass
class TextBlock:
    """A text content block."""

    type: str = field(default="text", init=False)
    text: str = ""
    font: str | None = None
    line_height: float | None = None


@dataclass
class ImageBlock:
    """An image content block."""

    type: str = field(default="image", init=False)
    src: str = ""
    natural_width: float | None = None
    natural_height: float | None = None


@dataclass
class SvgBlock:
    """An SVG content block."""

    type: str = field(default="svg", init=False)
    content: str = ""
    view_box: tuple[float, float] | None = None  # (width, height)


ContentBlock = TextBlock | ImageBlock | SvgBlock


# ---------------------------------------------------------------------------
# Measured / positioned block types
# ---------------------------------------------------------------------------


@dataclass
class MeasuredBlock:
    """A block after measurement — knows its natural dimensions.

    **Equality note:** The reactive ``measured-blocks`` node uses dimension-only equality
    (``type``, ``width``, ``height``, ``index``). Inner text layout data
    (``text_segments``, ``text_line_breaks``, ``text_char_positions``) is NOT compared
    for change detection. If you need text-level reactivity, use ``reactive_layout()``
    directly per text block.
    """

    index: int
    type: str
    width: float
    height: float
    text_segments: list[PreparedSegment] | None = None
    text_line_breaks: LineBreaksResult | None = None
    text_char_positions: list[CharPosition] | None = None


@dataclass
class PositionedBlock(MeasuredBlock):
    """A block after flow — positioned in the container."""

    x: float = 0.0
    y: float = 0.0


@dataclass
class BlockAdapters:
    """Adapters map for ``reactive_block_layout``."""

    text: MeasurementAdapter
    svg: SvgMeasurer | None = None
    image: ImageMeasurer | None = None


@dataclass
class ReactiveBlockLayoutBundle:
    """Result of the reactive block layout graph factory."""

    graph: Graph
    set_blocks: Any  # (list[ContentBlock]) -> None
    set_max_width: Any  # (float) -> None
    set_gap: Any  # (float) -> None
    measured_blocks: NodeImpl[list[MeasuredBlock]]
    block_flow: NodeImpl[list[PositionedBlock]]
    total_height: NodeImpl[float]


# ---------------------------------------------------------------------------
# Block measurement (pure functions)
# ---------------------------------------------------------------------------


def measure_block(
    block: ContentBlock,
    max_width: float,
    adapters: BlockAdapters,
    measure_cache: dict[str, dict[str, float]],
    default_font: str,
    default_line_height: float,
    index: int,
) -> MeasuredBlock:
    """Measure a single content block, returning natural dimensions."""
    if isinstance(block, TextBlock):
        font = block.font if block.font is not None else default_font
        line_height = block.line_height if block.line_height is not None else default_line_height
        segments = analyze_and_measure(block.text, font, adapters.text, measure_cache)
        line_breaks = compute_line_breaks(segments, max_width, adapters.text, font, measure_cache)
        char_positions = compute_char_positions(line_breaks, segments, line_height)
        height = line_breaks.line_count * line_height
        width = 0.0
        for line in line_breaks.lines:
            if line.width > width:
                width = line.width
        return MeasuredBlock(
            index=index,
            type="text",
            width=min(width, max_width),
            height=height,
            text_segments=segments,
            text_line_breaks=line_breaks,
            text_char_positions=char_positions,
        )

    if isinstance(block, ImageBlock):
        if block.natural_width is not None and block.natural_height is not None:
            w, h = block.natural_width, block.natural_height
        elif adapters.image is not None:
            dims = adapters.image.measure_image(block.src)
            w, h = dims["width"], dims["height"]
        else:
            raise ValueError(
                f"Image block at index {index} has no natural_width/natural_height "
                f"and no ImageMeasurer adapter"
            )
        if w > max_width:
            h = h * max_width / w
            w = max_width
        return MeasuredBlock(index=index, type="image", width=w, height=h)

    if isinstance(block, SvgBlock):
        if block.view_box is not None:
            w, h = block.view_box
        elif adapters.svg is not None:
            dims = adapters.svg.measure_svg(block.content)
            w, h = dims["width"], dims["height"]
        else:
            raise ValueError(
                f"SVG block at index {index} has no view_box and no SvgMeasurer adapter"
            )
        if w > max_width:
            h = h * max_width / w
            w = max_width
        return MeasuredBlock(index=index, type="svg", width=w, height=h)

    raise TypeError(f"Unknown block type: {type(block)}")


def measure_blocks(
    blocks: list[ContentBlock],
    max_width: float,
    adapters: BlockAdapters,
    measure_cache: dict[str, dict[str, float]],
    default_font: str,
    default_line_height: float,
) -> list[MeasuredBlock]:
    """Measure all blocks in a content array."""
    return [
        measure_block(
            block,
            max_width,
            adapters,
            measure_cache,
            default_font,
            default_line_height,
            i,
        )
        for i, block in enumerate(blocks)
    ]


# ---------------------------------------------------------------------------
# Block flow (pure function)
# ---------------------------------------------------------------------------


def compute_block_flow(
    measured: list[MeasuredBlock],
    gap: float,
) -> list[PositionedBlock]:
    """Vertical stacking flow: blocks top-to-bottom, left-aligned, separated by gap."""
    result: list[PositionedBlock] = []
    y = 0.0
    for i, m in enumerate(measured):
        result.append(
            PositionedBlock(
                index=m.index,
                type=m.type,
                width=m.width,
                height=m.height,
                text_segments=m.text_segments,
                text_line_breaks=m.text_line_breaks,
                text_char_positions=m.text_char_positions,
                x=0.0,
                y=y,
            )
        )
        y += m.height + (gap if i < len(measured) - 1 else 0)
    return result


def compute_total_height(flow: list[PositionedBlock]) -> float:
    """Compute total height from positioned blocks."""
    if not flow:
        return 0.0
    last = flow[-1]
    return last.y + last.height


# ---------------------------------------------------------------------------
# Reactive graph factory
# ---------------------------------------------------------------------------


def reactive_block_layout(
    adapters: BlockAdapters,
    *,
    name: str = "reactive-block-layout",
    blocks: list[ContentBlock] | None = None,
    max_width: float = 800,
    gap: float = 0,
    default_font: str = "16px sans-serif",
    default_line_height: float = 20,
) -> ReactiveBlockLayoutBundle:
    """Create a reactive block layout graph for mixed content.

    Parameters
    ----------
    adapters:
        Block measurement adapters (text required, svg/image optional).
    name:
        Graph name.
    blocks:
        Initial content blocks.
    max_width:
        Initial max width constraint (px). Values ``< 0`` are clamped to ``0``.
    gap:
        Vertical gap between blocks (px).
    default_font:
        Default CSS font string for text blocks without explicit font.
    default_line_height:
        Default line height for text blocks without explicit line_height.
    """
    g = Graph(name)
    measure_cache: dict[str, dict[str, float]] = {}

    # --- State nodes ---
    blocks_node: NodeImpl[list[ContentBlock]] = state(blocks or [], name="blocks")
    max_width_node: NodeImpl[float] = state(max(0.0, max_width), name="max-width")
    gap_node: NodeImpl[float] = state(gap, name="gap")

    # --- Derived: measured-blocks ---
    def _invalidate_cache(msg: tuple[Any, ...], _dep_index: int, _actions: Any) -> bool:
        if msg[0] == MessageType.INVALIDATE:
            measure_cache.clear()
            clear_fn = getattr(adapters.text, "clear_cache", None)
            if callable(clear_fn):
                clear_fn()
        return False

    def _compute_measured(deps: list[Any], _actions: Any) -> list[MeasuredBlock]:
        blks: list[ContentBlock] = deps[0]
        mw: float = deps[1]
        t0 = monotonic_ns()
        result = measure_blocks(
            blks, mw, adapters, measure_cache, default_font, default_line_height
        )
        elapsed = monotonic_ns() - t0

        meta = measured_blocks_node.meta
        if meta:
            bc = meta.get("block-count")
            if bc is not None:
                emit_with_batch(bc.down, [(MessageType.DATA, len(result))], phase=3)
            lt = meta.get("layout-time-ns")
            if lt is not None:
                emit_with_batch(lt.down, [(MessageType.DATA, elapsed)], phase=3)

        return result

    def _measured_equals(a: list[MeasuredBlock] | None, b: list[MeasuredBlock] | None) -> bool:
        if a is None or b is None:
            return a is b
        if len(a) != len(b):
            return False
        return all(
            ma.type == mb.type
            and ma.width == mb.width
            and ma.height == mb.height
            and ma.index == mb.index
            for ma, mb in zip(a, b, strict=True)
        )

    measured_blocks_node: NodeImpl[list[MeasuredBlock]] = derived(
        [blocks_node, max_width_node],
        _compute_measured,
        name="measured-blocks",
        meta={"block-count": 0, "layout-time-ns": 0},
        on_message=_invalidate_cache,
        equals=_measured_equals,
    )

    # --- Derived: block-flow ---
    def _compute_flow(deps: list[Any], _actions: Any) -> list[PositionedBlock]:
        meas: list[MeasuredBlock] = deps[0]
        g_val: float = deps[1]
        return compute_block_flow(meas, g_val)

    def _flow_equals(a: list[PositionedBlock] | None, b: list[PositionedBlock] | None) -> bool:
        if a is None or b is None:
            return a is b
        if len(a) != len(b):
            return False
        return all(
            pa.x == pb.x and pa.y == pb.y and pa.width == pb.width and pa.height == pb.height
            for pa, pb in zip(a, b, strict=True)
        )

    block_flow_node: NodeImpl[list[PositionedBlock]] = derived(
        [measured_blocks_node, gap_node],
        _compute_flow,
        name="block-flow",
        equals=_flow_equals,
    )

    # --- Derived: total-height ---
    total_height_node: NodeImpl[float] = derived(
        [block_flow_node],
        lambda deps, _a: compute_total_height(deps[0]),
        name="total-height",
    )

    # --- Register in graph ---
    g.add("blocks", blocks_node)
    g.add("max-width", max_width_node)
    g.add("gap", gap_node)
    g.add("measured-blocks", measured_blocks_node)
    g.add("block-flow", block_flow_node)
    g.add("total-height", total_height_node)

    # --- Edges (for describe() visibility) ---
    g.connect("blocks", "measured-blocks")
    g.connect("max-width", "measured-blocks")
    g.connect("measured-blocks", "block-flow")
    g.connect("gap", "block-flow")
    g.connect("block-flow", "total-height")

    return ReactiveBlockLayoutBundle(
        graph=g,
        set_blocks=lambda b: g.set("blocks", b),
        set_max_width=lambda mw: g.set("max-width", max(0.0, mw)),
        set_gap=lambda gv: g.set("gap", gv),
        measured_blocks=measured_blocks_node,
        block_flow=block_flow_node,
        total_height=total_height_node,
    )
