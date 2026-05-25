"""Claude Code transcript parsing and normalization helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


DEFAULT_TOOL_RESULT_TEXT_LIMIT = 100


@dataclass(frozen=True)
class ScreenStatus:
    state: str
    reason: str


@dataclass(frozen=True)
class TranscriptRecord:
    event: dict
    start_offset: int
    end_offset: int


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
    count = max(1, count)
    answers: list[str] = []
    for turn in _turn_events(events):
        current_text_blocks: list[str] = []
        for event in turn:
            if str(event.get("type") or event.get("event") or event.get("role") or "") != "assistant":
                continue
            text_blocks = _extract_text_blocks(_event_content(event))
            if text_blocks:
                current_text_blocks = text_blocks
        if current_text_blocks:
            answers.append("\n".join(current_text_blocks))
    return answers[-count:]


def format_latest_turn(events: Sequence[dict]) -> str | None:
    return format_latest_turns(events, count=1)


def format_latest_turns(events: Sequence[dict], count: int = 1) -> str | None:
    count = max(1, count)
    turns = _turn_events(events)
    formatted = [_format_turn_events(turn) for turn in turns[-count:]]
    formatted = [turn for turn in formatted if turn]
    if not formatted:
        return None
    if len(formatted) == 1:
        return formatted[0]
    return "\n\n".join(
        f"--- turn {index}/{len(formatted)} ---\n\n{turn}" for index, turn in enumerate(formatted, start=1)
    )


def target_turn_events(events: Sequence[dict], state: object | None = None) -> list[dict]:
    turns = _turn_events(events)
    if not turns:
        return []

    last_prompt = getattr(state, "last_prompt", "") if state is not None else ""
    if not last_prompt:
        return turns[-1]

    matched = [turn for turn in turns if _turn_matches_prompt(turn, last_prompt)]
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
                normalized.append(
                    _compact_payload(
                        {
                            "event": "tool_result",
                            "timestamp": timestamp,
                            "tool_use_id": _tool_result_use_id(content),
                            **_truncated_text_payload("text", _format_tool_result_content(content), tool_result_limit),
                            "is_error": _tool_result_is_error(content),
                            **_tool_use_result_preview(event.get("toolUseResult"), tool_result_limit),
                        }
                    )
                )
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


def latest_usage(events: Sequence[dict]) -> dict:
    for event in reversed(events):
        usage = _extract_usage(event)
        if usage:
            return usage
    return {}


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


def _format_turn_events(turn_events: Sequence[dict]) -> str | None:
    if not turn_events:
        return None

    sections: list[tuple[str, str]] = []
    for event in turn_events:
        event_type = str(event.get("type") or event.get("event") or event.get("role") or "")
        content = _event_content(event)
        if _is_metadata_event(event_type):
            continue
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
    return event_type == "user" and prompt in _format_user_content(_event_content(first))


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
    text = _format_user_content(content).strip()
    return text.startswith("당신은 Claude Code 세션 활동 요약 작성자입니다.")


def _is_interruption_user_content(content: object) -> bool:
    text = _format_user_content(content).strip()
    return text.startswith("[Request interrupted by user") and text.endswith("]")


def _is_tool_result_content(content: object) -> bool:
    return any(isinstance(item, dict) and item.get("type") == "tool_result" for item in _content_items(content))


def _format_tool_result_content(content: object) -> str:
    for item in _content_items(content):
        if isinstance(item, dict) and item.get("type") == "tool_result":
            value = item.get("content")
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                return "\n".join(
                    str(part.get("text")) for part in value if isinstance(part, dict) and isinstance(part.get("text"), str)
                )
    return ""


def _tool_result_is_error(content: object) -> bool | None:
    for item in _content_items(content):
        if isinstance(item, dict) and item.get("type") == "tool_result":
            value = item.get("is_error")
            return value if isinstance(value, bool) else None
    return None


def _tool_result_use_id(content: object) -> str | None:
    for item in _content_items(content):
        if isinstance(item, dict) and item.get("type") == "tool_result":
            value = item.get("tool_use_id")
            return value if isinstance(value, str) else None
    return None


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


def _assistant_text(event: dict) -> str:
    return "\n".join(_extract_text_blocks(_event_content(event)))


def _extract_text_blocks(content: object) -> list[str]:
    text_blocks = []
    for item in _content_items(content):
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            text_blocks.append(item["text"])
    return text_blocks


def _content_types(content: object) -> list[str]:
    types = []
    for item in _content_items(content):
        if isinstance(item, dict) and isinstance(item.get("type"), str):
            types.append(str(item["type"]))
    return types


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


def _extract_usage(event: dict) -> dict:
    for value in (event.get("usage"), _nested_value(event, "message", "usage"), _nested_value(event, "response", "usage")):
        if isinstance(value, dict):
            return value
    return {}


def _extract_context(event: dict) -> dict:
    for value in (
        event.get("context"),
        event.get("context_window"),
        event.get("context_usage"),
        _nested_value(event, "message", "context"),
        _nested_value(event, "response", "context"),
    ):
        if isinstance(value, dict):
            return {str(key): item for key, item in value.items() if isinstance(item, int | float | str)}
    return {}


def _nested_value(source: dict, *keys: str) -> object:
    value: object = source
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _numeric_value(source: Mapping[str, object], *keys: str) -> int | float | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            return value
    return None


def _compact_payload(payload: dict) -> dict:
    return {key: value for key, value in payload.items() if value is not None}
