"""Run the D808 cross-runtime observable traces using the public Python API.

Run with PYTHONPATH=src uv run python scripts/probe_rereview.py. Pass the root
spec/fixtures/d797-rereview.v1.json path to compare against its shared expectations.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from graphrefly import DATA, Ctx, Graph, Recipe, restore_graph


def quiet_terminal() -> dict[str, object]:
    calls: list[tuple[list[list[object]], Any]] = []
    with Graph() as graph:
        source = graph.state(0)
        quiet = graph.node(
            [source], lambda ctx: ctx.emit(99) if ctx.deps[0].terminal else None, quiet=True
        )
        graph.retain(
            graph.node(
                [quiet],
                lambda ctx: calls.append((ctx.deps[0].waves, ctx.deps[0].terminal)),
                complete="manual",
            )
        )
        calls.clear()
        source.complete()
    return {"runs": len(calls), "waves": calls[0][0], "terminal": calls[0][1][0]}


def stop_traces() -> tuple[list[object], list[object]]:
    observed: list[object] = []
    subscribed: list[object] = []
    with Graph() as graph:
        source = graph.state(0)

        def run(ctx: Ctx) -> None:
            if ctx.deps[0].value == 1:
                ctx.emit(1)
                ctx.emit(2)

        node = graph.node([source], run)

        def observe_sink(messages: Any) -> None:
            values = [message[1] for message in messages if message[0] == DATA]
            if values:
                observed.extend(values)
                stop_observed()

        def subscribe_sink(messages: Any) -> None:
            values = [message[1] for message in messages if message[0] == DATA]
            if values:
                subscribed.extend(values)
                stop_subscribed()

        stop_observed = graph.observe([node], lambda event: observe_sink(event.msgs))
        stop_subscribed = node.subscribe(subscribe_sink)
        source.set(1)
    return observed, subscribed


def empty_and_late() -> tuple[dict[str, int], list[object]]:
    with Graph() as graph:
        held: list[Ctx] = []
        arrays: list[object] = []
        node = graph.node([], lambda ctx: held.append(ctx))
        node.subscribe(arrays.append)
        arrays.clear()
        held[0].down([])
        empty = {"arrays": len(arrays)}
        state = graph.state(0)
        seen: list[object] = []
        node.subscribe(lambda messages: seen.extend(m[1] for m in messages if m[0] == DATA))

        def fail(value: object) -> object:
            raise ValueError("failed updater")

        def body() -> None:
            state.set(fail)
            held[0].emit(42)

        with suppress(ValueError):
            graph.batch(body)
    return empty, seen


def replace_cleanup() -> list[object]:
    seen: list[object] = []
    with Graph() as graph:
        ledger = graph.state(0)
        ledger.subscribe(lambda messages: seen.extend(m[1] for m in messages if m[0] == DATA))
        seen.clear()

        def setup(ctx: Ctx) -> Callable[[], None]:
            ctx.state = 42
            return lambda: ledger.set(ctx.state)

        node = graph.node([], lambda ctx: None, setup=setup)
        graph.retain(node)
        graph.replace(node, [], lambda ctx: None)
    return seen


def ended_batch() -> dict[str, int]:
    reports: list[object] = []
    runs: list[object] = []
    with Graph(report=reports.append) as graph:
        trigger = graph.state(0)
        ended = graph.state(0)
        other = graph.state(0)

        def updater(value: object) -> int:
            runs.append(value)
            return 1

        def request(messages: Any) -> None:
            if any(m == (DATA, 1) for m in messages):
                ended.complete()
                graph.batch(lambda: (other.set(updater), ended.set(updater)))

        trigger.subscribe(request)
        trigger.set(1)
        result = {"updaterRuns": len(runs), "otherValue": other.cache(), "reports": len(reports)}
    return result


def missing_recipe() -> str:
    with Graph() as graph:
        graph.node([], lambda ctx: None, name="n", recipe=Recipe("absent"))
        try:
            restore_graph(graph.checkpoint(), registry={})
        except Exception as error:
            return str(error)
    raise AssertionError("restore unexpectedly succeeded")


def main() -> None:
    observed, subscribed = stop_traces()
    empty, late = empty_and_late()
    result = {
        "quietTerminal": quiet_terminal(),
        "observeStop": observed,
        "subscribeStop": subscribed,
        "emptyDown": empty,
        "lateAfterCommitFailure": late,
        "replaceCleanup": replace_cleanup(),
        "endedBatch": ended_batch(),
        "checkpointMissingRecipe": missing_recipe(),
    }
    if len(sys.argv) > 1:
        expected = json.loads(Path(sys.argv[1]).read_text())["probeExpected"]
        assert result == expected, (result, expected)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
