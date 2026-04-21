"""
backend.app.repo_chunk_extractor
=================================
GRAPH_RECONSTRUCTION_LAYER_V1

Deterministic structural extractor for graph reconstruction.

NO AI.
NO GUESSING.
PURE PATTERN EXTRACTION.

Produces structural metadata that is stored on RepoChunk rows so the
retrieval layer and any downstream graph-reconstruction tool can reason
about code structure without re-parsing.

Supported languages
-------------------
The extractor uses language-agnostic regex patterns that correctly match
the primary structural constructs across Python, JavaScript/TypeScript,
Java, Kotlin, Go, Ruby, Swift, C/C++, C#, Rust, and PHP.

Output fields
-------------
chunk_type    CLASS | FUNCTION | IMPORT | CONFIG | DATA | DOC
symbol        Primary symbol defined in the chunk (first match only)
dependencies  Unique list of imported/referenced module names
graph_group   File path (logical grouping for graph segmentation)
start_line    1-based first line of the chunk content
end_line      1-based last line of the chunk content
"""

from __future__ import annotations

import os
import posixpath
import re
from typing import Any

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# Class / type declarations (Python, JS/TS, Java, Kotlin, C#, Swift, Rust, Ruby)
_CLASS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bclass\s+(\w+)"),        # Python, JS/TS, Java, Kotlin, C#, Swift, PHP
    re.compile(r"\binterface\s+(\w+)"),    # Java, TS, C#, Swift, Kotlin
    re.compile(r"\benum\s+(\w+)"),         # Java, TS, Kotlin, C#, Swift, Rust
    re.compile(r"\bstruct\s+(\w+)"),       # Go, Rust, C/C++, Swift
    re.compile(r"\bimpl\s+(\w+)"),         # Rust
    re.compile(r"\btrait\s+(\w+)"),        # Rust
    re.compile(r"\bprotocol\s+(\w+)"),     # Swift
    re.compile(r"\bobject\s+(\w+)"),       # Kotlin, Scala
]

# Function / method declarations
_FUNC_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bdef\s+(\w+)"),                          # Python, Ruby
    re.compile(r"\bfunc\s+(\w+)"),                         # Go, Swift
    re.compile(r"\bfun\s+(\w+)"),                          # Kotlin
    re.compile(r"\bfunction\s+(\w+)"),                     # JS/TS/PHP
    re.compile(r"\bfn\s+(\w+)"),                           # Rust
    re.compile(  # Java/C#
        r"(?:public|private|protected|static|async|override)"
        r"\s+\w[\w<>\[\]]*\s+(\w+)\s*\("
    ),
    re.compile(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\("),  # JS arrow fn
]

# Import / dependency declarations
_IMPORT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^import\s+([\w\.]+)"),                        # Python bare import / Java
    re.compile(r"^from\s+([\w\.]+)\s+import"),                 # Python from-import
    re.compile(r'^import\s+[{]?.*[}]?\s+from\s+["\']([^"\']+)["\']'),  # JS/TS named
    re.compile(r'^(?:const|let|var)\s+\w.*=\s*require\(["\']([^"\']+)["\']'),  # Node require
    re.compile(r'^use\s+([\w:]+)'),                            # Rust use
    re.compile(r'^using\s+([\w\.]+)'),                         # C# using
    re.compile(r'^import\s+([\w\.]+);'),                       # Java/Kotlin import
    re.compile(r'^require\s+["\']([^"\']+)["\']'),             # Ruby require
    re.compile(r'^#include\s+[<"]([^>"]+)[>"]'),               # C/C++ include
    re.compile(r'^use\s+([\w\\]+)'),                           # PHP use
]

