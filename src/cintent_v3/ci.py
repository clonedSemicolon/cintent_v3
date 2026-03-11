"""CI integration module for cintent_v3.

Handles:
- Metadata collection (git info, CI environment, timing)
- Artifact packaging (call graph + metadata as zip)
- Artifact parsing (extract call graph from zip or plain JSON)
- CI-specific evaluation pipeline
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any
from zipfile import ZipFile, ZIP_DEFLATED


# ─── Metadata Collection ─────────────────────────────────────────────────────


def collect_metadata(project_root: str) -> dict[str, Any]:
    """Collect metadata about the current environment.

    Gathers:
    - Git info: repository, branch, commit SHA
    - CI info: detected CI system, run ID, job ID
    - Environment: Python version, OS, workspace path
    - Timing: timestamp
    """
    meta: dict[str, Any] = {
        "tool": "cintent_v3",
        "tool_version": "0.1.0",
        "python_version": sys.version,
        "platform": sys.platform,
        "workspace": os.path.abspath(project_root),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Git info
    meta.update(_collect_git_info(project_root))

    # CI environment
    meta.update(_detect_ci_environment())

    return meta


def _collect_git_info(project_root: str) -> dict[str, str]:
    """Collect git repository information."""
    info: dict[str, str] = {}

    git_commands = {
        "commit_sha": ["git", "rev-parse", "HEAD"],
        "branch": ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        "repository": ["git", "config", "--get", "remote.origin.url"],
    }

    for key, cmd in git_commands.items():
        try:
            result = subprocess.run(
                cmd,
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                info[key] = result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    return info


def _detect_ci_environment() -> dict[str, str]:
    """Detect CI environment from environment variables."""
    ci_info: dict[str, str] = {}

    if os.environ.get("GITHUB_ACTIONS") == "true":
        ci_info["ci_system"] = "github_actions"
        for env_key, meta_key in [
            ("GITHUB_REPOSITORY", "ci_repository"),
            ("GITHUB_SHA", "ci_commit"),
            ("GITHUB_REF", "ci_ref"),
            ("GITHUB_RUN_ID", "ci_run_id"),
            ("GITHUB_JOB", "ci_job"),
            ("GITHUB_WORKFLOW", "ci_workflow"),
            ("GITHUB_WORKSPACE", "ci_workspace"),
        ]:
            val = os.environ.get(env_key)
            if val:
                ci_info[meta_key] = val
    elif os.environ.get("CI") == "true":
        ci_info["ci_system"] = "generic"
    else:
        ci_info["ci_system"] = "local"

    return ci_info


# ─── Artifact Packaging ──────────────────────────────────────────────────────


def package_artifact(
    callgraph_path: str,
    metadata: dict[str, Any],
    output_path: str,
    stats: dict[str, Any] | None = None,
) -> str:
    """Package call graph + metadata into a zip artifact for CI upload.

    Creates a zip containing:
    - callgraph.json   : the raw call graph
    - metadata.json    : environment + git + CI info
    - stats.json       : optional tracing statistics

    Args:
        callgraph_path: Path to the call graph JSON file.
        metadata: Metadata dictionary from collect_metadata().
        output_path: Path for the output zip file.
        stats: Optional tracing statistics.

    Returns:
        Path to the created zip file.
    """
    if not output_path.endswith(".zip"):
        output_path += ".zip"

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    with ZipFile(output_path, "w", compression=ZIP_DEFLATED) as zf:
        zf.write(callgraph_path, "callgraph.json")
        zf.writestr(
            "metadata.json",
            json.dumps(metadata, indent=2, default=str),
        )
        if stats:
            zf.writestr(
                "stats.json",
                json.dumps(stats, indent=2, default=str),
            )

    return output_path


# ─── Artifact Parsing ────────────────────────────────────────────────────────


def parse_artifact(artifact_path: str) -> tuple[dict, dict | None, dict | None]:
    """Parse a cintent_v3 CI artifact.

    Accepts:
    - A zip file containing callgraph.json + metadata.json
    - A plain JSON call graph file

    Returns:
        (call_graph, metadata, stats) — metadata and stats may be None.
    """
    if artifact_path.endswith(".zip"):
        return _parse_zip_artifact(artifact_path)
    elif artifact_path.endswith(".json"):
        return _parse_json_artifact(artifact_path)
    else:
        # Try JSON first, then zip
        try:
            return _parse_json_artifact(artifact_path)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _parse_zip_artifact(artifact_path)


def _parse_zip_artifact(
    zip_path: str,
) -> tuple[dict, dict | None, dict | None]:
    """Parse a cintent_v3 zip artifact."""
    with ZipFile(zip_path, "r") as zf:
        callgraph_data = json.loads(zf.read("callgraph.json"))

        metadata = None
        if "metadata.json" in zf.namelist():
            metadata = json.loads(zf.read("metadata.json"))

        stats = None
        if "stats.json" in zf.namelist():
            stats = json.loads(zf.read("stats.json"))

    return callgraph_data, metadata, stats


def _parse_json_artifact(json_path: str) -> tuple[dict, dict | None, dict | None]:
    """Parse a plain JSON call graph file."""
    with open(json_path, "r", encoding="utf-8") as f:
        callgraph_data = json.load(f)
    return callgraph_data, None, None


# ─── CI Path Resolution ──────────────────────────────────────────────────────


def resolve_ci_paths(
    call_graph: dict[str, list[str]],
    workspace_prefix: str | None = None,
) -> dict[str, list[str]]:
    """Resolve CI-specific paths in function names.

    In CI environments, function FQNs may contain absolute paths
    like /home/runner/work/repo/repo/module.func. This strips the
    workspace prefix to produce clean module.func names.

    Args:
        call_graph: The raw call graph.
        workspace_prefix: CI workspace path to strip. Auto-detected if None.

    Returns:
        Call graph with cleaned FQN paths.
    """
    if workspace_prefix is None:
        # Try to auto-detect from FQN patterns
        workspace_prefix = _detect_workspace_prefix(call_graph)

    if not workspace_prefix:
        return call_graph

    # Ensure prefix ends with separator
    if not workspace_prefix.endswith(("/", "\\")):
        workspace_prefix += "/"

    resolved: dict[str, list[str]] = {}
    for caller, callees in call_graph.items():
        clean_caller = _strip_prefix(caller, workspace_prefix)
        clean_callees = [_strip_prefix(c, workspace_prefix) for c in callees]
        resolved[clean_caller] = clean_callees

    return resolved


def _detect_workspace_prefix(call_graph: dict[str, list[str]]) -> str | None:
    """Auto-detect CI workspace prefix from function names."""
    import re

    # Common CI workspace patterns
    patterns = [
        r"(/home/runner/work/[^/]+/[^/]+/)",    # GitHub Actions
        r"(/github/workspace/)",                  # GitHub Actions (container)
        r"(D:\\a\\[^\\]+\\[^\\]+\\)",            # GitHub Actions (Windows)
    ]

    all_names = list(call_graph.keys())
    for callees in call_graph.values():
        all_names.extend(callees)

    for pattern in patterns:
        for name in all_names[:100]:  # Check first 100 for speed
            m = re.search(pattern, name)
            if m:
                return m.group(1)

    return None


def _strip_prefix(name: str, prefix: str) -> str:
    """Strip workspace prefix from a function name, converting path to module."""
    if prefix in name:
        # Find where the prefix appears and strip it
        idx = name.index(prefix)
        name = name[:idx] + name[idx + len(prefix) :]

    return name
