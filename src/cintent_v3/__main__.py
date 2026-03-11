"""CLI entry point for cintent_v3.

Provides commands for:
  - trace:            Run tests with deterministic call graph tracing
  - evaluate:         Compare a call graph against ground truth (DynaPyt etc.)
  - normalize:        Normalize a call graph JSON file
  - generate-conftest: Generate conftest.py for automatic pytest tracing
  - stats:            Print statistics about a call graph
  - ci setup:         Prepare a project for CI tracing
  - ci collect:       Run tracing + package as CI artifact
  - ci evaluate:      Evaluate a CI artifact against ground truth

Usage:
    python -m cintent_v3 trace /path/to/project -o callgraph.json
    python -m cintent_v3 evaluate callgraph.json --ground-truth dynapyt.json
    python -m cintent_v3 normalize dynapyt.json -o normalized.json
    python -m cintent_v3 generate-conftest /path/to/project
    python -m cintent_v3 stats callgraph.json
    python -m cintent_v3 ci setup /path/to/project
    python -m cintent_v3 ci collect /path/to/project -o artifact.zip
    python -m cintent_v3 ci evaluate artifact.zip -g ground_truth.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from cintent_v3.callgraph import (
    export_call_graph,
    get_call_graph_stats,
    load_call_graph,
)
from cintent_v3.evaluator import (
    evaluate_from_files,
    print_edge_diff,
)
from cintent_v3.normalizer import normalize_call_graph
from cintent_v3.pytest_plugin import CintentPytestPlugin, generate_conftest
from cintent_v3.tracer import CallGraphTracer


def _pre_import_project_modules(tracer: CallGraphTracer, project_root: str) -> None:
    """Import all project Python modules while tracer is active.

    This captures module-level calls (e.g., `grab.base -> typing.TypeVar`)
    that happen at import time. Without this, modules imported before tracing
    starts would have their module-level edges missing.
    """
    import glob
    import importlib

    tracer.start()
    try:
        # Find all .py files in the project
        pattern = os.path.join(project_root, "**", "*.py")
        py_files = glob.glob(pattern, recursive=True)

        for filepath in py_files:
            relpath = os.path.relpath(filepath, project_root)
            # Skip test files (they'll be imported by pytest)
            parts = relpath.replace(os.sep, "/").split("/")
            if any(p.startswith("test") or p == "conftest.py" for p in parts):
                continue

            # Convert file path to module name
            module_name = relpath.replace(os.sep, ".").replace("/", ".")
            if module_name.endswith(".py"):
                module_name = module_name[:-3]
            if module_name.endswith(".__init__"):
                module_name = module_name[:-9]

            # Skip already-imported modules
            if module_name in sys.modules:
                continue

            try:
                importlib.import_module(module_name)
            except Exception:
                # Some modules may fail to import (missing deps, etc.)
                pass
    finally:
        tracer.stop()


def cmd_trace(args: argparse.Namespace) -> None:
    """Run pytest with deterministic call graph tracing."""
    project_root = os.path.normpath(os.path.abspath(args.project_root))

    if not os.path.isdir(project_root):
        print(f"Error: Project root '{project_root}' does not exist.", file=sys.stderr)
        sys.exit(1)

    output_path = args.output or os.path.join(project_root, "cintent_v3_callgraph.json")

    if args.method == "plugin":
        # Method 1: Run pytest programmatically with our plugin
        try:
            import pytest
        except ImportError:
            print("Error: pytest is required. Install it with: pip install pytest", file=sys.stderr)
            sys.exit(1)

        tracer = CallGraphTracer(
            project_root=project_root,
            record_external=args.record_external,
        )

        # Pre-import project modules under tracing to capture module-level calls.
        # This catches edges like `grab.base -> typing.TypeVar` that happen at
        # import time and would be missed if modules are already cached.
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        _pre_import_project_modules(tracer, project_root)

        plugin = CintentPytestPlugin(tracer=tracer, output_path=output_path)

        test_args = [project_root]
        if args.test_path:
            # Resolve test path relative to project root if not absolute
            test_path = args.test_path
            if not os.path.isabs(test_path):
                test_path = os.path.join(project_root, test_path)
            test_args = [test_path]
        if args.pytest_args:
            test_args.extend(args.pytest_args)

        print(f"[cintent_v3] Tracing project: {project_root}")
        print(f"[cintent_v3] Output: {output_path}")
        print(f"[cintent_v3] Running: pytest {' '.join(test_args)}")

        exit_code = pytest.main(test_args, plugins=[plugin])
        sys.exit(exit_code)

    elif args.method == "conftest":
        # Method 2: Generate conftest and let user run pytest separately
        conftest_path = generate_conftest(
            project_root=project_root,
            output_dir=args.conftest_dir or project_root,
        )
        print(f"[cintent_v3] Generated: {conftest_path}")
        print(f"[cintent_v3] Run your tests with:")
        print(f"  cd {project_root}")
        print(f"  pytest -p conftest_cintent_v3")

    elif args.method == "subprocess":
        # Method 3: Run pytest in a subprocess with the plugin auto-loaded
        conftest_path = generate_conftest(project_root=project_root)
        test_args = ["pytest"]
        if args.test_path:
            test_args.append(args.test_path)
        else:
            test_args.append(project_root)
        test_args.extend(["-p", "conftest_cintent_v3"])
        if args.pytest_args:
            test_args.extend(args.pytest_args)

        print(f"[cintent_v3] Running: {' '.join(test_args)}")
        result = subprocess.run(test_args, cwd=project_root)

        # Clean up generated conftest
        if os.path.exists(conftest_path):
            os.remove(conftest_path)

        sys.exit(result.returncode)


def cmd_evaluate(args: argparse.Namespace) -> None:
    """Compare a call graph against ground truth."""
    if not os.path.isfile(args.tool_graph):
        print(f"Error: Tool graph '{args.tool_graph}' not found.", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(args.ground_truth):
        print(f"Error: Ground truth '{args.ground_truth}' not found.", file=sys.stderr)
        sys.exit(1)

    project_packages = None
    if args.packages:
        project_packages = set(args.packages)

    result = evaluate_from_files(
        tool_path=args.tool_graph,
        ground_truth_path=args.ground_truth,
        project_packages=project_packages,
        project_only=not args.all_edges,
        both_project=args.both_project,
        caller_project=args.caller_project,
        overlap_only=args.overlap_only,
        output_path=args.output,
    )

    print(result.summary())

    if args.verbose:
        print_edge_diff(result, max_edges=args.max_diff)

    if args.output:
        print(f"\nDetailed results written to: {args.output}")


def cmd_normalize(args: argparse.Namespace) -> None:
    """Normalize a call graph JSON file."""
    if not os.path.isfile(args.input):
        print(f"Error: Input file '{args.input}' not found.", file=sys.stderr)
        sys.exit(1)

    call_graph = load_call_graph(args.input)
    normalized = normalize_call_graph(call_graph)

    output_path = args.output or args.input.replace(".json", "_normalized.json")
    export_call_graph(normalized, output_path)

    original_edges = sum(len(v) for v in call_graph.values())
    norm_edges = sum(len(v) for v in normalized.values())
    print(f"Normalized: {len(call_graph)} callers, {original_edges} edges")
    print(f"       ->   {len(normalized)} callers, {norm_edges} edges")
    print(f"Output: {output_path}")


def cmd_generate_conftest(args: argparse.Namespace) -> None:
    """Generate conftest.py for automatic pytest tracing."""
    project_root = os.path.normpath(os.path.abspath(args.project_root))
    if not os.path.isdir(project_root):
        print(f"Error: Project root '{project_root}' does not exist.", file=sys.stderr)
        sys.exit(1)

    output_dir = args.output_dir or project_root
    conftest_path = generate_conftest(project_root=project_root, output_dir=output_dir)
    print(f"Generated: {conftest_path}")
    print(f"Usage: cd {project_root} && pytest -p conftest_cintent_v3")


def cmd_stats(args: argparse.Namespace) -> None:
    """Print statistics about a call graph."""
    if not os.path.isfile(args.input):
        print(f"Error: Input file '{args.input}' not found.", file=sys.stderr)
        sys.exit(1)

    call_graph = load_call_graph(args.input)
    stats = get_call_graph_stats(call_graph)

    if args.normalize:
        call_graph = normalize_call_graph(call_graph)
        stats_norm = get_call_graph_stats(call_graph)
        print("Raw call graph:")
        for k, v in stats.items():
            print(f"  {k}: {v}")
        print("Normalized call graph:")
        for k, v in stats_norm.items():
            print(f"  {k}: {v}")
    else:
        for k, v in stats.items():
            print(f"  {k}: {v}")


# ─── CI Commands ──────────────────────────────────────────────────────────────


def cmd_ci_setup(args: argparse.Namespace) -> None:
    """Prepare a project for CI tracing.

    Generates conftest.py and prints GitHub Actions workflow snippet.
    """
    from cintent_v3.ci import collect_metadata

    project_root = os.path.normpath(os.path.abspath(args.project_root))
    if not os.path.isdir(project_root):
        print(f"Error: Project root '{project_root}' does not exist.", file=sys.stderr)
        sys.exit(1)

    output_dir = args.output_dir or project_root
    conftest_path = generate_conftest(project_root=project_root, output_dir=output_dir)
    print(f"[cintent_v3] Generated conftest: {conftest_path}")

    # Also generate a GitHub Actions workflow snippet
    workflow_snippet = _get_workflow_snippet(project_root)
    print(f"\n[cintent_v3] Add this to your GitHub Actions workflow:\n")
    print(workflow_snippet)

    if args.workflow_file:
        workflow_dir = os.path.join(project_root, ".github", "workflows")
        os.makedirs(workflow_dir, exist_ok=True)
        workflow_path = os.path.join(workflow_dir, args.workflow_file)
        full_workflow = _get_full_workflow(project_root)
        with open(workflow_path, "w", encoding="utf-8") as f:
            f.write(full_workflow)
        print(f"\n[cintent_v3] Workflow written to: {workflow_path}")


def cmd_ci_collect(args: argparse.Namespace) -> None:
    """Run tracing and package as a CI artifact.

    This is the one-command CI integration: trace tests, then produce
    a downloadable zip artifact with call graph + metadata.
    """
    from cintent_v3.ci import collect_metadata, package_artifact

    project_root = os.path.normpath(os.path.abspath(args.project_root))
    if not os.path.isdir(project_root):
        print(f"Error: Project root '{project_root}' does not exist.", file=sys.stderr)
        sys.exit(1)

    try:
        import pytest
    except ImportError:
        print("Error: pytest is required. Install it with: pip install pytest", file=sys.stderr)
        sys.exit(1)

    # Set up output directory
    output_dir = args.output_dir or os.environ.get("CINTENT_V3_OUTPUT") or project_root
    os.makedirs(output_dir, exist_ok=True)

    cg_path = os.path.join(output_dir, "cintent_v3_callgraph.json")

    tracer = CallGraphTracer(
        project_root=project_root,
        record_external=args.record_external,
    )

    # Pre-import project modules
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    _pre_import_project_modules(tracer, project_root)

    plugin = CintentPytestPlugin(tracer=tracer, output_path=cg_path)

    test_args = [project_root]
    if args.test_path:
        test_path = args.test_path
        if not os.path.isabs(test_path):
            test_path = os.path.join(project_root, test_path)
        test_args = [test_path]
    if args.pytest_args:
        test_args.extend(args.pytest_args)

    print(f"[cintent_v3:ci] Tracing project: {project_root}")
    print(f"[cintent_v3:ci] Output directory: {output_dir}")
    print(f"[cintent_v3:ci] Running: pytest {' '.join(test_args)}")

    exit_code = pytest.main(test_args, plugins=[plugin])

    # Collect metadata and package artifact
    metadata = collect_metadata(project_root)
    metadata["tracing_seconds"] = plugin._start_time  # Will be replaced with actual stats
    metadata["exit_status"] = exit_code
    tracer_stats = tracer.get_stats()

    artifact_path = args.output or os.path.join(output_dir, "cintent_v3_artifact.zip")
    package_artifact(
        callgraph_path=cg_path,
        metadata=metadata,
        output_path=artifact_path,
        stats=tracer_stats,
    )

    print(f"\n[cintent_v3:ci] Artifact packaged: {artifact_path}")
    print(f"[cintent_v3:ci] Upload this file as a CI artifact for offline evaluation.")

    sys.exit(exit_code)


def cmd_ci_evaluate(args: argparse.Namespace) -> None:
    """Evaluate a CI artifact against ground truth.

    Accepts:
    - A cintent_v3 zip artifact (callgraph.json + metadata.json)
    - A plain call graph JSON file
    - An existing cintent_improved zip (speedscope-based)

    Pipeline:
    1. Parse artifact → extract call graph
    2. Resolve CI paths (strip workspace prefix)
    3. Normalize (module aliases, function aliases)
    4. Evaluate against ground truth
    """
    from cintent_v3.ci import parse_artifact, resolve_ci_paths

    if not os.path.isfile(args.artifact):
        print(f"Error: Artifact '{args.artifact}' not found.", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(args.ground_truth):
        print(f"Error: Ground truth '{args.ground_truth}' not found.", file=sys.stderr)
        sys.exit(1)

    # Step 1: Parse artifact
    print("[cintent_v3:ci] Step 1/4: Parsing artifact...")
    call_graph, metadata, stats = parse_artifact(args.artifact)

    if metadata:
        print(f"  Tool:       {metadata.get('tool', 'unknown')}")
        print(f"  Workspace:  {metadata.get('workspace', 'unknown')}")
        print(f"  Timestamp:  {metadata.get('timestamp', 'unknown')}")
        if metadata.get("ci_system"):
            print(f"  CI System:  {metadata['ci_system']}")
    if stats:
        print(f"  Unique edges: {stats.get('unique_edges', '?')}")
        print(f"  Total calls:  {stats.get('total_calls', '?')}")

    raw_callers = len(call_graph)
    raw_edges = sum(len(v) for v in call_graph.values())
    print(f"  Raw graph: {raw_callers} callers, {raw_edges} edges")

    # Step 2: Resolve CI paths
    print("\n[cintent_v3:ci] Step 2/4: Resolving CI paths...")
    workspace = None
    if metadata and metadata.get("workspace"):
        workspace = metadata["workspace"]
    if args.workspace:
        workspace = args.workspace

    call_graph = resolve_ci_paths(call_graph, workspace)
    resolved_callers = len(call_graph)
    resolved_edges = sum(len(v) for v in call_graph.values())
    print(f"  Resolved graph: {resolved_callers} callers, {resolved_edges} edges")

    # Step 3: Normalize
    print("\n[cintent_v3:ci] Step 3/4: Normalizing...")
    normalized = normalize_call_graph(call_graph)
    norm_callers = len(normalized)
    norm_edges = sum(len(v) for v in normalized.values())
    print(f"  Normalized graph: {norm_callers} callers, {norm_edges} edges")

    # Optionally write intermediate results
    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
        export_call_graph(
            call_graph,
            os.path.join(args.output_dir, "step2_resolved.json"),
        )
        export_call_graph(
            normalized,
            os.path.join(args.output_dir, "step3_normalized.json"),
        )

    # Step 4: Evaluate
    print("\n[cintent_v3:ci] Step 4/4: Evaluating against ground truth...")

    # Save normalized to temp file for evaluate_from_files
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump(normalized, tmp)
        tmp_path = tmp.name

    try:
        project_packages = set(args.packages) if args.packages else None

        result = evaluate_from_files(
            tool_path=tmp_path,
            ground_truth_path=args.ground_truth,
            project_packages=project_packages,
            project_only=not args.all_edges,
            both_project=args.both_project,
            caller_project=args.caller_project,
            overlap_only=args.overlap_only,
            output_path=args.output,
        )

        print(result.summary())

        if args.verbose:
            print_edge_diff(result, max_edges=args.max_diff)

        if args.output:
            print(f"\nDetailed results written to: {args.output}")
    finally:
        os.unlink(tmp_path)


def _get_workflow_snippet(project_root: str) -> str:
    """Generate a GitHub Actions workflow snippet for cintent_v3."""
    return """    # --- cintent_v3: Deterministic Call Graph Tracing ---
    - name: Install cintent_v3
      run: pip install cintent_v3  # or: pip install git+https://github.com/YOUR_USERNAME/cintent_v3.git

    - name: Run tests with call graph tracing
      run: python -m cintent_v3 ci collect . -o cintent_v3_artifact.zip

    - name: Upload call graph artifact
      uses: actions/upload-artifact@v4
      if: always()
      with:
        name: cintent_v3-logs
        path: |
          cintent_v3_artifact.zip
          cintent_v3_callgraph.json
          cintent_v3_metadata.json"""


def _get_full_workflow(project_root: str) -> str:
    """Generate a complete GitHub Actions workflow for cintent_v3."""
    return """# cintent_v3: Deterministic Call Graph Tracing for CI
