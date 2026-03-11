"""Call graph data structure, serialization, and I/O.

Handles exporting the traced call graph to JSON in DynaPyt-compatible format,
and loading call graphs from JSON files for comparison.
"""

from __future__ import annotations

import json
import os
from typing import Any


def export_call_graph(
    call_graph: dict[str, list[str]],
    output_path: str,
) -> None:
    """Export a call graph to JSON (DynaPyt-compatible format).

    The output format is:
        {
            "caller_fqn": ["callee_fqn_1", "callee_fqn_2", ...],
            ...
        }

    This mirrors DynaPyt's output format where keys are fully-qualified
    caller names and values are lists of callee FQNs.

    Args:
        call_graph: Dict mapping caller FQN to list of callee FQNs.
        output_path: Path to write the JSON file.
    """
    out_dir = os.path.dirname(output_path)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(call_graph, f, indent=4, sort_keys=True)


def load_call_graph(input_path: str) -> dict[str, list[str]]:
    """Load a call graph from a JSON file.

    Supports both DynaPyt format and cintent_v3 format (identical structure).

    Args:
        input_path: Path to the JSON file.

    Returns:
        Dict mapping caller FQN to list of callee FQNs.
    """
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Ensure all values are lists
    return {k: list(v) if isinstance(v, (list, set)) else [v] for k, v in data.items()}


def call_graph_to_edges(call_graph: dict[str, list[str]]) -> set[tuple[str, str]]:
    """Convert a call graph dict to a set of (caller, callee) edges.

    Args:
        call_graph: Dict mapping caller FQN to list of callee FQNs.

    Returns:
        Set of (caller, callee) tuples.
    """
    edges = set()
    for caller, callees in call_graph.items():
        for callee in callees:
            edges.add((caller, callee))
    return edges


def edges_to_call_graph(edges: set[tuple[str, str]]) -> dict[str, list[str]]:
    """Convert a set of edges to a call graph dict.

    Args:
        edges: Set of (caller, callee) tuples.

    Returns:
        Dict mapping caller FQN to sorted list of callee FQNs.
    """
    graph: dict[str, set[str]] = {}
    for caller, callee in edges:
        if caller not in graph:
            graph[caller] = set()
        graph[caller].add(callee)
    return {k: sorted(v) for k, v in sorted(graph.items())}


def get_call_graph_stats(call_graph: dict[str, list[str]]) -> dict[str, Any]:
    """Get statistics about a call graph.

    Returns:
        Dict with edge_count, caller_count, callee_count, unique_functions.
    """
    all_callees = set()
    total_edges = 0
    for caller, callees in call_graph.items():
        unique_callees = set(callees)
        all_callees.update(unique_callees)
        total_edges += len(unique_callees)

    all_functions = set(call_graph.keys()) | all_callees
    return {
        "edge_count": total_edges,
        "caller_count": len(call_graph),
        "callee_count": len(all_callees),
        "unique_functions": len(all_functions),
    }
