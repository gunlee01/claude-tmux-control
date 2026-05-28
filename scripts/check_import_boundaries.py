#!/usr/bin/env python3
"""Validate module dependency boundaries for ctc_* extraction modules."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
FACADE_MODULE = "claude_tmux_control"

FORBIDDEN_IMPORTS: dict[str, set[str]] = {
    "ctc_tmux": {"ctc_cli", "ctc_bridge_sessions"},
    "ctc_launch": {"ctc_cli", "ctc_bridge_sessions"},
    "ctc_pricing": {"ctc_cli", "ctc_bridge_sessions"},
    "ctc_state": {
        "ctc_cli",
        "ctc_bridge_sessions",
        "ctc_streaming",
        "ctc_transcripts",
        "ctc_reap",
    },
    "ctc_transcripts": {"ctc_cli", "ctc_bridge_sessions", "ctc_state", "ctc_streaming", "ctc_reap"},
    "ctc_streaming": {"ctc_cli", "ctc_bridge_sessions", "ctc_reap"},
    "ctc_reap": {"ctc_cli", "ctc_bridge_sessions"},
    "ctc_bridge_sessions": {"ctc_cli"},
}


def imported_roots(source: str) -> set[str]:
    tree = ast.parse(source)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split(".", 1)[0])
    return imports


def violations_for_sources(sources: Mapping[str, str]) -> list[str]:
    violations: list[str] = []
    for module, source in sorted(sources.items()):
        imports = imported_roots(source)
        forbidden = set(FORBIDDEN_IMPORTS.get(module, set()))
        if module.startswith("ctc_"):
            forbidden.add(FACADE_MODULE)
        for imported in sorted(imports & forbidden):
            violations.append(f"{module}: forbidden import: {imported}")
    return violations


def source_files() -> dict[str, str]:
    sources = {}
    for path in sorted(ROOT.glob("ctc_*.py")):
        sources[path.stem] = path.read_text(encoding="utf-8")
    return sources


def main() -> int:
    violations = violations_for_sources(source_files())
    if violations:
        for violation in violations:
            print(violation, file=sys.stderr)
        return 1
    print("import boundary check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
