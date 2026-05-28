"""High-level bridge session metadata, replay, and transcript helpers."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ctc_pricing import cost_totals_from_completed_turns
from ctc_state import (
    DEFAULT_WEB_SESSION_PREFIX,
    StreamRuntime,
    read_bridge_state,
    validate_or_create_session_id,
    web_session_state_path,
    web_tmux_session_name,
)
from ctc_streaming import DEFAULT_TOOL_RESULT_TEXT_LIMIT, _capture_screen_status, _compact_payload, _maybe_retry_unanchored_submit
from ctc_transcripts import (
    extract_transcript_session_id,
    resolve_high_level_transcript,
    transcript_identity,
    transcript_matches_or_omits_session_id,
    transcript_matches_session_id,
)
from transcript_events import normalize_stream_record, read_transcript_records


def build_session_info_payload(
    session_id: str,
    state_dir: Path,
    root: Path,
    controller: object,
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
    controller: object,
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


def wait_for_high_level_transcript(
    root: Path,
    runtime: StreamRuntime,
    timeout: float,
    interval: float,
    controller: object | None = None,
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
    if stat.st_dev != baseline.get("st_dev") or stat.st_ino != baseline.get("st_ino"):
        return True
    baseline_size = _int_or_none(baseline.get("size"))
    if baseline_size is not None and stat.st_size < baseline_size:
        return True
    baseline_offset = _int_or_none(baseline.get("offset"))
    if baseline_offset is not None and stat.st_size < baseline_offset:
        return True
    return False
