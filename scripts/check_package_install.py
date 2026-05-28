#!/usr/bin/env python3
"""Verify installed import/package contract from outside the repository."""

from __future__ import annotations

import importlib
import re
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def pyproject_modules() -> set[str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"py-modules\s*=\s*\[(.*?)\]", text, re.DOTALL)
    if not match:
        raise RuntimeError("pyproject_missing_py_modules")
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def source_modules() -> set[str]:
    modules = {"claude_tmux_control", "transcript_events"}
    modules.update(path.stem for path in ROOT.glob("ctc_*.py"))
    return modules


def verify_pyproject_inventory() -> None:
    missing = sorted(source_modules() - pyproject_modules())
    if missing:
        raise RuntimeError("pyproject_missing_modules: " + ", ".join(missing))


def verify_imports_from_clean_cwd() -> None:
    modules = sorted(pyproject_modules())
    with tempfile.TemporaryDirectory() as tmp:
        original_cwd = Path.cwd()
        try:
            sys.path = [entry for entry in sys.path if Path(entry or ".").resolve() != ROOT]
            Path(tmp).resolve()
            import os

            os.chdir(tmp)
            for module in modules:
                importlib.import_module(module)
        finally:
            import os

            os.chdir(original_cwd)


def verify_pricing_data_file() -> None:
    ctc = importlib.import_module("claude_tmux_control")
    table = ctc.load_pricing_table()
    if not isinstance(table, dict) or "models" not in table:
        raise RuntimeError("pricing_table_unavailable")


def main() -> int:
    verify_pyproject_inventory()
    verify_imports_from_clean_cwd()
    verify_pricing_data_file()
    print("package install check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
