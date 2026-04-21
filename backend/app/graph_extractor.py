"""
backend.app.graph_extractor
============================
MQP-CONTRACT: GRAPH-EXTRACTION-LAYER v1.0

Pure-Python, memory-only extraction of structural graph data from source files.

NO external dependencies.  NO filesystem access.  NO network calls.
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
            name = line.split("def ")[1].split("(")[0]
            symbols.append((name, "function", i + 1))

        if line.startswith("class "):
            name = line.split("class ")[1].split("(")[0].replace(":", "")
            symbols.append((name, "class", i + 1))

    return symbols


def extract_imports(content: str) -> List[str]:
    imports = []
    lines = content.splitlines()

    for line in lines:
        line = line.strip()

        if line.startswith("import "):
            imports.append(line.replace("import ", "").strip())

        if line.startswith("from "):
            parts = line.split(" ")
            if len(parts) > 1:
                imports.append(parts[1])

    return imports


def extract_graph(path: str, content: bytes) -> dict:
    text = content.decode(errors="ignore")

    language = detect_language(path)
    symbols: List[Tuple[str, str, int]] = []

    if language == "python":
        symbols = extract_python_symbols(text)

    imports = extract_imports(text)

    return {
        "language": language,
        "symbols": symbols,
        "imports": imports,
    }
