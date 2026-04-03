"""Reactive layout pattern — standalone importable module.

Usage::

    from graphrefly.patterns.reactive_layout import reactive_layout, CliMeasureAdapter
"""

from graphrefly.patterns.reactive_layout.measurement_adapters import (
    CliMeasureAdapter,
    ImageSizeAdapter,
    PillowMeasureAdapter,
    PrecomputedAdapter,
    SvgBoundsAdapter,
)
from graphrefly.patterns.reactive_layout.reactive_block_layout import (
    BlockAdapters,
    ContentBlock,
    ImageBlock,
    ImageMeasurer,
    MeasuredBlock,
    PositionedBlock,
    ReactiveBlockLayoutBundle,
    SvgBlock,
    SvgMeasurer,
    TextBlock,
    compute_block_flow,
    compute_total_height,
    measure_block,
    measure_blocks,
    reactive_block_layout,
)
from graphrefly.patterns.reactive_layout.reactive_layout import (
    CharPosition,
    LayoutLine,
    LineBreaksResult,
    MeasurementAdapter,
    PreparedSegment,
    ReactiveLayoutBundle,
    SegmentBreakKind,
    analyze_and_measure,
    compute_char_positions,
    compute_line_breaks,
    reactive_layout,
)

__all__ = [
    # reactive_layout
    "CharPosition",
    "LayoutLine",
    "LineBreaksResult",
    "MeasurementAdapter",
    "PreparedSegment",
    "ReactiveLayoutBundle",
    "SegmentBreakKind",
    "analyze_and_measure",
    "compute_char_positions",
    "compute_line_breaks",
    "reactive_layout",
    # reactive_block_layout
    "BlockAdapters",
    "ContentBlock",
    "ImageBlock",
    "ImageMeasurer",
    "MeasuredBlock",
    "PositionedBlock",
    "ReactiveBlockLayoutBundle",
    "SvgBlock",
    "SvgMeasurer",
    "TextBlock",
    "compute_block_flow",
    "compute_total_height",
    "measure_block",
    "measure_blocks",
    "reactive_block_layout",
    # measurement_adapters
    "CliMeasureAdapter",
    "ImageSizeAdapter",
    "PillowMeasureAdapter",
    "PrecomputedAdapter",
    "SvgBoundsAdapter",
]
