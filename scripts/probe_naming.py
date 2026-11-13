"""Compare real Python module IDs with the root R-naming fixture.

Run with PYTHONPATH=src uv run python scripts/probe_naming.py
../graphrefly/spec/fixtures/node-naming.v1.json. No network or async runner is used.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from graphrefly import (
    Graph,
    GraphReflyValueError,
    wire_bridge,
    wire_bridge_ack_driver,
    wire_edge_group_in,
)


def _ids(graph: Graph) -> set[str]:
    return {row["id"] for row in graph.describe()}


def module_names() -> dict[str, Any]:
    with Graph() as graph:
        bridge = wire_bridge(graph, session_id="s1")
        # Returned public handles establish output roles; internal topology roles
        # are read from the constructor's real registration record.
        nodes = bridge._nodes
        wire = {
            "ids": sorted(_ids(graph)),
            "roles": {
                "core": nodes.core,
                "ackTimeouts": bridge.ack_timeouts.name,
                "events": bridge.events.name,
                "frames": bridge.frames.name,
                "issues": bridge.issues.name,
                "outboundBytes": bridge.outbound_bytes.name,
                "status": bridge.status.name,
                "statusLedger": nodes.status_ledger,
                "values": bridge.values.name,
            },
        }
    with Graph() as graph:
        frames = graph.state(name="input")
        before = _ids(graph)
        inbound = wire_edge_group_in(
            graph, group="g", frames=frames, edges=["x", "issues"], name="bridge"
        )
        incoming = {
            "ids": sorted(_ids(graph) - before),
            "roles": {
                "gate": inbound.gate.name,
                "edgeX": inbound.inbound["x"].name,
                "edgeIssues": inbound.inbound["issues"].name,
                "issues": inbound.issues.name,
            },
        }
    with Graph() as graph:
        bridge = wire_bridge(graph, session_id="s1")
        clock = graph.state(0, name="clock")
        before = _ids(graph)
        driver = wire_bridge_ack_driver(graph, bridge, clock=clock, timeout_ms=10)
        ids = _ids(graph) - before
        # The core is the sole node remaining after the returned projection roles.
        outputs = {driver.timeouts.name, driver.issues.name, driver.status.name}
        cores = ids - outputs
        assert len(cores) == 1, f"unexpected ack driver core inventory: {sorted(cores)}"
        ack = {
            "ids": sorted(ids),
            "roles": {
                "core": cores.pop(),
                "timeouts": driver.timeouts.name,
                "issues": driver.issues.name,
                "status": driver.status.name,
            },
        }
    return {
        "wireBridge": wire,
        "edgeGroupIn": incoming,
        "ackDriver": ack,
        "workItemSeeds": None,
        "cascadingCache": None,
    }


def main() -> None:
    fixture = json.loads(Path(sys.argv[1]).read_text())
    assert fixture["schema"] == "graphrefly/node-naming@1"
    with Graph() as graph:
        for name, message in (("a::b", fixture["error"]), ("a:", fixture["boundaryError"])):
            try:
                graph.state(1, name=name)
            except GraphReflyValueError as error:
                assert str(error) == message
            else:
                raise AssertionError(f"invalid local name admitted: {name}")
        assert graph.describe() == []
    actual = module_names()
    expected = {key: entry["runtimes"]["py"] for key, entry in fixture["modules"].items()}
    assert actual == expected, json.dumps({"actual": actual, "expected": expected}, indent=2)
    print(json.dumps(actual, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