# Config file indicators (by extension or content)
_CONFIG_CONTENT_PATTERN = re.compile(
    r"^\s*[\w\-\.]+\s*[=:]\s*.+",  # key = value or key: value at line start
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_structure(content: str, path: str) -> dict[str, Any]:
    """
    Extract structural metadata from a single chunk.

    Parameters
    ----------
    content:
        Raw text content of the chunk.
    path:
        Source file path (used as graph_group and for extension-based hints).

    Returns
    -------
    dict with keys: chunk_type, symbol, dependencies, graph_group,
                    start_line, end_line
    """
    lines = content.splitlines()
    n_lines = len(lines)

    chunk_type, symbol = _detect_type_and_symbol(lines, path)
    dependencies = _extract_dependencies(lines)

    return {
        "chunk_type": chunk_type,
        "symbol": symbol,
        "dependencies": dependencies,
        "graph_group": path,
        "start_line": 1,
        "end_line": n_lines if n_lines > 0 else 1,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _detect_type_and_symbol(lines: list[str], path: str) -> tuple[str, str | None]:
    """
    Scan lines to determine the primary structural type and symbol.

    Precedence: CLASS > FUNCTION > IMPORT > CONFIG > DATA > DOC
    Returns ``(chunk_type, symbol_or_None)``.
    """
    found_class: str | None = None
    found_func: str | None = None
    has_imports = False

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "/*", "*", "<!--", "--")):
            continue

        # Class / type detection (highest precedence)
        if found_class is None:
            for pat in _CLASS_PATTERNS:
                m = pat.search(stripped)
                if m:
                    found_class = m.group(1)
                    break

        # Function detection
        if found_func is None:
            for pat in _FUNC_PATTERNS:
                m = pat.search(stripped)
                if m:
                    found_func = m.group(1)
                    break

        # Import detection
        if not has_imports:
            for pat in _IMPORT_PATTERNS:
                if pat.match(stripped):
                    has_imports = True
                    break

    # Precedence: CLASS > FUNCTION > IMPORT
    if found_class:
        return "CLASS", found_class
    if found_func:
        return "FUNCTION", found_func
    if has_imports:
        return "IMPORT", None

    # Extension-based config detection
    _, ext = _splitext(path)
    if ext in {".yaml", ".yml", ".toml", ".ini", ".conf", ".env", ".properties"}:
        return "CONFIG", None

    # Content-based DATA detection (JSON/structured data)
    if _looks_like_data(lines):
        return "DATA", None

    return "DOC", None


def _extract_dependencies(lines: list[str]) -> list[str]:
    """
    Extract all dependency references (import targets) from a chunk.

    Returns a deduplicated sorted list of module/package names.
    """
    deps: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        for pat in _IMPORT_PATTERNS:
            m = pat.match(stripped)
            if m:
                dep = m.group(1)
                if dep:
                    # Normalise: take only the top-level package
                    top = dep.split(".")[0].split("/")[0].split("::")[0].split("\\")[0]
                    if top and top.isidentifier():
                        deps.add(top)
                break  # one pattern match per line is enough
    return sorted(deps)


def _looks_like_data(lines: list[str]) -> bool:
    """Heuristic: chunk looks like JSON/YAML data if it starts with [ or {."""
    for line in lines:
        stripped = line.strip()
        if stripped:
            return stripped.startswith(("{", "["))
    return False


def _splitext(path: str) -> tuple[str, str]:
    """Return ``(root, ext)`` with ext lowercased."""
    root, ext = os.path.splitext(path)
    return root, ext.lower()


# ---------------------------------------------------------------------------
# REPO_GRAPH_RESOLUTION_V1 — Import resolution, call extraction, entry points
# ---------------------------------------------------------------------------

# Patterns that preserve full import paths (not just top-level package names).
# Used by extract_raw_imports() for dependency resolution.
# ORDER MATTERS: more-specific patterns first to avoid early short-circuit.
_FULL_IMPORT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^from\s+([\w\.]+)\s+import"),                         # Python from-import
    re.compile(r'^import\s+[{]?.*[}]?\s+from\s+["\']([^"\']+)["\']'), # JS/TS named (before bare!)
    re.compile(r'^(?:const|let|var)\s+\w.*=\s*require\(["\']([^"\']+)["\']'),  # Node require
    re.compile(r"^import\s+([\w\.]+)"),                                # Python bare / Java
    re.compile(r'^require\s+["\']([^"\']+)["\']'),                     # Ruby require
]

# Function-call patterns for call-edge extraction
_CALL_PATTERN = re.compile(r"\b([a-zA-Z_]\w*)\s*\(")
_METHOD_CALL_PATTERN = re.compile(r"\.\s*([a-zA-Z_]\w*)\s*\(")

