"""
backend.app.graph_extractor
============================
MQP-CONTRACT: GRAPH-EXTRACTION-LAYER v1.0

Memory-only, dependency-free graph extraction for ingested files.

Provides:
- Language detection by file extension
- Symbol extraction (functions, classes) from Python source
- Import/dependency extraction from source files
- Content hashing for deduplication
"""

from __future__ import annotations

import hashlib
import re
from typing import List, Tuple


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


def extract_graph(path: str, content: bytes) -> dict:
    text = content.decode(errors="ignore")

    language = detect_language(path)
    symbols = []
    imports = []

    if language == "python":
        symbols = extract_python_symbols(text)
        imports = extract_imports(text)

    return {
        "language": language,
        "symbols": symbols,
        "imports": imports,
    }
