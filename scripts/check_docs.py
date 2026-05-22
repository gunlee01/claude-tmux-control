#!/usr/bin/env python3
"""Validate Markdown language switches, paired translations, and local links."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
LANGUAGE_SWITCH_LINE = 3
LINK_PATTERN = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def read_line(path: Path, line_no: int) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < line_no:
        return ""
    return lines[line_no - 1].strip()


def expected_switch(path: Path) -> str | None:
    if path == ROOT / "README.md":
        return "[English](./README.md) | [한국어](./README.ko.md)"
    if path == ROOT / "README.ko.md":
        return "[English](./README.md) | [한국어](./README.ko.md)"
    if path == ROOT / "examples" / "README.md":
        return "[English](./README.md) | [한국어](./README.ko.md)"
    if path == ROOT / "examples" / "README.ko.md":
        return "[English](./README.md) | [한국어](./README.ko.md)"
    if path.parent == ROOT / "docs" and path.suffix == ".md":
        if path.name.endswith(".ko.md"):
            english_name = path.name.replace(".ko.md", ".md")
            return f"[English](./{english_name}) | [한국어](./{path.name})"
        korean_name = path.name.replace(".md", ".ko.md")
        return f"[English](./{path.name}) | [한국어](./{korean_name})"
    return None


def validate_language_switches(errors: list[str]) -> None:
    docs = [
        ROOT / "README.md",
        ROOT / "README.ko.md",
        ROOT / "examples" / "README.md",
        ROOT / "examples" / "README.ko.md",
    ]
    docs.extend(sorted((ROOT / "docs").glob("*.md")))

    for path in docs:
        expected = expected_switch(path)
        if expected is None:
            continue
        actual = read_line(path, LANGUAGE_SWITCH_LINE)
        if actual != expected:
            fail(errors, f"{path.relative_to(ROOT)}: expected language switch {expected!r}, got {actual!r}")


def validate_translation_pairs(errors: list[str]) -> None:
    pairs = [ROOT / "README.md"]
    pairs.append(ROOT / "examples" / "README.md")
    pairs.extend(path for path in sorted((ROOT / "docs").glob("*.md")) if not path.name.endswith(".ko.md"))

    for english in pairs:
        korean = english.with_name(english.name.replace(".md", ".ko.md"))
        if english.name == "README.md" and english.parent == ROOT:
            korean = ROOT / "README.ko.md"
        if not korean.exists():
            fail(errors, f"{english.relative_to(ROOT)}: missing Korean pair {korean.relative_to(ROOT)}")

    for korean in [ROOT / "README.ko.md", *sorted((ROOT / "docs").glob("*.ko.md"))]:
        if korean == ROOT / "README.ko.md":
            english = ROOT / "README.md"
        else:
            english = korean.with_name(korean.name.replace(".ko.md", ".md"))
        if not english.exists():
            fail(errors, f"{korean.relative_to(ROOT)}: missing English pair {english.relative_to(ROOT)}")

    for korean in [ROOT / "examples" / "README.ko.md"]:
        english = korean.with_name(korean.name.replace(".ko.md", ".md"))
        if not english.exists():
            fail(errors, f"{korean.relative_to(ROOT)}: missing English pair {english.relative_to(ROOT)}")


def is_external_link(target: str) -> bool:
    return target.startswith(("http://", "https://", "mailto:", "tel:"))


def validate_local_links(errors: list[str]) -> None:
    for path in sorted(ROOT.rglob("*.md")):
        if any(part in {".git", ".venv", "venv", "temporary_docs"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        for match in LINK_PATTERN.finditer(text):
            target = match.group(1).strip()
            if not target or target.startswith("#") or is_external_link(target):
                continue
            target_without_anchor = target.split("#", 1)[0]
            if not target_without_anchor:
                continue
            local_target = (path.parent / unquote(target_without_anchor)).resolve()
            try:
                local_target.relative_to(ROOT)
            except ValueError:
                fail(errors, f"{path.relative_to(ROOT)}: link escapes repository: {target}")
                continue
            if not local_target.exists():
                fail(errors, f"{path.relative_to(ROOT)}: missing link target: {target}")


def main() -> int:
    errors: list[str] = []
    validate_language_switches(errors)
    validate_translation_pairs(errors)
    validate_local_links(errors)

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print("docs check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
