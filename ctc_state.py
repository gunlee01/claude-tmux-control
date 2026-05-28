"""State paths, locks, and bridge state persistence helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


DEFAULT_STATE_DIR = Path.home() / ".cache" / "claude-tmux-control"
DEFAULT_WEB_SESSION_PREFIX = "ctc-csess-"
STATE_SCHEMA_VERSION = 1


class StateGenerationConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionState:
    session: str
    last_prompt: str
    cwd: str | None = None
    session_id: str | None = None


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
    submit_retry_enabled: bool = True


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


def session_state_path(session: str, state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", session)
    return state_dir / f"{safe_session}.json"


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
        "transcript": _transcript_file_state(transcript, runtime.before_send_offset) if transcript else None,
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
            "before_send_transcript": _transcript_file_state(transcript, runtime.before_send_offset) if transcript else None,
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


def _transcript_file_state(path: Path | None, offset: int | None = None) -> dict | None:
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


def _prompt_hash(prompt: str) -> str:
    return "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _utc_timestamp(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))
