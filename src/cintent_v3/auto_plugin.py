"""Auto-loading pytest plugin for cintent_v3.

This module is registered as a pytest11 entry point. When the package is
installed, pytest auto-discovers it. Tracing activates only when
CINTENT_V3_ENABLED=1 is set in the environment (set by action.yml).

Output files are written to the working directory (or CINTENT_V3_OUTPUT).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from cintent_v3.callgraph import export_call_graph
from cintent_v3.tracer import CallGraphTracer

_ENABLED = os.environ.get("CINTENT_V3_ENABLED", "0") == "1"
_tracer: CallGraphTracer | None = None
_start_time: float | None = None


def pytest_configure(config):
    """Start tracing at the earliest pytest hook if enabled."""
    global _tracer, _start_time
    if not _ENABLED:
        return

    project_root = os.environ.get("CINTENT_V3_PROJECT_ROOT", os.getcwd())
    _tracer = CallGraphTracer(
        project_root=project_root,
        record_external=False,
    )
    _start_time = time.time()
    _tracer.start()
    print(f"[cintent_v3] Tracing started for: {project_root}")


def pytest_sessionfinish(session, exitstatus):
    """Stop tracing and export call graph + metadata + stats."""
    global _tracer, _start_time
    if _tracer is None:
        return

    _tracer.stop()
    elapsed = time.time() - _start_time if _start_time else 0

    output_dir = os.environ.get("CINTENT_V3_OUTPUT", os.getcwd())
    os.makedirs(output_dir, exist_ok=True)

    call_graph = _tracer.get_call_graph()
    stats = _tracer.get_stats()

    # Write call graph
    cg_path = os.path.join(output_dir, "cintent_v3_callgraph.json")
    export_call_graph(call_graph, cg_path)

    # Write metadata
    project_root = os.environ.get("CINTENT_V3_PROJECT_ROOT", os.getcwd())
    metadata = {
        "tool": "cintent_v3",
        "workspace": os.path.abspath(project_root),
        "python_version": sys.version,
        "platform": sys.platform,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tracing_seconds": round(elapsed, 2),
        "exit_status": exitstatus,
    }

    for key, cmd in [("commit_sha", "git rev-parse HEAD"),
                     ("branch", "git rev-parse --abbrev-ref HEAD"),
                     ("repository", "git config --get remote.origin.url")]:
        try:
            r = subprocess.run(cmd.split(), cwd=project_root,
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                metadata[key] = r.stdout.strip()
        except Exception:
            pass

    if os.environ.get("GITHUB_ACTIONS") == "true":
        metadata["ci_system"] = "github_actions"
        for env_key in ["GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_REF",
                        "GITHUB_RUN_ID", "GITHUB_JOB", "GITHUB_WORKFLOW"]:
            val = os.environ.get(env_key)
            if val:
                metadata[env_key.lower()] = val

    meta_path = os.path.join(output_dir, "cintent_v3_metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    stats_path = os.path.join(output_dir, "cintent_v3_stats.json")
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)

    print(f"\n[cintent_v3] Traced {stats['unique_edges']} unique edges "
          f"({stats['total_calls']} total calls, "
          f"{stats['skipped_duplicate']} deduplicated)")
    print(f"[cintent_v3] Tracing time: {elapsed:.1f}s")
    print(f"[cintent_v3] Call graph exported to: {cg_path}")
    print(f"[cintent_v3] Metadata exported to:   {meta_path}")
