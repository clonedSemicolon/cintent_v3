"""Name normalization for fair call graph comparison across tools.

Different tools encode function names differently:
- DynaPyt: .DyPyBench.temp.project1.grab.document.Document.__init__
- cintent_v3: grab.document.Document.__init__
- CI runners: /home/runner/work/repo/repo/grab/document.py

This module provides normalization to project-relative canonical names
so that call graphs from different tools can be compared fairly.
"""

from __future__ import annotations

import re


# Known tool-specific path prefixes to strip
_TOOL_PREFIXES = [
    # DynaPyt / DyPyBench prefixes
    re.compile(r"^\.?DyPyBench\.temp\.project\d+\."),
    re.compile(r"^\.?DyPyBench\.temp\.\w+\."),
    # CI runner workspace prefixes
    re.compile(r"^/home/runner/work/[^/]+/[^/]+/"),
    re.compile(r"^/home/runner/work/[^/]+/"),
    # Generic CI workspace prefixes
    re.compile(r"^/workspace/"),
    re.compile(r"^/app/"),
    # Windows CI paths
    re.compile(r"^[A-Z]:\\[Aa]ctions-runner\\_work\\[^\\]+\\[^\\]+\\"),
]

# Patterns to clean up in FQNs
_CLEANUP_PATTERNS = [
    # Remove <module> markers
    (re.compile(r"<module>\.?"), ""),
    # Remove <locals> wrappers (keep the inner name)
    (re.compile(r"\.<locals>\."), "."),
    (re.compile(r"<locals>\."), ""),
    # Remove <lambda> number suffixes: <lambda0> -> <lambda>
    (re.compile(r"<lambda\d*>"), "<lambda>"),
    # Remove <listcomp>, <dictcomp>, <setcomp>, <genexpr>
    (re.compile(r"\.<(?:listcomp|dictcomp|setcomp|genexpr)>"), ""),
    # Remove .py extension from module paths
    (re.compile(r"\.py(?=\.|$)"), ""),
    # Collapse consecutive dots
    (re.compile(r"\.{2,}"), "."),
    # Strip leading/trailing dots
    (re.compile(r"^\.+|\.+$"), ""),
]

# Module alias resolution: internal CPython module names → public names.
# DynaPyt resolves to public module names while sys.setprofile() sees
# the internal implementation modules. Map internal → public for fair comparison.
_MODULE_ALIASES: dict[str, str] = {
    "_io": "io",
    "_thread": "threading",
    "_collections": "collections",
    "_functools": "functools",
    "_operator": "operator",
    "_heapq": "heapq",
    "_bisect": "bisect",
    "_random": "random",
    "_csv": "csv",
    "_json": "json",
    "_struct": "struct",
    "_pickle": "pickle",
    "_datetime": "datetime",
    "_socket": "socket",
    "_ssl": "ssl",
    "_hashlib": "hashlib",
    "_sha256": "hashlib",
    "_sha512": "hashlib",
    "_md5": "hashlib",
    "_signal": "signal",
    "_abc": "abc",
    "_weakref": "weakref",
    "_weakrefset": "weakref",
    "_contextvars": "contextvars",
    "_decimal": "decimal",
    "_string": "string",
    "_stat": "stat",
    "nt": "os",
    "posix": "os",
    "ntpath": "os.path",
    "posixpath": "os.path",
    "genericpath": "os.path",
}

# Specific function-level aliases for C implementations that map to
# different public API names. Applied AFTER module alias resolution.
_FUNCTION_ALIASES: dict[str, str] = {
    "os._path_exists": "os.path.exists",
    "os._path_isfile": "os.path.isfile",
    "os._path_isdir": "os.path.isdir",
    "os._path_islink": "os.path.islink",
    "os._path_normpath": "os.path.normpath",
}


def normalize_fqn(name: str) -> str:
    """Normalize a fully-qualified function name for comparison.

    Strips tool-specific prefixes, removes noise markers, resolves internal
    module aliases to public names, and produces a canonical name.

    Args:
        name: Raw FQN from any tool (DynaPyt, cintent, etc.)

    Returns:
        Normalized canonical name.

    Examples:
        >>> normalize_fqn(".DyPyBench.temp.project1.grab.document.Document.__init__")
        'grab.document.Document.__init__'
        >>> normalize_fqn("_io.open")
        'io.open'
    """
    result = name.strip()

    # Strip known tool-specific prefixes
    for prefix_re in _TOOL_PREFIXES:
        result = prefix_re.sub("", result)

    # Apply cleanup patterns
    for pattern, replacement in _CLEANUP_PATTERNS:
        result = pattern.sub(replacement, result)

    # Resolve internal module aliases to public names
    # e.g., _io.open -> io.open, nt._path_exists -> os._path_exists
    parts = result.split(".", 1)
    if parts[0] in _MODULE_ALIASES:
        public_mod = _MODULE_ALIASES[parts[0]]
        result = f"{public_mod}.{parts[1]}" if len(parts) > 1 else public_mod

    # Apply specific function-level aliases
    if result in _FUNCTION_ALIASES:
        result = _FUNCTION_ALIASES[result]

    return result


