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
    import os

    root, ext = os.path.splitext(path)
    return root, ext.lower()
