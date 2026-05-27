#!/usr/bin/env python3
"""Control an interactive Claude Code session through tmux."""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
import socket
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Protocol, Sequence


RunFn = Callable[..., subprocess.CompletedProcess[str]]
PACKAGE_NAME = "claude-tmux-control"
CLAUDE_OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_DANGEROUS_SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
CLAUDE_LAUNCH_COMMANDS = {"start", "launch", "chat"}
CLAUDE_EXECUTABLE = "claude"
DEFAULT_BUFFER_NAME = "claude-tmux-control"
DEFAULT_TRANSCRIPT_ROOT = Path.home() / ".claude"
DEFAULT_STATE_DIR = Path.home() / ".cache" / "claude-tmux-control"
DEFAULT_PRICING_TABLE = Path(__file__).with_name("claude_pricing.json")
DEFAULT_INSTALLED_PRICING_TABLE = Path(sys.prefix) / "share" / "claude-tmux-control" / "claude_pricing.json"
DEFAULT_CONTROLLED_PREFIX = "ctc-"
DEFAULT_WEB_SESSION_PREFIX = "ctc-csess-"
DEFAULT_ENV_FILE_NAME = ".ctc.env"
DEFAULT_PASTE_SUBMIT_DELAY_SECONDS = 0.25
UNANCHORED_SUBMIT_RETRY_SECONDS = 1.0
DEFAULT_TOOL_RESULT_TEXT_LIMIT = 100
STATE_SCHEMA_VERSION = 1
_PRICING_TABLE_CACHE: dict | None = None
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED_ENV_NAMES = {CLAUDE_OAUTH_TOKEN_ENV}
WORKING_PATTERNS = (
    re.compile(r"\besc\b.*\binterrupt\b", re.IGNORECASE),
    re.compile(r"\bthinking\b", re.IGNORECASE),
    re.compile(r"\brunning\b", re.IGNORECASE),
    re.compile(r"\bworking\b", re.IGNORECASE),
)
CONFIRMATION_PATTERNS = (
    re.compile(r"\ballow\b.*\?", re.IGNORECASE | re.DOTALL),
    re.compile(r"\byes\s*/\s*no\b", re.IGNORECASE),
    re.compile(r"\b(y/n|y/N|Y/n)\b"),
)
READY_PATTERNS = (
    re.compile(r"(^|\n)\s*claude>\s*$", re.IGNORECASE),
    re.compile(r"(^|\n)\s*>\s*$"),
    re.compile(r"(^|\n)\s*[❯❯›]\s*.*(?:\n|$)"),
)


def package_version() -> str:
    pyproject_path = Path(__file__).with_name("pyproject.toml")
    if pyproject_path.exists():
        in_project_section = False
        project_name: str | None = None
        project_version: str | None = None
        for line in pyproject_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                in_project_section = stripped == "[project]"
                continue
            if not in_project_section or "=" not in stripped:
                continue
            key, value = [part.strip() for part in stripped.split("=", 1)]
            if key == "name":
                project_name = value.strip('"')
            elif key == "version":
                project_version = value.strip('"')
        if project_name == PACKAGE_NAME and project_version:
            return project_version

    try:
        from importlib.metadata import version

        return version(PACKAGE_NAME)
    except Exception:
        return "0+unknown"


class ScreenCaptureController(Protocol):
    def capture_screen(self, session: str, height: int = 200) -> str:
        ...


class SessionNotFoundError(RuntimeError):
    pass


class StateGenerationConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ScreenStatus:
    state: str
    reason: str


@dataclass(frozen=True)
class SessionState:
    session: str
    last_prompt: str
    cwd: str | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class TranscriptRecord:
    event: dict
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class StreamRuntime:
    session_id: str
    tmux_session: str
    state_path: Path
    state_dir: Path
    cwd: Path
    prompt: str
    turn_id: str
    before_send_offset: int
    replay_start_offset: int
    before_send_transcript: Path | None = None
    started_at_monotonic: float = 0.0
    started_at_utc: str | None = None


class RenderedScreenFollower:
    def __init__(self):
        self._previous: str | None = None

    def diff(self, screen: str) -> str:
        if self._previous is None:
            self._previous = screen
            return screen

        previous = self._previous
        self._previous = screen

        if screen == previous:
            return ""
        if screen.startswith(previous):
            return screen[len(previous) :]
        return f"\n--- screen changed ---\n{screen}"


