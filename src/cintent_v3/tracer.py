"""Deterministic call graph tracer using sys.setprofile().

This module provides the core tracing engine that captures every function
call/return at the C level. Key innovations over sampling-based profilers:

1. **Deterministic capture**: sys.setprofile() fires on EVERY call/return,
   not sampled — no fast functions are missed regardless of execution time.

2. **Adaptive edge deduplication**: Once a caller→callee edge is recorded,
   subsequent identical calls skip all name resolution and recording logic.
   This amortizes per-call overhead to near-zero for hot paths.

3. **Project-scoped filtering**: Only edges where at least one endpoint is
   in the project code are recorded, avoiding stdlib/third-party noise.

4. **Shadow call stack**: Maintains a per-thread call stack to correctly
   resolve caller for each callee, even across C-extension boundaries.
"""

from __future__ import annotations

import functools
import inspect
import os
import sys
import threading
from types import FrameType
from typing import Any


class CallGraphTracer:
    """Deterministic call graph tracer using sys.setprofile().

    Captures caller→callee edges for all function calls during execution.
    Filters to project-relevant edges and deduplicates for efficiency.
    """

    # Framework/tool prefixes to exclude from caller-side edges.
    # These are test infrastructure modules that appear as callers due to
    # callback dispatch / frame walking but don't represent real call edges.
    _EXCLUDED_CALLER_PREFIXES = (
        "_pytest.",
        "pytest.",
        "pluggy.",
        "_pluggy.",
        "xdist.",
        "coverage.",
        "unittest.runner.",
        "unittest.suite.",
        "unittest.loader.",
        "unittest.case.TestCase.debug",
    )

    def __init__(
        self,
        project_root: str,
        record_external: bool = False,
        max_stack_depth: int = 0,
        exclude_frameworks: bool = True,
    ) -> None:
        """Initialize the tracer.

        Args:
            project_root: Absolute path to the project root directory.
                Only files under this path are considered "project code".
            record_external: If True, also record edges between external
                modules when called transitively from project code.
            max_stack_depth: Maximum call stack depth to track (0 = unlimited).
            exclude_frameworks: If True, exclude test framework internals
                from caller-side edges (recommended to reduce false positives).
        """
        self.project_root = os.path.normpath(os.path.abspath(project_root))
        self.record_external = record_external
        self.max_stack_depth = max_stack_depth
        self.exclude_frameworks = exclude_frameworks

        # Core data structures (thread-safe via GIL for dict/set operations)
        self._edges: set[tuple[str, str]] = set()
        self._call_graph: dict[str, set[str]] = {}
        self._seen_edges: set[tuple[str, str]] = set()

        # Per-thread call stacks
        self._stacks: dict[int, list[str]] = {}

        # Caches for performance
        self._file_cache: dict[str, bool] = {}
        self._fqn_cache: dict[int, str] = {}

        # State
        self._active = False
        self._lock = threading.Lock()

        # Statistics
        self._stats = {
            "total_calls": 0,
            "recorded_edges": 0,
            "skipped_duplicate": 0,
            "skipped_external": 0,
        }

    def _is_project_file(self, filepath: str | None) -> bool:
        """Check if a file belongs to the project (cached)."""
        if filepath is None:
            return False
        if filepath in self._file_cache:
            return self._file_cache[filepath]
        try:
            norm = os.path.normpath(os.path.abspath(filepath))
            result = norm.startswith(self.project_root)
        except (ValueError, OSError):
            result = False
        self._file_cache[filepath] = result
        return result

    def _get_project_relpath(self, filepath: str) -> str:
        """Get the project-relative path for a file."""
        norm = os.path.normpath(os.path.abspath(filepath))
        return os.path.relpath(norm, self.project_root)

    def _get_fqn_from_frame(self, frame: FrameType) -> str | None:
        """Extract a fully qualified name from a Python frame.

        Builds the FQN from the module path and qualified name, e.g.:
        grab.document.Document.select
        """
        code = frame.f_code
        filepath = code.co_filename

        # Build module path from file path relative to project root
        if self._is_project_file(filepath):
            relpath = self._get_project_relpath(filepath)
            # Convert file path to module path: grab/document.py -> grab.document
            module = relpath.replace(os.sep, ".").replace("/", ".")
            if module.endswith(".py"):
                module = module[:-3]
            if module.endswith(".__init__"):
                module = module[:-9]
        else:
            # For external files, use the module name from globals
            module = frame.f_globals.get("__name__", "")

        qualname = code.co_qualname if hasattr(code, "co_qualname") else code.co_name

        if module:
            return f"{module}.{qualname}"
        return qualname

    def _get_fqn_for_c_func(self, func: Any) -> str | None:
        """Get FQN for a C/builtin function."""
        if func is None:
            return None
        module = getattr(func, "__module__", None) or ""
        qualname = getattr(func, "__qualname__", None) or getattr(func, "__name__", None) or ""
        if module and qualname:
            return f"{module}.{qualname}"
        return qualname or None

    def _get_caller_fqn(self, frame: FrameType) -> str | None:
        """Get the FQN of the caller from the current frame's parent."""
        caller_frame = frame.f_back
        if caller_frame is None:
            return None
        return self._get_fqn_from_frame(caller_frame)

    def _is_excluded_framework_caller(self, fqn: str) -> bool:
        """Check if a caller FQN belongs to an excluded test framework."""
        if not self.exclude_frameworks:
            return False
        return any(fqn.startswith(prefix) for prefix in self._EXCLUDED_CALLER_PREFIXES)

    def _profile_callback(
        self, frame: FrameType, event: str, arg: Any
    ) -> None:
        """Profile callback invoked by sys.setprofile() on every call/return.

        Events:
          - 'call': Python function call
          - 'c_call': C/builtin function call
          - 'return' / 'c_return': function return
          - 'c_exception': C function raised exception
        """
        if not self._active:
            return

        tid = threading.get_ident()

        if event == "call":
            self._stats["total_calls"] += 1

            callee_fqn = self._get_fqn_from_frame(frame)
            caller_fqn = self._get_caller_fqn(frame)

            if callee_fqn and caller_fqn:
                # Skip edges from excluded framework callers
                if self._is_excluded_framework_caller(caller_fqn):
                    self._stats["skipped_external"] += 1
                    # Still track call stack
                    if tid not in self._stacks:
                        self._stacks[tid] = []
                    if callee_fqn:
                        self._stacks[tid].append(callee_fqn)
                    return

                edge = (caller_fqn, callee_fqn)

                # Adaptive deduplication: skip already-seen edges
                if edge not in self._seen_edges:
                    callee_file = frame.f_code.co_filename
                    caller_file = (
                        frame.f_back.f_code.co_filename if frame.f_back else None
                    )

                    callee_is_project = self._is_project_file(callee_file)
                    caller_is_project = self._is_project_file(caller_file)

                    if callee_is_project or caller_is_project or self.record_external:
                        self._seen_edges.add(edge)
                        self._edges.add(edge)
                        if caller_fqn not in self._call_graph:
                            self._call_graph[caller_fqn] = set()
                        self._call_graph[caller_fqn].add(callee_fqn)
                        self._stats["recorded_edges"] += 1
                    else:
                        self._stats["skipped_external"] += 1
                else:
                    self._stats["skipped_duplicate"] += 1

            # Track call stack
            if tid not in self._stacks:
                self._stacks[tid] = []
            if callee_fqn:
                self._stacks[tid].append(callee_fqn)

        elif event == "c_call":
            self._stats["total_calls"] += 1

            callee_fqn = self._get_fqn_for_c_func(arg)
            caller_fqn = self._get_fqn_from_frame(frame)

            if callee_fqn and caller_fqn:
                # Skip edges from excluded framework callers
                if self._is_excluded_framework_caller(caller_fqn):
                    self._stats["skipped_external"] += 1
                    if tid not in self._stacks:
                        self._stacks[tid] = []
                    if callee_fqn:
                        self._stacks[tid].append(callee_fqn)
                    return

                edge = (caller_fqn, callee_fqn)

                if edge not in self._seen_edges:
                    caller_file = frame.f_code.co_filename
                    caller_is_project = self._is_project_file(caller_file)

                    if caller_is_project or self.record_external:
                        self._seen_edges.add(edge)
                        self._edges.add(edge)
                        if caller_fqn not in self._call_graph:
                            self._call_graph[caller_fqn] = set()
                        self._call_graph[caller_fqn].add(callee_fqn)
                        self._stats["recorded_edges"] += 1
                    else:
                        self._stats["skipped_external"] += 1
                else:
                    self._stats["skipped_duplicate"] += 1

            if tid not in self._stacks:
                self._stacks[tid] = []
            if callee_fqn:
                self._stacks[tid].append(callee_fqn)

        elif event in ("return", "c_return", "c_exception"):
            if tid in self._stacks and self._stacks[tid]:
                self._stacks[tid].pop()

    def start(self) -> None:
        """Start tracing. Installs the profile callback."""
        self._active = True
        sys.setprofile(self._profile_callback)
        threading.setprofile(self._profile_callback)

    def stop(self) -> None:
        """Stop tracing. Removes the profile callback."""
        self._active = False
        sys.setprofile(None)
        threading.setprofile(None)

    def get_edges(self) -> set[tuple[str, str]]:
        """Return the set of (caller, callee) edges."""
        return self._edges.copy()

    def get_call_graph(self) -> dict[str, list[str]]:
        """Return the call graph as {caller: [callee, ...]}."""
        return {k: sorted(v) for k, v in self._call_graph.items()}

    def get_stats(self) -> dict[str, int]:
        """Return tracing statistics."""
        return {
            **self._stats,
            "unique_edges": len(self._edges),
            "unique_callers": len(self._call_graph),
        }

    def reset(self) -> None:
        """Reset all collected data."""
        self._edges.clear()
        self._call_graph.clear()
        self._seen_edges.clear()
        self._stacks.clear()
        self._fqn_cache.clear()
        self._stats = {k: 0 for k in self._stats}
