"""
backend.app.graph_extractor
============================
MQP-CONTRACT: GRAPH-EXTRACTION-LAYER v1.0
MQP-STEERING-CONTRACT: REPO-GRAPH-INTEGRITY-LOCK v1.0

Memory-only, dependency-free graph extraction for ingested files.

Provides:
- Language detection by file extension
- Symbol extraction (functions, classes) from Python source
- Import/dependency extraction from source files
- Entry point detection (main, server, framework)
- Symbol call graph extraction within a file
- Import resolution against a known file set (drop-on-fail, never store unresolved)
- Content hashing for deduplication
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Dict, FrozenSet, List, Optional, Tuple


def detect_language(path: str) -> str:
    if path.endswith(".py"):
        return "python"
    if path.endswith(".ts") or path.endswith(".js"):
        return "typescript"
    return "unknown"


def hash_content(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def extract_python_symbols(content: str) -> List[Tuple[str, str, int]]:
    symbols = []
    lines = content.splitlines()

    for i, line in enumerate(lines):
        if line.startswith("def "):
            parts = line.split("def ", 1)
            if len(parts) > 1:
                name = parts[1].split("(")[0].strip()
                if name:
                    symbols.append((name, "function", i + 1))

        if line.startswith("class "):
            parts = line.split("class ", 1)
            if len(parts) > 1:
                raw = parts[1].strip()
                # Handle both `class Foo:` and `class Foo(Base):`
                if "(" in raw:
                    name = raw.split("(")[0].strip()
                else:
                    name = raw.split(":")[0].strip()
                if name:
                    symbols.append((name, "class", i + 1))

    return symbols


def extract_imports(content: str) -> List[str]:
    imports = []
    lines = content.splitlines()

    for line in lines:
        stripped = line.strip()

        # Skip comment lines
        if stripped.startswith("#"):
            continue

        if stripped.startswith("import "):
            imports.append(stripped.replace("import ", "", 1).strip())

        if stripped.startswith("from "):
            parts = stripped.split(" ")
            if len(parts) > 1:
                imports.append(parts[1])

    return imports


def extract_entry_points(content: str, language: str) -> List[Tuple[str, int]]:
    """
    Detect entry points in source code.

    MQP-STEERING-CONTRACT: REPO-GRAPH-INTEGRITY-LOCK v1.0 — Section 4

    Returns a list of (entry_type, line_number) tuples.
    entry_type is one of: "main" | "server" | "framework"
    """
    results: List[Tuple[str, int]] = []
    lines = content.splitlines()

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue

        if language == "python":
            # if __name__ == "__main__"
            if re.search(r'if\s+__name__\s*==\s*["\']__main__["\']', stripped):
                results.append(("main", i + 1))
            # app = FastAPI()
            elif re.search(r'\bFastAPI\s*\(', stripped):
                results.append(("framework", i + 1))
            # app = Flask(__name__)
            elif re.search(r'\bFlask\s*\(', stripped):
                results.append(("framework", i + 1))

        elif language in ("typescript", "javascript"):
            # express()
            if re.search(r'\bexpress\s*\(\s*\)', stripped):
                results.append(("server", i + 1))
            # app.listen(
            elif re.search(r'\bapp\.listen\s*\(', stripped):
                results.append(("server", i + 1))

    return results


def extract_symbol_calls(
    content: str, known_symbol_names: List[str]
) -> Dict[str, List[str]]:
    """
    For each known top-level symbol (function/class), find calls to other known
    symbols within its body.

    MQP-STEERING-CONTRACT: REPO-GRAPH-INTEGRITY-LOCK v1.0 — Section 2

    Returns {caller_name: [callee_name, ...]} — only known symbols appear as keys
    or values.  The source_symbol_id is always a real persisted symbol; call edges
    are only created when the caller is a known symbol (never orphan edges).
    """
    if not known_symbol_names:
        return {}

    known = set(known_symbol_names)
    lines = content.splitlines()
    n = len(lines)

    # Locate each known top-level symbol and its start line (0-indexed)
    symbol_starts: List[Tuple[str, int]] = []
    for i, line in enumerate(lines):
        if line.startswith("def "):
            parts = line.split("def ", 1)
            if len(parts) > 1:
                name = parts[1].split("(")[0].strip()
                if name in known:
                    symbol_starts.append((name, i))
        elif line.startswith("class "):
            parts = line.split("class ", 1)
            if len(parts) > 1:
                raw = parts[1].strip()
                name = raw.split("(")[0].split(":")[0].strip()
                if name in known:
                    symbol_starts.append((name, i))

    result: Dict[str, List[str]] = {name: [] for name, _ in symbol_starts}

    for idx, (sym_name, start_idx) in enumerate(symbol_starts):
        # Body is lines after the def/class declaration
        body_start = start_idx + 1
        # Body ends when we hit a non-blank, non-indented line (next top-level def)
        body_end = n
        for j in range(body_start, n):
            raw_line = lines[j]
            if raw_line and not raw_line[0].isspace() and raw_line.strip():
                body_end = j
                break

        seen_callees: set = set()
        for j in range(body_start, body_end):
            stripped = lines[j].strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Match word tokens followed by '(' — these are function calls
            for call in re.findall(r'\b([A-Za-z_]\w*)\s*\(', stripped):
                if call in known and call != sym_name and call not in seen_callees:
                    result[sym_name].append(call)
                    seen_callees.add(call)

    return result


def resolve_import(
    import_str: str,
    source_path: str,
    all_paths: FrozenSet[str],
) -> Optional[str]:
    """
    Resolve a raw Python import string to a file path present in *all_paths*.

    MQP-STEERING-CONTRACT: REPO-GRAPH-INTEGRITY-LOCK v1.0 — Section 6

    Rules:
    - Relative imports (".utils", "..common") are resolved relative to the
      directory of *source_path*.
    - Absolute imports ("mymodule", "backend.app.models") are tried as a
      path from the repo root AND as a same-directory sibling.
    - Extension inference: .py, /__init__.py, .ts, .js are tried in order.
    - Returns the first match found in *all_paths*, or None (drop the edge).
    """
    source_dir = os.path.dirname(source_path)

    # Build list of candidate base paths to try (without extension)
    candidates: List[str] = []

    if import_str.startswith("."):
        # Relative import: count leading dots
        dots = len(import_str) - len(import_str.lstrip("."))
        module = import_str.lstrip(".")

        target_dir = source_dir
        for _ in range(dots - 1):
            parent = os.path.dirname(target_dir)
            target_dir = parent if parent != target_dir else target_dir

        if module:
            candidates.append(os.path.join(target_dir, module.replace(".", "/")))
        else:
            candidates.append(target_dir)
    else:
        # Absolute import — try repo-root path and same-directory sibling
        module_as_path = import_str.replace(".", "/")
        candidates.append(module_as_path)
        if source_dir:
            candidates.append(os.path.join(source_dir, module_as_path))

    extensions = [".py", "/__init__.py", ".ts", ".js"]
    for cand in candidates:
        for ext in extensions:
            full = cand + ext
            normalized = os.path.normpath(full).replace("\\", "/")
            if normalized in all_paths or full in all_paths:
                return normalized

    return None


def extract_graph(path: str, content: bytes) -> dict:
    text = content.decode(errors="ignore")

    language = detect_language(path)
    symbols: List[Tuple[str, str, int]] = []
    imports: List[str] = []
    entry_points: List[Tuple[str, int]] = []

    if language == "python":
        symbols = extract_python_symbols(text)
        imports = extract_imports(text)
        entry_points = extract_entry_points(text, language)
    elif language in ("typescript", "javascript"):
        entry_points = extract_entry_points(text, language)

    return {
        "language": language,
        "symbols": symbols,
        "imports": imports,
        "entry_points": entry_points,
    }
