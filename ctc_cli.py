"""CLI parser helpers for claude-tmux-control."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from ctc_launch import (
    CLAUDE_OAUTH_TOKEN_ENV,
    add_claude_launch_args,
    add_environment_args,
    _normalize_claude_args_option_values,
)
from ctc_state import DEFAULT_STATE_DIR
from ctc_streaming import (
    DEFAULT_READY_IDLE_SECONDS,
    DEFAULT_STREAM_SUBMIT_ENTERS,
    DEFAULT_TOOL_RESULT_TEXT_LIMIT,
    DEFAULT_TRANSCRIPT_ROOT,
)


PACKAGE_NAME = "claude-tmux-control"
DEFAULT_CONTROLLED_PREFIX = "ctc-"


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
    wait_ready.add_argument("--idle", type=float, default=DEFAULT_READY_IDLE_SECONDS, help="screen must stay stable this many seconds")
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
    ask.add_argument("--idle", type=float, default=DEFAULT_READY_IDLE_SECONDS, help="ready state must remain stable this many seconds")
    ask.add_argument(
        "--submit-enters",
        type=int,
        choices=(1, 2),
        default=DEFAULT_STREAM_SUBMIT_ENTERS,
        help="number of Enter key submits after tmux paste when reusing an active session",
    )
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
    cancel.add_argument("--reset", action="store_true", help="compatibility alias; cancel already clears active_turn")

    def add_replay_args(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("session_id", nargs="?", help="web-facing Claude session id UUID")
        command_parser.add_argument("--session-id", dest="session_id_option", help="web-facing Claude session id UUID")
        command_parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="bridge state directory")
        command_parser.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
        command_parser.add_argument("--last", "--count", "--tail", dest="count", type=int, default=1, help="number of recent turns to replay")
        command_parser.add_argument("--interval", type=float, default=2.0, help="seconds between transcript checks for active turn attach")
        command_parser.add_argument("--timeout", type=float, default=300.0, help="maximum seconds to stream active last turn")
        command_parser.add_argument("--idle", type=float, default=DEFAULT_READY_IDLE_SECONDS, help="ready state must remain stable this many seconds")
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
    stream.add_argument("--idle", type=float, default=DEFAULT_READY_IDLE_SECONDS, help="ready state must remain stable this many seconds")
    stream.add_argument(
        "--submit-enters",
        type=int,
        choices=(1, 2),
        default=DEFAULT_STREAM_SUBMIT_ENTERS,
        help="number of Enter key submits after tmux paste when reusing an active session",
    )
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
    chat.add_argument("--idle", type=float, default=DEFAULT_READY_IDLE_SECONDS, help="return to the input prompt after this many stable seconds")

    return parser.parse_args(argv)
