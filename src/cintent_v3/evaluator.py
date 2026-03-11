"""Evaluation module: compare call graphs against ground truth.

Computes precision, recall, and F1 score by normalizing edges from both
the tool under test and the ground truth (e.g., DynaPyt) to a common
project-relative canonical form, then performing set comparison.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from cintent_v3.callgraph import call_graph_to_edges, load_call_graph
from cintent_v3.normalizer import (
    detect_project_packages,
    expand_edges_for_matching,
    filter_both_project_edges,
    filter_caller_project_edges,
    filter_noise_callees,
    filter_project_edges,
    normalize_call_graph,
    normalize_edges,
)


@dataclass
class EvaluationResult:
    """Result of comparing a call graph against ground truth."""

    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    tool_edge_count: int = 0
    ground_truth_edge_count: int = 0
    project_packages: set[str] = field(default_factory=set)
    # Detailed edge sets for debugging
    tp_edges: set[tuple[str, str]] = field(default_factory=set)
    fp_edges: set[tuple[str, str]] = field(default_factory=set)
    fn_edges: set[tuple[str, str]] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dict."""
        return {
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "tool_edge_count": self.tool_edge_count,
            "ground_truth_edge_count": self.ground_truth_edge_count,
            "project_packages": sorted(self.project_packages),
        }

    def summary(self) -> str:
        """Return a human-readable summary."""
        lines = [
            "=" * 60,
            "  Call Graph Evaluation Results",
            "=" * 60,
            f"  Project packages:     {sorted(self.project_packages)}",
            f"  Tool edges:           {self.tool_edge_count}",
            f"  Ground truth edges:   {self.ground_truth_edge_count}",
            "-" * 60,
            f"  True positives:       {self.true_positives}",
            f"  False positives:      {self.false_positives}",
            f"  False negatives:      {self.false_negatives}",
            "-" * 60,
            f"  Precision:            {self.precision:.4f}",
            f"  Recall:               {self.recall:.4f}",
            f"  F1 Score:             {self.f1:.4f}",
            "=" * 60,
        ]
        return "\n".join(lines)


def evaluate(
    tool_graph: dict[str, list[str]],
    ground_truth_graph: dict[str, list[str]],
    project_packages: set[str] | None = None,
    project_only: bool = True,
    both_project: bool = False,
    caller_project: bool = False,
    overlap_only: bool = False,
) -> EvaluationResult:
    """Compare a tool's call graph against ground truth.

    Both graphs are normalized to canonical project-relative names before
    comparison, so tool-specific path differences don't affect results.

    Args:
        tool_graph: Call graph from the tool under evaluation.
        ground_truth_graph: Call graph from the ground truth tool (e.g., DynaPyt).
        project_packages: Set of project package names. Auto-detected if None.
        project_only: If True, only compare edges involving project code.
        both_project: If True, only compare edges where BOTH endpoints are project code.
        caller_project: If True, only compare edges where the CALLER is project code.
            This matches DynaPyt's output structure and is recommended for comparison.
        overlap_only: If True, only evaluate edges from callers present in BOTH
            tool and ground truth. This is the fair evaluation mode when the ground
            truth has incomplete caller coverage (e.g., DynaPyt only traced certain
            test functions). Prevents penalizing the tool for discovering correct
            edges from callers the ground truth never observed.

    Returns:
        EvaluationResult with precision, recall, F1, and detailed edge sets.
    """
    # Step 1: Normalize both call graphs
    norm_tool = normalize_call_graph(tool_graph)
    norm_gt = normalize_call_graph(ground_truth_graph)

    # Step 2: Auto-detect project packages if not provided
    if project_packages is None:
        # Detect from ground truth (more reliable, as it has complete coverage)
        project_packages = detect_project_packages(norm_gt)
        # Also include packages found in tool output
        project_packages |= detect_project_packages(norm_tool)

    # Step 3: Convert to edge sets
    tool_edges = call_graph_to_edges(norm_tool)
    gt_edges = call_graph_to_edges(norm_gt)

    # Step 4: Filter to project-relevant edges if requested
    if caller_project:
        tool_edges = filter_caller_project_edges(tool_edges, project_packages)
        gt_edges = filter_caller_project_edges(gt_edges, project_packages)
    elif both_project:
        tool_edges = filter_both_project_edges(tool_edges, project_packages)
        gt_edges = filter_both_project_edges(gt_edges, project_packages)
    elif project_only:
        tool_edges = filter_project_edges(tool_edges, project_packages)
        gt_edges = filter_project_edges(gt_edges, project_packages)

    # Step 5: Remove import/metaclass/typing noise callees
    tool_edges = filter_noise_callees(tool_edges)
    gt_edges = filter_noise_callees(gt_edges)

    # Step 6: Expand edges for fuzzy matching (class instantiation, __init__ variants)
    # Both tool and GT edges are expanded so matching works regardless of
    # which representation each tool uses.
    tool_edges = expand_edges_for_matching(tool_edges)
    gt_edges = expand_edges_for_matching(gt_edges)

    # Step 7: Restrict to overlapping callers if requested
    # This gives a fair comparison when the ground truth has incomplete coverage
    if overlap_only:
        tool_callers = {c for c, _ in tool_edges}
        gt_callers = {c for c, _ in gt_edges}
        shared_callers = tool_callers & gt_callers
        tool_edges = {(c, e) for c, e in tool_edges if c in shared_callers}
        gt_edges = {(c, e) for c, e in gt_edges if c in shared_callers}

    # Step 8: Compute comparison metrics
    tp_edges = tool_edges & gt_edges
    fp_edges = tool_edges - gt_edges
    fn_edges = gt_edges - tool_edges

    tp = len(tp_edges)
    fp = len(fp_edges)
    fn = len(fn_edges)

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0

    return EvaluationResult(
        precision=precision,
        recall=recall,
        f1=f1,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        tool_edge_count=len(tool_edges),
        ground_truth_edge_count=len(gt_edges),
        project_packages=project_packages,
        tp_edges=tp_edges,
        fp_edges=fp_edges,
        fn_edges=fn_edges,
    )


