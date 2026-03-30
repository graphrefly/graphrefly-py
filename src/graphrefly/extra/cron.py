"""Minimal 5-field cron parser and matcher (minute hour day-of-month month day-of-week).

Ported from graphrefly-ts extra/cron.ts for from_cron (roadmap 2.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


@dataclass
class CronSchedule:
    minutes: set[int] = field(default_factory=set)
    hours: set[int] = field(default_factory=set)
    days_of_month: set[int] = field(default_factory=set)
    months: set[int] = field(default_factory=set)
    days_of_week: set[int] = field(default_factory=set)


def _parse_field(field_str: str, min_val: int, max_val: int) -> set[int]:
    result: set[int] = set()
    for part in field_str.split(","):
        pieces = part.split("/")
        range_str = pieces[0]
        step = int(pieces[1]) if len(pieces) > 1 else 1
        if step < 1:
            msg = f"Invalid cron step: {part}"
            raise ValueError(msg)
        if range_str == "*":
            start, end = min_val, max_val
        elif "-" in range_str:
            a, b = range_str.split("-")
            start, end = int(a), int(b)
        else:
            start = int(range_str)
            end = start
        if start < min_val or end > max_val:
            msg = f"Cron field out of range: {field_str} ({min_val}-{max_val})"
            raise ValueError(msg)
        if start > end:
            msg = f"Invalid cron range: {start}-{end} in {field_str}"
            raise ValueError(msg)
        for i in range(start, end + 1, step):
            result.add(i)
    return result


def parse_cron(expr: str) -> CronSchedule:
    """Parse a standard 5-field cron expression."""
    parts = expr.strip().split()
    if len(parts) != 5:  # noqa: PLR2004
        msg = f"Invalid cron: expected 5 fields, got {len(parts)}"
        raise ValueError(msg)
    return CronSchedule(
        minutes=_parse_field(parts[0], 0, 59),
        hours=_parse_field(parts[1], 0, 23),
        days_of_month=_parse_field(parts[2], 1, 31),
        months=_parse_field(parts[3], 1, 12),
        days_of_week=_parse_field(parts[4], 0, 6),
    )


def matches_cron(schedule: CronSchedule, dt: datetime) -> bool:
    """True if dt matches every field of schedule."""
    return (
        dt.minute in schedule.minutes
        and dt.hour in schedule.hours
        and dt.day in schedule.days_of_month
        and dt.month in schedule.months
        and dt.isoweekday() % 7 in schedule.days_of_week
    )


__all__ = ["CronSchedule", "matches_cron", "parse_cron"]
