#!/usr/bin/env python3
"""Run behavior-neutral refactor contract checks.

This script is intentionally limited to checks that do not require live Claude
auth. Use it before and after each module-extraction phase.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


PHASE_TESTS = {
    "0": ["tests.test_claude_tmux_control.RefactorCompatibilityContractTest"],
    "1": ["tests.test_claude_tmux_control.TranscriptTest", "tests.test_claude_tmux_control.StreamTest"],
    "2": [
        "tests.test_claude_tmux_control.TmuxControllerTest",
        "tests.test_claude_tmux_control.FollowUntilIdleTest",
        "tests.test_claude_tmux_control.RenderedFollowerTest",
    ],
    "3": ["tests.test_claude_tmux_control.CliTest", "tests.test_claude_tmux_control.HighLevelStreamSetupTest"],
    "4": ["tests.test_claude_tmux_control.TranscriptTest", "tests.test_claude_tmux_control.StreamTest"],
    "5": ["tests.test_claude_tmux_control.StreamTest", "tests.test_claude_tmux_control.HighLevelSessionInfoTest"],
    "6": ["tests.test_claude_tmux_control.HighLevelStreamSetupTest"],
    "7": ["tests.test_claude_tmux_control.ScreenStatusTest", "tests.test_claude_tmux_control.StreamTest"],
    "8": [
        "tests.test_claude_tmux_control.HighLevelStreamSetupTest",
        "tests.test_claude_tmux_control.HighLevelSessionInfoTest",
        "tests.test_claude_tmux_control.HighLevelAskTest",
        "tests.test_claude_tmux_control.HighLevelCancelTest",
        "tests.test_claude_tmux_control.HighLevelAttachTest",
        "tests.test_claude_tmux_control.HighLevelReplayTest",
        "tests.test_claude_tmux_control.ReapTest",
    ],
    "9": ["tests.test_claude_tmux_control.CliTest", "tests.test_claude_tmux_control.CliIntegrationTest"],
}


def py_compile_targets() -> list[str]:
    targets = [
        ROOT / "claude_tmux_control.py",
        ROOT / "transcript_events.py",
        ROOT / "scripts" / "stream_question.py",
        ROOT / "scripts" / "web_chat_client.py",
        ROOT / "scripts" / "check_docs.py",
        ROOT / "scripts" / "check_import_boundaries.py",
        ROOT / "scripts" / "check_package_install.py",
        ROOT / "scripts" / "refactor_contract_check.py",
    ]
    targets.extend(sorted(ROOT.glob("ctc_*.py")))
    return [str(path.relative_to(ROOT)) for path in targets if path.exists()]


def command_plan(phase: str) -> list[list[str]]:
    commands: list[list[str]] = []
    if phase == "all":
        commands.append([sys.executable, "-m", "unittest", "discover", "-s", "tests"])
    else:
        tests = PHASE_TESTS[phase]
        commands.append([sys.executable, "-m", "unittest", *tests])
    commands.append([sys.executable, "-m", "py_compile", *py_compile_targets()])
    commands.append([sys.executable, "scripts/check_docs.py"])
    commands.append([sys.executable, "scripts/check_import_boundaries.py"])
    commands.append([sys.executable, "claude_tmux_control.py", "--version"])
    commands.append([sys.executable, "claude_tmux_control.py", "--help"])
    return commands


def run_command(command: list[str], dry_run: bool) -> int:
    print("+ " + " ".join(command), flush=True)
    if dry_run:
        return 0
    completed = subprocess.run(command, cwd=ROOT, check=False)
    return completed.returncode


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run refactor contract checks.")
    parser.add_argument(
        "--phase",
        choices=["all", *sorted(PHASE_TESTS.keys(), key=int)],
        default="all",
        help="phase-specific test gate to run; 'all' runs the full local contract gate",
    )
    parser.add_argument("--dry-run", action="store_true", help="print commands without running them")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    for command in command_plan(args.phase):
        exit_code = run_command(command, args.dry_run)
        if exit_code != 0:
            return exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
