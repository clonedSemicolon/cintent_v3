# DESIGN.md — cintent_v3 Technical Design Document

## 1. Motivation & Problem Analysis

### 1.1 The Sampling Gap (Pyinstrument / cintent v1)

Pyinstrument uses a **statistical sampling profiler** that interrupts execution ~1000 times per second and records the current call stack. This approach has a fundamental limitation for call graph extraction:

**Functions that execute in <1ms have a high probability of never being sampled.**

For a function executing in 0.1ms, the probability of being captured in any single sample is:
```
P(capture) = 0.1ms / 1ms = 0.1 = 10%
```

If the function is called only once during a test (common for test helpers, setup/teardown, error handlers), it will be **missed 90% of the time**. This creates systematically incomplete call graphs. 

### 1.2 The Overhead Problem (DynaPyt)

DynaPyt uses **AST-level instrumentation**: it rewrites every Python source file to insert callbacks at every function entry/exit point. This captures everything but introduces:

1. **Startup overhead**: Parsing and rewriting every source file
2. **Per-call overhead**: Every function call triggers Python-level callback code
3. **I/O overhead**: Trace events written to disk during execution
4. **Memory overhead**: Full trace log stored

In CI environments with resource constraints and timeout limits, DynaPyt often exceeds practical execution budgets (10-100x slowdown).

### 1.3 The Insight: We Don't Need Counts, Just Edges

For call graph analysis, we need to know **which functions call which other functions** — not how many times. This insight enables a dramatic optimization:

> **Record each unique (caller, callee) edge exactly once. Skip all subsequent occurrences.**

This transforms the cost model from O(total_calls) to O(unique_edges), which is typically 100-1000x smaller.

---

## 2. Technical Approach

### 2.1 `sys.setprofile()` — The Foundation

Python's `sys.setprofile()` installs a C-level callback that fires on:
- `call`: Python function call
- `return`: Python function return
- `c_call`: C/builtin function call
- `c_return`: C/builtin function return
- `c_exception`: C function raised exception

Key properties:
- **Deterministic**: Fires on every call/return, not sampled
- **C-level**: Callback invocation overhead is minimal (~100ns)
- **Per-thread**: Can be installed per-thread via `threading.setprofile()`
- **Non-invasive**: No source code modification needed

### 2.2 Adaptive Edge Deduplication

The profile callback performs this logic:

```python
def _profile_callback(self, frame, event, arg):
    if event == "call":
        callee_fqn = self._get_fqn_from_frame(frame)
        caller_fqn = self._get_caller_fqn(frame)
        edge = (caller_fqn, callee_fqn)
        
        # KEY OPTIMIZATION: Skip entirely if we've seen this edge
        if edge not in self._seen_edges:
            if self._is_project_relevant(frame):
                self._seen_edges.add(edge)
                self._edges.add(edge)
```

The `set.__contains__` check is O(1) with ~50ns overhead. For a test suite making 1M function calls with 500 unique edges:

| Approach | Operations | Est. Time |
|----------|-----------|-----------|
| Full logging (DynaPyt-style) | 1,000,000 × full processing | ~500ms |
| Adaptive dedup (cintent_v3) | 500 × full + 999,500 × set check | ~50ms |

**10x less overhead** for the same result.

### 2.3 FQN Resolution Strategy

For each function call, we need the fully-qualified name (FQN). The approach differs by function type:

**Python functions** (event=`call`):
```
frame.f_code.co_filename  →  project-relative module path
frame.f_code.co_qualname  →  Class.method or function name
Combine: module.path.Class.method
```

**C/builtin functions** (event=`c_call`):
```
arg.__module__    →  module name (e.g., "builtins")
arg.__qualname__  →  qualified name (e.g., "dict.items")
Combine: builtins.dict.items
```

### 2.4 Project Scoping

A project file is any `.py` file under `project_root`. The check is cached per file path:

```python
def _is_project_file(self, filepath):
    if filepath in self._file_cache:
        return self._file_cache[filepath]
    result = os.path.abspath(filepath).startswith(self.project_root)
    self._file_cache[filepath] = result
    return result
```

An edge is recorded if:
- **Default mode**: caller OR callee is project code
- **Both-project mode**: both caller AND callee are project code
- **External mode**: all edges (useful for dependency analysis)

---

## 3. Normalization Strategy

### 3.1 The Cross-Tool Problem

Different tools use different naming conventions:

```
DynaPyt:    .DyPyBench.temp.project1.grab.document.Document.__init__
cintent_v3: grab.document.Document.__init__
Pyinstrument: grab/document.py:Document.__init__
```

Direct string comparison would yield 0% match. We need normalization.

### 3.2 Normalization Pipeline

Applied in order:

1. **Strip tool prefixes**: Remove `.DyPyBench.temp.projectN.`, `/home/runner/work/.../`
2. **Remove noise markers**: `<module>`, `<locals>`, `<listcomp>`, `<genexpr>`
3. **Normalize lambdas**: `<lambda0>` → `<lambda>`
4. **Remove .py extensions**: `grab.document.py` → `grab.document`
5. **Collapse dots**: `grab..document` → `grab.document`
6. **Strip edges**: Remove leading/trailing dots
7. **Module alias resolution**: Map internal CPython modules to public names:
   - `_io.open` → `io.open`
   - `nt._path_exists` → `os.path.exists`
   - `_thread._local` → `threading._local`
   - `posixpath.split` / `ntpath.split` → `os.path.split`
   - `genericpath.exists` → `os.path.exists`
8. **Function-level aliases**: Map C implementation names to public API:
   - `os._path_exists` → `os.path.exists`

