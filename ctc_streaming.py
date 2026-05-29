"""Streaming status and JSONL payload helpers."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ctc_pricing import (
    add_session_cost_to_turn_cost,
    aggregate_turn_usage,
    count_turn_usage_calls,
    estimate_turn_cost,
    result_total_cost,
)
from ctc_transcripts import read_transcript_events
from transcript_events import (
    ScreenStatus,
    TranscriptRecord,
    analyze_transcript_status,
    analyze_turn_status,
    extract_latest_answer_text,
    latest_context,
    latest_model,
    normalize_stream_events,
    normalize_stream_record,
    target_turn_events,
)


DEFAULT_TRANSCRIPT_ROOT = Path.home() / ".claude"
DEFAULT_STREAM_SUBMIT_ENTERS = 2
DEFAULT_READY_IDLE_SECONDS = 3.5
DEFAULT_SCREEN_STATUS_LINES = 15
UNANCHORED_SUBMIT_RETRY_SECONDS = 1.0
DEFAULT_TOOL_RESULT_TEXT_LIMIT = 100
WORKING_PATTERNS = (
    re.compile(
        r"(?m)^\s*[\u2722\u2733-\u273f·]\s+\S.*(?:…|\.{3})\s*"
        r"\([^)\n]*\b\d+\s*(?:m|s)\b[^)\n]*\)",
        re.IGNORECASE,
    ),
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


def stream_transcript_until_done(
    transcript: Path,
    state: object | None,
    controller: object,
    session: str,
    interval: float = 0.5,
    timeout: float = 300.0,
    idle_seconds: float = DEFAULT_READY_IDLE_SECONDS,
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


def _write_jsonl(write: Callable[[str], object], payload: dict) -> None:
    write(json.dumps(_compact_payload(payload), ensure_ascii=False) + "\n")
    if hasattr(sys.stdout, "flush"):
        sys.stdout.flush()


def _capture_screen_status(controller: object, tmux_session: str) -> ScreenStatus:
    try:
        return analyze_screen_status(controller.capture_screen(tmux_session, height=80))
    except subprocess.CalledProcessError:
        return ScreenStatus("unknown", "screen unavailable")


def _maybe_retry_unanchored_submit(
    controller: object,
    runtime: object,
    screen_status: ScreenStatus,
    already_retried: bool,
    current_time: float,
) -> bool:
    if not getattr(runtime, "submit_retry_enabled", False):
        return already_retried
    if already_retried:
        return True
    if screen_status.state in {"working", "needs_confirmation"}:
        return False
    if current_time - getattr(runtime, "started_at_monotonic") < UNANCHORED_SUBMIT_RETRY_SECONDS:
        return False
    send_enter = getattr(controller, "send_enter", None)
    if not callable(send_enter):
        return False
    send_enter(getattr(runtime, "tmux_session"))
    return True


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
    runtime: object,
    turn_events: Sequence[dict],
    status: ScreenStatus,
    completed_offset: int,
) -> dict:
    return _compact_payload(
        {
            "event": "done",
            "session_id": getattr(runtime, "session_id"),
            "turn_id": getattr(runtime, "turn_id"),
            "event_id": f"{getattr(runtime, 'turn_id')}:done:{completed_offset}",
            "source_offset": completed_offset,
            "source_end_offset": completed_offset,
            "block_index": -1,
            "state": status.state,
            "reason": status.reason,
            "answer": extract_latest_answer_text(turn_events),
        }
    )


def high_level_metrics_payload(
    runtime: object,
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
            "session_id": getattr(runtime, "session_id"),
            "turn_id": getattr(runtime, "turn_id"),
            "event_id": f"{getattr(runtime, 'turn_id')}:metrics:{completed_offset}",
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


def _elapsed_ms(runtime: object, current_monotonic: float) -> int:
    return max(0, int(round((current_monotonic - getattr(runtime, "started_at_monotonic")) * 1000)))


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
    return event_type in {"attachment", "ai-title", "permission-mode", "mode", "file-history-snapshot", "last-prompt", "system"}


def _event_content(event: dict) -> object:
    message = event.get("message")
    if isinstance(message, dict) and "content" in message:
        return message.get("content")
    return event.get("content")


def _bottom_screen_area(screen: str, lines: int = DEFAULT_SCREEN_STATUS_LINES) -> str:
    return "\n".join(screen.splitlines()[-lines:])