def normalize_edge(
    caller: str, callee: str
) -> tuple[str, str]:
    """Normalize a single call graph edge.

    Args:
        caller: Raw caller FQN.
        callee: Raw callee FQN.

    Returns:
        Tuple of (normalized_caller, normalized_callee).
    """
    return normalize_fqn(caller), normalize_fqn(callee)


def normalize_edges(
    edges: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    """Normalize a set of call graph edges.

    Args:
        edges: Set of (caller, callee) tuples with raw FQNs.

    Returns:
        Set of (normalized_caller, normalized_callee) tuples.
    """
    return {normalize_edge(c, e) for c, e in edges}


def expand_edges_for_matching(
    edges: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    """Expand edges with alternative representations for fuzzy matching.

    DynaPyt records class instantiations as `caller -> ClassName` while
    sys.setprofile() records `caller -> ClassName.__init__`. This function
    expands edges so both forms exist, enabling matching regardless of
    which representation each tool uses.

    Also handles builtin types (list, dict, set, etc.) which DynaPyt records
    as `builtins.list` but our tracer may record differently.

    Args:
        edges: Set of normalized (caller, callee) tuples.

    Returns:
        Expanded edge set including alternative representations.
    """
    # Builtin types that DynaPyt records as class references
    _BUILTIN_TYPES = {
        "builtins.list", "builtins.dict", "builtins.set", "builtins.tuple",
        "builtins.frozenset", "builtins.int", "builtins.float", "builtins.str",
        "builtins.bytes", "builtins.bool", "builtins.complex",
        "builtins.range", "builtins.enumerate", "builtins.map", "builtins.filter",
        "builtins.zip", "builtins.reversed", "builtins.sorted", "builtins.type",
        "builtins.object", "builtins.super", "builtins.property",
        "builtins.staticmethod", "builtins.classmethod",
        "builtins.ValueError", "builtins.TypeError", "builtins.KeyError",
        "builtins.IndexError", "builtins.AttributeError", "builtins.RuntimeError",
        "builtins.StopIteration", "builtins.OSError", "builtins.IOError",
        "builtins.FileNotFoundError", "builtins.NotImplementedError",
        "builtins.Exception", "builtins.BaseException",
    }
    expanded = set(edges)
    for caller, callee in edges:
        # If callee ends with .__init__, also add edge without it
        # e.g., (A, X.__init__) → also add (A, X)
        if callee.endswith(".__init__"):
            class_name = callee[:-9]  # strip .__init__
            expanded.add((caller, class_name))

        # If callee is a known builtin type, also generate __init__ form
        if callee in _BUILTIN_TYPES:
            expanded.add((caller, f"{callee}.__init__"))

        # If callee looks like a class (last segment starts uppercase),
        # also add the __init__ form
        # e.g., (A, grab.transport.Urllib3Transport) → also add (A, grab.transport.Urllib3Transport.__init__)
        elif "." in callee:
            last_part = callee.rsplit(".", 1)[-1]
            if last_part and last_part[0].isupper() and not last_part.startswith("__"):
                expanded.add((caller, f"{callee}.__init__"))
    return expanded


def normalize_call_graph(
    call_graph: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Normalize all names in a call graph.

    Args:
        call_graph: Dict mapping caller FQN to list of callee FQNs.

    Returns:
        Normalized call graph with canonical names.
    """
    normalized: dict[str, set[str]] = {}
    for caller, callees in call_graph.items():
        norm_caller = normalize_fqn(caller)
        if norm_caller not in normalized:
            normalized[norm_caller] = set()
        for callee in callees:
            normalized[norm_caller].add(normalize_fqn(callee))
    return {k: sorted(v) for k, v in sorted(normalized.items())}


def extract_project_name(fqn: str) -> str | None:
    """Extract the top-level project/package name from a normalized FQN.

    Args:
        fqn: Normalized fully-qualified name.

    Returns:
        First segment (project/package name), or None if empty.

    Examples:
        >>> extract_project_name("grab.document.Document.__init__")
        'grab'
    """
    parts = fqn.split(".")
    return parts[0] if parts and parts[0] else None


def is_project_fqn(fqn: str, project_packages: set[str]) -> bool:
    """Check if a normalized FQN belongs to the project.

    Args:
        fqn: Normalized fully-qualified name.
        project_packages: Set of known project top-level package names.

    Returns:
        True if the FQN's top-level package is in project_packages.
    """
    pkg = extract_project_name(fqn)
    return pkg is not None and pkg in project_packages


def filter_project_edges(
    edges: set[tuple[str, str]],
    project_packages: set[str],
) -> set[tuple[str, str]]:
    """Filter edges to only those where at least one endpoint is project code.

    Args:
        edges: Set of normalized (caller, callee) edges.
        project_packages: Set of known project top-level package names.

    Returns:
        Filtered edge set.
    """
    return {
        (c, e)
        for c, e in edges
        if is_project_fqn(c, project_packages) or is_project_fqn(e, project_packages)
    }


def filter_both_project_edges(
    edges: set[tuple[str, str]],
    project_packages: set[str],
) -> set[tuple[str, str]]:
    """Filter edges to only those where BOTH endpoints are project code.

    Args:
        edges: Set of normalized (caller, callee) edges.
        project_packages: Set of known project top-level package names.

    Returns:
        Filtered edge set where both caller and callee are project code.
    """
    return {
        (c, e)
        for c, e in edges
        if is_project_fqn(c, project_packages) and is_project_fqn(e, project_packages)
    }


def filter_caller_project_edges(
    edges: set[tuple[str, str]],
    project_packages: set[str],
) -> set[tuple[str, str]]:
    """Filter edges to only those where the CALLER is project code.

    This matches DynaPyt's output structure where callers are always
    project functions but callees can be builtins/stdlib/third-party.

    Args:
        edges: Set of normalized (caller, callee) edges.
        project_packages: Set of known project top-level package names.

    Returns:
        Filtered edge set where the caller is project code.
    """
    return {
        (c, e)
        for c, e in edges
        if is_project_fqn(c, project_packages)
    }


def detect_project_packages(
    call_graph: dict[str, list[str]],
    known_externals: set[str] | None = None,
) -> set[str]:
    """Auto-detect project package names from a call graph.

    Heuristic: project packages appear frequently as top-level segments
    and are not known standard library / third-party packages.

    Args:
        call_graph: Normalized call graph.
        known_externals: Optional set of known external package names to exclude.

    Returns:
        Set of detected project package names.
    """
    if known_externals is None:
        known_externals = {
            "builtins", "typing", "collections", "abc", "functools",
            "os", "sys", "re", "json", "io", "copy", "logging",
            "threading", "queue", "time", "random", "inspect",
            "http", "urllib", "email", "pathlib", "contextlib",
            "hashlib", "base64", "struct", "pickle", "csv",
            "_thread", "_io", "posixpath", "genericpath", "ntpath",
            # Common third-party
            "numpy", "pandas", "pytest", "setuptools", "pip",
            "lxml", "certifi", "urllib3", "requests",
            "selection", "unicodec", "procstat",
        }

    package_counts: dict[str, int] = {}
    for caller, callees in call_graph.items():
        pkg = extract_project_name(caller)
        if pkg and pkg not in known_externals:
            package_counts[pkg] = package_counts.get(pkg, 0) + 1
        for callee in callees:
            pkg = extract_project_name(callee)
            if pkg and pkg not in known_externals:
                package_counts[pkg] = package_counts.get(pkg, 0) + 1

    if not package_counts:
        return set()

    # The most frequent non-external package is likely the project
    max_count = max(package_counts.values())
    threshold = max_count * 0.1  # Include packages with at least 10% of max frequency
    return {pkg for pkg, count in package_counts.items() if count >= threshold}


# Import/metaclass machinery callees that represent Python internal plumbing,
# not meaningful application-level call edges. Filtering these improves
# precision without sacrificing real recall.
_NOISE_CALLEE_PREFIXES = (
    "importlib._bootstrap",
    "importlib._bootstrap_external",
    "abc.ABCMeta.__new__",
    "abc.ABCMeta.__init__",
    "abc.ABCMeta.__subclasscheck__",
    "abc.ABCMeta.__instancecheck__",
    "builtins.__build_class__",
    "typing._GenericAlias",
    "typing._SpecialForm",
    "typing._tp_cache",
    "typing._type_check",
    "typing.__getattr__",
    "typing._collect_parameters",
    "enum.EnumMeta",
    "enum.EnumType",
)


def filter_noise_callees(
    edges: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    """Remove edges to import/metaclass/typing internal callees.

    These are Python plumbing calls (class creation, import resolution,
    type annotation evaluation) that sys.setprofile() captures but no
    dynamic analysis ground truth (DynaPyt, etc.) records. Filtering them
    improves precision in cross-tool comparisons.

    Args:
        edges: Set of (caller, callee) tuples.

    Returns:
        Filtered edge set with noise callees removed.
    """
    return {
        (c, e) for c, e in edges
        if not any(e.startswith(prefix) for prefix in _NOISE_CALLEE_PREFIXES)
    }