# Python keywords and builtins to exclude from call lists
_PYTHON_KEYWORDS: frozenset[str] = frozenset(
    {
        "if", "else", "elif", "for", "while", "return", "import", "from",
        "class", "def", "try", "except", "finally", "with", "as", "in",
        "and", "or", "not", "is", "None", "True", "False", "pass", "break",
        "continue", "raise", "yield", "async", "await", "lambda", "del",
        "assert", "global", "nonlocal",
        # Common builtins
        "print", "len", "range", "type", "str", "int", "float", "list",
        "dict", "set", "tuple", "bool", "open", "super", "object",
        "isinstance", "issubclass", "hasattr", "getattr", "setattr",
        "callable", "iter", "next", "enumerate", "zip", "map", "filter",
        "sorted", "reversed", "max", "min", "sum", "abs", "round",
        "repr", "format", "hex", "oct", "bin", "hash", "id",
    }
)

# Entry-point filename patterns
_ENTRY_FILENAME_PATTERNS: dict[str, str] = {
    "main.py": "main",
    "app.py": "main",
    "index.ts": "main",
    "index.js": "main",
    "index.tsx": "main",
}
_ENTRY_PREFIX_PATTERNS: dict[str, str] = {
    "server": "server",
    "cli": "cli",
    "test_": "test",
    "tests_": "test",
}

# Content-based entry point patterns (checked in order; first match wins)
# Each entry is (compiled_pattern, entry_type)
_CONTENT_ENTRY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Python __main__ guard
    (re.compile(r'if\s+__name__\s*==\s*["\']__main__["\']'), "main"),
    # FastAPI application instantiation
    (re.compile(r'\bapp\s*=\s*FastAPI\s*\('), "framework"),
    # Flask application instantiation
    (re.compile(r'\bapp\s*=\s*Flask\s*\('), "server"),
    # Express.js: const app = express()
    (re.compile(r'\bexpress\s*\(\s*\)'), "server"),
    # Express.js: app.listen(
    (re.compile(r'\bapp\s*\.\s*listen\s*\('), "server"),
]
# Symbol patterns to extract ALL symbols (not just the first one per chunk)
_ALL_CLASS_PATTERNS = _CLASS_PATTERNS
_ALL_FUNC_PATTERNS = _FUNC_PATTERNS


def extract_raw_imports(content: str) -> list[str]:
    """
    Extract raw import paths from file content without normalisation.

    Returns a list of unique import strings preserving their original form
    (e.g. ``".utils"``, ``"./models"``, ``"os.path"``) for use with
    :func:`resolve_import_path`.
    """
    imports: list[str] = []
    seen: set[str] = set()
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for pat in _FULL_IMPORT_PATTERNS:
            m = pat.match(stripped)
            if m:
                raw = m.group(1)
                if raw and raw not in seen:
                    seen.add(raw)
                    imports.append(raw)
                break
    return imports


def resolve_import_path(
    import_str: str,
    source_path: str,
    all_paths: set[str],
) -> str | None:
    """
    Resolve *import_str* to an actual file path that exists in *all_paths*.

    Handles:
    - Python relative imports (``.utils``, ``..models``)
    - JS/TS relative imports (``./utils``, ``../models``)
    - Same-directory module resolution
    - Extension inference (.py, .ts, .js, .tsx, .jsx, etc.)

    Returns the resolved path string or ``None`` if unresolvable.
    """
    source_dir = posixpath.dirname(source_path)
    _, source_ext = os.path.splitext(source_path)
    source_ext = source_ext.lower()

    # JS/TS-style relative: starts with "./" or "../"
    if import_str.startswith("./") or import_str.startswith("../"):
        base = posixpath.normpath(posixpath.join(source_dir, import_str))
        return _first_matching_path(base, source_ext, all_paths)

    # Python relative import: starts with one or more dots
    if import_str.startswith("."):
        dots = len(import_str) - len(import_str.lstrip("."))
        module_part = import_str[dots:]

        # Navigate up (dots - 1) directories from source_dir
        base_dir = source_dir
        for _ in range(dots - 1):
            base_dir = posixpath.dirname(base_dir) if base_dir else base_dir

        if module_part:
            module_path = module_part.replace(".", "/")
            full_base = posixpath.join(base_dir, module_path) if base_dir else module_path
        else:
            full_base = base_dir

        return _first_matching_path(full_base, source_ext, all_paths)

    # Absolute / package import — try same-directory resolution
    # Convert dotted package path to directory path
    module_path = import_str.replace(".", "/")
    if source_dir:
        candidate = posixpath.join(source_dir, module_path)
        result = _first_matching_path(candidate, source_ext, all_paths)
        if result:
            return result

    # Top-level resolution
    return _first_matching_path(module_path, source_ext, all_paths)