# Generated by: python -m cintent_v3 ci setup <project>
#
# This workflow runs your test suite with deterministic call graph tracing
# using sys.setprofile(). Unlike sampling-based profilers (pyinstrument),
# this captures EVERY function call — no sampling gap.
#
# Download the artifact and evaluate locally:
#   python -m cintent_v3 ci evaluate cintent_v3_artifact.zip -g ground_truth.json

name: cintent_v3 Call Graph

on:
  push:
    branches: [ main, master ]
  pull_request:
    branches: [ main, master ]

jobs:
  trace:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.11", "3.12", "3.13"]

    steps:
    - uses: actions/checkout@v4

    - name: Set up Python ${{ matrix.python-version }}
      uses: actions/setup-python@v5
      with:
        python-version: ${{ matrix.python-version }}

    - name: Install dependencies
      run: |
        python -m pip install --upgrade pip
        pip install -e .
        pip install pytest

    - name: Install cintent_v3
      run: pip install cintent_v3

    - name: Run tests with call graph tracing
      run: python -m cintent_v3 ci collect . -o cintent_v3_artifact.zip
      continue-on-error: true

    - name: Upload call graph artifact
      uses: actions/upload-artifact@v4
      if: always()
      with:
        name: cintent_v3-logs-py${{ matrix.python-version }}
        path: |
          cintent_v3_artifact.zip
          cintent_v3_callgraph.json
          cintent_v3_metadata.json
          cintent_v3_stats.json