### 3.3 Project Package Detection

Auto-detect project packages by frequency analysis:

1. Collect all top-level package names from the call graph
2. Filter out known externals (builtins, typing, os, sys, numpy, etc.)
3. The remaining packages appearing with ≥10% frequency of the most common are project packages

This heuristic works because project code dominates test-driven call graphs.

---

## 4. Evaluation Methodology

### 4.1 Edge-Level Comparison

We compare at the level of **unique (caller, callee) edges** after normalization:

```
TP = edges in both tool and ground truth
FP = edges only in tool (extra edges)
FN = edges only in ground truth (missing edges)

Precision = TP / (TP + FP)
Recall    = TP / (TP + FN)
F1        = 2 × P × R / (P + R)
```

### 4.2 Why Edge-Level, Not Node-Level

Node-level comparison (function coverage) is too coarse — a tool might capture a function as a caller but miss specific callee relationships. Edge-level comparison captures the **structure** of the call graph.

### 4.3 Edge Expansion for Cross-Tool Matching

DynaPyt and sys.setprofile() represent the same call differently:
- DynaPyt records class instantiation as `caller → ClassName`
- sys.setprofile() records it as `caller → ClassName.__init__`

The evaluator **expands** edges by generating both forms:
- `(A, X.__init__)` → also generates `(A, X)`
- `(A, X)` where X is a class → also generates `(A, X.__init__)`
- Handles builtin types: `builtins.list`, `builtins.dict`, `builtins.ValueError`, etc.

### 4.4 Noise Callee Filtering

sys.setprofile() captures Python internal plumbing that no GT tool records:
- `importlib._bootstrap.*` — import machinery
- `abc.ABCMeta.__new__` — metaclass creation
- `builtins.__build_class__` — class definition
- `typing._GenericAlias`, `typing._tp_cache` — type annotation evaluation

These are filtered out before comparison to avoid inflating false positives.

### 4.5 Overlap-Only Evaluation

When the ground truth has **incomplete caller coverage** (common: DynaPyt may only trace specific test functions), the "full" evaluation penalizes the tool for discovering correct edges from untraced callers.

**Overlap-only mode** restricts evaluation to callers present in BOTH tool output and ground truth. This gives a fair apples-to-apples comparison:

```
Full mode:      2500 tool edges vs 450 GT edges → Precision 13% (misleading)
Overlap-only:   530 tool edges vs 447 GT edges  → Precision 63% (fair)
```

### 4.6 Filtering Modes

| Mode | Filter | Use Case |
|------|--------|----------|
| `caller-project` (recommended) | Caller is project code | Matches DynaPyt output structure |
| `both-project` | Both endpoints are project code | Project-internal structure |
| `project-only` (default) | At least one endpoint is project code | Standard evaluation |
| `all-edges` | No filtering | Complete behavioral comparison |
| `--overlap-only` (flag) | Shared callers only | Fair eval for incomplete GT |

### 4.7 Results on grab project

| Mode | Precision | Recall | F1 |
|------|-----------|--------|-----|
| Caller-project (full) | 0.1340 | 0.7374 | 0.2268 |
| **Caller-project + overlap** | **0.6347** | **0.7539** | **0.6892** |
| Both-project (full) | 0.1332 | 0.7795 | 0.2275 |
| **Both-project + overlap** | **0.7488** | **0.8042** | **0.7755** |

---

## 5. Performance Model

### 5.1 Overhead Breakdown

Per function call in steady state (after first occurrence):

| Component | Cost | Notes |
|-----------|------|-------|
| C-level callback dispatch | ~100ns | Python VM overhead for setprofile |
| Event type check | ~10ns | Python string comparison |
| Edge set lookup | ~50ns | `set.__contains__` with tuple key |
| **Total (hot path)** | **~160ns** | Substantially less than DynaPyt's ~5μs |

Per function call on first occurrence of an edge:

| Component | Cost | Notes |
|-----------|------|-------|
| FQN resolution (caller) | ~300ns | Frame inspection + string ops |
| FQN resolution (callee) | ~300ns | Same |
| Project file check | ~200ns | First time only, then cached |
| Edge recording | ~100ns | `set.add` |
| **Total (cold path)** | **~1μs** | One-time cost per unique edge |

### 5.2 Amortized Overhead Example

For a typical test suite:
- 1,000,000 total function calls
- 500 unique edges
- Average 2,000 calls per unique edge

```
Cold path:  500 × 1μs     = 0.5ms
Hot path:   999,500 × 160ns = 160ms
Total overhead:              ~160ms

For a test suite running 60s:
Relative overhead: 160ms / 60s = 0.27%
```

Compare with DynaPyt: 1,000,000 × 5μs = 5s → 8.3% overhead

---

## 6. Limitations & Future Work

### 6.1 Current Limitations

1. **Generator/coroutine edges**: `yield` and `await` don't fire `call` events, so generator-to-consumer edges may be incomplete
2. **Metaclass `__init_subclass__`**: Some metaclass hooks fire before the profile callback is installed
3. **C extension internal calls**: Calls between C functions inside the same extension are not visible
4. **Thread safety**: The GIL protects shared data structures, but `threading.setprofile()` only covers new threads

### 6.2 Future Enhancements

1. **AST augmentation**: Use lightweight static analysis to pre-populate edges that are hard to capture dynamically (decorators, metaclasses)
2. **Import hook tracking**: Capture module-level code execution via import hooks  
3. **Warm-up mode**: Trace for N seconds, then disable for performance-critical phases
4. **Incremental evaluation**: Compare call graphs across multiple CI runs to detect behavioral changes
