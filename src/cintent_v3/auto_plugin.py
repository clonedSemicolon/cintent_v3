"""Auto-loading pytest plugin for cintent_v3.

This module is loaded via pytest11 entry point (registered in pyproject.toml).
Tracing activates only when CINTENT_V3_ENABLED=1.

Gracefully skips on Python < 3.10 (cintent_v3 core requires 3.10+).
"""

import os
import sys

_tracer = None
_start_time = None


def _is_enabled():
    """Check env var at call time, not import time."""
    return os.environ.get("CINTENT_V3_ENABLED", "0") == "1"


def _is_supported():
    return sys.version_info >= (3, 10)


def pytest_configure(config):
    """Start tracing at the earliest pytest hook if enabled."""
    global _tracer, _start_time
    if not _is_enabled():
        print("[cintent_v3] Plugin loaded but CINTENT_V3_ENABLED != 1, skipping.")
        return
    if not _is_supported():
        print(f"[cintent_v3] Skipping: requires Python >= 3.10 (current: {sys.version})")
        return

    try:
        import time
        from cintent_v3.tracer import CallGraphTracer

        project_root = os.environ.get("CINTENT_V3_PROJECT_ROOT", os.getcwd())
        _tracer = CallGraphTracer(
            project_root=project_root,
            record_external=False,
        )
        _start_time = time.time()
        _tracer.start()
        print(f"[cintent_v3] Tracing started for: {project_root}")
    except Exception as exc:
        print(f"[cintent_v3] ERROR: Failed to start tracer: {exc}")
        _tracer = None


def pytest_sessionfinish(session, exitstatus):
    """Stop tracing and export call graph + metadata + stats."""
    global _tracer, _start_time
    if _tracer is None:
        # No tracer running — write skip metadata if we're in CI so artifacts aren't empty
        if os.environ.get("GITHUB_ACTIONS") == "true" and _is_enabled():
            output_dir = os.environ.get("CINTENT_V3_OUTPUT", "")
            if output_dir:
                os.makedirs(output_dir, exist_ok=True)
                import json
                skip_meta = {
                    "tool": "cintent_v3",
                    "skipped": True,
                    "reason": "tracer_not_started",
                    "python_version": sys.version,
                }
                meta_path = os.path.join(output_dir, "cintent_v3_metadata.json")
                if not os.path.exists(meta_path):
                    with open(meta_path, "w", encoding="utf-8") as f:
                        json.dump(skip_meta, f, indent=2)
        return

    try:
        import json
        import subprocess
        import time
        from cintent_v3.callgraph import export_call_graph

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
                            "GITHUB_RUN_ID", "GITHUB_JOB", "GITHUB_WORKFLOW",
                            "GITHUB_RUN_NUMBER", "GITHUB_RUN_ATTEMPT",
                            "GITHUB_WORKFLOW_REF", "GITHUB_REF_NAME"]:
                val = os.environ.get(env_key)
                if val:
                    metadata[env_key.lower()] = val

        # Include matrix context and step ID from bash wrapper (setup-cintent compat)
        matrix_ctx = os.environ.get("CINTENT_MATRIX", "")
        if matrix_ctx:
            metadata["matrix"] = matrix_ctx
        step_id = os.environ.get("CINTENT_STEP_ID", "")
        if step_id:
            metadata["step_id"] = step_id
        nonblocking = os.environ.get("CINTENT_NONBLOCKING", "")
        if nonblocking:
            metadata["nonblocking"] = nonblocking

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
    except Exception as exc:
        print(f"[cintent_v3] ERROR during export: {exc}")
        import traceback
        traceback.print_exc()
