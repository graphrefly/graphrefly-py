"""Differential check of graphrefly._kernel against the JS reference kernel.

Random graphs and host operations come from graphrefly
sessions/active/python-kernel-benchmark/gen.py; the JS reference runs them through
run.mjs (needs ``node``); this script runs the same scenarios on graphrefly._kernel
and compares the sink traces entry by entry. The root checkout is only read.

    uv run python scripts/diff_reference.py --count 3000 --seed 0

``--skip`` drops scenarios using an operation the kernel does not support yet.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from graphrefly._kernel import DATA, INVALIDATE, Ctx, Graph, Report
from graphrefly._sentinel import SENTINEL

DEFAULT_BENCH = (
    Path(__file__).resolve().parents[2] / "graphrefly/sessions/active/python-kernel-benchmark"
)


def sum_of(ctx: Ctx) -> Any:
    return sum(d.value if d.has else 0 for d in ctx.deps)


def f_sum(ctx: Ctx) -> None:
    if all(d.has for d in ctx.deps):
        ctx.down([(DATA, sum_of(ctx))])


def f_sumv(ctx: Ctx) -> None:
    ctx.down([(DATA, sum_of(ctx))])


def f_waves(ctx: Ctx) -> None:
    total = sum(
        sum(len(w) for w in d.waves) + (100 if d.terminal else 0) + (1000 if d.invalidated else 0)
        for d in ctx.deps
    )
    ctx.down([(DATA, total)])


def f_even(ctx: Ctx) -> None:
    s = sum_of(ctx)
    if s % 2 == 0:
        ctx.down([(DATA, s)])


def f_throw(ctx: Ctx) -> None:
    s = sum_of(ctx)
    if s > 7:
        raise Exception("big")
    ctx.down([(DATA, s)])


def f_twice(ctx: Ctx) -> None:
    s = sum_of(ctx)
    ctx.down([(DATA, s)])
    ctx.down([(DATA, s + 1)])


def f_inv(ctx: Ctx) -> None:
    s = sum_of(ctx)
    ctx.down([(INVALIDATE,)] if s % 3 == 0 else [(DATA, s)])


FNS = {
    "sum": f_sum,
    "sumv": f_sumv,
    "waves": f_waves,
    "even": f_even,
    "throw": f_throw,
    "twice": f_twice,
    "inv": f_inv,
}


def plain(value: object) -> object:
    return str(value) if isinstance(value, BaseException) else value


def run_scenario(sc: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def flush() -> None:
        assert g.trace is not None
        for entry in g.trace:
            out.append(
                {
                    "to": entry.to,
                    "from": entry.sender,
                    "msgs": [[m[0], *(plain(x) for x in m[1:])] for m in entry.msgs],
                }
            )
        g.trace.clear()

    def report(r: Report) -> None:
        flush()
        out.append({"report": r.source})

    g = Graph(report=report, trace=True)
    for n in sc["nodes"]:
        if n["kind"] == "state":
            if n["initial"] is None:
                g.state(n["id"])
            else:
                g.state(n["id"], n["initial"])
        else:
            opts = n["opts"]
            terminal = opts.get("terminal", {})
            g.node(
                n["id"],
                n["deps"],
                FNS[n["fn"]],
                complete=terminal.get("complete", "auto"),
                error=terminal.get("error", "auto"),
                invalidate=opts.get("invalidate", "auto"),
            )

    def run(op: list[Any]) -> None:
        kind = op[0]
        try:
            if kind == "subscribe":
                g.subscribe(op[1], op[2])
            elif kind == "unsubscribe":
                g.unsubscribe(op[1], op[2])
            elif kind == "set":
                g.set(op[1], op[2])
            elif kind == "setfn":
                b = op[2]
                g.set(op[1], lambda p: (0 if p is SENTINEL else p) + b)
            elif kind == "invalidate":
                g.invalidate(op[1])
            elif kind == "complete":
                g.complete(op[1])
            elif kind == "error":
                g.error(op[1], op[2])
            elif kind == "batch":
                graph: Any = g  # batch arrives in phase 2
                ops = op[1]

                def body() -> None:
                    for inner in ops:
                        run(inner)

                graph.batch(body)
        except Exception as error:
            flush()
            out.append({"thrown": str(error)})
        flush()

    for op in sc["ops"]:
        run(op)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--bench", type=Path, default=DEFAULT_BENCH)
    parser.add_argument("--skip", nargs="*", default=[], help="op kinds to drop scenarios for")
    args = parser.parse_args()
    gen = subprocess.run(
        ["python3", "-I", str(args.bench / "gen.py"), str(args.count), str(args.seed)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    scenarios = json.loads(gen)

    def uses(ops: list[Any], kinds: set[str]) -> bool:
        return any(op[0] in kinds or (op[0] == "batch" and uses(op[1], kinds)) for op in ops)

    skip = set(args.skip)
    kept = [sc for sc in scenarios if not uses(sc["ops"], skip)]
    js = json.loads(
        subprocess.run(
            ["node", str(args.bench / "run.mjs")],
            input=json.dumps(kept),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    py = [run_scenario(sc) for sc in kept]
    bad = [i for i, (a, b) in enumerate(zip(js, py, strict=True)) if a != b]
    entries = sum(map(len, js))
    print(
        f"scenarios={len(kept)} (of {len(scenarios)}, skipped ops {sorted(skip)}) "
        f"trace_entries={entries} mismatches={len(bad)}"
    )
    for i in bad[:3]:
        print("SCENARIO", json.dumps(kept[i]))
        a, b = js[i], py[i]
        for k in range(max(len(a), len(b))):
            x = a[k] if k < len(a) else None
            y = b[k] if k < len(b) else None
            if x != y:
                print(" first diff at", k, "\n  js", x, "\n  py", y)
                break
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
