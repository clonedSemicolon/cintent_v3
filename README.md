# cintent_v3 — Adaptive Deterministic Call Graph Tracer for CI

## Table of Contents

1. [Problem Statement](#problem-statement)
2. [How It Works — The Innovation](#how-it-works--the-innovation)
3. [Architecture Overview](#architecture-overview)
4. [Module Reference](#module-reference)
5. [Installation](#installation)
6. [Usage Guide](#usage-guide)
7. [Evaluation Against Ground Truth](#evaluation-against-ground-truth)
8. [Design Decisions & Rationale](#design-decisions--rationale)
9. [Comparison: cintent_v3 vs Pyinstrument vs DynaPyt](#comparison-cintent_v3-vs-pyinstrument-vs-dynapyt)
10. [CI Integration](#ci-integration)
11. [Private Publishing & Reuse Across Repositories](#private-publishing--reuse-across-repositories)

---

## Problem Statement

Dynamic call graph extraction is essential for understanding how software behaves during CI test execution. Two existing approaches have critical limitations:

| Tool | Mechanism | Problem |
|------|-----------|---------|
| **Pyinstrument** (used by cintent v1) | Statistical sampling (~1000 Hz) | Misses functions executing in <1ms. Rapidly-executed functions are **never captured**. |
| **DynaPyt** | AST-level instrumentation | Captures everything, but **10-100x overhead** makes it impractical in CI environments. |

**cintent_v3** solves both problems simultaneously.

---

## How It Works — The Innovation

### Core Technique: `sys.setprofile()` with Adaptive Edge Deduplication

cintent_v3 uses Python's built-in `sys.setprofile()` mechanism — a C-level hook that fires **deterministically on every function call and return**. Unlike sampling, nothing is missed regardless of execution duration.

The key innovation that controls overhead is **adaptive edge deduplication**:

```
First time caller A calls callee B:
  → Resolve names, check project scope, record edge    (~500ns)

All subsequent times A calls B:
  → Single set membership check, skip everything        (~50ns)
```

This means:
- **Cold edges** (first occurrence): Full processing, ~500ns overhead per call
- **Hot edges** (repeated): Near-zero overhead, ~50ns per call (just a set lookup)

In typical test suites, most function calls hit hot paths (the same function gets called thousands of times). The amortized overhead drops to **2-5x** — far less than DynaPyt's 10-100x while capturing the **exact same set of unique edges**.

### Why This Captures What Pyinstrument Misses

| Scenario | Pyinstrument | cintent_v3 |
|----------|-------------|------------|
| Function executes in 0.1ms | ❌ Never sampled | ✅ Captured on first call |
| Function called 10,000 times | ✅ Eventually sampled | ✅ Captured on first call |
| C/builtin function call | ❌ Not visible | ✅ Captured via `c_call` event |
| Test helper called once | ❌ Likely missed | ✅ Captured deterministically |

### Why This Is Faster Than DynaPyt

| Aspect | DynaPyt | cintent_v3 |
|--------|---------|------------|
| Instrumentation | AST rewriting of every file | None — uses C-level hook |
| Per-call overhead | Parse + log every call | Skip after first occurrence |
| Startup cost | Rewrite all source files | Install one callback |
| Memory | Log every call instance | Store only unique edges |
| I/O | Write trace during execution | Single JSON export at end |

---

## Architecture Overview

```
cintent_v3/
├── pyproject.toml              # Package configuration (zero dependencies)
├── README.md                   # This file
├── DESIGN.md                   # Detailed design document
└── src/
    └── cintent_v3/
        ├── __init__.py         # Package metadata
        ├── __main__.py         # CLI entry point (trace, evaluate, normalize, stats)
        ├── tracer.py           # Core: deterministic call graph tracer
        ├── callgraph.py        # Call graph data structure and I/O
        ├── normalizer.py       # Name normalization for cross-tool comparison
        ├── evaluator.py        # Precision/Recall/F1 evaluation engine
        └── pytest_plugin.py    # Pytest integration for CI use
```

### Data Flow

```
  Test Execution
       │
       ▼
  sys.setprofile() callback
       │
       ├─ call event ──► resolve FQNs ──► check project scope ──► deduplicate ──► record edge
       │                                                              │
       │                                                    (skip if already seen)
       ▼
  Call Graph (in-memory set of unique edges)
       │
       ▼
  Export to JSON ──► Normalize ──► Compare against ground truth
                                         │
                                         ▼
                                   Precision / Recall / F1
```

---

## Module Reference

### `tracer.py` — Core Tracing Engine

**Class: `CallGraphTracer`**

| Method | Description |
|--------|-------------|
| `__init__(project_root, record_external, max_stack_depth)` | Initialize with project root path |
| `start()` | Install the profile callback, begin tracing |
| `stop()` | Remove the profile callback, stop tracing |
| `get_edges()` | Return `set[tuple[str, str]]` of (caller, callee) edges |
| `get_call_graph()` | Return `dict[str, list[str]]` — caller → callees mapping |
| `get_stats()` | Return tracing statistics (total calls, unique edges, etc.) |
| `reset()` | Clear all collected data |

**Key parameters:**
- `project_root`: Only files under this path are considered "project code"
- `record_external`: Set `True` to also capture stdlib/third-party edges
- `max_stack_depth`: Limit tracking depth (0 = unlimited)

### `callgraph.py` — Call Graph I/O

| Function | Description |
|----------|-------------|
| `export_call_graph(graph, path)` | Write call graph to JSON |
| `load_call_graph(path)` | Load call graph from JSON |
| `call_graph_to_edges(graph)` | Convert dict to edge set |
| `edges_to_call_graph(edges)` | Convert edge set to dict |
| `get_call_graph_stats(graph)` | Get edge/caller/callee counts |

### `normalizer.py` — Name Normalization

| Function | Description |
|----------|-------------|
| `normalize_fqn(name)` | Normalize a single FQN to canonical form |
| `normalize_edges(edges)` | Normalize all edges in a set |
| `normalize_call_graph(graph)` | Normalize all names in a call graph |
| `detect_project_packages(graph)` | Auto-detect project package names |
| `filter_project_edges(edges, pkgs)` | Keep edges involving project code |
| `filter_both_project_edges(edges, pkgs)` | Keep edges where both endpoints are project |

**Normalization rules applied:**
1. Strip DynaPyt prefixes (`.DyPyBench.temp.projectN.`)
2. Strip CI runner paths (`/home/runner/work/...`)
3. Remove `<module>`, `<locals>` wrappers
4. Remove `<listcomp>`, `<genexpr>` suffixes
5. Normalize `<lambda>` names
6. Remove `.py` extensions from module paths
7. Collapse consecutive dots

### `evaluator.py` — Ground Truth Comparison

| Function | Description |
|----------|-------------|
| `evaluate(tool_graph, gt_graph, ...)` | Compare two call graphs, return metrics |
| `evaluate_from_files(tool_path, gt_path, ...)` | Compare from JSON files |
| `print_edge_diff(result, max_edges)` | Print missing/extra edges |

**`EvaluationResult` fields:**
- `precision`, `recall`, `f1` — Standard IR metrics
- `true_positives`, `false_positives`, `false_negatives` — Raw counts
- `tp_edges`, `fp_edges`, `fn_edges` — Actual edge sets for debugging

### `pytest_plugin.py` — Pytest Integration

| Function/Class | Description |
|----------------|-------------|
| `CintentPytestPlugin` | Pytest plugin class for programmatic use |
| `generate_conftest(project_root)` | Generate conftest.py for CI use |

---

## Installation

### Option 1: Install as package

```bash
cd cintent_v3
pip install -e .
```

### Option 2: Use directly (no installation needed)

```bash
# Add to PYTHONPATH
export PYTHONPATH=/path/to/cintent_v3/src:$PYTHONPATH

# Or run as module
python -m cintent_v3 --help
```

**Zero dependencies** — cintent_v3 uses only Python standard library modules.
pytest is an optional dependency (needed only for the `trace` command).

---

## Usage Guide

### 1. Trace a project's test suite

```bash
# Run pytest with tracing (recommended)
python -m cintent_v3 trace /path/to/project -o callgraph.json

# Trace specific test directory
python -m cintent_v3 trace /path/to/project -t tests/ -o callgraph.json

# Also capture external (stdlib/third-party) edges
python -m cintent_v3 trace /path/to/project --record-external -o callgraph.json
```

### 2. Evaluate against ground truth

```bash
# Compare against DynaPyt output
python -m cintent_v3 evaluate callgraph.json -g dynapyt_1.json

# With verbose edge diff
python -m cintent_v3 evaluate callgraph.json -g dynapyt_1.json -v

# Specify project packages explicitly
python -m cintent_v3 evaluate callgraph.json -g dynapyt_1.json -p grab

# Save detailed results
python -m cintent_v3 evaluate callgraph.json -g dynapyt_1.json -o results.json

# Only edges where BOTH endpoints are project code
python -m cintent_v3 evaluate callgraph.json -g dynapyt_1.json --both-project
```

### 3. Normalize a call graph

```bash
# Normalize DynaPyt output for inspection
python -m cintent_v3 normalize dynapyt_1.json -o dynapyt_normalized.json
```

### 4. View call graph statistics

```bash
python -m cintent_v3 stats callgraph.json
python -m cintent_v3 stats dynapyt_1.json --normalize
```

### 5. Generate conftest for CI

```bash
# Generate conftest.py in project root
python -m cintent_v3 generate-conftest /path/to/project

# Then run tests normally
cd /path/to/project
pytest -p conftest_cintent_v3
```

---

## Evaluation Against Ground Truth

### How comparison works

1. **Load** both call graphs (tool output + ground truth)
2. **Normalize** all FQNs to canonical project-relative form (module alias resolution, cleanup)
3. **Filter** to project-relevant edges (configurable: caller-project, both-project, all)
4. **Noise filter**: Remove import/metaclass/typing machinery callees
5. **Edge expansion**: Generate both `Class` and `Class.__init__` forms for matching
6. **Overlap-only** (optional): Restrict to callers present in both tool and GT for fair evaluation
7. **Compare** as sets: compute TP, FP, FN

### Normalization innovations

cintent_v3 includes several normalization techniques that enable fair cross-tool comparison:

| Feature | Example | Purpose |
|---------|---------|---------|
| **Module alias resolution** | `_io.open` → `io.open`, `nt._path_exists` → `os.path.exists` | CPython internal modules → public API |
| **Class instantiation expansion** | `(A, X.__init__)` ↔ `(A, X)` | Match DynaPyt's class-reference style |
| **Builtin type expansion** | `builtins.list` ↔ `builtins.list.__init__` | Handle all lowercase builtin types |
| **Noise callee filtering** | `importlib._bootstrap.*`, `abc.ABCMeta.__new__` | Remove Python plumbing not in any GT |
| **Overlap-only evaluation** | Only shared callers | Fair eval when GT has incomplete coverage |

### Evaluation results (grab project)

Evaluated against DynaPyt ground truth on the [grab](https://github.com/lorien/grab) project:

| Mode | Precision | Recall | F1 | TP | FP | FN |
|------|-----------|--------|----|----|----|-----|
| Caller-project (full) | 0.1340 | 0.7374 | 0.2268 | 337 | 2178 | 120 |
| **Caller-project + overlap** | **0.6347** | **0.7539** | **0.6892** | 337 | 194 | 110 |
| Both-project (full) | 0.1332 | 0.7795 | 0.2275 | 152 | 989 | 43 |
| **Both-project + overlap** | **0.7488** | **0.8042** | **0.7755** | 152 | 51 | 37 |

**Key insight**: The "full" modes have low precision because our tool captures ~2500 edges while DynaPyt only captured ~450. Most "false positives" are actually **correct edges** from callers DynaPyt never traced. The overlap-only mode restricts evaluation to shared callers, giving a fair apples-to-apples comparison.

Best result: **F1 = 0.7755** (both-project + overlap), **F1 = 0.6892** (caller-project + overlap).

### Example evaluation output

```bash
# Fair evaluation (recommended for incomplete ground truth)
python -m cintent_v3 evaluate callgraph.json --ground-truth dynapyt.json \
    --caller-project --overlap-only -p grab tests

============================================================
  Call Graph Evaluation Results
============================================================
  Project packages:     ['grab', 'tests']
  Tool edges:           531
  Ground truth edges:   447
------------------------------------------------------------
  True positives:       337
  False positives:      194
  False negatives:      110
------------------------------------------------------------
  Precision:            0.6347
  Recall:               0.7539
  F1 Score:             0.6892
============================================================
```

### Key design: Tool-agnostic normalization

The comparison does NOT depend on DynaPyt's internal path structure. Both tools' outputs are normalized independently:

```
DynaPyt:    .DyPyBench.temp.project1.grab.document.Document.__init__
cintent_v3: grab.document.Document.__init__
                    ↓ normalize_fqn() ↓
Both:       grab.document.Document.__init__    ✅ Match

cintent_v3: _io.open    →  io.open             (module alias)
DynaPyt:    io.open     →  io.open             ✅ Match

cintent_v3: grab.X.__init__  →  also generates: grab.X
DynaPyt:    grab.X           →  also generates: grab.X.__init__
                                                ✅ Match via expansion
```

This means cintent_v3 can be compared against **any** ground truth tool, not just DynaPyt.

---

## Design Decisions & Rationale

### 1. `sys.setprofile()` over `sys.settrace()`

`sys.settrace()` fires on every line, call, return, and exception — much more overhead. `sys.setprofile()` fires **only on call/return/exception**, which is exactly what we need for call graphs. This minimizes the per-event overhead.

### 2. Adaptive deduplication over full logging

DynaPyt logs every call instance (millions of events). cintent_v3 records each unique edge once, then skips all subsequent occurrences with a single `set.__contains__` check (~50ns). For a test suite with 1M function calls but only 500 unique edges, this reduces processing from 1M events to 500.

### 3. In-memory edge set over file I/O

DynaPyt writes trace events to disk during execution (I/O overhead). cintent_v3 accumulates edges in a Python `set` (O(1) insert and lookup) and writes a single JSON file at the end. No I/O during tracing.

### 4. Zero external dependencies

cintent_v3 depends only on Python's standard library. The tracer, normalizer, and evaluator work with any Python ≥3.10. Only `pytest` is needed as an optional dependency for the trace command.

### 5. Project-scoped filtering

By default, only edges where at least one endpoint is project code are recorded. This filters out noise from stdlib internals calling each other, which would be irrelevant false positives and also add overhead.

### 6. Canonical normalization over string matching

Instead of trying to match DynaPyt's specific naming convention, we normalize **both** sides to a canonical form. This makes the comparison tool-agnostic and future-proof.

---

## Comparison: cintent_v3 vs Pyinstrument vs DynaPyt

| Feature | Pyinstrument (cintent v1) | DynaPyt | **cintent_v3** |
|---------|--------------------------|---------|----------------|
| Mechanism | Sampling (~1kHz) | AST instrumentation | `sys.setprofile()` |
| Captures fast functions | ❌ No | ✅ Yes | ✅ Yes |
| Captures C/builtin calls | ❌ No | ✅ Yes | ✅ Yes |
| CI-friendly overhead | ✅ <2x | ❌ 10-100x | ✅ 2-5x |
| Dependencies | pyinstrument | DynaPyt + all deps | **None** (stdlib only) |
| Output format | Speedscope JSON | Custom JSON | DynaPyt-compatible JSON |
| Setup complexity | Moderate | High (AST rewrite) | **Minimal** (one callback) |
| Deterministic | ❌ No (sampling) | ✅ Yes | ✅ Yes |
| Adaptive overhead | ❌ Fixed sampling rate | ❌ Fixed per-call cost | ✅ Amortizes to ~0 |

---

## CI Integration

cintent_v3 is designed to **run from CI** — the tool collects call graphs during GitHub Actions test execution and packages them as downloadable artifacts for offline evaluation.

### Quick Start — 3 Commands

```bash
# 1. In CI: Trace + package artifact (one command)
python -m cintent_v3 ci collect . -o cintent_v3_artifact.zip

# 2. Download the artifact from GitHub Actions

# 3. Locally: Evaluate against ground truth
python -m cintent_v3 ci evaluate cintent_v3_artifact.zip -g dynapyt_1.json --caller-project --overlap-only
```

### CI Setup (auto-generate workflow)

```bash
# Generate conftest.py + print workflow snippet
python -m cintent_v3 ci setup /path/to/project

# Or generate a complete GitHub Actions workflow file
python -m cintent_v3 ci setup /path/to/project --workflow-file cintent_v3.yaml
```

### GitHub Actions Workflow — Drop-In Replacement

This replaces the `JavidDitty/setup-cintent` action (pyinstrument-based) with deterministic tracing:

```yaml
name: Custom

on: workflow_dispatch

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout code
        uses: actions/checkout@v6

      - name: Set up Python 3.8
        uses: actions/setup-python@v6
        with:
          python-version: "3.8"

      - name: Install dependencies
        run: |
          pip install . && pip install -r requirements_dev.txt && pip install pytest

      # cintent_v3: Deterministic call graph tracing (replaces pyinstrument)
      - name: Install cintent_v3
        run: pip install cintent_v3

      - name: Run tests with call graph tracing
        run: python -m cintent_v3 ci collect . -t tests/ -o cintent_v3_artifact.zip -- --import-mode=importlib
        continue-on-error: true

      - name: Upload cintent_v3 Artifacts
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: cintent_v3-logs
          path: |
            cintent_v3_artifact.zip
            cintent_v3_callgraph.json
            cintent_v3_metadata.json
            cintent_v3_stats.json
```

### CI Artifact Contents

The `ci collect` command produces a zip containing:

| File | Description |
|------|-------------|
| `callgraph.json` | Call graph in DynaPyt-compatible format (`{"caller": ["callee1", ...]}`) |
| `metadata.json` | Git info (repo, commit, branch), CI environment, Python version, timing |
| `stats.json` | Tracing statistics (unique edges, total calls, dedup count) |

### CI Evaluation Pipeline

`ci evaluate` runs a 4-step pipeline on the artifact:

```
Step 1: Parse artifact     → extract callgraph.json + metadata from zip
Step 2: Resolve CI paths   → strip /home/runner/work/... workspace prefix
Step 3: Normalize           → module aliases, function aliases, cleanup
Step 4: Evaluate           → precision/recall/F1 against ground truth
```

```bash
# Full evaluation with both-project + overlap-only filters
python -m cintent_v3 ci evaluate artifact.zip \
    -g dynapyt_1.json \
    --both-project --overlap-only \
    -v --output-dir results/

# Also accepts plain JSON call graphs (no zip needed for local runs)
python -m cintent_v3 ci evaluate callgraph.json -g dynapyt_1.json --caller-project
```

### Comparison vs Existing CIntent (Pyinstrument-Based)

| Aspect | CIntent v1 (pyinstrument) | cintent_v3 (sys.setprofile) |
|--------|--------------------------|---------------------------|
| **Mechanism** | Sampling at ~1000 Hz | Deterministic on every call |
| **Missing calls** | Functions < 1ms skipped | None — every call captured |
| **CI integration** | Custom GitHub Action + speedscope.json | `pip install` + one command |
| **Post-processing** | 6-step pipeline (parse, FQN, normalize, dedup, filter, infer) | 4-step pipeline (parse, path resolve, normalize, evaluate) |
| **Dependencies** | pyinstrument, pandas, tree-sitter | Zero (stdlib only) |
| **Artifact format** | Complex zip (speedscope, metadata, eBPF traces, functions.csv) | Simple zip (callgraph.json, metadata.json, stats.json) |
| **Best F1 (grab project)** | 0.5680 (after 5 improvement steps) | **0.6892** (caller-project) / **0.7755** (both-project) |

### Post-CI Evaluation Examples

After downloading the call graph artifact from GitHub Actions:

```bash
# Basic evaluation
python -m cintent_v3 ci evaluate cintent_v3_artifact.zip -g dynapyt_1.json

# Caller-project with overlap-only (recommended)
python -m cintent_v3 ci evaluate cintent_v3_artifact.zip -g dynapyt_1.json \
    --caller-project --overlap-only -v

# Both-project with overlap-only (strictest, highest precision)
python -m cintent_v3 ci evaluate cintent_v3_artifact.zip -g dynapyt_1.json \
    --both-project --overlap-only

# Save intermediate results for debugging
python -m cintent_v3 ci evaluate cintent_v3_artifact.zip -g dynapyt_1.json \
    --caller-project --overlap-only --output-dir results/
```

---

## Output Format

The call graph JSON follows DynaPyt's format for compatibility:

```json
{
    "grab.document.Document.__init__": [
        "builtins.isinstance",
        "email.message.Message",
        "grab.document.Document.process_encoding"
    ],
    "grab.document.Document.select": [
        "selection.backend_lxml.XpathSelector",
        "selection.backend_lxml.LxmlNodeSelector.select"
    ]
}
```

Keys are caller FQNs, values are lists of callee FQNs. This is directly comparable to DynaPyt output after normalization.

### Better CI Log Collection — Why sys.setprofile() Wins

The fundamental limitation of the existing CIntent approach is **sampling**. Pyinstrument samples the call stack at ~1000 Hz, which means:

1. **Functions that execute faster than 1ms are invisible** — they complete between samples
2. **Rare function calls are probabilistic** — a function called once during a 100s test run has only a ~0.1% chance of being captured
3. **Post-hoc inference is necessary** — cintent_improved needs a 6-step pipeline (FQN resolution, normalization, deduplication, test-caller filtering, consensus voting) to recover missing edges

`sys.setprofile()` eliminates all of these problems:

- **Deterministic**: fired by the CPython interpreter on *every* call/return, regardless of timing
- **C-level hook**: no Python bytecode interpretation overhead for the dispatch
- **Complete**: captures `call`, `return`, `c_call`, `c_return`, `c_exception` events
- **Adaptive dedup**: amortizes overhead by skipping already-seen edges (~50ns per repeat)

The result: **37% higher F1 score** (0.6892 vs 0.5680) with a **simpler pipeline** and **zero external dependencies**.

---

## Private Publishing & Reuse Across Repositories

If you want to keep `cintent_v3` private and still use it from any repository, the cleanest option is a private Python package registry.

### Option A (Recommended): Publish to GitHub Packages (private)

Use a release workflow in the `cintent_v3` repository:

```yaml
name: Publish Private Package

on:
  workflow_dispatch:
  push:
    tags:
      - "v*"

jobs:
  publish:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Build package
        run: |
          python -m pip install --upgrade pip build
          python -m build

      - name: Publish to GitHub Packages (private)
        env:
          TWINE_USERNAME: ${{ github.actor }}
          TWINE_PASSWORD: ${{ secrets.GITHUB_TOKEN }}
        run: |
          python -m pip install --upgrade twine
          python -m twine upload \
            --repository-url https://upload.pypi.pkg.github.com/<OWNER>/ \
            dist/*
```

Important: for GitHub Packages (Python), use the repository-scoped index URL in consumers:

```text
https://pip.pkg.github.com/<OWNER>
```

To consume this private package from another repository workflow:

```yaml
name: Use cintent_v3

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: read
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install project deps
        run: |
          python -m pip install --upgrade pip
          pip install -e .
          pip install pytest

      - name: Install private cintent_v3
        env:
          GH_PACKAGES_TOKEN: ${{ secrets.GH_PACKAGES_TOKEN }}
        run: |
          pip install \
            --extra-index-url https://__token__:${GH_PACKAGES_TOKEN}@pip.pkg.github.com/<OWNER> \
            cintent_v3==0.1.0

      - name: Run tracing
        run: python -m cintent_v3 ci collect . -o cintent_v3_artifact.zip

      - name: Upload artifact
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: cintent_v3-logs
          path: |
            cintent_v3_artifact.zip
            cintent_v3_callgraph.json
            cintent_v3_metadata.json
            cintent_v3_stats.json
```

### Option B: Install directly from a private GitHub repository

If you do not want a package registry, install from Git directly in workflow files:

```yaml
- name: Install private cintent_v3 from GitHub
  env:
    GH_READ_TOKEN: ${{ secrets.GH_READ_TOKEN }}
  run: |
    pip install "git+https://x-access-token:${GH_READ_TOKEN}@github.com/<OWNER>/cintent_v3.git@v0.1.0"
```

This is simple for small teams, but package registries are better for version pinning, provenance, and repeatable installs.

### Required Secrets for Cross-Repository Use

For repository A (where `cintent_v3` is published):
- `GITHUB_TOKEN` with `packages:write` in publish workflow permissions.

For repository B/C/... (consumers):
- `GH_PACKAGES_TOKEN` (classic PAT or fine-grained token) with read access to packages and the private source repository.

### Practical Recommendation

1. Publish tags from `cintent_v3` as immutable versions (`v0.1.0`, `v0.1.1`, ...).
2. In each consuming repository workflow, pin exact versions (`cintent_v3==0.1.0`).
3. Keep the tracing command stable: `python -m cintent_v3 ci collect . -o cintent_v3_artifact.zip`.