"""


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="cintent_v3",
        description="Adaptive Deterministic Call Graph Tracer for CI Environments",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- trace ---
    trace_parser = subparsers.add_parser(
        "trace",
        help="Run tests with deterministic call graph tracing",
    )
    trace_parser.add_argument(
        "project_root",
        help="Path to the project root directory",
    )
    trace_parser.add_argument(
        "-o", "--output",
        help="Output path for the call graph JSON (default: <project>/cintent_v3_callgraph.json)",
    )
    trace_parser.add_argument(
        "-m", "--method",
        choices=["plugin", "conftest", "subprocess"],
        default="plugin",
        help="Tracing method (default: plugin)",
    )
    trace_parser.add_argument(
        "-t", "--test-path",
        help="Specific test path to run (default: project root)",
    )
    trace_parser.add_argument(
        "--record-external",
        action="store_true",
        help="Also record edges between external modules",
    )
    trace_parser.add_argument(
        "--conftest-dir",
        help="Directory for generated conftest (conftest method only)",
    )
    trace_parser.add_argument(
        "pytest_args",
        nargs="*",
        help="Additional arguments to pass to pytest",
    )

    # --- evaluate ---
    eval_parser = subparsers.add_parser(
        "evaluate",
        help="Compare a call graph against ground truth",
    )
    eval_parser.add_argument(
        "tool_graph",
        help="Path to the tool's call graph JSON",
    )
    eval_parser.add_argument(
        "--ground-truth", "-g",
        required=True,
        help="Path to the ground truth call graph JSON (e.g., DynaPyt output)",
    )
    eval_parser.add_argument(
        "-o", "--output",
        help="Path to write detailed results JSON",
    )
    eval_parser.add_argument(
        "-p", "--packages",
        nargs="+",
        help="Project package names (auto-detected if omitted)",
    )
    eval_parser.add_argument(
        "--all-edges",
        action="store_true",
        help="Compare all edges, not just project-related ones",
    )
    eval_parser.add_argument(
        "--both-project",
        action="store_true",
        help="Only compare edges where BOTH caller and callee are project code",
    )
    eval_parser.add_argument(
        "--caller-project",
        action="store_true",
        help="Only compare edges where the CALLER is project code (matches DynaPyt output structure)",
    )
    eval_parser.add_argument(
        "--overlap-only",
        action="store_true",
        help="Only evaluate on callers present in BOTH tool and ground truth. "
             "Fair evaluation when ground truth has incomplete caller coverage.",
    )
    eval_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print detailed edge diff",
    )
    eval_parser.add_argument(
        "--max-diff",
        type=int,
        default=20,
        help="Max edges to print in diff (default: 20)",
    )

    # --- normalize ---
    norm_parser = subparsers.add_parser(
        "normalize",
        help="Normalize a call graph JSON file",
    )
    norm_parser.add_argument(
        "input",
        help="Path to the call graph JSON file",
    )
    norm_parser.add_argument(
        "-o", "--output",
        help="Output path (default: <input>_normalized.json)",
    )

    # --- generate-conftest ---
    conftest_parser = subparsers.add_parser(
        "generate-conftest",
        help="Generate conftest.py for automatic pytest tracing",
    )
    conftest_parser.add_argument(
        "project_root",
        help="Path to the project root directory",
    )
    conftest_parser.add_argument(
        "--output-dir",
        help="Directory to write conftest.py (default: project root)",
    )

    # --- stats ---
    stats_parser = subparsers.add_parser(
        "stats",
        help="Print statistics about a call graph",
    )
    stats_parser.add_argument(
        "input",
        help="Path to the call graph JSON file",
    )
    stats_parser.add_argument(
        "-n", "--normalize",
        action="store_true",
        help="Also show stats after normalization",
    )

    # --- ci (with subcommands) ---
    ci_parser = subparsers.add_parser(
        "ci",
        help="CI integration commands (setup, collect, evaluate)",
    )
    ci_subparsers = ci_parser.add_subparsers(dest="ci_command", required=True)

    # ci setup
    ci_setup_parser = ci_subparsers.add_parser(
        "setup",
        help="Prepare a project for CI tracing",
    )
    ci_setup_parser.add_argument(
        "project_root",
        help="Path to the project root directory",
    )
    ci_setup_parser.add_argument(
        "--output-dir",
        help="Directory to write conftest.py (default: project root)",
    )
    ci_setup_parser.add_argument(
        "--workflow-file",
        help="Write a complete GitHub Actions workflow (e.g., 'cintent_v3.yaml')",
    )

    # ci collect
    ci_collect_parser = ci_subparsers.add_parser(
        "collect",
        help="Run tracing and package as a CI artifact",
    )
    ci_collect_parser.add_argument(
        "project_root",
        help="Path to the project root directory",
    )
    ci_collect_parser.add_argument(
        "-o", "--output",
        help="Output path for the artifact zip (default: cintent_v3_artifact.zip)",
    )
    ci_collect_parser.add_argument(
        "--output-dir",
        help="Directory for intermediate files (default: project root or CINTENT_V3_OUTPUT)",
    )
    ci_collect_parser.add_argument(
        "-t", "--test-path",
        help="Specific test path to run",
    )
    ci_collect_parser.add_argument(
        "--record-external",
        action="store_true",
        help="Also record edges between external modules",
    )
    ci_collect_parser.add_argument(
        "pytest_args",
        nargs="*",
        help="Additional arguments to pass to pytest",
    )

    # ci evaluate
    ci_eval_parser = ci_subparsers.add_parser(
        "evaluate",
        help="Evaluate a CI artifact against ground truth",
    )
    ci_eval_parser.add_argument(
        "artifact",
        help="Path to the CI artifact (zip or JSON)",
    )
    ci_eval_parser.add_argument(
        "--ground-truth", "-g",
        required=True,
        help="Path to the ground truth call graph JSON",
    )
    ci_eval_parser.add_argument(
        "-o", "--output",
        help="Path to write detailed results JSON",
    )
    ci_eval_parser.add_argument(
        "--output-dir",
        help="Directory to write intermediate step results",
    )
    ci_eval_parser.add_argument(
        "--workspace",
        help="CI workspace path to strip (auto-detected from metadata if omitted)",
    )
    ci_eval_parser.add_argument(
        "-p", "--packages",
        nargs="+",
        help="Project package names (auto-detected if omitted)",
    )
    ci_eval_parser.add_argument(
        "--all-edges",
        action="store_true",
        help="Compare all edges, not just project-related ones",
    )
    ci_eval_parser.add_argument(
        "--both-project",
        action="store_true",
        help="Only compare edges where BOTH caller and callee are project code",
    )
    ci_eval_parser.add_argument(
        "--caller-project",
        action="store_true",
        help="Only compare edges where the CALLER is project code",
    )
    ci_eval_parser.add_argument(
        "--overlap-only",
        action="store_true",
        help="Only evaluate on callers present in BOTH tool and ground truth",
    )
    ci_eval_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print detailed edge diff",
    )
    ci_eval_parser.add_argument(
        "--max-diff",
        type=int,
        default=20,
        help="Max edges to print in diff (default: 20)",
    )

    return parser


def main() -> None:
    """Entry point."""
    parser = build_parser()
    args = parser.parse_args()

    match args.command:
        case "trace":
            cmd_trace(args)
        case "evaluate":
            cmd_evaluate(args)
        case "normalize":
            cmd_normalize(args)
        case "generate-conftest":
            cmd_generate_conftest(args)
        case "stats":
            cmd_stats(args)
        case "ci":
            match args.ci_command:
                case "setup":
                    cmd_ci_setup(args)
                case "collect":
                    cmd_ci_collect(args)
                case "evaluate":
                    cmd_ci_evaluate(args)


if __name__ == "__main__":
    main()