def evaluate_from_files(
    tool_path: str,
    ground_truth_path: str,
    project_packages: set[str] | None = None,
    project_only: bool = True,
    both_project: bool = False,
    caller_project: bool = False,
    overlap_only: bool = False,
    output_path: str | None = None,
) -> EvaluationResult:
    """Evaluate call graphs from JSON files.

    Args:
        tool_path: Path to the tool's call graph JSON.
        ground_truth_path: Path to the ground truth call graph JSON.
        project_packages: Set of project package names. Auto-detected if None.
        project_only: If True, only compare edges involving project code.
        both_project: If True, only compare edges where BOTH endpoints are project code.
        caller_project: If True, only compare edges where the CALLER is project code.
        overlap_only: If True, only evaluate on callers present in both tool and GT.
        output_path: Optional path to write detailed results JSON.

    Returns:
        EvaluationResult with all comparison metrics.
    """
    tool_graph = load_call_graph(tool_path)
    gt_graph = load_call_graph(ground_truth_path)

    result = evaluate(
        tool_graph=tool_graph,
        ground_truth_graph=gt_graph,
        project_packages=project_packages,
        project_only=project_only,
        both_project=both_project,
        caller_project=caller_project,
        overlap_only=overlap_only,
    )

    if output_path:
        out_dir = os.path.dirname(output_path)
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        output_data = result.to_dict()
        output_data["false_positive_edges"] = sorted(
            [list(e) for e in result.fp_edges]
        )
        output_data["false_negative_edges"] = sorted(
            [list(e) for e in result.fn_edges]
        )
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2)

    return result


def print_edge_diff(result: EvaluationResult, max_edges: int = 20) -> None:
    """Print a readable diff of missing and extra edges.

    Args:
        result: EvaluationResult from evaluate().
        max_edges: Maximum number of edges to print per category.
    """
    if result.fn_edges:
        print(f"\n--- Missing edges (false negatives): {len(result.fn_edges)} ---")
        for i, (caller, callee) in enumerate(sorted(result.fn_edges)):
            if i >= max_edges:
                print(f"  ... and {len(result.fn_edges) - max_edges} more")
                break
            print(f"  {caller}  ->  {callee}")

    if result.fp_edges:
        print(f"\n--- Extra edges (false positives): {len(result.fp_edges)} ---")
        for i, (caller, callee) in enumerate(sorted(result.fp_edges)):
            if i >= max_edges:
                print(f"  ... and {len(result.fp_edges) - max_edges} more")
                break
            print(f"  {caller}  ->  {callee}")
