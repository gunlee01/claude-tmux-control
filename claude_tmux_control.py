#!/usr/bin/env python3
"""Control an interactive Claude Code session through tmux."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence


RunFn = Callable[..., subprocess.CompletedProcess[str]]
CLAUDE_OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
CLAUDE_DANGEROUS_SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
CLAUDE_LAUNCH_COMMANDS = {"start", "launch", "chat"}
DEFAULT_BUFFER_NAME = "claude-tmux-control"
DEFAULT_TRANSCRIPT_ROOT = Path.home() / ".claude"
DEFAULT_STATE_DIR = Path.home() / ".cache" / "claude-tmux-control"
DEFAULT_CONTROLLED_PREFIX = "ctc-"
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


class ScreenCaptureController(Protocol):
    def capture_screen(self, session: str, height: int = 200) -> str:
        ...


class SessionNotFoundError(RuntimeError):
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
        self._run(["tmux", "paste-buffer", "-d", "-b", DEFAULT_BUFFER_NAME, "-t", session], check=True)
        if submit:
            self._run(["tmux", "send-keys", "-t", session, "Enter"], check=True)

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
    parser = argparse.ArgumentParser(
        prog="claude-tmux-control",
        description="Start, feed, and read an interactive Claude Code session through tmux.",
    )
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    start = subparsers.add_parser("start", help="Create or reuse a tmux session running Claude Code.")
    start.add_argument("session", help="tmux session name")
    start.add_argument("--command", dest="claude_command", default="claude", help="command to run in tmux")
    start.add_argument("--cwd", default=str(Path.cwd()), help="working directory for a new session")
    start.add_argument("--attach", action="store_true", help="attach to the tmux session after starting it")
    start.add_argument(
        "--oauth-token-env",
        default=CLAUDE_OAUTH_TOKEN_ENV,
        help=f"source environment variable to pass as {CLAUDE_OAUTH_TOKEN_ENV}",
    )

    launch = subparsers.add_parser("launch", help="Run Claude Code inside an existing tmux session.")
    launch.add_argument("session", help="tmux session name")
    launch.add_argument("--command", dest="claude_command", default="claude", help="command to paste and run")

    send = subparsers.add_parser("send", help="Paste text into the Claude Code tmux session.")
    send.add_argument("session", help="tmux session name")
    send.add_argument("prompt", nargs="*", help="prompt text; reads stdin when omitted")
    send.add_argument("--no-enter", action="store_true", help="paste only, without pressing Enter")

    capture = subparsers.add_parser("capture", help="Print the rendered tmux pane text.")
    capture.add_argument("session", help="tmux session name")
    capture.add_argument("--height", type=int, default=200, help="number of pane history lines to capture")

    watch = subparsers.add_parser("watch", help="Continuously print changed tmux pane text.")
    watch.add_argument("session", help="tmux session name")
    watch.add_argument("--height", type=int, default=200, help="number of pane history lines to capture")
    watch.add_argument("--interval", type=float, default=1.0, help="seconds between captures")

    follow = subparsers.add_parser("follow", help="Append rendered screen changes to stdout and optionally a file.")
    follow.add_argument("session", help="tmux session name")
    follow.add_argument("--height", type=int, default=200, help="number of pane history lines to capture")
    follow.add_argument("--interval", type=float, default=0.5, help="seconds between captures")
    follow.add_argument("--append", type=Path, help="file to append rendered screen changes")

    status = subparsers.add_parser("status", help="Infer whether Claude Code is working, ready, or waiting.")
    status.add_argument("session", help="tmux session name")
    status.add_argument("--height", type=int, default=80, help="number of pane history lines to inspect")
    status.add_argument("--transcript", type=Path, help="specific transcript JSONL path")
    status.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
    status.add_argument("--screen-only", action="store_true", help="do not use transcript state")

    wait_ready = subparsers.add_parser("wait-ready", help="Wait until the rendered screen looks ready for input.")
    wait_ready.add_argument("session", help="tmux session name")
    wait_ready.add_argument("--height", type=int, default=80, help="number of pane history lines to inspect")
    wait_ready.add_argument("--interval", type=float, default=0.5, help="seconds between captures")
    wait_ready.add_argument("--timeout", type=float, default=120.0, help="maximum seconds to wait")
    wait_ready.add_argument("--idle", type=float, default=2.0, help="screen must stay stable this many seconds")
    wait_ready.add_argument("--transcript", type=Path, help="specific transcript JSONL path")
    wait_ready.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
    wait_ready.add_argument("--screen-only", action="store_true", help="do not use transcript state")

    events = subparsers.add_parser("events", help="Read Claude Code transcript JSONL events.")
    events.add_argument("session", nargs="?", help="tmux session name used to resolve the matching transcript")
    events.add_argument("--transcript", type=Path, help="specific transcript JSONL path")
    events.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
    events.add_argument("--tail", type=int, default=20, help="number of latest events to print")
    events.add_argument("--follow", action="store_true", help="keep reading new transcript events")
    events.add_argument("--json", action="store_true", help="print raw JSON events")

    answer = subparsers.add_parser("answer", help="Print the latest assistant text answer for a tmux session.")
    answer.add_argument("session", help="tmux session name used to resolve the matching transcript")
    answer.add_argument("--transcript", type=Path, help="specific transcript JSONL path")
    answer.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
    answer.add_argument("--wait", action="store_true", help="wait until the session is ready before printing")
    answer.add_argument("--timeout", type=float, default=120.0, help="maximum seconds to wait with --wait")
    answer.add_argument("--count", "--tail", dest="count", type=int, default=1, help="number of recent answers to print")

    turn = subparsers.add_parser("turn", help="Print the latest turn with thinking, tool calls, tool results, and text.")
    turn.add_argument("session", help="tmux session name used to resolve the matching transcript")
    turn.add_argument("--transcript", type=Path, help="specific transcript JSONL path")
    turn.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
    turn.add_argument("--follow", action="store_true", help="keep refreshing the latest turn")
    turn.add_argument("--interval", type=float, default=1.0, help="seconds between refreshes with --follow")
    turn.add_argument("--count", "--tail", dest="count", type=int, default=1, help="number of recent turns to print")

    stream = subparsers.add_parser("stream", help="Stream the current turn as JSONL until the answer is complete.")
    stream.add_argument("session", help="tmux session name used to resolve the matching transcript")
    stream.add_argument("--transcript", type=Path, help="specific transcript JSONL path")
    stream.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
    stream.add_argument("--interval", type=float, default=0.5, help="seconds between transcript checks")
    stream.add_argument("--timeout", type=float, default=300.0, help="maximum seconds to stream")
    stream.add_argument("--idle", type=float, default=2.0, help="ready state must remain stable this many seconds")

    kill = subparsers.add_parser("kill", help="Terminate one tmux session.")
    kill.add_argument("session", help="tmux session name")

    reap = subparsers.add_parser("reap", help="Terminate controlled sessions idle for too long.")
    reap.add_argument("--idle-seconds", type=float, required=True, help="minimum input idle seconds before killing")
    reap.add_argument("--prefix", default=DEFAULT_CONTROLLED_PREFIX, help="only reap sessions with this prefix")
    reap.add_argument("--dry-run", action="store_true", help="print sessions that would be killed without killing")
    reap.add_argument("--root", type=Path, default=DEFAULT_TRANSCRIPT_ROOT, help="Claude config/transcript directory")
    reap.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR, help="claude-tmux-control state directory")

    chat = subparsers.add_parser("chat", help="Create or reuse a session, then send prompts interactively.")
    chat.add_argument("session", help="tmux session name")
    chat.add_argument("--command", dest="claude_command", default="claude", help="command to run in tmux")
    chat.add_argument("--cwd", default=str(Path.cwd()), help="working directory for a new session")
    chat.add_argument(
        "--oauth-token-env",
        default=CLAUDE_OAUTH_TOKEN_ENV,
        help=f"source environment variable to pass as {CLAUDE_OAUTH_TOKEN_ENV}",
    )
    chat.add_argument("--height", type=int, default=200, help="number of pane history lines to capture")
    chat.add_argument("--interval", type=float, default=0.5, help="seconds between captures after sending input")
    chat.add_argument("--idle", type=float, default=2.0, help="return to the input prompt after this many stable seconds")

    args, claude_args = parser.parse_known_args(argv)
    passthrough = _normalize_passthrough_args(claude_args)
    if passthrough and args.command_name not in CLAUDE_LAUNCH_COMMANDS:
        parser.error(f"unrecognized arguments: {' '.join(claude_args)}")
    args.claude_args = passthrough if args.command_name in CLAUDE_LAUNCH_COMMANDS else []
    return args


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
        created = controller.start_session(
            args.session,
            build_claude_command(args.claude_command, args.claude_args),
            args.cwd,
            env=claude_environment_from_args(args),
        )
        write_session_state(_session_state_path(args.session), args.session, "", Path(args.cwd))
        print(f"{'created' if created else 'reused'} session: {args.session}")
        if args.attach:
            controller.attach(args.session)
        return 0

    if args.command_name == "launch":
        controller.launch_in_existing_session(args.session, build_claude_command(args.claude_command, args.claude_args))
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

    if args.command_name == "stream":
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
        controller.start_session(
            args.session,
            build_claude_command(args.claude_command, args.claude_args),
            args.cwd,
            env=claude_environment_from_args(args),
        )
        write_session_state(_session_state_path(args.session), args.session, "", Path(args.cwd))
        return _chat(controller, args.session, args.height, args.interval, args.idle)

    raise ValueError(f"unsupported command: {args.command_name}")


def build_claude_command(command: str, claude_args: Sequence[str] = ()) -> str:
    passthrough = " ".join(shlex.quote(arg) for arg in claude_args)
    full_command = f"{command} {passthrough}" if passthrough else command
    if _has_permission_override(command, claude_args):
        return full_command
    return f"{full_command} {CLAUDE_DANGEROUS_SKIP_PERMISSIONS_FLAG}"


def claude_environment_from_args(
    args: argparse.Namespace,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source_env = getattr(args, "oauth_token_env", None)
    if not source_env:
        return {}
    env = os.environ if environ is None else environ
    token = env.get(source_env)
    if not token:
        return {}
    return {CLAUDE_OAUTH_TOKEN_ENV: token}


def _has_permission_override(command: str, claude_args: Sequence[str] = ()) -> bool:
    return (
        CLAUDE_DANGEROUS_SKIP_PERMISSIONS_FLAG in command
        or "--permission-mode" in command
        or CLAUDE_DANGEROUS_SKIP_PERMISSIONS_FLAG in claude_args
        or "--permission-mode" in claude_args
    )


def _normalize_passthrough_args(args: Sequence[str]) -> list[str]:
    values = list(args)
    if values and values[0] == "--":
        return values[1:]
    return values


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

    if args.command_name not in CLAUDE_LAUNCH_COMMANDS:
        return None

    executable = _extract_shell_command_executable(args.claude_command)
    if executable and not which(executable):
        return "\n".join(
            [
                f"Claude Code executable not found in PATH: {executable}",
                "Install Claude Code CLI first, then retry.",
                "Example: curl -fsSL https://claude.ai/install.sh | bash",
                "After install, confirm with: claude --version",
            ]
        )
    return None


def _extract_shell_command_executable(command: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None

    while tokens and _is_shell_assignment(tokens[0]):
        tokens.pop(0)

    if tokens and tokens[0] == "env":
        tokens.pop(0)
        while tokens:
            token = tokens[0]
            if token == "--":
                tokens.pop(0)
                break
            if _is_shell_assignment(token):
                tokens.pop(0)
                continue
            if token in {"-i", "--ignore-environment"}:
                tokens.pop(0)
                continue
            if token in {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}:
                tokens.pop(0)
                if tokens:
                    tokens.pop(0)
                continue
            if token.startswith("-"):
                tokens.pop(0)
                continue
            break

    return tokens[0] if tokens else None


def _is_shell_assignment(token: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token))


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

    transcript = args.transcript or resolve_transcript_path(args.root, state)
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

    transcript = args.transcript or resolve_transcript_path(args.root, state)
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
    )
    return 0 if status.state == "ready" else 3


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

        state_path = session_state_path(session, state_dir)
        state = read_session_state(state_path)
        if state is None:
            continue

        idle = current_time - state_path.stat().st_mtime
        if idle < idle_seconds:
            continue
        if session_is_working(controller, session, state, root):
            continue

        action = "would-kill" if dry_run else "killed"
        if not dry_run:
            controller.kill_session(session)
        results.append({"session": session, "idle_seconds": idle, "action": action})
    return results


def session_is_working(controller: TmuxController, session: str, state: SessionState, root: Path) -> bool:
    transcript_path, pending_prompt = resolve_status_transcript_path(root, state)
    if pending_prompt:
        return True
    try:
        screen = controller.capture_screen(session, height=80)
    except subprocess.CalledProcessError:
        screen = ""
    status = analyze_combined_status(screen, transcript_path=transcript_path)
    return status.state == "working"


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
        if _is_tool_result_content(_event_content(latest_event)):
            return ScreenStatus("working", "latest transcript event after user is tool_result")
        return ScreenStatus("working", "latest transcript event after user is user")

    if latest_type in {"tool_use", "tool_result"}:
        return ScreenStatus("working", f"latest transcript event after user is {latest_type}")

    if latest_type == "assistant":
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


def normalize_stream_events(events: Sequence[dict]) -> list[dict]:
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
                        "text": _format_tool_result_content(content),
                        "is_error": _tool_result_is_error(content),
                        "result": event.get("toolUseResult"),
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
                        "text": event.get("tool_output") or event.get("content") or "",
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
            if item_type == "thinking" and isinstance(item.get("thinking"), str):
                normalized.append(
                    _compact_payload({"event": "thinking", "timestamp": timestamp, "text": item["thinking"]})
                )
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
        for payload in normalize_stream_events(new_events):
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
    return event_type == "user" and prompt in _format_user_content(_event_content(first))


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
    return text.startswith("당신은 Claude Code 세션 활동 요약 작성자입니다.")


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
    encoded = str(cwd.absolute()).replace("/", "-")
    return root / "projects" / encoded


def _iter_state_transcript_paths(root: Path, state: SessionState) -> list[Path]:
    if state.cwd:
        project_dir = project_transcript_dir(root, Path(state.cwd))
        if project_dir.is_dir():
            return list(project_dir.rglob("*.jsonl"))
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


if __name__ == "__main__":
    raise SystemExit(main())
