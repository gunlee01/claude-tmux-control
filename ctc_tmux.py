"""tmux controller and rendered-screen helpers for claude-tmux-control."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Mapping, Protocol


RunFn = Callable[..., subprocess.CompletedProcess[str]]
DEFAULT_BUFFER_NAME = "claude-tmux-control"
DEFAULT_PASTE_SUBMIT_DELAY_SECONDS = 0.25
DEFAULT_SECOND_SUBMIT_DELAY_SECONDS = 1.0


class ScreenCaptureController(Protocol):
    def capture_screen(self, session: str, height: int = 200) -> str:
        ...

    def send_escape(self, session: str) -> None:
        ...

    def kill_session(self, session: str) -> None:
        ...


class SessionNotFoundError(RuntimeError):
    pass


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

    def send_prompt(self, session: str, prompt: str, submit: bool = True, submit_enters: int = 1) -> None:
        if submit_enters not in {1, 2}:
            raise ValueError("submit_enters_must_be_1_or_2")
        self._run(["tmux", "load-buffer", "-b", DEFAULT_BUFFER_NAME, "-"], input=prompt, text=True, check=True)
        paste_args = ["tmux", "paste-buffer", "-d", "-b", DEFAULT_BUFFER_NAME, "-t", session]
        if "\n" in prompt or "\r" in prompt:
            paste_args.insert(2, "-p")
        self._run(paste_args, check=True)
        if submit:
            time.sleep(DEFAULT_PASTE_SUBMIT_DELAY_SECONDS)
            self.send_enter(session)
            if submit_enters == 2:
                time.sleep(DEFAULT_SECOND_SUBMIT_DELAY_SECONDS)
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