class TmuxController:
    def __init__(self, run: RunFn = subprocess.run):
        self._run = run

    def start_session(
        self,
        session: str,
        command: str = "claude",
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> bool:
        if self.session_exists(session):
            return False

        args = ["tmux", "new-session", "-d", "-s", session]
        for key, value in sorted((env or {}).items()):
            args.extend(["-e", f"{key}={value}"])
        if cwd is not None:
            args.extend(["-c", str(cwd)])
        args.append(command)
        self._run(args, check=True)
        return True

    def session_exists(self, session: str) -> bool:
        result = self._run(["tmux", "has-session", "-t", session], check=False, capture_output=True, text=True)
        return result.returncode == 0

    def list_sessions(self) -> list[str]:
        result = self._run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def kill_session(self, session: str) -> None:
        if not self.session_exists(session):
            raise SessionNotFoundError(f"tmux session not found: {session}")
        self._run(["tmux", "kill-session", "-t", session], check=True)

    def send_prompt(self, session: str, prompt: str, submit: bool = True) -> None:
        self._run(["tmux", "load-buffer", "-b", DEFAULT_BUFFER_NAME, "-"], input=prompt, text=True, check=True)
        paste_args = ["tmux", "paste-buffer", "-d", "-b", DEFAULT_BUFFER_NAME, "-t", session]
        if "\n" in prompt or "\r" in prompt:
            paste_args.insert(2, "-p")
        self._run(paste_args, check=True)
        if submit:
            time.sleep(DEFAULT_PASTE_SUBMIT_DELAY_SECONDS)
            self.send_enter(session)

    def send_enter(self, session: str) -> None:
        self._run(["tmux", "send-keys", "-t", session, "Enter"], check=True)

    def send_escape(self, session: str) -> None:
        self._run(["tmux", "send-keys", "-t", session, "Escape"], check=True)

    def launch_in_existing_session(self, session: str, command: str = "claude") -> None:
        if not self.session_exists(session):
            raise SessionNotFoundError(f"tmux session not found: {session}")
        self.send_prompt(session, command)

    def capture_screen(self, session: str, height: int = 200) -> str:
        result = self._run(
            ["tmux", "capture-pane", "-p", "-t", session, "-S", f"-{height}"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.rstrip("\n") + "\n" if result.stdout else ""

    def attach(self, session: str) -> None:
        self._run(["tmux", "attach-session", "-t", session], check=True)

    def pane_current_path(self, session: str) -> Path | None:
        result = self._run(
            ["tmux", "display-message", "-p", "-t", session, "#{pane_current_path}"],
            check=True,
            capture_output=True,
            text=True,
        )
        path = result.stdout.strip()
        return Path(path) if path else None


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    argv = _normalize_claude_args_option_values(argv)
    parser = argparse.ArgumentParser(
        prog="ctc",
        description="Start, feed, and read an interactive Claude Code session through tmux.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""High-level web/client commands:
  ctc stream --cwd PATH [--session-id UUID] PROMPT
  ctc stream --attach --session-id UUID
  ctc ask --cwd PATH [--session-id UUID] PROMPT
  ctc cancel UUID
  ctc last UUID --last 1
  ctc replay UUID --last 1
  ctc info UUID --json
  ctc list --json
  ctc stats [UUID] --json
  ctc reap --idle-seconds 1800 --prefix ctc-

Low-level tmux/debug commands:
  ctc start TMUX_SESSION --cwd PATH
  ctc send TMUX_SESSION PROMPT
  ctc answer TMUX_SESSION
  ctc turn TMUX_SESSION
  ctc events TMUX_SESSION

Do not pass ctc-csess-$SESSION_ID to low-level start.
Use ctc stream --session-id "$SESSION_ID" to create or resume web sessions.

Docs:
  docs/quickstart-web-client.md
  docs/cli-manual.md
  docs/operations.md""",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {package_version()}")
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    start = subparsers.add_parser("start", help="LOW: create/reuse a named tmux session for manual debugging.")
    start.add_argument("session", help="tmux session name")
    start.add_argument("--cwd", default=str(Path.cwd()), help="working directory for a new session")
    start.add_argument("--attach", action="store_true", help="attach to the tmux session after starting it")
    add_claude_launch_args(start)
    start.add_argument(
        "--oauth-token-env",
        default=CLAUDE_OAUTH_TOKEN_ENV,
        help=f"source environment variable to pass as {CLAUDE_OAUTH_TOKEN_ENV}",
    )
    add_environment_args(start)

    launch = subparsers.add_parser("launch", help="LOW: run Claude Code inside an existing tmux session.")
    launch.add_argument("session", help="tmux session name")
    add_claude_launch_args(launch)

    send = subparsers.add_parser("send", help="LOW: paste text into a tmux session.")
    send.add_argument("session", help="tmux session name")
    send.add_argument("prompt", nargs="*", help="prompt text; reads stdin when omitted")
    send.add_argument("--no-enter", action="store_true", help="paste only, without pressing Enter")

    capture = subparsers.add_parser("capture", help="LOW: print rendered tmux pane text.")
    capture.add_argument("session", help="tmux session name")
    capture.add_argument("--height", type=int, default=200, help="number of pane history lines to capture")

    watch = subparsers.add_parser("watch", help="LOW: continuously print changed tmux pane text.")
    watch.add_argument("session", help="tmux session name")
    watch.add_argument("--height", type=int, default=200, help="number of pane history lines to capture")
    watch.add_argument("--interval", type=float, default=1.0, help="seconds between captures")

    follow = subparsers.add_parser("follow", help="LOW: append rendered screen changes to stdout and optionally a file.")
    follow.add_argument("session", help="tmux session name")
    follow.add_argument("--height", type=int, default=200, help="number of pane history lines to capture")
    follow.add_argument("--interval", type=float, default=0.5, help="seconds between captures")
    follow.add_argument("--append", type=Path, help="file to append rendered screen changes")

    status = subparsers.add_parser("status", help="LOW: infer whether a tmux session is working, ready, or waiting.")
    status.add_argument("session", help="tmux session name")
    status.add_argument("--height", type=int, default=80, help="number of pane history lines to inspect")
    status.add_argument("--transcript", type=Path, help="specific transcript JSONL path")
    status.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
    status.add_argument("--screen-only", action="store_true", help="do not use transcript state")

    wait_ready = subparsers.add_parser("wait-ready", help="LOW: wait until rendered screen looks ready for input.")
    wait_ready.add_argument("session", help="tmux session name")
    wait_ready.add_argument("--height", type=int, default=80, help="number of pane history lines to inspect")
    wait_ready.add_argument("--interval", type=float, default=0.5, help="seconds between captures")
    wait_ready.add_argument("--timeout", type=float, default=120.0, help="maximum seconds to wait")
    wait_ready.add_argument("--idle", type=float, default=2.0, help="screen must stay stable this many seconds")
    wait_ready.add_argument("--transcript", type=Path, help="specific transcript JSONL path")
    wait_ready.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
    wait_ready.add_argument("--screen-only", action="store_true", help="do not use transcript state")

    events = subparsers.add_parser("events", help="LOW: read transcript events for a tmux session or explicit transcript.")
    events.add_argument("session", nargs="?", help="tmux session name used to resolve the matching transcript")
    events.add_argument("--transcript", type=Path, help="specific transcript JSONL path")
    events.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
    events.add_argument("--tail", type=int, default=20, help="number of latest events to print")
    events.add_argument("--follow", action="store_true", help="keep reading new transcript events")
    events.add_argument("--json", action="store_true", help="print raw JSON events")

    answer = subparsers.add_parser("answer", help="LOW: print latest assistant text answer for a tmux session.")
    answer.add_argument("session", help="tmux session name used to resolve the matching transcript")
    answer.add_argument("--transcript", type=Path, help="specific transcript JSONL path")
    answer.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
    answer.add_argument("--wait", action="store_true", help="wait until the session is ready before printing")
    answer.add_argument("--timeout", type=float, default=120.0, help="maximum seconds to wait with --wait")
    answer.add_argument("--count", "--tail", dest="count", type=int, default=1, help="number of recent answers to print")

    turn = subparsers.add_parser("turn", help="LOW: print latest turn for a tmux session.")
    turn.add_argument("session", help="tmux session name used to resolve the matching transcript")
    turn.add_argument("--transcript", type=Path, help="specific transcript JSONL path")
    turn.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
    turn.add_argument("--follow", action="store_true", help="keep refreshing the latest turn")
    turn.add_argument("--interval", type=float, default=1.0, help="seconds between refreshes with --follow")
    turn.add_argument("--count", "--tail", dest="count", type=int, default=1, help="number of recent turns to print")

    info = subparsers.add_parser("info", help="WEB: print high-level web session metadata.")
    info.add_argument("session_id", help="web-facing Claude session id UUID")
    info.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="bridge state directory")
    info.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
    info.add_argument("--json", action="store_true", help="print machine-readable JSON")

    list_cmd = subparsers.add_parser("list", help="WEB: list high-level controlled web sessions.")
    list_cmd.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="bridge state directory")
    list_cmd.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
    list_cmd.add_argument("--json", action="store_true", help="print machine-readable JSON")

    stats = subparsers.add_parser("stats", help="WEB/LOW: print transcript model, usage, and context stats.")
    stats.add_argument("session", nargs="?", help="web session id UUID or low-level tmux session name")
    stats.add_argument("--session-id", dest="session_id_option", help="web-facing Claude session id UUID")
    stats.add_argument("--transcript", type=Path, help="specific transcript JSONL path")
    stats.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="bridge state directory")
    stats.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
    stats.add_argument("--json", action="store_true", help="print machine-readable JSON")

    ask = subparsers.add_parser("ask", help="WEB: run one high-level turn and print final answer/metrics JSON.")
    ask.add_argument("prompt", nargs="*", help="prompt text")
    ask.add_argument("--session-id", help="web-facing Claude session id UUID for high-level ask")
    ask.add_argument("--cwd", type=Path, required=True, help="working directory for high-level ask")
    add_claude_launch_args(ask)
    ask.add_argument(
        "--oauth-token-env",
        default=CLAUDE_OAUTH_TOKEN_ENV,
        help=f"source environment variable to pass as {CLAUDE_OAUTH_TOKEN_ENV}",
    )
    add_environment_args(ask)
    ask.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="bridge state directory")
    ask.add_argument("--transcript", type=Path, help="specific transcript JSONL path")
    ask.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
    ask.add_argument("--interval", type=float, default=2.0, help="seconds between transcript checks")
    ask.add_argument("--timeout", type=float, default=300.0, help="maximum seconds to wait")
    ask.add_argument("--idle", type=float, default=2.0, help="ready state must remain stable this many seconds")
    ask.add_argument(
        "--tool-result-limit",
        type=int,
        default=DEFAULT_TOOL_RESULT_TEXT_LIMIT,
        help="maximum tool_result text/result preview characters; negative disables truncation",
    )
    ask.add_argument("--json", action="store_true", help="accepted for symmetry; ask always prints JSON")

    cancel = subparsers.add_parser(
        "cancel",
        help="WEB: send Escape to cancel the active Claude Code turn for a high-level session.",
    )
    cancel.add_argument("session_id", nargs="?", help="web-facing Claude session id UUID")
    cancel.add_argument("--session-id", dest="session_id_option", help="web-facing Claude session id UUID")
    cancel.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="bridge state directory")
    cancel.add_argument("--json", action="store_true", help="accepted for symmetry; cancel always prints JSON")
    cancel.add_argument("--reset", action="store_true", help="move active_turn to last_turn after cancelling")

    def add_replay_args(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("session_id", nargs="?", help="web-facing Claude session id UUID")
        command_parser.add_argument("--session-id", dest="session_id_option", help="web-facing Claude session id UUID")
        command_parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="bridge state directory")
        command_parser.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
        command_parser.add_argument("--last", "--count", "--tail", dest="count", type=int, default=1, help="number of recent turns to replay")
        command_parser.add_argument("--interval", type=float, default=2.0, help="seconds between transcript checks for active turn attach")
        command_parser.add_argument("--timeout", type=float, default=300.0, help="maximum seconds to stream active last turn")
        command_parser.add_argument("--idle", type=float, default=2.0, help="ready state must remain stable this many seconds")
        command_parser.add_argument(
            "--tool-result-limit",
            type=int,
            default=DEFAULT_TOOL_RESULT_TEXT_LIMIT,
            help="maximum tool_result text/result preview characters; negative disables truncation",
        )

    last = subparsers.add_parser(
        "last",
        help="WEB: alias for replaying the last high-level turn events.",
    )
    add_replay_args(last)

    replay = subparsers.add_parser(
        "replay",
        help="WEB: replay recent high-level turn events, attaching to the active last turn when present.",
    )
    add_replay_args(replay)

    stream = subparsers.add_parser("stream", help="WEB/LOW: stream high-level turn, or low-level tmux turn without --cwd.")
    stream.add_argument(
        "session",
        nargs="?",
        help="low-level tmux session name, or first prompt word when --cwd is used",
    )
    stream.add_argument("prompt", nargs="*", help="high-level prompt text when --cwd is used")
    stream.add_argument("--session-id", help="web-facing Claude session id UUID for high-level stream")
    stream.add_argument("--cwd", type=Path, help="working directory for high-level stream")
    add_claude_launch_args(stream)
    stream.add_argument(
        "--oauth-token-env",
        default=CLAUDE_OAUTH_TOKEN_ENV,
        help=f"source environment variable to pass as {CLAUDE_OAUTH_TOKEN_ENV}",
    )
    add_environment_args(stream)
    stream.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="bridge state directory")
    stream.add_argument("--transcript", type=Path, help="specific transcript JSONL path")
    stream.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
    stream.add_argument("--interval", type=float, default=2.0, help="seconds between transcript checks")
    stream.add_argument("--timeout", type=float, default=300.0, help="maximum seconds to stream")
    stream.add_argument("--idle", type=float, default=2.0, help="ready state must remain stable this many seconds")
    stream.add_argument("--attach", action="store_true", help="attach to the active high-level turn without sending")
    stream.add_argument(
        "--tool-result-limit",
        type=int,
        default=DEFAULT_TOOL_RESULT_TEXT_LIMIT,
        help="maximum tool_result text/result preview characters; negative disables truncation",
    )

    kill = subparsers.add_parser("kill", help="OPS: terminate one tmux session by tmux session name.")
    kill.add_argument("session", help="tmux session name")

    reap = subparsers.add_parser("reap", help="OPS: terminate controlled tmux sessions idle for too long.")
    reap.add_argument("--idle-seconds", type=float, required=True, help="minimum input idle seconds before killing")
    reap.add_argument("--prefix", default=DEFAULT_CONTROLLED_PREFIX, help="only reap sessions with this prefix")
    reap.add_argument("--dry-run", action="store_true", help="print sessions that would be killed without killing")
    reap.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
    reap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="claude-tmux-control state directory")

    chat = subparsers.add_parser("chat", help="LOW: create/reuse a tmux session, then send prompts interactively.")
    chat.add_argument("session", help="tmux session name")
    chat.add_argument("--cwd", default=str(Path.cwd()), help="working directory for a new session")
    add_claude_launch_args(chat)
    chat.add_argument(
        "--oauth-token-env",
        default=CLAUDE_OAUTH_TOKEN_ENV,
        help=f"source environment variable to pass as {CLAUDE_OAUTH_TOKEN_ENV}",
    )
    add_environment_args(chat)
    chat.add_argument("--height", type=int, default=200, help="number of pane history lines to capture")
    chat.add_argument("--interval", type=float, default=0.5, help="seconds between captures after sending input")
    chat.add_argument("--idle", type=float, default=2.0, help="return to the input prompt after this many stable seconds")

    return parser.parse_args(argv)


def _normalize_claude_args_option_values(argv: Sequence[str]) -> list[str]:
    values = list(argv)
    normalized: list[str] = []
    index = 0
    while index < len(values):
        value = values[index]
        if value == "--claude-args" and index + 1 < len(values):
            normalized.append(f"--claude-args={values[index + 1]}")
            index += 2
            continue
        normalized.append(value)
        index += 1
    return normalized


def add_claude_launch_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", help="Claude model for newly launched Claude Code sessions")
    parser.add_argument(
        "--claude-args",
        dest="claude_args_string",
        help="trusted Claude Code CLI arguments, parsed without shell execution",
    )


def add_environment_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--env",
        dest="env_names",
        action="append",
        default=[],
        metavar="NAME",
        help="copy a named environment variable from the ctc process into a newly created tmux session",
    )
    parser.add_argument(
        "--env-file",
        dest="env_files",
        action="append",
        type=Path,
        default=[],
        metavar="PATH",
        help=f"read environment variables from PATH for newly created tmux sessions; defaults to cwd/{DEFAULT_ENV_FILE_NAME}",
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    controller = TmuxController()

    dependency_error = check_runtime_dependencies(args)
    if dependency_error:
        print(dependency_error, file=sys.stderr)
        return 127

    try:
        return _run_command(args, controller)
    except SessionNotFoundError as error:
        print(str(error), file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        print(f"tmux command failed: {' '.join(map(str, error.cmd))}", file=sys.stderr)
        if error.stderr:
            print(error.stderr, file=sys.stderr)
        return error.returncode or 1


def _run_command(args: argparse.Namespace, controller: TmuxController) -> int:
    if args.command_name == "start":
        if controller.session_exists(args.session):
            created = False
        else:
            try:
                env = claude_environment_from_args(args)
                claude_args = claude_args_from_options(args)
            except ValueError as error:
                print(str(error), file=sys.stderr)
                return 2
            created = controller.start_session(
                args.session,
                build_claude_command(claude_args),
                args.cwd,
                env=env,
            )
        write_session_state(_session_state_path(args.session), args.session, "", Path(args.cwd))
        print(f"{'created' if created else 'reused'} session: {args.session}")
        if args.attach:
            controller.attach(args.session)
        return 0

    if args.command_name == "launch":
        try:
            claude_args = claude_args_from_options(args)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
        controller.launch_in_existing_session(args.session, build_claude_command(claude_args))
        print(f"launched command in existing session: {args.session}")
        return 0

    if args.command_name == "send":
        prompt = " ".join(args.prompt) if args.prompt else sys.stdin.read()
        controller.send_prompt(args.session, prompt, submit=not args.no_enter)
        if not args.no_enter:
            write_session_state(_session_state_path(args.session), args.session, prompt, controller.pane_current_path(args.session))
        return 0

    if args.command_name == "capture":
        print(controller.capture_screen(args.session, args.height), end="")
        return 0

    if args.command_name == "watch":
        return _watch(controller, args.session, args.height, args.interval)

    if args.command_name == "follow":
        return _follow_screen(controller, args.session, args.height, args.interval, args.append)

    if args.command_name == "status":
        state = read_session_state(_session_state_path(args.session)) or SessionState(
            session=args.session,
            last_prompt="",
            cwd=str(controller.pane_current_path(args.session) or ""),
        )
        transcript_path = None
        pending_prompt = False
        if not args.screen_only:
            transcript_path, pending_prompt = resolve_status_transcript_path(args.root, state, args.transcript)
        status = (
            ScreenStatus("working", "waiting for transcript to record last prompt")
            if pending_prompt
            else analyze_combined_status(controller.capture_screen(args.session, args.height), transcript_path=transcript_path)
        )
        print(f"{status.state}: {status.reason}")
        return 0

    if args.command_name == "wait-ready":
        state = read_session_state(_session_state_path(args.session)) or SessionState(
            session=args.session,
            last_prompt="",
            cwd=str(controller.pane_current_path(args.session) or ""),
        )
        status = wait_until_ready(
            controller,
            args.session,
            height=args.height,
            interval=args.interval,
            timeout=args.timeout,
            idle_seconds=args.idle,
            transcript_resolver=None
            if args.screen_only
            else lambda: resolve_status_transcript_path(args.root, state, args.transcript),
        )
        print(f"{status.state}: {status.reason}")
        return 0 if status.state == "ready" else 3

    if args.command_name == "events":
        transcript = args.transcript or _resolve_events_transcript(args, controller)
        if transcript is None:
            print(f"no transcript found under {args.root}", file=sys.stderr)
            return 2
        return _print_transcript_events(transcript, args.tail, args.follow, args.json)

    if args.command_name == "answer":
        return _print_latest_answer(args, controller)

    if args.command_name == "turn":
        return _print_latest_turn(args, controller)

    if args.command_name == "info":
        return _print_high_level_info(args, controller)

    if args.command_name == "list":
        return _print_high_level_list(args, controller)

    if args.command_name == "stats":
        return _print_stats(args, controller)

    if args.command_name == "ask":
        return _run_high_level_ask(args, controller)

    if args.command_name == "cancel":
        return _run_high_level_cancel(args, controller)

    if args.command_name in {"last", "replay"}:
        return _run_high_level_replay(args, controller)

    if args.command_name == "stream":
        if _is_high_level_stream_args(args):
            return _run_high_level_stream(args, controller)
        if getattr(args, "model", None) or getattr(args, "claude_args_string", None):
            print("claude_launch_args_require_cwd", file=sys.stderr)
            return 2
        if not args.session:
            print("stream requires SESSION for low-level mode or --cwd for high-level mode", file=sys.stderr)
            return 2
        return _print_stream(args, controller)

    if args.command_name == "kill":
        controller.kill_session(args.session)
        _remove_session_state(args.session)
        print(f"killed session: {args.session}")
        return 0

    if args.command_name == "reap":
        results = reap_idle_sessions(
            controller,
            idle_seconds=args.idle_seconds,
            prefix=args.prefix,
            dry_run=args.dry_run,
            state_dir=args.state_dir,
            root=args.root,
        )
        if not results:
            print("no idle sessions")
            return 0
        for result in results:
            print(f"{result['action']} {result['session']} idle={result['idle_seconds']:.0f}s")
        return 0

    if args.command_name == "chat":
        if not controller.session_exists(args.session):
            try:
                env = claude_environment_from_args(args)
                claude_args = claude_args_from_options(args)
            except ValueError as error:
                print(str(error), file=sys.stderr)
                return 2
            controller.start_session(
                args.session,
                build_claude_command(claude_args),
                args.cwd,
                env=env,
            )
        write_session_state(_session_state_path(args.session), args.session, "", Path(args.cwd))
        return _chat(controller, args.session, args.height, args.interval, args.idle)

    raise ValueError(f"unsupported command: {args.command_name}")


def claude_args_from_options(args: argparse.Namespace) -> list[str]:
    try:
        values = shlex.split(getattr(args, "claude_args_string", None) or "")
    except ValueError as error:
        raise ValueError("invalid_claude_args") from error
    model = getattr(args, "model", None)
    if model and _has_model_option(values):
        raise ValueError("duplicate_model")
    if model:
        values.extend(["--model", model])
    return values


def build_claude_command(claude_args: Sequence[str] = ()) -> str:
    args = list(claude_args)
    if not _has_permission_override(args):
        args.append(CLAUDE_DANGEROUS_SKIP_PERMISSIONS_FLAG)
    return _shell_join([CLAUDE_EXECUTABLE, *args])


def claude_environment_from_args(
    args: argparse.Namespace,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ if environ is None else environ
    result: dict[str, str] = {}

    for env_file in _env_files_from_args(args):
        result.update(read_env_file(env_file))

    for name in getattr(args, "env_names", []) or []:
        _validate_env_name(name)
        if name in RESERVED_ENV_NAMES:
            raise ValueError(f"reserved_env: {name}")
        if name not in env:
            raise ValueError(f"missing_env: {name}")
        result[name] = env[name]

    source_env = getattr(args, "oauth_token_env", None)
    if not source_env:
        return result
    token = env.get(source_env)
    if not token:
        return result
    result[CLAUDE_OAUTH_TOKEN_ENV] = token
    return result


def preseed_claude_project_trust(
    cwd: Path,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> None:
    trusted_dir = str(cwd.expanduser().resolve())
    home_dir = home.expanduser() if home is not None else Path.home()
    config_dir_value = (env or {}).get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_CONFIG_DIR")
    config_dir = Path(config_dir_value).expanduser() if config_dir_value else home_dir / ".claude"
    config_dir.mkdir(parents=True, exist_ok=True)

    global_config_path = home_dir / ".claude.json"
    global_config = _read_json_object(global_config_path)
    projects = global_config.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    project = projects.get(trusted_dir)
    if not isinstance(project, dict):
        project = {}
    if not isinstance(project.get("allowedTools"), list):
        project["allowedTools"] = []
    project["hasTrustDialogAccepted"] = True
    project["hasCompletedProjectOnboarding"] = True
    project["projectOnboardingSeenCount"] = max(int(project.get("projectOnboardingSeenCount") or 0), 4)
    projects[trusted_dir] = project
    global_config["hasCompletedOnboarding"] = True
    global_config["projects"] = projects
    _write_json_object(global_config_path, global_config)

    settings_path = config_dir / "settings.json"
    settings = _read_json_object(settings_path)
    settings["skipDangerousModePermissionPrompt"] = True
    _write_json_object(settings_path, settings)


def _read_json_object(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_object(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _env_files_from_args(args: argparse.Namespace) -> list[Path]:
    explicit = list(getattr(args, "env_files", []) or [])
    if explicit:
        return [path.expanduser() for path in explicit]

    cwd = getattr(args, "cwd", None)
    if cwd is None:
        return []
    default_path = Path(cwd).expanduser().resolve() / DEFAULT_ENV_FILE_NAME
    return [default_path] if default_path.exists() else []


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError(f"invalid_env_file: {path}") from error

    for index, original_line in enumerate(lines, start=1):
        line = original_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ValueError(f"invalid_env_file: {path}:{index}")
        name, value = line.split("=", 1)
        name = name.strip()
        if not ENV_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"invalid_env_file: {path}:{index}")
        if name in RESERVED_ENV_NAMES:
            raise ValueError(f"reserved_env: {name}")
        values[name] = _unquote_env_value(value.strip())
    return values


def _validate_env_name(name: str) -> None:
    if not ENV_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"invalid_env: {name}")


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _has_permission_override(claude_args: Sequence[str]) -> bool:
    return any(
        arg == CLAUDE_DANGEROUS_SKIP_PERMISSIONS_FLAG
        or arg == "--permission-mode"
        or arg.startswith("--permission-mode=")
        for arg in claude_args
    )


def _has_model_option(claude_args: Sequence[str]) -> bool:
    return any(arg == "--model" or arg.startswith("--model=") for arg in claude_args)


def _shell_join(args: Sequence[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def check_runtime_dependencies(
    args: argparse.Namespace,
    which: Callable[[str], str | None] = shutil.which,
) -> str | None:
    if not which("tmux"):
        return "\n".join(
            [
                "tmux not found in PATH.",
                "Install tmux first, then retry.",
                "Example: sudo yum install -y tmux",
            ]
        )

    if args.command_name not in CLAUDE_LAUNCH_COMMANDS and not (
        args.command_name in {"stream", "ask"} and getattr(args, "cwd", None)
    ):
        return None

    if not which(CLAUDE_EXECUTABLE):
        return "\n".join(
            [
                f"Claude Code executable not found in PATH: {CLAUDE_EXECUTABLE}",
                "Install Claude Code CLI first, then retry.",
                "Example: curl -fsSL https://claude.ai/install.sh | bash",
                "After install, confirm with: claude --version",
            ]
        )
    return None


def _resolve_events_transcript(args: argparse.Namespace, controller: TmuxController) -> Path | None:
    if not args.session:
        return find_latest_transcript(args.root)

    state = read_session_state(_session_state_path(args.session)) or SessionState(
        session=args.session,
        last_prompt="",
        cwd=str(controller.pane_current_path(args.session) or ""),
    )
    return resolve_transcript_path(args.root, state)


def _print_latest_answer(args: argparse.Namespace, controller: TmuxController) -> int:
    state = read_session_state(_session_state_path(args.session)) or SessionState(
        session=args.session,
        last_prompt="",
        cwd=str(controller.pane_current_path(args.session) or ""),
    )

    transcript = args.transcript or resolve_session_transcript_path(args.root, state)
    if transcript is None:
        print(f"no transcript found under {args.root}", file=sys.stderr)
        return 2

    if args.wait:
        status = wait_until_ready(
            controller,
            args.session,
            timeout=args.timeout,
            transcript_resolver=lambda: resolve_status_transcript_path(args.root, state, args.transcript),
        )
        if status.state != "ready":
            print(f"not ready: {status.reason}", file=sys.stderr)
            return 3

    events, _ = read_transcript_events(transcript)
    answers = extract_answer_texts(events, count=args.count)
    if not answers:
        print("no completed assistant text answer found", file=sys.stderr)
        return 4

    print(_join_numbered_blocks(answers, "answer"))
    return 0


def _print_latest_turn(args: argparse.Namespace, controller: TmuxController) -> int:
    state = read_session_state(_session_state_path(args.session)) or SessionState(
        session=args.session,
        last_prompt="",
        cwd=str(controller.pane_current_path(args.session) or ""),
    )

    transcript = args.transcript or resolve_session_transcript_path(args.root, state)
    if transcript is None:
        print(f"no transcript found under {args.root}", file=sys.stderr)
        return 2

    previous = None
    while True:
        events, _ = read_transcript_events(transcript)
        formatted = format_latest_turns(events, count=args.count)
        if formatted is None:
            print("no turn found", file=sys.stderr)
            return 4
        if formatted != previous:
            if args.follow and previous is not None:
                print("\033[2J\033[H", end="")
            print(formatted)
            previous = formatted
        if not args.follow:
            return 0
        time.sleep(args.interval)


def _print_high_level_info(args: argparse.Namespace, controller: TmuxController) -> int:
    try:
        payload = build_session_info_payload(args.session_id, args.state_dir, args.root, controller)
    except ValueError as error:
        print(json.dumps({"event": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(_format_session_info(payload))
    return 0


def _print_high_level_list(args: argparse.Namespace, controller: TmuxController) -> int:
    payload = build_session_list_payload(args.state_dir, args.root, controller)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        sessions = payload.get("sessions")
        if not sessions:
            print("no sessions")
            return 0
        for item in sessions:
            print(
                f"{item.get('session_id')} "
                f"tmux={item.get('tmux_session')} "
                f"active={item.get('tmux_active')} "
                f"cwd={item.get('cwd') or ''}"
            )
    return 0


def _print_stats(args: argparse.Namespace, controller: TmuxController) -> int:
    try:
        transcript, session_id = resolve_stats_transcript(args, controller)
    except ValueError as error:
        print(json.dumps({"event": "error", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if transcript is None:
        print(json.dumps({"event": "error", "error": "transcript_missing"}, ensure_ascii=False), file=sys.stderr)
        return 2

    try:
        payload = build_stats_payload(transcript, session_id=session_id)
    except OSError as error:
        print(json.dumps({"event": "error", "error": "transcript_read_failed", "detail": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(_format_stats(payload))
    return 0


def resolve_stats_transcript(args: argparse.Namespace, controller: TmuxController) -> tuple[Path | None, str | None]:
    session_id = getattr(args, "session_id_option", None)
    session = getattr(args, "session", None)
    if session_id is None and session:
        try:
            session_id = validate_or_create_session_id(session)
        except ValueError:
            session_id = None

    if args.transcript is not None:
        return args.transcript, session_id

    if session_id:
        actual_session_id = validate_or_create_session_id(session_id)
        state = read_bridge_state(web_session_state_path(actual_session_id, args.state_dir)) or {}
        cwd = state.get("cwd")
        return _session_info_transcript_path(state, args.root, cwd, actual_session_id), actual_session_id

    if session:
        state = read_session_state(session_state_path(session, args.state_dir)) or SessionState(
            session=session,
            last_prompt="",
            cwd=str(controller.pane_current_path(session) or ""),
        )
        transcript = resolve_session_transcript_path(args.root, state)
        return transcript, None

    return None, None


def build_stats_payload(transcript: Path, session_id: str | None = None) -> dict:
    records, offset = read_transcript_records(transcript)
    events = [record.event for record in records]
    usage = normalize_usage(latest_usage(events))
    context = latest_context(events) or None
    model = latest_model(events)
    cost = estimate_turn_cost(model, usage)
    return _compact_payload(
        {
            "event": "stats",
            "session_id": session_id or extract_transcript_session_id(transcript),
            "transcript_path": str(transcript),
            "read_offset": offset,
            "event_count": len(events),
            "model": model,
            "usage": usage,
            "context": context,
            "cost": cost,
        }
    )


def _format_stats(payload: Mapping[str, object]) -> str:
    lines = [
        f"transcript: {payload.get('transcript_path')}",
        f"session_id: {payload.get('session_id') or ''}",
        f"model: {payload.get('model') or ''}",
        f"events: {payload.get('event_count')}",
    ]
    usage = payload.get("usage")
    if isinstance(usage, Mapping):
        lines.append(f"usage: {json.dumps(usage, ensure_ascii=False, sort_keys=True)}")
    context = payload.get("context")
    if isinstance(context, Mapping):
        lines.append(f"context: {json.dumps(context, ensure_ascii=False, sort_keys=True)}")
    cost = payload.get("cost")
    if isinstance(cost, Mapping):
        lines.append(f"cost: {json.dumps(cost, ensure_ascii=False, sort_keys=True)}")
    return "\n".join(lines)


def build_session_info_payload(
    session_id: str,
    state_dir: Path,
    root: Path,
    controller: TmuxController,
    now: Callable[[], float] = time.time,
) -> dict:
    actual_session_id = validate_or_create_session_id(session_id)
    state_path = web_session_state_path(actual_session_id, state_dir)
    state = read_bridge_state(state_path) or {}
    state_mtime = state_path.stat().st_mtime if state_path.exists() else None
    tmux_session = str(state.get("tmux_session") or web_tmux_session_name(actual_session_id))
    cwd = state.get("cwd")
    transcript = _session_info_transcript_path(state, root, cwd, actual_session_id)
    active_turn = state.get("active_turn") if isinstance(state.get("active_turn"), dict) else None
    last_turn = state.get("last_turn") if isinstance(state.get("last_turn"), dict) else None
    completed_turns = state.get("completed_turns")
    completed_count = len(completed_turns) if isinstance(completed_turns, list) else 0
    return _compact_payload(
        {
            "event": "info",
            "session_id": actual_session_id,
            "tmux_session": tmux_session,
            "tmux_active": controller.session_exists(tmux_session),
            "cwd": cwd if isinstance(cwd, str) else None,
            "state_path": str(state_path),
            "state_exists": state_path.exists(),
            "state_mtime": state_mtime,
            "idle_seconds": now() - state_mtime if state_mtime is not None else None,
            "transcript_path": str(transcript) if transcript else None,
            "claude_transcript_session_id": extract_transcript_session_id(transcript) if transcript else None,
            "active_turn": active_turn,
            "active_turn_recovery": active_turn_recovery_payload(active_turn),
            "last_turn": last_turn,
            "completed_turn_count": completed_count,
            "usage_totals": state.get("usage_totals") if isinstance(state.get("usage_totals"), dict) else None,
            "cost_totals": state.get("cost_totals") if isinstance(state.get("cost_totals"), dict) else None,
        }
    )


def active_turn_recovery_payload(active_turn: Mapping[str, object] | None) -> dict | None:
    if not isinstance(active_turn, Mapping):
        return None
    stream_state = str(active_turn.get("stream_state") or "active")
    if stream_state == "failed":
        return {
            "state": stream_state,
            "can_attach": False,
            "can_cancel": True,
            "can_send_new_prompt": False,
            "recommended_action": "inspect_or_kill",
            "description": "turn state is failed; inspect the session or kill it before sending a new prompt",
        }
    if stream_state in {"timeout", "interrupted"}:
        return {
            "state": stream_state,
            "can_attach": True,
            "can_cancel": True,
            "can_send_new_prompt": False,
            "recommended_action": "attach_or_retry",
            "description": "completion is unconfirmed; attach or retry with the same session before sending a new prompt",
        }
    if stream_state == "active":
        return {
            "state": stream_state,
            "can_attach": True,
            "can_cancel": True,
            "can_send_new_prompt": False,
            "recommended_action": "wait_or_attach",
            "description": "turn is still active; wait, attach, queue, or cancel before sending a new prompt",
        }
    return {
        "state": stream_state,
        "can_attach": False,
        "can_cancel": False,
        "can_send_new_prompt": False,
        "recommended_action": "inspect",
        "description": "turn state is not recognized; inspect before sending a new prompt",
    }


def build_session_list_payload(
    state_dir: Path,
    root: Path,
    controller: TmuxController,
    now: Callable[[], float] = time.time,
) -> dict:
    session_ids: set[str] = set()
    sessions_dir = state_dir / "sessions"
    if sessions_dir.is_dir():
        for path in sessions_dir.glob("*.json"):
            try:
                session_ids.add(validate_or_create_session_id(path.stem))
            except ValueError:
                continue

    for tmux_session in controller.list_sessions():
        if not tmux_session.startswith(DEFAULT_WEB_SESSION_PREFIX):
            continue
        candidate = tmux_session[len(DEFAULT_WEB_SESSION_PREFIX) :]
        try:
            session_ids.add(validate_or_create_session_id(candidate))
        except ValueError:
            continue

    current_time = now()
    sessions = [
        build_session_info_payload(session_id, state_dir, root, controller, now=lambda: current_time)
        for session_id in sorted(session_ids)
    ]
    return {"event": "list", "sessions": sessions, "count": len(sessions)}


def _session_info_transcript_path(state: Mapping[str, object], root: Path, cwd: object, session_id: str) -> Path | None:
    transcript = state.get("transcript")
    if isinstance(transcript, Mapping) and isinstance(transcript.get("path"), str):
        path = Path(str(transcript["path"]))
        if path.exists() and transcript_matches_or_omits_session_id(path, session_id):
            return path
    if isinstance(cwd, str) and cwd:
        return resolve_high_level_transcript(root, Path(cwd), dict(state), session_id=session_id)
    return None


def transcript_matches_or_omits_session_id(path: Path, session_id: str) -> bool:
    transcript_session_id = extract_transcript_session_id(path)
    return transcript_session_id is None or transcript_session_id == session_id


def extract_transcript_session_id(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as file:
            for line in file:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    session_id = _event_session_id(event)
                    if session_id:
                        return session_id
    except OSError:
        return None
    return None


def _format_session_info(payload: Mapping[str, object]) -> str:
    lines = [
        f"session_id: {payload.get('session_id')}",
        f"tmux_session: {payload.get('tmux_session')}",
        f"tmux_active: {payload.get('tmux_active')}",
    ]
    if payload.get("cwd"):
        lines.append(f"cwd: {payload.get('cwd')}")
    if payload.get("transcript_path"):
        lines.append(f"transcript_path: {payload.get('transcript_path')}")
    if payload.get("claude_transcript_session_id"):
        lines.append(f"claude_transcript_session_id: {payload.get('claude_transcript_session_id')}")
    lines.append(f"completed_turn_count: {payload.get('completed_turn_count') or 0}")
    return "\n".join(lines)


def _print_stream(args: argparse.Namespace, controller: TmuxController) -> int:
    state = read_session_state(_session_state_path(args.session)) or SessionState(
        session=args.session,
        last_prompt="",
        cwd=str(controller.pane_current_path(args.session) or ""),
    )

    transcript = args.transcript or _wait_for_stream_transcript(args.root, state, args.timeout, args.interval)
    if transcript is None:
        print(f"no transcript found under {args.root}", file=sys.stderr)
        return 2

    status = stream_transcript_until_done(
        transcript,
        state,
        controller,
        args.session,
        interval=args.interval,
        timeout=args.timeout,
        idle_seconds=args.idle,
        tool_result_limit=args.tool_result_limit,
    )
    return 0 if status.state == "ready" else 3


def _is_high_level_stream_args(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "cwd", None)
        or getattr(args, "session_id", None)
        or getattr(args, "prompt", None)
        or getattr(args, "attach", False)
    )


def _high_level_prompt_from_args(args: argparse.Namespace) -> str:
    parts = []
    session = getattr(args, "session", None)
    if session:
        parts.append(session)
    parts.extend(getattr(args, "prompt", None) or [])
    return " ".join(parts)


def _run_high_level_stream(args: argparse.Namespace, controller: TmuxController) -> int:
    result = run_high_level_turn(args, controller, sys.stdout.write)
    if result.get("error"):
        print(json.dumps({"event": "error", "error": result["error"]}, ensure_ascii=False), file=sys.stderr)
    elif result.get("stderr"):
        print(str(result["stderr"]), file=sys.stderr)
    return int(result.get("exit_code") or 0)


def _run_high_level_ask(args: argparse.Namespace, controller: TmuxController) -> int:
    result = run_high_level_turn(args, controller, lambda _line: None)
    if result.get("error"):
        print(json.dumps({"event": "error", "error": result["error"]}, ensure_ascii=False), file=sys.stderr)
        return int(result.get("exit_code") or 1)

    done = result.get("done") if isinstance(result.get("done"), dict) else {}
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else None
    status = result.get("status")
    payload = _compact_payload(
        {
            "event": "ask_result",
            "session_id": result.get("session_id"),
            "turn_id": result.get("turn_id"),
            "state": status.state if isinstance(status, ScreenStatus) else None,
            "reason": status.reason if isinstance(status, ScreenStatus) else result.get("stderr"),
            "answer": done.get("answer") if isinstance(done, dict) else None,
            "metrics": metrics,
            "events_seen": len(result.get("events") or []),
        }
    )
    print(json.dumps(payload, ensure_ascii=False))
    return int(result.get("exit_code") or 0)


def _run_high_level_cancel(args: argparse.Namespace, controller: TmuxController) -> int:
    result = run_high_level_cancel(args, controller)
    stream = sys.stderr if result.get("error") else sys.stdout
    print(json.dumps(_compact_payload(result), ensure_ascii=False), file=stream)
    return int(result.get("exit_code") or 0)


def run_high_level_cancel(args: argparse.Namespace, controller: TmuxController) -> dict:
    session_id = getattr(args, "session_id_option", None) or getattr(args, "session_id", None)
    reset_requested = bool(getattr(args, "reset", False))
    if not session_id:
        return {"event": "error", "exit_code": 2, "error": "cancel requires SESSION_ID or --session-id"}
    try:
        actual_session_id = validate_or_create_session_id(session_id)
    except ValueError as error:
        return {"event": "error", "exit_code": 2, "error": str(error)}

    state_path = web_session_state_path(actual_session_id, args.state_dir)
    state = read_bridge_state(state_path) or {}
    state_exists = bool(state)
    tmux_session = str(state.get("tmux_session") or web_tmux_session_name(actual_session_id))
    initial_active = state.get("active_turn")
    initial_active_turn_id = initial_active.get("turn_id") if isinstance(initial_active, dict) else None
    if not controller.session_exists(tmux_session):
        if reset_requested:
            reset_result = reset_high_level_active_turn(
                state_path,
                initial_active_turn_id,
                expected_active_turn=initial_active if isinstance(initial_active, dict) else None,
            )
            return {
                "event": "cancel",
                "exit_code": 0,
                "session_id": actual_session_id,
                "tmux_session": tmux_session,
                "state_exists": state_exists,
                "active_turn_present": initial_active_turn_id is not None,
                "active_turn_id": initial_active_turn_id,
                "reset_requested": True,
                "reset_applied": reset_result["reset_applied"],
                "moved_turn_id": reset_result["moved_turn_id"],
                "state_after": reset_result["state_after"],
                "tmux_session_missing": True,
            }
        return {
            "event": "error",
            "exit_code": 2,
            "error": "tmux_session_missing",
            "session_id": actual_session_id,
            "tmux_session": tmux_session,
            "state_exists": state_exists,
        }

    try:
        controller.send_escape(tmux_session)
    except subprocess.CalledProcessError as error:
        payload = {
            "event": "error",
            "exit_code": 5,
            "error": "cancel_failed",
            "detail": str(error),
            "session_id": actual_session_id,
            "tmux_session": tmux_session,
            "state_exists": state_exists,
        }
        if reset_requested:
            payload["reset_requested"] = True
        return payload

    reset_result = None
    if reset_requested:
        reset_result = reset_high_level_active_turn(
            state_path,
            initial_active_turn_id,
            expected_active_turn=initial_active if isinstance(initial_active, dict) else None,
        )

    payload = {
        "event": "cancel",
        "exit_code": 0,
        "session_id": actual_session_id,
        "tmux_session": tmux_session,
        "state_exists": state_exists,
        "active_turn_present": initial_active_turn_id is not None,
        "active_turn_id": initial_active_turn_id,
        "sent_key": "Escape",
    }
    if reset_requested and reset_result is not None:
        payload.update(
            {
                "reset_requested": True,
                "reset_applied": reset_result["reset_applied"],
                "moved_turn_id": reset_result["moved_turn_id"],
                "state_after": reset_result["state_after"],
            }
        )
    return payload


def reset_high_level_active_turn(
    state_path: Path,
    turn_id: object = None,
    expected_active_turn: dict | None = None,
) -> dict:
    before = read_bridge_state(state_path) or {}
    active = before.get("active_turn")
    target_active = expected_active_turn if isinstance(expected_active_turn, dict) else active
    moved_turn_id = target_active.get("turn_id") if isinstance(target_active, dict) else None
    reset_applied = False
    if isinstance(target_active, dict):
        if turn_id is not None:
            if isinstance(active, dict) and active.get("turn_id") == turn_id:
                _move_active_turn_to_last_turn(state_path, turn_id=turn_id)
        else:
            original_active = dict(target_active)

            def mutate(state: dict) -> dict | None:
                if state.get("active_turn") != original_active:
                    return None
                state["last_turn"] = state["active_turn"]
                state["active_turn"] = None
                return state

            reset_applied = mutate_high_level_state(state_path, mutate) is not None
    after = read_bridge_state(state_path) or {}
    if turn_id is not None:
        reset_applied = isinstance(active, dict) and active.get("turn_id") == turn_id and after.get("active_turn") is None
    return {
        "reset_applied": reset_applied,
        "moved_turn_id": moved_turn_id if reset_applied else None,
        "state_after": {"active_turn": after.get("active_turn")},
    }


def _run_high_level_replay(args: argparse.Namespace, controller: TmuxController) -> int:
    result = run_high_level_replay(args, controller, sys.stdout.write)
    if result.get("error"):
        print(json.dumps({"event": "error", "error": result["error"]}, ensure_ascii=False), file=sys.stderr)
    elif result.get("stderr"):
        print(str(result["stderr"]), file=sys.stderr)
    return int(result.get("exit_code") or 0)


def run_high_level_replay(
    args: argparse.Namespace,
    controller: TmuxController,
    write: Callable[[str], object],
) -> dict:
    command_label = "last" if getattr(args, "command_name", "") == "last" else "replay"
    session_id = getattr(args, "session_id_option", None) or getattr(args, "session_id", None)
    if not session_id:
        return {"exit_code": 2, "error": f"{command_label} requires SESSION_ID or --session-id"}
    if args.count < 1:
        return {"exit_code": 2, "error": f"{command_label} --last must be >= 1"}
    try:
        actual_session_id = validate_or_create_session_id(session_id)
    except ValueError as error:
        return {"exit_code": 2, "error": str(error)}

    state_path = web_session_state_path(actual_session_id, args.state_dir)
    state = read_bridge_state(state_path) or {}
    if not state:
        return {"exit_code": 2, "error": "session_state_missing"}

    active = state.get("active_turn")
    include_active = isinstance(active, dict) and active.get("stream_state") in {"active", "timeout", "interrupted"}
    active_turn_id = active.get("turn_id") if include_active else None
    completed = state.get("completed_turns") if isinstance(state.get("completed_turns"), list) else []
    completed_turns = [turn for turn in completed if isinstance(turn, dict)]
    completed_count = max(0, args.count - (1 if include_active else 0))
    selected_completed = completed_turns[-completed_count:] if completed_count else []

    emitted: list[dict] = []

    def emit_completed_turns(turns: Sequence[Mapping[str, object]]) -> None:
        for turn in turns:
            for payload in replay_completed_turn_payloads(
                state=state,
                turn=turn,
                root=args.root,
                session_id=actual_session_id,
                tool_result_limit=args.tool_result_limit,
            ):
                emitted.append(payload)
                _write_jsonl(write, payload)

    def capture_and_write(line: str) -> object:
        for raw in line.splitlines():
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                emitted.append(payload)
        return write(line)

    if include_active:
        attach_args = argparse.Namespace(
            session_id=actual_session_id,
            state_dir=args.state_dir,
            root=args.root,
            interval=args.interval,
            timeout=args.timeout,
            idle=args.idle,
            tool_result_limit=args.tool_result_limit,
            transcript=None,
        )
        try:
            runtime, transcript = prepare_high_level_attach(
                controller=controller,
                session_id=actual_session_id,
                state_dir=args.state_dir,
                root=args.root,
            )
        except ValueError as error:
            return {"exit_code": 2, "error": str(error), "events": emitted}
        except RuntimeError as error:
            if str(error) != "no_active_turn" or not active_turn_id:
                return {"exit_code": 5, "error": str(error), "events": emitted}
            latest_state = read_bridge_state(state_path) or {}
            latest_completed = latest_state.get("completed_turns") if isinstance(latest_state.get("completed_turns"), list) else []
            latest_completed_turns = [turn for turn in latest_completed if isinstance(turn, dict)]
            replay_window = latest_completed_turns[-args.count :]
            if any(turn.get("turn_id") == active_turn_id for turn in replay_window):
                state = latest_state
                selected_completed = replay_window
                emit_completed_turns(selected_completed)
                return {
                    "exit_code": 0,
                    "session_id": actual_session_id,
                    "events": emitted,
                    "replayed_completed": len(selected_completed),
                }
            return {"exit_code": 5, "error": str(error), "events": emitted}

        emit_completed_turns(selected_completed)
        result = _stream_prepared_high_level_turn(attach_args, controller, runtime, transcript, capture_and_write)
        result["replayed_completed"] = len(selected_completed)
        result["events"] = emitted
        return result

    emit_completed_turns(selected_completed)

    if not selected_completed:
        return {"exit_code": 4, "error": "no_replayable_turns", "events": emitted}
    return {"exit_code": 0, "session_id": actual_session_id, "events": emitted, "replayed_completed": len(selected_completed)}


def run_high_level_turn(
    args: argparse.Namespace,
    controller: TmuxController,
    write: Callable[[str], object],
) -> dict:
    if getattr(args, "attach", False):
        if not getattr(args, "session_id", None):
            return {"exit_code": 2, "error": "attach requires --session-id"}
        try:
            runtime, transcript = prepare_high_level_attach(
                controller=controller,
                session_id=args.session_id,
                state_dir=args.state_dir,
                root=args.root,
            )
        except ValueError as error:
            return {"exit_code": 2, "error": str(error)}
        except RuntimeError as error:
            return {"exit_code": 5, "error": str(error)}
        return _stream_prepared_high_level_turn(args, controller, runtime, transcript, write)

    if args.cwd is None:
        return {"exit_code": 2, "error": "high-level stream requires --cwd"}

    prompt = _high_level_prompt_from_args(args)
    if not prompt:
        return {"exit_code": 2, "error": "high-level stream requires prompt text"}

    try:
        runtime = prepare_high_level_stream(
            controller=controller,
            cwd=args.cwd,
            prompt=prompt,
            root=args.root,
            state_dir=args.state_dir,
            session_id=args.session_id,
            claude_args_builder=lambda: claude_args_from_options(args),
            env_builder=lambda: claude_environment_from_args(args),
        )
    except ValueError as error:
        return {"exit_code": 2, "error": str(error)}
    except RuntimeError as error:
        return {"exit_code": 5, "error": str(error)}

    try:
        transcript = args.transcript or wait_for_high_level_transcript(
            args.root,
            runtime,
            args.timeout,
            args.interval,
            controller=controller,
        )
    except KeyboardInterrupt:
        _mark_turn_interrupted(runtime)
        return {
            "exit_code": 130,
            "session_id": runtime.session_id,
            "turn_id": runtime.turn_id,
            "status": ScreenStatus("interrupted", "stream interrupted by client"),
            "events": [],
        }
    if transcript is None:
        _mark_turn_timeout(runtime)
        return {
            "exit_code": 2,
            "session_id": runtime.session_id,
            "turn_id": runtime.turn_id,
            "status": ScreenStatus("timeout", f"no transcript found under {args.root}"),
            "stderr": f"no transcript found under {args.root}",
            "events": [],
        }

    return _stream_prepared_high_level_turn(args, controller, runtime, transcript, write)


def _stream_prepared_high_level_turn(
    args: argparse.Namespace,
    controller: TmuxController,
    runtime: StreamRuntime,
    transcript: Path,
    write: Callable[[str], object],
) -> dict:
    captured: list[dict] = []

    def capture_and_write(line: str) -> object:
        for raw in line.splitlines():
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                captured.append(payload)
        return write(line)

    try:
        status = stream_high_level_transcript_until_done(
            transcript,
            runtime,
            controller,
            root=args.root,
            interval=args.interval,
            timeout=args.timeout,
            idle_seconds=args.idle,
            tool_result_limit=args.tool_result_limit,
            write=capture_and_write,
        )
    except KeyboardInterrupt:
        _mark_turn_interrupted(runtime)
        return {
            "exit_code": 130,
            "session_id": runtime.session_id,
            "turn_id": runtime.turn_id,
            "status": ScreenStatus("interrupted", "stream interrupted by client"),
            "events": captured,
        }
    done = next((payload for payload in reversed(captured) if payload.get("event") == "done"), None)
    metrics = next((payload for payload in reversed(captured) if payload.get("event") == "metrics"), None)
    return {
        "exit_code": 0 if status.state == "ready" else 3,
        "session_id": runtime.session_id,
        "turn_id": runtime.turn_id,
        "status": status,
        "events": captured,
        "done": done,
        "metrics": metrics,
    }


def prepare_high_level_stream(
    controller: TmuxController,
    cwd: Path,
    prompt: str,
    root: Path = DEFAULT_TRANSCRIPT_ROOT,
    state_dir: Path = DEFAULT_STATE_DIR,
    session_id: str | None = None,
    claude_args: Sequence[str] = (),
    claude_args_builder: Callable[[], Sequence[str]] | None = None,
    env: Mapping[str, str] | None = None,
    env_builder: Callable[[], Mapping[str, str]] | None = None,
    preseed_project_trust: Callable[[Path, Mapping[str, str] | None], object] = preseed_claude_project_trust,
    now: Callable[[], float] = time.time,
) -> StreamRuntime:
    actual_session_id = validate_or_create_session_id(session_id)
    tmux_session = web_tmux_session_name(actual_session_id)
    canonical_cwd = cwd.expanduser().resolve()
    state_path = web_session_state_path(actual_session_id, state_dir)
    lock_path = web_session_lock_path(actual_session_id, state_dir)
    runtime: StreamRuntime | None = None

    with exclusive_file_lock(lock_path):
        state = read_bridge_state(state_path) or {}
        state_cwd = state.get("cwd")
        if isinstance(state_cwd, str) and Path(state_cwd) != canonical_cwd:
            raise ValueError("session_cwd_mismatch")

        active_turn = state.get("active_turn")
        if isinstance(active_turn, dict) and active_turn.get("claude_state") not in {None, "ready"}:
            if recover_stale_active_turn(
                state_path,
                state,
                controller,
                tmux_session,
                root=root,
                cwd=canonical_cwd,
                state_dir=state_dir,
                session_id=actual_session_id,
            ):
                state = read_bridge_state(state_path) or {}
            else:
                raise RuntimeError("turn_in_progress")

        tmux_exists = controller.session_exists(tmux_session)
        new_session_env: Mapping[str, str] | None = env
        new_session_claude_args: Sequence[str] = claude_args
        if not tmux_exists and claude_args_builder is not None:
            new_session_claude_args = claude_args_builder()
        if not tmux_exists and env_builder is not None:
            new_session_env = env_builder()

        transcript = resolve_high_level_transcript(root, canonical_cwd, state, session_id=actual_session_id)
        before_send_offset = transcript.stat().st_size if transcript and transcript.exists() else 0
        turn_id = make_turn_id(now)
        runtime = StreamRuntime(
            session_id=actual_session_id,
            tmux_session=tmux_session,
            state_path=state_path,
            state_dir=state_dir,
            cwd=canonical_cwd,
            prompt=prompt,
            turn_id=turn_id,
            before_send_offset=before_send_offset,
            replay_start_offset=before_send_offset,
            before_send_transcript=transcript,
            started_at_monotonic=time.monotonic(),
            started_at_utc=_utc_timestamp(now()),
        )

        pending_state = build_pending_turn_state(
            state=state,
            runtime=runtime,
            transcript=transcript,
            wall_time=now(),
        )
        _write_high_level_state(state_path, pending_state)

        try:
            if tmux_exists:
                screen_status = analyze_screen_status(controller.capture_screen(tmux_session, height=80))
                if screen_status.state != "ready":
                    raise RuntimeError("turn_in_progress")
                controller.send_prompt(tmux_session, prompt)
            else:
                resume = bool(state or transcript)
                preseed_project_trust(canonical_cwd, new_session_env)
                command = build_initial_claude_command(new_session_claude_args, actual_session_id, resume=resume, prompt=prompt)
                controller.start_session(tmux_session, command=command, cwd=canonical_cwd, env=new_session_env)
        except KeyboardInterrupt:
            _mark_turn_interrupted(runtime)
            raise
        except Exception:
            _clear_active_turn_after_failed_send(state_path)
            raise

    if runtime is None:
        raise RuntimeError("stream_runtime_missing")

    return runtime


def prepare_high_level_attach(
    controller: TmuxController,
    session_id: str,
    state_dir: Path = DEFAULT_STATE_DIR,
    root: Path = DEFAULT_TRANSCRIPT_ROOT,
) -> tuple[StreamRuntime, Path]:
    actual_session_id = validate_or_create_session_id(session_id)
    state_path = web_session_state_path(actual_session_id, state_dir)
    state = read_bridge_state(state_path) or {}
    active = state.get("active_turn")
    if not isinstance(active, dict) or active.get("stream_state") not in {"active", "timeout", "interrupted"}:
        raise RuntimeError("no_active_turn")

    cwd_value = state.get("cwd")
    if not isinstance(cwd_value, str) or not cwd_value:
        raise RuntimeError("session_cwd_missing")
    cwd = Path(cwd_value)
    tmux_session = str(state.get("tmux_session") or web_tmux_session_name(actual_session_id))
    if not controller.session_exists(tmux_session):
        raise RuntimeError("tmux_session_missing")

    transcript = _attach_transcript_path(state, active, root, cwd, actual_session_id)
    if transcript is None:
        raise RuntimeError("transcript_missing")

    replay_start = _int_or_none(active.get("anchor_start_offset"))
    if replay_start is None:
        replay_start = _int_or_none(active.get("replay_start_offset"))
    if replay_start is None:
        replay_start = _int_or_none(active.get("read_offset"))
    if replay_start is None:
        replay_start = 0

    wall_started = _parse_utc_timestamp(str(active.get("before_send_wall_time_utc") or ""))
    started_monotonic = time.monotonic()
    if wall_started is not None:
        started_monotonic = max(0.0, time.monotonic() - max(0.0, time.time() - wall_started))

    def mark_attached(latest: dict) -> dict | None:
        latest_active = latest.get("active_turn")
        if not isinstance(latest_active, dict) or latest_active.get("turn_id") != active.get("turn_id"):
            return None
        latest_active["owner_pid"] = os.getpid()
        latest_active["owner_hostname"] = socket.gethostname()
        latest_active["heartbeat_at"] = _utc_timestamp(time.time())
        latest_active["stream_state"] = "active"
        latest["active_turn"] = latest_active
        return latest

    mutate_high_level_state(state_path, mark_attached)

    return (
        StreamRuntime(
            session_id=actual_session_id,
            tmux_session=tmux_session,
            state_path=state_path,
            state_dir=state_dir,
            cwd=cwd,
            prompt=str(active.get("prompt_preview") or ""),
            turn_id=str(active.get("turn_id") or make_turn_id()),
            before_send_offset=replay_start,
            replay_start_offset=replay_start,
            before_send_transcript=transcript,
            started_at_monotonic=started_monotonic,
            started_at_utc=str(active.get("before_send_wall_time_utc") or "") or None,
        ),
        transcript,
    )


def replay_completed_turn_payloads(
    state: Mapping[str, object],
    turn: Mapping[str, object],
    root: Path,
    session_id: str,
    tool_result_limit: int | None = DEFAULT_TOOL_RESULT_TEXT_LIMIT,
) -> list[dict]:
    turn_id = str(turn.get("turn_id") or "")
    if not turn_id:
        return []
    cwd_value = state.get("cwd")
    cwd = Path(str(cwd_value)) if isinstance(cwd_value, str) and cwd_value else Path(".")
    transcript = completed_turn_transcript_path(turn, state, root, cwd, session_id)
    completed_offset = _int_or_none(turn.get("completed_offset"))
    anchor_start = _int_or_none(turn.get("anchor_start_offset"))
    payloads: list[dict] = []
    if transcript is not None and completed_offset is not None and anchor_start is not None:
        try:
            records, _read_offset = read_transcript_records(transcript, anchor_start)
            file_identity = transcript_identity(transcript)
            for record in records:
                if record.start_offset >= completed_offset:
                    break
                if record.end_offset > completed_offset:
                    break
                for payload in normalize_stream_record(record, turn_id, file_identity, tool_result_limit=tool_result_limit):
                    payload["session_id"] = session_id
                    payloads.append(payload)
        except OSError:
            payloads = []

    synthetic_offset = completed_offset if completed_offset is not None else 0
    payloads.append(
        _compact_payload(
            {
                "event": "done",
                "session_id": session_id,
                "turn_id": turn_id,
                "event_id": f"{turn_id}:done:{synthetic_offset}",
                "source_offset": synthetic_offset,
                "source_end_offset": synthetic_offset,
                "block_index": -1,
                "state": "ready",
                "reason": "replayed completed turn",
                "answer": turn.get("answer"),
            }
        )
    )
    payloads.append(replay_metrics_payload(turn, state, session_id, synthetic_offset))
    return payloads


def completed_turn_transcript_path(
    turn: Mapping[str, object],
    state: Mapping[str, object],
    root: Path,
    cwd: Path,
    session_id: str,
) -> Path | None:
    saw_stored_path = False
    for source in (turn.get("transcript"), turn.get("before_send_transcript"), state.get("transcript")):
        if not isinstance(source, Mapping) or not isinstance(source.get("path"), str):
            continue
        saw_stored_path = True
        path = validated_stored_transcript_path(source)
        if path is not None:
            return path
    if saw_stored_path:
        return None
    return resolve_high_level_transcript(root, cwd, dict(state), session_id=session_id)


def validated_stored_transcript_path(source: Mapping[str, object]) -> Path | None:
    path_value = source.get("path")
    if not isinstance(path_value, str):
        return None
    path = Path(path_value)
    try:
        stat = path.stat()
    except OSError:
        return None

    stored_dev = _int_or_none(source.get("st_dev"))
    stored_ino = _int_or_none(source.get("st_ino"))
    if stored_dev is not None and stat.st_dev != stored_dev:
        return None
    if stored_ino is not None and stat.st_ino != stored_ino:
        return None

    stored_offset = _int_or_none(source.get("offset"))
    if stored_offset is not None and stat.st_size < stored_offset:
        return None
    stored_size = _int_or_none(source.get("size"))
    if stored_size is not None and stat.st_size < stored_size:
        return None
    return path


def replay_metrics_payload(
    turn: Mapping[str, object],
    state: Mapping[str, object],
    session_id: str,
    completed_offset: int,
) -> dict:
    cost = turn.get("cost")
    if isinstance(cost, Mapping):
        cost = dict(cost)
        turn_id = turn.get("turn_id")
        turns = state.get("completed_turns")
        if isinstance(turns, list):
            prefix: list[Mapping[str, object]] = []
            for item in turns:
                if not isinstance(item, Mapping):
                    continue
                prefix.append(item)
                if item.get("turn_id") == turn_id:
                    break
            totals = cost_totals_from_completed_turns(prefix)
            if totals.get("currency") == "USD" and totals.get("session_usd") is not None:
                cost["session_usd"] = totals["session_usd"]
    return _compact_payload(
        {
            "event": "metrics",
            "session_id": session_id,
            "turn_id": turn.get("turn_id"),
            "event_id": f"{turn.get('turn_id')}:metrics:{completed_offset}",
            "source_offset": completed_offset,
            "source_end_offset": completed_offset,
            "block_index": -1,
            "scope": "turn_final",
            "elapsed_ms": turn.get("elapsed_ms"),
            "model": turn.get("model"),
            "usage": turn.get("usage"),
            "context": turn.get("context"),
            "cost": cost,
        }
    )


def _attach_transcript_path(
    state: Mapping[str, object],
    active: Mapping[str, object],
    root: Path,
    cwd: Path,
    session_id: str,
) -> Path | None:
    for source in (active.get("before_send_transcript"), state.get("transcript")):
        if isinstance(source, Mapping) and isinstance(source.get("path"), str):
            path = Path(str(source["path"]))
            if path.exists() and transcript_matches_or_omits_session_id(path, session_id):
                return path
    return resolve_high_level_transcript(root, cwd, dict(state), session_id=session_id)


def _int_or_none(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def validate_or_create_session_id(session_id: str | None = None) -> str:
    if not session_id:
        return str(uuid.uuid4())
    try:
        parsed = uuid.UUID(session_id)
    except ValueError as error:
        raise ValueError("invalid_session_id") from error
    if str(parsed) != session_id.lower():
        raise ValueError("invalid_session_id")
    return str(parsed)


def web_tmux_session_name(session_id: str) -> str:
    return f"{DEFAULT_WEB_SESSION_PREFIX}{session_id}"


def web_session_state_path(session_id: str, state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    validate_or_create_session_id(session_id)
    return state_dir / "sessions" / f"{session_id}.json"


def web_session_lock_path(session_id: str, state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    validate_or_create_session_id(session_id)
    return state_dir / "locks" / f"{session_id}.lock"


@contextmanager
def exclusive_file_lock(path: Path, stale_seconds: float = 300.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    stale_broken = False
    try:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not break_stale_lock(path, stale_seconds=stale_seconds):
                raise
            stale_broken = True
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        payload = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "started_at": time.time(),
            "stale_broken": stale_broken,
        }
        os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        yield
    except FileExistsError as error:
        raise RuntimeError("send_lock_busy") from error
    finally:
        if fd is not None:
            os.close(fd)
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def break_stale_lock(path: Path, stale_seconds: float = 300.0) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False

    if time.time() - stat.st_mtime < stale_seconds:
        return False

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        try:
            path.unlink()
            return True
        except OSError:
            return False

    if not isinstance(payload, dict):
        try:
            path.unlink()
            return True
        except OSError:
            return False
    started_at = payload.get("started_at")
    if not isinstance(started_at, int | float):
        try:
            path.unlink()
            return True
        except OSError:
            return False
    if time.time() - started_at < stale_seconds:
        return False
    pid = payload.get("pid")
    if isinstance(pid, int) and process_exists(pid):
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_bridge_state(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_high_level_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_write_lock_path(path)
    with exclusive_file_lock(lock_path):
        current = read_bridge_state(path)
        expected_generation = int(payload.get("generation") or 0)
        current_generation = int((current or {}).get("generation") or 0)
        if current is not None and current_generation != expected_generation:
            raise StateGenerationConflict("state_generation_conflict")

        payload = dict(payload)
        payload["generation"] = expected_generation + 1
        payload["updated_at"] = _utc_timestamp(time.time())
        payload["writer_pid"] = os.getpid()
        payload["writer_hostname"] = socket.gethostname()
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)


def state_write_lock_path(state_path: Path) -> Path:
    return state_path.with_suffix(state_path.suffix + ".write.lock")


def mutate_high_level_state(path: Path, mutate: Callable[[dict], dict | None], max_attempts: int = 3) -> dict | None:
    for _attempt in range(max_attempts):
        state = read_bridge_state(path) or {}
        updated = mutate(state)
        if updated is None:
            return None
        try:
            _write_high_level_state(path, updated)
            return updated
        except StateGenerationConflict:
            continue
    raise StateGenerationConflict("state_generation_conflict")


def build_pending_turn_state(state: dict, runtime: StreamRuntime, transcript: Path | None, wall_time: float) -> dict:
    payload = {
        "schema_version": STATE_SCHEMA_VERSION,
        "generation": state.get("generation", 0),
        "session_id": runtime.session_id,
        "tmux_session": runtime.tmux_session,
        "cwd": str(runtime.cwd),
        "created_at": state.get("created_at") or _utc_timestamp(wall_time),
        "transcript": transcript_file_state(transcript, runtime.before_send_offset) if transcript else None,
        "active_turn": {
            "turn_id": runtime.turn_id,
            "claude_state": "starting",
            "stream_state": "active",
            "owner_pid": os.getpid(),
            "owner_hostname": socket.gethostname(),
            "stream_epoch": int((state.get("active_turn") or {}).get("stream_epoch") or 0) + 1
            if isinstance(state.get("active_turn"), dict)
            else 1,
            "heartbeat_at": _utc_timestamp(wall_time),
            "prompt_hash": _prompt_hash(runtime.prompt),
            "prompt_preview": runtime.prompt[:200],
            "before_send_wall_time_utc": _utc_timestamp(wall_time),
            "before_send_transcript": transcript_file_state(transcript, runtime.before_send_offset) if transcript else None,
            "no_transcript_baseline": transcript is None,
            "anchor_start_offset": None,
            "anchor_end_offset": None,
            "replay_start_offset": runtime.replay_start_offset,
            "read_offset": runtime.before_send_offset,
            "last_stdout_flushed_offset": runtime.before_send_offset,
            "completed_offset": None,
            "anchor_strategy": None,
        },
        "last_turn": state.get("last_turn"),
        "completed_turns": state.get("completed_turns") if isinstance(state.get("completed_turns"), list) else [],
        "usage_totals": state.get("usage_totals") if isinstance(state.get("usage_totals"), dict) else {},
        "cost_totals": state.get("cost_totals") if isinstance(state.get("cost_totals"), dict) else {},
    }
    return payload


def transcript_file_state(path: Path | None, offset: int | None = None) -> dict | None:
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return {
        "path": str(path),
        "st_dev": stat.st_dev,
        "st_ino": stat.st_ino,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "offset": stat.st_size if offset is None else offset,
    }


def build_initial_claude_command(
    claude_args: Sequence[str],
    session_id: str,
    resume: bool,
    prompt: str | None = None,
) -> str:
    session_flag = "--resume" if resume else "--session-id"
    args = [*claude_args, session_flag, session_id]
    if not _has_permission_override(claude_args):
        args.append(CLAUDE_DANGEROUS_SKIP_PERMISSIONS_FLAG)
    command = _shell_join([CLAUDE_EXECUTABLE, *args])
    if prompt is not None:
        command += " -- " + _shell_ansi_c_quote(prompt)
    return command


def _shell_ansi_c_quote(value: str) -> str:
    if "\0" in value:
        raise ValueError("prompt_contains_nul")
    replacements = {
        "\\": "\\\\",
        "'": "\\'",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    return "$'" + "".join(replacements.get(char, char) for char in value) + "'"


def make_turn_id(now: Callable[[], float] = time.time) -> str:
    return time.strftime("turn_%Y%m%dT%H%M%SZ", time.gmtime(now())) + f"-{uuid.uuid4().hex[:8]}"


def _prompt_hash(prompt: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _utc_timestamp(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def wait_for_high_level_transcript(
    root: Path,
    runtime: StreamRuntime,
    timeout: float,
    interval: float,
    controller: ScreenCaptureController | None = None,
    sleep: Callable[[float], object] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> Path | None:
    deadline = now() + timeout
    retried_unanchored_submit = False
    while now() < deadline:
        if runtime.before_send_transcript is not None:
            try:
                replaced = transcript_replaced_since_baseline(runtime.before_send_transcript, runtime.state_path)
                if (
                    transcript_matches_session_id(runtime.before_send_transcript, runtime.session_id)
                    and (
                        runtime.before_send_transcript.stat().st_size > runtime.before_send_offset
                        or (replaced and runtime.before_send_transcript.stat().st_size > 0)
                    )
                ):
                    return runtime.before_send_transcript
            except OSError:
                pass
        candidate = resolve_high_level_transcript(
            root,
            runtime.cwd,
            read_bridge_state(runtime.state_path) or {},
            session_id=runtime.session_id,
        )
        if candidate is not None:
            try:
                if candidate != runtime.before_send_transcript or candidate.stat().st_size > runtime.before_send_offset:
                    return candidate
            except OSError:
                pass
        if controller is not None:
            screen_status = _capture_screen_status(controller, runtime.tmux_session)
            retried_unanchored_submit = _maybe_retry_unanchored_submit(
                controller,
                runtime,
                screen_status,
                retried_unanchored_submit,
                now(),
            )
        sleep(interval)
    return None


def transcript_replaced_since_baseline(path: Path, state_path: Path) -> bool:
    state = read_bridge_state(state_path) or {}
    active = state.get("active_turn")
    if not isinstance(active, dict):
        return False
    baseline = active.get("before_send_transcript")
    if not isinstance(baseline, dict):
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    return stat.st_dev != baseline.get("st_dev") or stat.st_ino != baseline.get("st_ino")


def resolve_high_level_transcript(
    root: Path,
    cwd: Path,
    state: dict | None = None,
    session_id: str | None = None,
) -> Path | None:
    state = state or {}
    state_session_id = state.get("session_id")
    target_session_id = session_id or state_session_id if isinstance(state_session_id, str) else session_id
    project_dir = project_transcript_dir(root, cwd)
    candidates: list[Path] = []
    transcript = state.get("transcript")
    if isinstance(transcript, dict) and isinstance(transcript.get("path"), str):
        state_path = Path(transcript["path"])
        if (
            state_path.exists()
            and _path_under_project_transcript_dir(state_path, root, cwd)
            and transcript_matches_session_id(state_path, target_session_id)
        ):
            candidates.append(state_path)

    if project_dir.is_dir():
        for path in project_dir.rglob("*.jsonl"):
            if path not in candidates and transcript_matches_session_id(path, target_session_id):
                candidates.append(path)

    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))


def transcript_matches_session_id(path: Path, session_id: str | None) -> bool:
    if not session_id:
        return True
    try:
        with path.open("r", encoding="utf-8", errors="replace") as file:
            for line in file:
                if session_id in line:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if _event_session_id(event) == session_id:
                        return True
    except OSError:
        return False
    return False


def _event_session_id(event: dict) -> str | None:
    for key in ("sessionId", "session_id"):
        value = event.get(key)
        if isinstance(value, str):
            return value
    message = event.get("message")
    if isinstance(message, dict):
        for key in ("sessionId", "session_id"):
            value = message.get(key)
            if isinstance(value, str):
                return value
    return None


def _path_under_project_transcript_dir(path: Path, root: Path, cwd: Path) -> bool:
    project_dir = project_transcript_dir(root, cwd).resolve()
    try:
        path.resolve().relative_to(project_dir)
    except ValueError:
        return False
    return True


def _wait_for_stream_transcript(root: Path, state: SessionState, timeout: float, interval: float) -> Path | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        transcript, pending_prompt = resolve_status_transcript_path(root, state)
        if transcript is not None:
            return transcript
        if not pending_prompt:
            return resolve_transcript_path(root, state)
        time.sleep(interval)
    return None


def reap_idle_sessions(
    controller: TmuxController,
    idle_seconds: float,
    prefix: str = DEFAULT_CONTROLLED_PREFIX,
    dry_run: bool = False,
    state_dir: Path = DEFAULT_STATE_DIR,
    root: Path = DEFAULT_TRANSCRIPT_ROOT,
    now: Callable[[], float] = time.time,
) -> list[dict]:
    results = []
    current_time = now()
    for session in controller.list_sessions():
        if prefix and not session.startswith(prefix):
            continue

        reap_state = resolve_reap_session_state(session, state_dir)
        if reap_state is None:
            continue
        state_path, state, active_working = reap_state

        idle = current_time - state_path.stat().st_mtime
        if idle < idle_seconds:
            continue
        if active_working:
            recovered = (
                high_level_reap_active_turn_is_recoverable(controller, session, state_path, root)
                if dry_run
                else recover_high_level_reap_active_turn(controller, session, state_path, state_dir, root)
            )
            if recovered and dry_run:
                active_working = False
            elif recovered:
                refreshed = resolve_reap_session_state(session, state_dir)
                if refreshed is None:
                    continue
                state_path, state, active_working = refreshed
        if session_is_working(controller, session, state, root):
            continue

        action = "would-kill" if dry_run else "killed"
        if not dry_run:
            controller.kill_session(session)
        results.append({"session": session, "idle_seconds": idle, "action": action})
    return results


def high_level_reap_active_turn_is_recoverable(
    controller: TmuxController,
    session: str,
    state_path: Path,
    root: Path = DEFAULT_TRANSCRIPT_ROOT,
) -> bool:
    if not session.startswith(DEFAULT_WEB_SESSION_PREFIX):
        return False

    session_id = session[len(DEFAULT_WEB_SESSION_PREFIX) :]
    try:
        validate_or_create_session_id(session_id)
    except ValueError:
        return False

    state = read_bridge_state(state_path)
    if state is None:
        return False
    active = state.get("active_turn")
    if not isinstance(active, dict) or active.get("claude_state") in {None, "ready"}:
        return False

    try:
        screen_status = analyze_screen_status(controller.capture_screen(session, height=80))
    except subprocess.CalledProcessError:
        return False
    if screen_status.state != "ready":
        return False

    cwd_value = state.get("cwd")
    cwd = Path(cwd_value) if isinstance(cwd_value, str) else Path(".")
    return collect_recoverable_active_turn(state, root, cwd, session_id) is not None


def recover_high_level_reap_active_turn(
    controller: TmuxController,
    session: str,
    state_path: Path,
    state_dir: Path = DEFAULT_STATE_DIR,
    root: Path = DEFAULT_TRANSCRIPT_ROOT,
) -> bool:
    if not session.startswith(DEFAULT_WEB_SESSION_PREFIX):
        return False

    session_id = session[len(DEFAULT_WEB_SESSION_PREFIX) :]
    try:
        validate_or_create_session_id(session_id)
    except ValueError:
        return False

    state = read_bridge_state(state_path)
    if state is None:
        return False
    active = state.get("active_turn")
    if not isinstance(active, dict) or active.get("claude_state") in {None, "ready"}:
        return False

    try:
        screen_status = analyze_screen_status(controller.capture_screen(session, height=80))
    except subprocess.CalledProcessError:
        return False
    if screen_status.state != "ready":
        return False

    cwd_value = state.get("cwd")
    cwd = Path(cwd_value) if isinstance(cwd_value, str) else None
    return finalize_recoverable_active_turn(
        state_path=state_path,
        state=state,
        controller_status=screen_status,
        root=root,
        cwd=cwd,
        state_dir=state_dir,
        session_id=session_id,
        tmux_session=session,
    )


def resolve_reap_session_state(session: str, state_dir: Path) -> tuple[Path, SessionState, bool] | None:
    high_level_state = resolve_high_level_reap_session_state(session, state_dir)
    if high_level_state is not None:
        return high_level_state

    state_path = session_state_path(session, state_dir)
    state = read_session_state(state_path)
    if state is None:
        return None
    return state_path, state, False


def resolve_high_level_reap_session_state(session: str, state_dir: Path) -> tuple[Path, SessionState, bool] | None:
    if not session.startswith(DEFAULT_WEB_SESSION_PREFIX):
        return None

    session_id = session[len(DEFAULT_WEB_SESSION_PREFIX) :]
    try:
        validate_or_create_session_id(session_id)
    except ValueError:
        return None

    state_path = web_session_state_path(session_id, state_dir)
    state = read_bridge_state(state_path)
    if state is None:
        return None

    cwd = state.get("cwd")
    prompt = _bridge_state_reap_prompt(state)
    active = state.get("active_turn")
    active_working = isinstance(active, dict) and active.get("claude_state") not in {None, "ready"}
    return (
        state_path,
        SessionState(session=session, last_prompt=prompt, cwd=cwd if isinstance(cwd, str) else None, session_id=session_id),
        active_working,
    )


def _bridge_state_reap_prompt(state: Mapping[str, object]) -> str:
    active = state.get("active_turn")
    if isinstance(active, Mapping) and isinstance(active.get("prompt_preview"), str):
        return str(active["prompt_preview"])
    last_turn = state.get("last_turn")
    if isinstance(last_turn, Mapping) and isinstance(last_turn.get("prompt_preview"), str):
        return str(last_turn["prompt_preview"])
    return ""


def session_is_working(controller: TmuxController, session: str, state: SessionState, root: Path) -> bool:
    transcript_path, pending_prompt = resolve_status_transcript_path(root, state)
    try:
        screen = controller.capture_screen(session, height=80)
    except subprocess.CalledProcessError:
        screen = ""
    if pending_prompt:
        return analyze_screen_status(screen).state != "ready"
    status = analyze_combined_status(screen, transcript_path=transcript_path)
    return status.state in {"working", "needs_confirmation", "unknown"}


def _watch(controller: TmuxController, session: str, height: int, interval: float) -> int:
    previous = None
    while True:
        screen = controller.capture_screen(session, height)
        if screen != previous:
            print("\033[2J\033[H", end="")
            print(screen, end="")
            sys.stdout.flush()
            previous = screen
        time.sleep(interval)


def _follow_screen(
    controller: TmuxController,
    session: str,
    height: int,
    interval: float,
    append_path: Path | None,
) -> int:
    follower = RenderedScreenFollower()
    file_handle = append_path.open("a", encoding="utf-8") if append_path else None
    try:
        while True:
            changed = follower.diff(controller.capture_screen(session, height))
            if changed:
                print(changed, end="")
                sys.stdout.flush()
                if file_handle:
                    file_handle.write(changed)
                    file_handle.flush()
            time.sleep(interval)
    finally:
        if file_handle:
            file_handle.close()


def _chat(controller: TmuxController, session: str, height: int, interval: float, idle_seconds: float) -> int:
    print(controller.capture_screen(session, height), end="")
    while True:
        try:
            prompt = input("\nclaude> ")
        except EOFError:
            print()
            return 0

        if prompt.strip() in {"/quit", "/exit"}:
            return 0

        controller.send_prompt(session, prompt)
        write_session_state(_session_state_path(session), session, prompt, controller.pane_current_path(session))
        follow_until_idle(controller, session, height=height, interval=interval, idle_seconds=idle_seconds)


def follow_until_idle(
    controller: ScreenCaptureController,
    session: str,
    height: int = 200,
    interval: float = 0.5,
    idle_seconds: float = 2.0,
    write: Callable[[str], object] = sys.stdout.write,
    sleep: Callable[[float], object] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> None:
    previous = None
    last_changed_at = now()

    while True:
        screen = controller.capture_screen(session, height)
        if screen != previous:
            write(screen)
            sys.stdout.flush()
            previous = screen
            last_changed_at = now()
        elif now() - last_changed_at >= idle_seconds:
            return

        sleep(interval)


def analyze_screen_status(screen: str) -> ScreenStatus:
    active_area = _bottom_screen_area(screen)

    for pattern in CONFIRMATION_PATTERNS:
        if pattern.search(active_area):
            return ScreenStatus("needs_confirmation", f"matched {pattern.pattern}")

    for pattern in WORKING_PATTERNS:
        if pattern.search(active_area):
            return ScreenStatus("working", f"matched {pattern.pattern}")

    for pattern in READY_PATTERNS:
        if pattern.search(active_area):
            return ScreenStatus("ready", f"matched {pattern.pattern}")

    return ScreenStatus("unknown", "no ready, working, or confirmation marker matched")


def analyze_combined_status(screen: str, transcript_path: Path | None = None) -> ScreenStatus:
    screen_status = analyze_screen_status(screen)
    if transcript_path is None:
        return screen_status

    events, _ = read_transcript_events(transcript_path)
    transcript_status = analyze_transcript_status(events)
    if transcript_status.state == "working":
        return transcript_status
    if transcript_status.state == "needs_confirmation":
        return transcript_status
    if screen_status.state == "ready" and transcript_status.state == "ready":
        return ScreenStatus("ready", f"{screen_status.reason}; transcript ready")
    if screen_status.state == "ready" and transcript_status.state == "unknown":
        return screen_status
    return screen_status


def analyze_transcript_status(events: Sequence[dict]) -> ScreenStatus:
    latest_turn = _latest_turn_events(events)
    if not latest_turn:
        return ScreenStatus("unknown", "no user turn found in transcript")
    return analyze_turn_status(latest_turn)


def analyze_turn_status(turn_events: Sequence[dict]) -> ScreenStatus:
    if not turn_events:
        return ScreenStatus("unknown", "no target user turn found in transcript")

    latest_type = ""
    latest_event: dict | None = None
    for event in turn_events:
        event_type = str(event.get("type") or event.get("event") or event.get("role") or "")
        if _is_metadata_event(event_type):
            continue
        latest_type = event_type
        latest_event = event

    if latest_event is None:
        return ScreenStatus("unknown", "no meaningful transcript event after user")

    if latest_type == "user":
        content = _event_content(latest_event)
        if _is_interruption_user_content(content):
            return ScreenStatus("ready", "target turn was interrupted by user")
        if _is_tool_result_content(content):
            return ScreenStatus("working", "latest transcript event after user is tool_result")
        return ScreenStatus("working", "latest transcript event after user is user")

    if latest_type in {"tool_use", "tool_result"}:
        return ScreenStatus("working", f"latest transcript event after user is {latest_type}")

    if latest_type == "assistant":
        has_stop_reason, stop_reason = _assistant_stop_reason(latest_event)
        if has_stop_reason and stop_reason is None:
            return ScreenStatus("working", "latest assistant transcript has pending stop_reason")
        if stop_reason == "tool_use":
            return ScreenStatus("working", "latest assistant transcript stopped for tool_use")
        if stop_reason == "pause_turn":
            return ScreenStatus("working", "latest assistant transcript paused turn")
        content_types = _content_types(_event_content(latest_event))
        if "tool_use" in content_types:
            return ScreenStatus("working", "latest assistant transcript requested tool_use")
        if content_types and all(content_type == "thinking" for content_type in content_types):
            return ScreenStatus("working", "latest assistant transcript content is thinking")
        if "text" in content_types or not content_types:
            return ScreenStatus("ready", "latest assistant transcript event is complete enough")

    return ScreenStatus("unknown", f"latest transcript event after user is {latest_type or 'unknown'}")


def extract_latest_answer_text(events: Sequence[dict]) -> str | None:
    answers = extract_answer_texts(events, count=1)
    return answers[-1] if answers else None


def extract_answer_texts(events: Sequence[dict], count: int = 1) -> list[str]:
    answers = []
    for turn_events in _turn_events(events):
        current_text_blocks: list[str] = []
        for event in turn_events:
            event_type = str(event.get("type") or event.get("event") or event.get("role") or "")
            if event_type != "assistant":
                continue
            text_blocks = _extract_text_blocks(_event_content(event))
            if text_blocks:
                current_text_blocks = text_blocks
        if current_text_blocks:
            answers.append("\n".join(current_text_blocks))
    return answers[-max(count, 1) :]


def format_latest_turn(events: Sequence[dict]) -> str | None:
    formatted_turns = format_latest_turns(events, count=1)
    if formatted_turns is None:
        return None
    if formatted_turns.startswith("\n\n--- turn "):
        return formatted_turns.split("\n\n", 2)[2]
    return formatted_turns


def format_latest_turns(events: Sequence[dict], count: int = 1) -> str | None:
    turns = _turn_events(events)
    if not turns:
        return None

    selected_turns = turns[-max(count, 1) :]
    formatted = [_format_turn_events(turn_events) for turn_events in selected_turns]
    formatted = [item for item in formatted if item]
    if not formatted:
        return None
    if len(formatted) == 1:
        return formatted[0]
    return "\n\n".join(f"--- turn {index}/{len(formatted)} ---\n\n{item}" for index, item in enumerate(formatted, start=1))


def _format_turn_events(turn_events: Sequence[dict]) -> str | None:
    if not turn_events:
        return None

    sections: list[tuple[str, str]] = []
    for event in turn_events:
        event_type = str(event.get("type") or event.get("event") or event.get("role") or "")
        content = _event_content(event)
        if event_type == "user":
            if _is_tool_result_content(content):
                sections.append(("[tool_result]", _format_tool_result_content(content)))
            else:
                user_text = _format_user_content(content)
                if user_text:
                    sections.append(("[user]", user_text))
            continue

        if event_type != "assistant":
            continue

        for item in _content_items(content):
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "thinking" and isinstance(item.get("thinking"), str):
                sections.append(("[thinking]", item["thinking"]))
            elif item_type == "tool_use":
                name = str(item.get("name") or item.get("tool_name") or "tool")
                tool_input = item.get("input") or item.get("tool_input") or {}
                sections.append((f"[tool_use] {name}", json.dumps(tool_input, ensure_ascii=False)))
            elif item_type == "text" and isinstance(item.get("text"), str):
                sections.append(("[assistant]", item["text"]))

    if not sections:
        return None
    return "\n\n".join(f"{header}\n{body}" for header, body in sections)


def _latest_turn_events(events: Sequence[dict]) -> list[dict]:
    turns = _turn_events(events)
    return turns[-1] if turns else []


def target_turn_events(events: Sequence[dict], state: SessionState | None = None) -> list[dict]:
    turns = _turn_events(events)
    if not turns:
        return []
    if state is None or not state.last_prompt:
        return turns[-1]

    matched = [turn for turn in turns if _turn_matches_prompt(turn, state.last_prompt)]
    return matched[-1] if matched else []


def normalize_stream_events(
    events: Sequence[dict],
    tool_result_limit: int | None = DEFAULT_TOOL_RESULT_TEXT_LIMIT,
) -> list[dict]:
    normalized: list[dict] = []
    for event in events:
        event_type = str(event.get("type") or event.get("event") or event.get("role") or "")
        timestamp = str(event.get("timestamp", "")) or None
        content = _event_content(event)

        if event_type == "user":
            if _is_tool_result_content(content):
                tool_result = _compact_payload(
                    {
                        "event": "tool_result",
                        "timestamp": timestamp,
                        "tool_use_id": _tool_result_use_id(content),
                        **_truncated_text_payload("text", _format_tool_result_content(content), tool_result_limit),
                        "is_error": _tool_result_is_error(content),
                        **_tool_use_result_preview(event.get("toolUseResult"), tool_result_limit),
                    }
                )
                normalized.append(tool_result)
            elif not _is_internal_user_content(content):
                user_text = _format_user_content(content)
                if user_text:
                    normalized.append(_compact_payload({"event": "user", "timestamp": timestamp, "text": user_text}))
            continue

        if event_type == "tool_use":
            normalized.append(
                _compact_payload(
                    {
                        "event": "tool_use",
                        "timestamp": timestamp,
                        "name": event.get("tool_name") or event.get("name"),
                        "input": event.get("tool_input") or event.get("input") or {},
                    }
                )
            )
            continue

        if event_type == "tool_result":
            normalized.append(
                _compact_payload(
                    {
                        "event": "tool_result",
                        "timestamp": timestamp,
                        **_truncated_text_payload(
                            "text",
                            str(event.get("tool_output") or event.get("content") or ""),
                            tool_result_limit,
                        ),
                    }
                )
            )
            continue

        if event_type != "assistant":
            continue

        for item in _content_items(content):
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "thinking":
                normalized.append(_compact_payload(_thinking_payload(item, timestamp)))
            elif item_type == "tool_use":
                normalized.append(
                    _compact_payload(
                        {
                            "event": "tool_use",
                            "timestamp": timestamp,
                            "id": item.get("id"),
                            "caller": item.get("caller"),
                            "name": item.get("name") or item.get("tool_name"),
                            "input": item.get("input") or item.get("tool_input") or {},
                        }
                    )
                )
            elif item_type == "text" and isinstance(item.get("text"), str):
                normalized.append(
                    _compact_payload({"event": "assistant_text", "timestamp": timestamp, "text": item["text"]})
                )
    return normalized


def stream_transcript_until_done(
    transcript: Path,
    state: SessionState | None,
    controller: ScreenCaptureController,
    session: str,
    interval: float = 0.5,
    timeout: float = 300.0,
    idle_seconds: float = 2.0,
    write: Callable[[str], object] = sys.stdout.write,
    sleep: Callable[[float], object] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    tool_result_limit: int | None = DEFAULT_TOOL_RESULT_TEXT_LIMIT,
) -> ScreenStatus:
    deadline = now() + timeout
    emitted_event_count = 0
    ready_since: float | None = None
    last_status = ScreenStatus("unknown", "not inspected yet")

    while now() < deadline:
        events, _ = read_transcript_events(transcript)
        turn_events = target_turn_events(events, state)
        screen_status = analyze_screen_status(controller.capture_screen(session, height=80))

        if len(turn_events) < emitted_event_count:
            emitted_event_count = 0

        new_events = turn_events[emitted_event_count:]
        for payload in normalize_stream_events(new_events, tool_result_limit=tool_result_limit):
            write(json.dumps(payload, ensure_ascii=False) + "\n")
            if hasattr(sys.stdout, "flush"):
                sys.stdout.flush()
        if new_events:
            emitted_event_count = len(turn_events)
            ready_since = None

        transcript_status = analyze_turn_status(turn_events)
        if transcript_status.state == "ready":
            if screen_status.state == "ready":
                last_status = ScreenStatus("ready", f"{screen_status.reason}; transcript ready")
                if ready_since is None:
                    ready_since = now()
                elif now() - ready_since >= idle_seconds:
                    _write_stream_done(turn_events, last_status, write)
                    return last_status
            else:
                ready_since = None
                last_status = screen_status
        else:
            ready_since = None
            last_status = transcript_status

        sleep(interval)

    timeout_status = ScreenStatus("timeout", f"not ready after {timeout:.1f}s; last={last_status.state}")
    write(json.dumps({"event": "timeout", "state": timeout_status.state, "reason": timeout_status.reason}) + "\n")
    return timeout_status


def stream_high_level_transcript_until_done(
    transcript: Path,
    runtime: StreamRuntime,
    controller: ScreenCaptureController,
    root: Path = DEFAULT_TRANSCRIPT_ROOT,
    interval: float = 2.0,
    timeout: float = 300.0,
    idle_seconds: float = 2.0,
    write: Callable[[str], object] = sys.stdout.write,
    sleep: Callable[[float], object] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    tool_result_limit: int | None = DEFAULT_TOOL_RESULT_TEXT_LIMIT,
) -> ScreenStatus:
    deadline = now() + timeout
    read_offset = runtime.before_send_offset
    ready_since: float | None = None
    last_status = ScreenStatus("unknown", "not inspected yet")
    current_turn_events: list[dict] = []
    file_identity = transcript_identity(transcript)
    last_file_identity = file_identity
    completed_offset = runtime.before_send_offset
    anchored = False
    retried_unanchored_submit = False

    while now() < deadline:
        try:
            stat = transcript.stat()
        except OSError:
            stat = None
        current_identity = transcript_identity(transcript) if stat is not None else None
        if stat is None or stat.st_size < read_offset or current_identity != last_file_identity:
            replacement = resolve_high_level_transcript(
                root,
                runtime.cwd,
                read_bridge_state(runtime.state_path) or {},
                session_id=runtime.session_id,
            )
            if replacement is not None:
                transcript = replacement
                file_identity = transcript_identity(transcript)
                last_file_identity = file_identity
                read_offset = 0
                anchored = False
                current_turn_events = []

        records, read_offset = read_transcript_records(transcript, read_offset)
        if records:
            for record in records:
                if not anchored:
                    if not _is_anchor_user_record(record, runtime.prompt):
                        continue
                    anchored = True
                    _mark_turn_anchor(runtime, record.start_offset, record.end_offset)
                current_turn_events.append(record.event)
                completed_offset = record.end_offset
                for payload in normalize_stream_record(
                    record,
                    runtime.turn_id,
                    file_identity,
                    tool_result_limit=tool_result_limit,
                ):
                    payload["session_id"] = runtime.session_id
                    _write_jsonl(write, payload)
                    _update_active_turn_offsets(runtime, read_offset, record.end_offset)
            ready_since = None
            _mark_turn_working(runtime, current_turn_events, read_offset)

        screen_status = analyze_screen_status(controller.capture_screen(runtime.tmux_session, height=80))
        if not anchored:
            retried_unanchored_submit = _maybe_retry_unanchored_submit(
                controller,
                runtime,
                screen_status,
                retried_unanchored_submit,
                now(),
            )
        transcript_status = analyze_turn_status(current_turn_events)
        if transcript_status.state == "ready" and screen_status.state == "ready":
            last_status = ScreenStatus("ready", f"{screen_status.reason}; transcript ready")
            if ready_since is None:
                ready_since = now()
            elif now() - ready_since >= idle_seconds:
                elapsed_ms = _elapsed_ms(runtime, now())
                state = read_bridge_state(runtime.state_path) or {}
                done = high_level_done_payload(runtime, current_turn_events, last_status, completed_offset)
                metrics = high_level_metrics_payload(
                    runtime,
                    current_turn_events,
                    completed_offset,
                    elapsed_ms=elapsed_ms,
                    state=state,
                )
                _write_jsonl(write, done)
                _write_jsonl(write, metrics)
                _mark_turn_done(runtime, current_turn_events, completed_offset, elapsed_ms, metrics, transcript)
                return last_status
        else:
            ready_since = None
            last_status = transcript_status if transcript_status.state != "unknown" else screen_status

        sleep(interval)

    timeout_status = ScreenStatus("timeout", f"not ready after {timeout:.1f}s; last={last_status.state}")
    _write_jsonl(
        write,
        {
            "event": "timeout",
            "session_id": runtime.session_id,
            "turn_id": runtime.turn_id,
            "state": timeout_status.state,
            "reason": timeout_status.reason,
        },
    )
    _mark_turn_timeout(runtime)
    return timeout_status


def _write_jsonl(write: Callable[[str], object], payload: dict) -> None:
    write(json.dumps(_compact_payload(payload), ensure_ascii=False) + "\n")
    if hasattr(sys.stdout, "flush"):
        sys.stdout.flush()


def _capture_screen_status(controller: ScreenCaptureController, tmux_session: str) -> ScreenStatus:
    try:
        return analyze_screen_status(controller.capture_screen(tmux_session, height=80))
    except subprocess.CalledProcessError:
        return ScreenStatus("unknown", "screen unavailable")


def _maybe_retry_unanchored_submit(
    controller: object,
    runtime: StreamRuntime,
    screen_status: ScreenStatus,
    already_retried: bool,
    current_time: float,
) -> bool:
    if already_retried:
        return True
    if screen_status.state in {"working", "needs_confirmation"}:
        return False
    if current_time - runtime.started_at_monotonic < UNANCHORED_SUBMIT_RETRY_SECONDS:
        return False
    send_enter = getattr(controller, "send_enter", None)
    if not callable(send_enter):
        return False
    send_enter(runtime.tmux_session)
    return True


def read_transcript_records(path: Path, offset: int = 0) -> tuple[list[TranscriptRecord], int]:
    records: list[TranscriptRecord] = []
    with path.open("r", encoding="utf-8", errors="replace") as file:
        file.seek(offset)
        while True:
            start = file.tell()
            line = file.readline()
            if not line:
                return records, file.tell()
            end = file.tell()
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                if not line.endswith("\n"):
                    return records, start
                continue
            if isinstance(event, dict):
                records.append(TranscriptRecord(event=event, start_offset=start, end_offset=end))


def transcript_identity(path: Path) -> str:
    stat = path.stat()
    return f"dev{stat.st_dev}-ino{stat.st_ino}"


def normalize_stream_record(
    record: TranscriptRecord,
    turn_id: str,
    file_identity: str,
    tool_result_limit: int | None = DEFAULT_TOOL_RESULT_TEXT_LIMIT,
) -> list[dict]:
    payloads = normalize_stream_events([record.event], tool_result_limit=tool_result_limit)
    for index, payload in enumerate(payloads):
        payload["turn_id"] = turn_id
        payload["event_id"] = (
            f"{turn_id}:{file_identity}:{record.start_offset:016d}-{record.end_offset:016d}:"
            f"block{index}:{payload['event']}"
        )
        payload["source_offset"] = record.start_offset
        payload["source_end_offset"] = record.end_offset
        payload["block_index"] = index
    return payloads


def _thinking_payload(item: Mapping[str, object], timestamp: str | None) -> dict:
    text = _first_string(item, "thinking", "text", "summary")
    has_signature = isinstance(item.get("signature"), str) and bool(item.get("signature"))
    payload = {
        "event": "thinking",
        "timestamp": timestamp,
        "text": text or "",
        "text_available": bool(text),
        "has_signature": has_signature,
    }
    if not text and has_signature:
        payload["note"] = "thinking text unavailable; signature present"
    return payload


def _first_string(source: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _truncated_text_payload(key: str, text: str, limit: int | None) -> dict:
    if limit is None or limit < 0 or len(text) <= limit:
        return {key: text}
    return {
        key: text[:limit] + "...",
        f"{key}_truncated": True,
        f"{key}_full_length": len(text),
    }


def _tool_use_result_preview(result: object, limit: int | None) -> dict:
    if result is None:
        return {}
    if isinstance(result, str):
        preview = result
    else:
        try:
            preview = json.dumps(result, ensure_ascii=False, sort_keys=True)
        except TypeError:
            preview = str(result)
    payload = _truncated_text_payload("result_preview", preview, limit)
    if "result_preview_truncated" not in payload:
        payload["result_preview_truncated"] = False
    payload["result_preview_full_length"] = len(preview)
    return payload


def _is_anchor_user_record(record: TranscriptRecord, prompt: str) -> bool:
    event = record.event
    event_type = str(event.get("type") or event.get("event") or event.get("role") or "")
    if event_type != "user":
        return False
    content = _event_content(event)
    if _is_tool_result_content(content) or _is_internal_user_content(content) or _is_interruption_user_content(content):
        return False
    user_text = _format_user_content(content)
    if not prompt:
        return True
    normalized_prompt = prompt.strip()
    normalized_user_text = user_text.strip()
    if normalized_prompt and normalized_prompt in normalized_user_text:
        return True
    return _text_contains_with_normalized_whitespace(user_text, prompt)


def _text_contains_with_normalized_whitespace(text: str, needle: str) -> bool:
    normalized_text = _normalize_match_whitespace(text)
    normalized_needle = _normalize_match_whitespace(needle)
    return bool(normalized_needle) and normalized_needle in normalized_text


def _normalize_match_whitespace(value: str) -> str:
    return re.sub(r"[ \t\f\v]+", " ", value.strip())


def _is_external_user_record(record: TranscriptRecord) -> bool:
    event = record.event
    event_type = str(event.get("type") or event.get("event") or event.get("role") or "")
    if event_type != "user":
        return False
    content = _event_content(event)
    return not _is_tool_result_content(content) and not _is_internal_user_content(content) and not _is_interruption_user_content(content)


def high_level_done_payload(
    runtime: StreamRuntime,
    turn_events: Sequence[dict],
    status: ScreenStatus,
    completed_offset: int,
) -> dict:
    return _compact_payload(
        {
            "event": "done",
            "session_id": runtime.session_id,
            "turn_id": runtime.turn_id,
            "event_id": f"{runtime.turn_id}:done:{completed_offset}",
            "source_offset": completed_offset,
            "source_end_offset": completed_offset,
            "block_index": -1,
            "state": status.state,
            "reason": status.reason,
            "answer": extract_latest_answer_text(turn_events),
        }
    )


def high_level_metrics_payload(
    runtime: StreamRuntime,
    turn_events: Sequence[dict],
    completed_offset: int,
    elapsed_ms: int | None = None,
    state: Mapping[str, object] | None = None,
) -> dict:
    usage = aggregate_turn_usage(turn_events)
    api_call_count = count_turn_usage_calls(turn_events)
    if usage and api_call_count:
        usage["api_call_count"] = api_call_count
    context = latest_context(turn_events)
    model = latest_model(turn_events)
    normalized_usage = usage or None
    cost = result_total_cost(turn_events) or estimate_turn_cost(model, normalized_usage)
    cost = add_session_cost_to_turn_cost(cost, state)
    return _compact_payload(
        {
            "event": "metrics",
            "session_id": runtime.session_id,
            "turn_id": runtime.turn_id,
            "event_id": f"{runtime.turn_id}:metrics:{completed_offset}",
            "source_offset": completed_offset,
            "source_end_offset": completed_offset,
            "block_index": -1,
            "scope": "turn_final",
            "elapsed_ms": elapsed_ms,
            "model": model,
            "usage": normalized_usage,
            "context": context or None,
            "cost": cost,
        }
    )


def latest_usage(events: Sequence[dict]) -> dict:
    for event in reversed(events):
        usage = _extract_usage(event)
        if usage:
            return usage
    return {}


def aggregate_turn_usage(events: Sequence[dict]) -> dict:
    totals: dict[str, int | float] = {}
    seen_usage_ids: set[tuple[str, str]] = set()
    for event in events:
        if _is_result_event(event):
            continue
        raw_usage = _extract_usage(event)
        if not raw_usage:
            continue
        usage_id = _usage_event_identity(event)
        if usage_id is not None:
            if usage_id in seen_usage_ids:
                continue
            seen_usage_ids.add(usage_id)
        usage = normalize_usage(raw_usage)
        if not usage:
            continue
        for key, value in usage.items():
            totals[key] = totals.get(key, 0) + value
    if totals:
        return totals
    return normalize_usage(latest_usage(events)) or {}


def count_turn_usage_calls(events: Sequence[dict]) -> int:
    count = 0
    seen_usage_ids: set[tuple[str, str]] = set()
    for event in events:
        if _is_result_event(event):
            continue
        raw_usage = _extract_usage(event)
        if not raw_usage:
            continue
        if not normalize_usage(raw_usage):
            continue
        usage_id = _usage_event_identity(event)
        if usage_id is not None:
            if usage_id in seen_usage_ids:
                continue
            seen_usage_ids.add(usage_id)
        count += 1
    return count


def _usage_event_identity(event: Mapping[str, object]) -> tuple[str, str] | None:
    request_id = event.get("requestId") or event.get("request_id")
    message_id = _nested_value(event, "message", "id") or _nested_value(event, "response", "id")
    if request_id is None and message_id is None:
        return None
    return (str(request_id or ""), str(message_id or ""))


def normalize_usage(usage: Mapping[str, object]) -> dict | None:
    if not usage:
        return None
    normalized = {
        "input_tokens": _numeric_value(usage, "input_tokens"),
        "cache_read_tokens": _numeric_value(usage, "cache_read_input_tokens", "cache_read_tokens"),
        "cache_write_tokens": _numeric_value(usage, "cache_creation_input_tokens", "cache_write_tokens"),
        "output_tokens": _numeric_value(usage, "output_tokens"),
    }
    return {key: value for key, value in normalized.items() if value is not None}


def latest_context(events: Sequence[dict]) -> dict:
    for event in reversed(events):
        context = _extract_context(event)
        if context:
            return context
    return {}


def latest_model(events: Sequence[dict]) -> str | None:
    for event in reversed(events):
        for value in (
            event.get("model"),
            _nested_value(event, "message", "model"),
            _nested_value(event, "response", "model"),
        ):
            if isinstance(value, str) and value:
                return value
    return None


def result_total_cost(events: Sequence[dict]) -> dict | None:
    for event in reversed(events):
        if not _is_result_event(event):
            continue
        total_cost_usd = _numeric_value(event, "total_cost_usd")
        if total_cost_usd is not None:
            return {
                "estimated": False,
                "currency": "USD",
                "source": "claude_result_total_cost_usd",
                "turn_usd": round(float(total_cost_usd), 8),
            }
    return None


def _is_result_event(event: Mapping[str, object]) -> bool:
    return str(event.get("type") or event.get("event") or "") == "result"


def estimate_turn_cost(
    model: str | None,
    usage: Mapping[str, object] | None,
    pricing_table: Mapping[str, object] | None = None,
) -> dict:
    if not usage:
        return {"estimated": False, "reason": "usage_unavailable"}
    if not model:
        return {"estimated": False, "reason": "model_unavailable"}

    table = pricing_table or load_pricing_table()
    if not table:
        return {"estimated": False, "reason": "pricing_table_unavailable"}

    selection = select_pricing_model(model, table)
    if selection is None:
        return {"estimated": False, "reason": "pricing_model_not_found", "model": model}

    model_id, model_pricing, match_type = selection
    rates = model_pricing.get("rates_per_mtok")
    if not isinstance(rates, dict):
        return {"estimated": False, "reason": "pricing_rates_missing", "model": model}

    cache_write_ttl = str(table.get("default_cache_write_ttl") or "1h")
    cache_write_key = "cache_write_1h" if cache_write_ttl == "1h" else "cache_write_5m"
    used_rates = {
        "input": _float_value(rates, "input"),
        "cache_read": _float_value(rates, "cache_read"),
        "cache_write": _float_value(rates, cache_write_key),
        "output": _float_value(rates, "output"),
    }
    if any(value is None for value in used_rates.values()):
        return {"estimated": False, "reason": "pricing_rates_incomplete", "model": model_id}

    line_items = {
        "input_usd": _usd_line_item(usage, "input_tokens", used_rates["input"]),
        "cache_read_usd": _usd_line_item(usage, "cache_read_tokens", used_rates["cache_read"]),
        "cache_write_usd": _usd_line_item(usage, "cache_write_tokens", used_rates["cache_write"]),
        "output_usd": _usd_line_item(usage, "output_tokens", used_rates["output"]),
    }
    turn_usd = round(sum(line_items.values()), 8)
    return {
        "estimated": True,
        "currency": str(table.get("currency") or "USD"),
        "pricing_version": str(table.get("version") or ""),
        "pricing_source": str(table.get("source_url") or ""),
        "pricing_checked_at": str(table.get("checked_at") or ""),
        "model": model_id,
        "model_match": match_type,
        "cache_write_ttl": cache_write_ttl,
        "rates_per_mtok": used_rates,
        "line_items": line_items,
        "turn_usd": turn_usd,
    }


def add_session_cost_to_turn_cost(cost: Mapping[str, object], state: Mapping[str, object] | None) -> dict:
    enriched = dict(cost)
    turn_usd = _numeric_value(enriched, "turn_usd")
    if turn_usd is None:
        return enriched
    previous_total = 0.0
    if isinstance(state, Mapping):
        cost_totals = state.get("cost_totals")
        if isinstance(cost_totals, Mapping):
            previous_total = float(_numeric_value(cost_totals, "session_usd") or 0.0)
    enriched["session_usd"] = round(previous_total + float(turn_usd), 8)
    return enriched


def _elapsed_ms(runtime: StreamRuntime, current_monotonic: float) -> int:
    return max(0, int(round((current_monotonic - runtime.started_at_monotonic) * 1000)))


def build_completed_turn_record(
    runtime: StreamRuntime,
    turn_events: Sequence[dict],
    completed_offset: int,
    elapsed_ms: int | None,
    metrics: Mapping[str, object],
    transcript: Path | None = None,
) -> dict:
    return _compact_payload(
        {
            "turn_id": runtime.turn_id,
            "session_id": runtime.session_id,
            "completed_at": _utc_timestamp(time.time()),
            "started_at": runtime.started_at_utc,
            "answer": extract_latest_answer_text(turn_events),
            "anchor_start_offset": _active_turn_value(runtime.state_path, "anchor_start_offset"),
            "anchor_end_offset": _active_turn_value(runtime.state_path, "anchor_end_offset"),
            "completed_offset": completed_offset,
            "transcript": transcript_file_state(transcript, completed_offset),
            "elapsed_ms": elapsed_ms,
            "model": metrics.get("model"),
            "usage": metrics.get("usage"),
            "context": metrics.get("context"),
            "cost": _turn_cost_for_completed_record(metrics.get("cost")),
        }
    )


def add_completed_turn_to_state(state: dict, completed_record: Mapping[str, object]) -> dict:
    payload = dict(state)
    existing = payload.get("completed_turns")
    turns = [turn for turn in existing if isinstance(turn, dict)] if isinstance(existing, list) else []
    turn_id = completed_record.get("turn_id")
    turns = [turn for turn in turns if turn.get("turn_id") != turn_id]
    turns.append(dict(completed_record))
    turns = turns[-200:]
    payload["completed_turns"] = turns
    payload["usage_totals"] = usage_totals_from_completed_turns(turns)
    payload["cost_totals"] = cost_totals_from_completed_turns(turns)
    return payload


def usage_totals_from_completed_turns(turns: Sequence[Mapping[str, object]]) -> dict:
    totals: dict[str, int | float] = {}
    for turn in turns:
        usage = turn.get("usage")
        if not isinstance(usage, Mapping):
            continue
        for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"):
            value = _numeric_value(usage, key)
            if value is not None:
                totals[key] = totals.get(key, 0) + value
    return totals


def cost_totals_from_completed_turns(turns: Sequence[Mapping[str, object]]) -> dict:
    session_usd = 0.0
    has_cost = False
    for turn in turns:
        cost = turn.get("cost")
        if not isinstance(cost, Mapping):
            continue
        if cost.get("currency") != "USD":
            continue
        turn_usd = _numeric_value(cost, "turn_usd")
        if turn_usd is None:
            continue
        has_cost = True
        session_usd += float(turn_usd)
    if not has_cost:
        return {}
    return {"currency": "USD", "session_usd": round(session_usd, 8)}


def _turn_cost_for_completed_record(cost: object) -> dict | None:
    if not isinstance(cost, Mapping):
        return None
    turn_cost = dict(cost)
    turn_cost.pop("session_usd", None)
    return turn_cost


def _active_turn_value(state_path: Path, key: str) -> object:
    state = read_bridge_state(state_path) or {}
    active = state.get("active_turn")
    if isinstance(active, Mapping):
        return active.get(key)
    return None


def resolve_pricing_table_path(path: Path = DEFAULT_PRICING_TABLE) -> Path:
    if path != DEFAULT_PRICING_TABLE:
        return path
    if path.exists():
        return path
    return DEFAULT_INSTALLED_PRICING_TABLE


def load_pricing_table(path: Path | None = None) -> dict | None:
    global _PRICING_TABLE_CACHE
    requested_path = path or DEFAULT_PRICING_TABLE
    uses_default_path = requested_path == DEFAULT_PRICING_TABLE
    if uses_default_path and _PRICING_TABLE_CACHE is not None:
        return _PRICING_TABLE_CACHE
    resolved_path = resolve_pricing_table_path(requested_path)
    try:
        table = json.loads(resolved_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if uses_default_path:
        _PRICING_TABLE_CACHE = table
    return table


def select_pricing_model(model: str, table: Mapping[str, object]) -> tuple[str, Mapping[str, object], str] | None:
    models = table.get("models")
    if not isinstance(models, dict):
        return None

    normalized_model = _pricing_key(model)
    aliases: list[tuple[str, str]] = []
    for model_id, model_pricing in models.items():
        if not isinstance(model_id, str) or not isinstance(model_pricing, dict):
            continue
        values = [model_id]
        raw_aliases = model_pricing.get("aliases")
        if isinstance(raw_aliases, list):
            values.extend(alias for alias in raw_aliases if isinstance(alias, str))
        for alias in values:
            aliases.append((_pricing_key(alias), model_id))

    for alias_key, model_id in sorted(aliases, key=lambda item: len(item[0]), reverse=True):
        if normalized_model == alias_key or normalized_model.startswith(alias_key + "-"):
            model_pricing = models.get(model_id)
            if isinstance(model_pricing, dict):
                return model_id, model_pricing, "exact"

    family = _pricing_family(normalized_model)
    families = table.get("families")
    if not family or not isinstance(families, dict):
        return None
    family_config = families.get(family)
    if not isinstance(family_config, dict):
        return None
    latest = family_config.get("latest")
    if not isinstance(latest, str):
        return None
    model_pricing = models.get(latest)
    if not isinstance(model_pricing, dict):
        return None
    return latest, model_pricing, "family_latest"


def _pricing_key(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-")


def _pricing_family(normalized_model: str) -> str | None:
    if "sonnet" in normalized_model:
        return "sonnet"
    if "opus" in normalized_model:
        return "opus"
    if "haiku" in normalized_model or "hiku" in normalized_model:
        return "haiku"
    return None


def _usd_line_item(usage: Mapping[str, object], token_key: str, rate_per_mtok: float | None) -> float:
    tokens = _numeric_value(usage, token_key) or 0
    if rate_per_mtok is None:
        return 0.0
    return round(float(tokens) * rate_per_mtok / 1_000_000, 8)


def _float_value(source: Mapping[str, object], key: str) -> float | None:
    value = source.get(key)
    if isinstance(value, int | float):
        return float(value)
    return None


def _numeric_value(source: Mapping[str, object], *keys: str) -> int | float | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, int | float):
            return value
    return None


def _nested_value(source: dict, *keys: str) -> object:
    value: object = source
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _update_active_turn_offsets(runtime: StreamRuntime, read_offset: int, flushed_offset: int) -> None:
    def mutate(state: dict) -> dict | None:
        active = state.get("active_turn")
        if not _active_turn_matches_runtime(active, runtime):
            return None
        active["read_offset"] = read_offset
        active["last_stdout_flushed_offset"] = flushed_offset
        active["heartbeat_at"] = _utc_timestamp(time.time())
        state["active_turn"] = active
        return state

    mutate_high_level_state(runtime.state_path, mutate)


def _mark_turn_anchor(runtime: StreamRuntime, anchor_start: int, anchor_end: int) -> None:
    def mutate(state: dict) -> dict | None:
        active = state.get("active_turn")
        if not _active_turn_matches_runtime(active, runtime):
            return None
        active["anchor_start_offset"] = anchor_start
        active["anchor_end_offset"] = anchor_end
        active["replay_start_offset"] = anchor_end
        active["anchor_strategy"] = "after_offset"
        state["active_turn"] = active
        return state

    mutate_high_level_state(runtime.state_path, mutate)


def _mark_turn_working(runtime: StreamRuntime, turn_events: Sequence[dict], read_offset: int) -> None:
    def mutate(state: dict) -> dict | None:
        active = state.get("active_turn")
        if not _active_turn_matches_runtime(active, runtime):
            return None
        active["claude_state"] = "working"
        active["stream_state"] = "active"
        state["active_turn"] = active
        return state

    mutate_high_level_state(runtime.state_path, mutate)


def _mark_turn_done(
    runtime: StreamRuntime,
    turn_events: Sequence[dict],
    completed_offset: int,
    elapsed_ms: int | None,
    metrics: Mapping[str, object],
    transcript: Path | None = None,
) -> None:
    def mutate(state: dict) -> dict | None:
        if not _active_turn_matches_runtime(state.get("active_turn"), runtime):
            return None
        completed_record = build_completed_turn_record(runtime, turn_events, completed_offset, elapsed_ms, metrics, transcript)
        state = _mark_active_turn_state(state, runtime, "ready", "done", completed_offset=completed_offset)
        transcript_state = transcript_file_state(transcript, completed_offset)
        if transcript_state:
            state["transcript"] = transcript_state
        last_turn = state.get("last_turn")
        if isinstance(last_turn, dict):
            last_turn["answer"] = completed_record.get("answer")
            last_turn["elapsed_ms"] = elapsed_ms
            last_turn["model"] = completed_record.get("model")
            last_turn["usage"] = completed_record.get("usage")
            last_turn["context"] = completed_record.get("context")
            last_turn["cost"] = completed_record.get("cost")
            state["last_turn"] = _compact_payload(last_turn)
        return add_completed_turn_to_state(state, completed_record)

    mutate_high_level_state(runtime.state_path, mutate)


def _mark_turn_timeout(runtime: StreamRuntime) -> None:
    def mutate(state: dict) -> dict | None:
        if not _active_turn_matches_runtime(state.get("active_turn"), runtime):
            return None
        return _mark_active_turn_state(state, runtime, "working", "timeout")

    mutate_high_level_state(runtime.state_path, mutate)


def _mark_turn_interrupted(runtime: StreamRuntime) -> None:
    def mutate(state: dict) -> dict | None:
        if not _active_turn_matches_runtime(state.get("active_turn"), runtime):
            return None
        return _mark_active_turn_state(state, runtime, "working", "interrupted")

    mutate_high_level_state(runtime.state_path, mutate)


def _active_turn_matches_runtime(active: object, runtime: StreamRuntime) -> bool:
    if not isinstance(active, dict):
        return False
    turn_id = active.get("turn_id")
    return turn_id in {None, runtime.turn_id}


def recover_stale_active_turn(
    state_path: Path,
    state: dict,
    controller: TmuxController,
    tmux_session: str,
    root: Path = DEFAULT_TRANSCRIPT_ROOT,
    cwd: Path | None = None,
    state_dir: Path = DEFAULT_STATE_DIR,
    session_id: str | None = None,
    stale_seconds: float = 300.0,
) -> bool:
    active = state.get("active_turn")
    if not isinstance(active, dict):
        return False
    stream_state = active.get("stream_state")
    if stream_state not in {"active", "timeout", "failed", "interrupted"}:
        return False

    heartbeat = _parse_utc_timestamp(str(active.get("heartbeat_at") or ""))
    if stream_state == "active" and heartbeat is not None and time.time() - heartbeat < stale_seconds:
        return False

    if not controller.session_exists(tmux_session):
        if stream_state in {"interrupted", "failed"}:
            _move_active_turn_to_last_turn(state_path, active.get("turn_id"))
            return True
        if heartbeat is not None and time.time() - heartbeat < stale_seconds:
            return False
        _move_active_turn_to_last_turn(state_path, active.get("turn_id"))
        return True

    try:
        screen_status = analyze_screen_status(controller.capture_screen(tmux_session, height=80))
    except subprocess.CalledProcessError:
        screen_status = ScreenStatus("unknown", "screen unavailable")
    if screen_status.state == "ready":
        if finalize_recoverable_active_turn(
            state_path=state_path,
            state=state,
            controller_status=screen_status,
            root=root,
            cwd=cwd,
            state_dir=state_dir,
            session_id=session_id,
            tmux_session=tmux_session,
        ):
            return True

    heartbeat = _parse_utc_timestamp(str(active.get("heartbeat_at") or ""))
    if heartbeat is not None and time.time() - heartbeat < stale_seconds:
        return False

    if screen_status.state == "ready":
        _move_active_turn_to_last_turn(state_path, active.get("turn_id"))
        return True

    return False


def _move_active_turn_to_last_turn(state_path: Path, turn_id: object = None) -> None:
    def mutate(state: dict) -> dict | None:
        active = state.get("active_turn")
        if not isinstance(active, dict):
            return None
        if turn_id is not None and active.get("turn_id") != turn_id:
            return None
        state["last_turn"] = active
        state["active_turn"] = None
        return state

    mutate_high_level_state(state_path, mutate)


def finalize_recoverable_active_turn(
    state_path: Path,
    state: dict,
    controller_status: ScreenStatus,
    root: Path = DEFAULT_TRANSCRIPT_ROOT,
    cwd: Path | None = None,
    state_dir: Path = DEFAULT_STATE_DIR,
    session_id: str | None = None,
    tmux_session: str | None = None,
) -> bool:
    active = state.get("active_turn")
    if not isinstance(active, dict):
        return False
    cwd_value = cwd or Path(str(state.get("cwd") or "."))
    actual_session_id = session_id or str(state.get("session_id") or "")
    if not actual_session_id:
        return False

    recovered = collect_recoverable_active_turn(state, root, cwd_value, actual_session_id)
    if recovered is None:
        return False
    transcript, turn_events, completed_offset = recovered

    prompt = str(active.get("prompt_preview") or "")
    start_offset = recoverable_turn_start_offset(active)
    wall_started = _parse_utc_timestamp(str(active.get("before_send_wall_time_utc") or ""))
    elapsed_ms = int(max(0.0, time.time() - wall_started) * 1000) if wall_started is not None else None
    runtime = StreamRuntime(
        session_id=actual_session_id,
        tmux_session=tmux_session or str(state.get("tmux_session") or web_tmux_session_name(actual_session_id)),
        state_path=state_path,
        state_dir=state_dir,
        cwd=cwd_value,
        prompt=prompt,
        turn_id=str(active.get("turn_id") or make_turn_id()),
        before_send_offset=start_offset,
        replay_start_offset=start_offset,
        before_send_transcript=transcript,
        started_at_monotonic=time.monotonic(),
        started_at_utc=str(active.get("before_send_wall_time_utc") or "") or None,
    )
    metrics = high_level_metrics_payload(
        runtime,
        turn_events,
        completed_offset,
        elapsed_ms=elapsed_ms,
        state=state,
    )
    _mark_turn_done(runtime, turn_events, completed_offset, elapsed_ms, metrics, transcript)
    latest_state = read_bridge_state(state_path) or {}
    last_turn = latest_state.get("last_turn")
    return (
        isinstance(last_turn, dict)
        and last_turn.get("turn_id") == runtime.turn_id
        and latest_state.get("active_turn") is None
        and controller_status.state == "ready"
    )


def collect_recoverable_active_turn(
    state: Mapping[str, object],
    root: Path,
    cwd: Path,
    session_id: str,
) -> tuple[Path, list[dict], int] | None:
    active = state.get("active_turn")
    if not isinstance(active, Mapping):
        return None
    transcript = _attach_transcript_path(state, active, root, cwd, session_id)
    if transcript is None:
        return None

    prompt = str(active.get("prompt_preview") or "")
    start_offset = recoverable_turn_start_offset(active)
    records, read_offset = read_transcript_records(transcript, start_offset)
    turn_events: list[dict] = []
    completed_offset = start_offset
    anchored = start_offset == _int_or_none(active.get("anchor_start_offset"))
    for record in records:
        if anchored and turn_events and _is_external_user_record(record):
            break
        if not anchored:
            if not _is_anchor_user_record(record, prompt):
                continue
            anchored = True
        turn_events.append(record.event)
        completed_offset = record.end_offset

    if not anchored:
        return None
    if analyze_turn_status(turn_events).state != "ready":
        return None
    if read_offset < transcript.stat().st_size:
        return None
    return transcript, turn_events, completed_offset


def recoverable_turn_start_offset(active: Mapping[str, object]) -> int:
    anchor = _int_or_none(active.get("anchor_start_offset"))
    if anchor is not None:
        return anchor
    source = active.get("before_send_transcript")
    if isinstance(source, Mapping):
        value = _int_or_none(source.get("offset"))
        if value is not None:
            return value
    for key in ("replay_start_offset", "read_offset"):
        value = _int_or_none(active.get(key))
        if value is not None:
            return value
    return 0


def _parse_utc_timestamp(value: str) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def _clear_active_turn_after_failed_send(state_path: Path) -> None:
    def mutate(state: dict) -> dict | None:
        active = state.get("active_turn")
        if not isinstance(active, dict):
            return None
        active["claude_state"] = "unknown"
        active["stream_state"] = "failed"
        active["failed_at"] = _utc_timestamp(time.time())
        state["last_turn"] = active
        state["active_turn"] = None
        return state

    mutate_high_level_state(state_path, mutate)


def _mark_active_turn_state(
    state: dict,
    runtime: StreamRuntime,
    claude_state: str,
    stream_state: str,
    completed_offset: int | None = None,
) -> dict:
    active = state.get("active_turn")
    if not isinstance(active, dict):
        return state
    active["claude_state"] = claude_state
    active["stream_state"] = stream_state
    if completed_offset is not None:
        active["completed_offset"] = completed_offset
        state["last_turn"] = active
        state["active_turn"] = None
    else:
        active["heartbeat_at"] = _utc_timestamp(time.time())
        state["active_turn"] = active
    return state


def _turn_events(events: Sequence[dict]) -> list[list[dict]]:
    turns: list[list[dict]] = []
    latest: list[dict] = []
    skipping_internal = False
    for event in events:
        event_type = str(event.get("type") or event.get("event") or event.get("role") or "")
        if event_type == "user" and not _is_tool_result_content(_event_content(event)):
            if _is_internal_user_content(_event_content(event)):
                if latest:
                    turns.append(latest)
                    latest = []
                skipping_internal = True
                continue
            if _is_interruption_user_content(_event_content(event)) and latest:
                latest.append(event)
                continue
            if latest:
                turns.append(latest)
            latest = [event]
            skipping_internal = False
        elif latest and not skipping_internal and not _is_metadata_event(event_type):
            latest.append(event)
    if latest:
        turns.append(latest)
    return turns


def _turn_matches_prompt(turn_events: Sequence[dict], prompt: str) -> bool:
    if not turn_events:
        return False
    first = turn_events[0]
    event_type = str(first.get("type") or first.get("event") or first.get("role") or "")
    if event_type != "user":
        return False
    user_text = _format_user_content(_event_content(first))
    return prompt in user_text or _text_contains_with_normalized_whitespace(user_text, prompt)


def _join_numbered_blocks(blocks: Sequence[str], label: str) -> str:
    if len(blocks) == 1:
        return blocks[0]
    return "\n\n".join(f"--- {label} {index}/{len(blocks)} ---\n\n{block}" for index, block in enumerate(blocks, start=1))


def _content_items(content: object) -> list[object]:
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _format_user_content(content: object) -> str:
    if isinstance(content, str):
        return content
    text_blocks = _extract_text_blocks(content)
    return "\n".join(text_blocks)


def _is_internal_user_content(content: object) -> bool:
    text = _format_user_content(content)
    return text.startswith("당신은 Claude Code 세션 활동 요약 작성자입니다.") or text.startswith("Base directory for this skill:")


def _is_interruption_user_content(content: object) -> bool:
    text = _format_user_content(content).strip()
    return text.startswith("[Request interrupted by user") and text.endswith("]")


def _is_tool_result_content(content: object) -> bool:
    if isinstance(content, list):
        return any(isinstance(item, dict) and item.get("type") == "tool_result" for item in content)
    return False


def _format_tool_result_content(content: object) -> str:
    if not isinstance(content, list):
        return ""
    outputs = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_result":
            continue
        value = item.get("content")
        if isinstance(value, str):
            outputs.append(value)
        elif isinstance(value, list):
            for nested in value:
                if isinstance(nested, dict) and isinstance(nested.get("text"), str):
                    outputs.append(nested["text"])
                elif isinstance(nested, str):
                    outputs.append(nested)
    return "\n".join(outputs)


def _tool_result_is_error(content: object) -> bool | None:
    if not isinstance(content, list):
        return None
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_result" and "is_error" in item:
            return bool(item.get("is_error"))
    return None


def _tool_result_use_id(content: object) -> str | None:
    if not isinstance(content, list):
        return None
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_result" and isinstance(item.get("tool_use_id"), str):
            return item["tool_use_id"]
    return None


def _write_stream_done(turn_events: Sequence[dict], status: ScreenStatus, write: Callable[[str], object]) -> None:
    payload = _compact_payload(
        {
            "event": "done",
            "state": status.state,
            "reason": status.reason,
            "answer": extract_latest_answer_text(turn_events),
        }
    )
    write(json.dumps(payload, ensure_ascii=False) + "\n")


def _compact_payload(payload: dict) -> dict:
    return {key: value for key, value in payload.items() if value is not None}


def _extract_text_blocks(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []

    text_blocks = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            text_blocks.append(item["text"])
    return text_blocks


def _is_metadata_event(event_type: str) -> bool:
    return event_type in {"attachment", "ai-title", "permission-mode", "file-history-snapshot", "last-prompt", "system"}


def _event_content(event: dict) -> object:
    message = event.get("message")
    if isinstance(message, dict) and "content" in message:
        return message.get("content")
    return event.get("content")


def _assistant_stop_reason(event: dict) -> tuple[bool, str | None]:
    message = event.get("message")
    if isinstance(message, dict) and "stop_reason" in message:
        value = message.get("stop_reason")
        return True, value if isinstance(value, str) else None
    if "stop_reason" in event:
        value = event.get("stop_reason")
        return True, value if isinstance(value, str) else None
    return False, None


def _bottom_screen_area(screen: str, lines: int = 10) -> str:
    return "\n".join(screen.splitlines()[-lines:])


def wait_until_ready(
    controller: ScreenCaptureController,
    session: str,
    height: int = 80,
    interval: float = 0.5,
    timeout: float = 120.0,
    idle_seconds: float = 2.0,
    transcript_path: Path | None = None,
    transcript_resolver: Callable[[], tuple[Path | None, bool]] | None = None,
    sleep: Callable[[float], object] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> ScreenStatus:
    deadline = now() + timeout
    previous = None
    stable_since = now()
    last_status = ScreenStatus("unknown", "not inspected yet")

    while now() < deadline:
        screen = controller.capture_screen(session, height)
        pending_prompt = False
        current_transcript_path = transcript_path
        if transcript_resolver is not None:
            current_transcript_path, pending_prompt = transcript_resolver()
        last_status = (
            ScreenStatus("working", "waiting for transcript to record last prompt")
            if pending_prompt
            else analyze_combined_status(screen, transcript_path=current_transcript_path)
        )
        if screen != previous:
            previous = screen
            stable_since = now()
        elif last_status.state == "ready" and now() - stable_since >= idle_seconds:
            return last_status

        if last_status.state == "needs_confirmation":
            return last_status

        sleep(interval)

    return ScreenStatus("timeout", f"not ready after {timeout:.1f}s; last={last_status.state}")


def find_latest_transcript(root: Path = DEFAULT_TRANSCRIPT_ROOT) -> Path | None:
    paths = sorted(_iter_transcript_paths(root))
    if not paths:
        return None
    return max(paths, key=lambda path: (path.stat().st_mtime_ns, path.name))


def resolve_transcript_path(root: Path = DEFAULT_TRANSCRIPT_ROOT, state: SessionState | None = None) -> Path | None:
    if state is not None:
        candidates = _iter_state_transcript_paths(root, state)
        if not candidates:
            return find_latest_transcript(root)
        if not state.last_prompt:
            return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))
        matched = [path for path in candidates if _file_has_user_prompt(path, state.last_prompt)]
        if matched:
            return max(matched, key=lambda path: (path.stat().st_mtime_ns, path.name))
        return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))
    return find_latest_transcript(root)


def resolve_session_transcript_path(root: Path, state: SessionState) -> Path | None:
    candidates = _iter_state_transcript_paths(root, state, allow_global_fallback=False)
    if not candidates:
        return None
    if not state.last_prompt:
        return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))
    matched = [path for path in candidates if _file_has_user_prompt(path, state.last_prompt)]
    if matched:
        return max(matched, key=lambda path: (path.stat().st_mtime_ns, path.name))
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))


def resolve_status_transcript_path(
    root: Path,
    state: SessionState | None,
    explicit_transcript: Path | None = None,
) -> tuple[Path | None, bool]:
    if explicit_transcript is not None:
        return explicit_transcript, False
    if state is None:
        return find_latest_transcript(root), False
    if not state.last_prompt:
        return resolve_transcript_path(root, state), False

    candidates = _iter_state_transcript_paths(root, state)
    matched = [path for path in candidates if _file_has_user_prompt(path, state.last_prompt)]
    if matched:
        return max(matched, key=lambda path: (path.stat().st_mtime_ns, path.name)), False
    return None, True


def project_transcript_dir(root: Path, cwd: Path) -> Path:
    encoded = str(cwd.absolute()).replace("/", "-").replace("_", "-").replace(".", "-")
    return root / "projects" / encoded


def _iter_state_transcript_paths(
    root: Path,
    state: SessionState,
    allow_global_fallback: bool = True,
) -> list[Path]:
    if state.cwd:
        project_dir = project_transcript_dir(root, Path(state.cwd))
        if project_dir.is_dir():
            paths = list(project_dir.rglob("*.jsonl"))
            if state.session_id:
                return [path for path in paths if transcript_matches_session_id(path, state.session_id)]
            return paths
    if not allow_global_fallback:
        return []
    return _iter_transcript_paths(root)


def _file_contains(path: Path, text: str) -> bool:
    try:
        return text in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _file_has_user_prompt(path: Path, prompt: str) -> bool:
    if not prompt:
        return False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as file:
            for line in file:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = str(event.get("type") or event.get("event") or event.get("role") or "")
                if event_type != "user":
                    continue
                content = _event_content(event)
                if _is_tool_result_content(content) or _is_internal_user_content(content):
                    continue
                if prompt in _format_user_content(content):
                    return True
    except OSError:
        return False
    return False


def _iter_transcript_paths(root: Path) -> list[Path]:
    if root.is_file() and root.suffix == ".jsonl":
        return [root]
    if not root.exists():
        return []

    paths = list(root.glob("*.jsonl"))
    transcripts_dir = root / "transcripts"
    if transcripts_dir.is_dir():
        paths.extend(transcripts_dir.glob("*.jsonl"))
    projects_dir = root / "projects"
    if projects_dir.is_dir():
        paths.extend(projects_dir.rglob("*.jsonl"))
    if not paths:
        paths.extend(root.rglob("*.jsonl"))
    return paths


def session_state_path(session: str, state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", session)
    return state_dir / f"{safe_session}.json"


def _session_state_path(session: str) -> Path:
    return session_state_path(session)


def _remove_session_state(session: str) -> None:
    try:
        _session_state_path(session).unlink()
    except FileNotFoundError:
        pass


def write_session_state(path: Path, session: str, prompt: str, cwd: Path | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"session": session, "last_prompt": prompt, "cwd": str(cwd) if cwd is not None else None}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def read_session_state(path: Path) -> SessionState | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    session = payload.get("session")
    last_prompt = payload.get("last_prompt")
    cwd = payload.get("cwd")
    if not isinstance(session, str) or not isinstance(last_prompt, str):
        return None
    return SessionState(session=session, last_prompt=last_prompt, cwd=cwd if isinstance(cwd, str) else None)


def read_transcript_events(path: Path, offset: int = 0) -> tuple[list[dict], int]:
    events = []
    with path.open("r", encoding="utf-8", errors="replace") as file:
        file.seek(offset)
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events, file.tell()


def format_transcript_event(event: dict) -> str:
    timestamp = str(event.get("timestamp", "unknown-time"))
    event_type = str(event.get("type") or event.get("event") or event.get("role") or "unknown")
    parts = [timestamp, event_type]

    tool_name = event.get("tool_name")
    if tool_name:
        parts.append(str(tool_name))

    message = event.get("message")
    if isinstance(message, dict):
        role = message.get("role")
        if role and role != event_type:
            parts.append(f"role={role}")
        content = message.get("content")
        content_types = _content_types(content)
        if content_types:
            parts.append(f"content={','.join(content_types)}")

    tool_input = event.get("tool_input")
    if isinstance(tool_input, dict):
        parts.append(f"input_keys={','.join(sorted(tool_input.keys()))}")

    tool_output = event.get("tool_output")
    if isinstance(tool_output, dict):
        parts.append(f"output_keys={','.join(sorted(tool_output.keys()))}")

    usage = _extract_usage(event)
    if usage:
        parts.append("usage=" + ",".join(f"{key}={value}" for key, value in sorted(usage.items())))

    context = _extract_context(event)
    if context:
        parts.append("context=" + ",".join(f"{key}={value}" for key, value in sorted(context.items())))

    return " ".join(parts)


def _content_types(content: object) -> list[str]:
    if isinstance(content, str):
        return ["text"]
    if not isinstance(content, list):
        return []

    content_types = []
    for item in content:
        if isinstance(item, dict):
            content_types.append(str(item.get("type", "object")))
        else:
            content_types.append(type(item).__name__)
    return content_types


def _extract_usage(event: dict) -> dict:
    for candidate in (event.get("usage"), _nested_dict(event, "message", "usage"), _nested_dict(event, "response", "usage")):
        if isinstance(candidate, dict):
            return {str(key): value for key, value in candidate.items() if isinstance(value, int | float | str)}
    return {}


def _extract_context(event: dict) -> dict:
    for key in ("context", "context_window", "context_usage"):
        value = event.get(key)
        if isinstance(value, dict):
            return {str(k): v for k, v in value.items() if isinstance(v, int | float | str)}
    return {}


def _nested_dict(source: dict, *keys: str) -> dict | None:
    value: object = source
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, dict) else None


def _print_transcript_events(transcript: Path, tail: int, follow: bool, raw_json: bool) -> int:
    offset = 0
    if tail > 0:
        lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[-tail:]
        for line in selected:
            _print_transcript_line(line, raw_json)
        offset = transcript.stat().st_size
    else:
        events, offset = read_transcript_events(transcript)
        for event in events:
            print(json.dumps(event, ensure_ascii=False) if raw_json else format_transcript_event(event))

    if not follow:
        return 0

    while True:
        events, offset = read_transcript_events(transcript, offset=offset)
        for event in events:
            print(json.dumps(event, ensure_ascii=False) if raw_json else format_transcript_event(event))
            sys.stdout.flush()
        time.sleep(0.5)


def _print_transcript_line(line: str, raw_json: bool) -> None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return
    if not isinstance(event, dict):
        return
    print(json.dumps(event, ensure_ascii=False) if raw_json else format_transcript_event(event))


def _use_transcript_events_module() -> None:
    import transcript_events

    globals().update(
        {
            "ScreenStatus": transcript_events.ScreenStatus,
            "TranscriptRecord": transcript_events.TranscriptRecord,
            "analyze_transcript_status": transcript_events.analyze_transcript_status,
            "analyze_turn_status": transcript_events.analyze_turn_status,
            "extract_latest_answer_text": transcript_events.extract_latest_answer_text,
            "extract_answer_texts": transcript_events.extract_answer_texts,
            "format_latest_turn": transcript_events.format_latest_turn,
            "format_latest_turns": transcript_events.format_latest_turns,
            "target_turn_events": transcript_events.target_turn_events,
            "normalize_stream_events": transcript_events.normalize_stream_events,
            "read_transcript_records": transcript_events.read_transcript_records,
            "normalize_stream_record": transcript_events.normalize_stream_record,
            "latest_usage": transcript_events.latest_usage,
            "normalize_usage": transcript_events.normalize_usage,
            "latest_context": transcript_events.latest_context,
            "latest_model": transcript_events.latest_model,
        }
    )


_use_transcript_events_module()


if __name__ == "__main__":
    raise SystemExit(main())