def _first_matching_path(
    base: str, preferred_ext: str, all_paths: set[str]
) -> str | None:
    """
    Try *base* with several file extensions and return the first match.

    Also checks ``base/__init__.py`` and ``base/index.{ts,js}`` for
    package/module imports.
    """
    # If base already has an extension and matches, return directly
    root, ext = os.path.splitext(base)
    if ext:
        norm = posixpath.normpath(base)
        return norm if norm in all_paths else None

    candidate_exts = [".py", ".ts", ".js", ".tsx", ".jsx", ".go", ".rs",
                      ".java", ".kt", ".rb", ".swift", ".cs", ".cpp", ".c"]

    # Preferred extension first
    ordered: list[str] = []
    if preferred_ext in candidate_exts:
        ordered.append(preferred_ext)
    for e in candidate_exts:
        if e not in ordered:
            ordered.append(e)

    norm_base = posixpath.normpath(base)
    for e in ordered:
        candidate = norm_base + e
        if candidate in all_paths:
            return candidate

    # Package/module index files
    for index in ("/__init__.py", "/index.ts", "/index.js"):
        candidate = posixpath.normpath(norm_base + index)
        if candidate in all_paths:
            return candidate

    return None


def extract_all_symbols(content: str, path: str) -> list[tuple[str, str]]:
    """
    Extract all named symbols from *content*.

    Returns a list of ``(name, symbol_type)`` tuples where *symbol_type* is
    one of ``"CLASS"`` or ``"FUNCTION"``.  Duplicates within the same file
    are deduplicated (first occurrence wins).
    """
    seen: set[str] = set()
    results: list[tuple[str, str]] = []

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "/*", "*", "<!--", "--")):
            continue

        # CLASS patterns have higher precedence
        for pat in _ALL_CLASS_PATTERNS:
            m = pat.search(stripped)
            if m:
                name = m.group(1)
                if name and name not in seen:
                    seen.add(name)
                    results.append((name, "CLASS"))
                break
        else:
            for pat in _ALL_FUNC_PATTERNS:
                m = pat.search(stripped)
                if m:
                    name = m.group(1)
                    if name and name not in seen:
                        seen.add(name)
                        results.append((name, "FUNCTION"))
                    break

    return results


def extract_call_names(content: str) -> list[str]:
    """
    Extract unique function/method call names from *content* via regex.

    Patterns matched:
    - ``function_name(``  — bare function call
    - ``object.method(``  — method call (captures ``method``)

    Common keywords and builtins are filtered out.
    Returns a sorted, deduplicated list.
    """
    calls: set[str] = set()
    for line in content.splitlines():
        # Method calls (object.method(…)) — captured by _METHOD_CALL_PATTERN
        for m in _METHOD_CALL_PATTERN.finditer(line):
            name = m.group(1)
            if name and name not in _PYTHON_KEYWORDS:
                calls.add(name)
        # Bare calls (function_name(…))
        for m in _CALL_PATTERN.finditer(line):
            name = m.group(1)
            if name and name not in _PYTHON_KEYWORDS:
                calls.add(name)
    return sorted(calls)


def detect_entry_type(path: str, content: str) -> str | None:
    """
    Detect whether *path* is a repo entry point and return its type.

    Returns one of ``"main"``, ``"cli"``, ``"server"``, ``"framework"``,
    ``"test"``, or ``None`` if the file is not an entry point.

    Detection rules (in precedence order)
    --------------------------------------
    1. Exact filename match (main.py, app.py, index.ts, index.js, …)
    2. Basename prefix match (server.*, cli.*, test_*, tests_*)
    3. Content-based detection:
       - Python ``if __name__ == "__main__"`` guard  → "main"
       - FastAPI  ``app = FastAPI()``                → "framework"
       - Flask    ``app = Flask(__name__)``           → "server"
       - Express  ``express()``                       → "server"
       - Express  ``app.listen(``                     → "server"
    """
    basename = posixpath.basename(path).lower()

    # Exact filename match
    if basename in _ENTRY_FILENAME_PATTERNS:
        return _ENTRY_FILENAME_PATTERNS[basename]

    # Prefix match
    for prefix, etype in _ENTRY_PREFIX_PATTERNS.items():
        if basename.startswith(prefix):
            return etype

    # Content-based detection
    for pattern, etype in _CONTENT_ENTRY_PATTERNS:
        if pattern.search(content):
            return etype

    return None
