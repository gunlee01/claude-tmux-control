"""Transcript path resolution and JSONL file helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping


DEFAULT_TRANSCRIPT_ROOT = Path.home() / ".claude"


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


def transcript_identity(path: Path) -> str:
    stat = path.stat()
    return f"dev{stat.st_dev}-ino{stat.st_ino}"


def find_latest_transcript(root: Path = DEFAULT_TRANSCRIPT_ROOT) -> Path | None:
    paths = sorted(_iter_transcript_paths(root))
    if not paths:
        return None
    return max(paths, key=lambda path: (path.stat().st_mtime_ns, path.name))


def resolve_transcript_path(root: Path = DEFAULT_TRANSCRIPT_ROOT, state: object | None = None) -> Path | None:
    if state is not None:
        candidates = _iter_state_transcript_paths(root, state)
        if not candidates:
            return find_latest_transcript(root)
        if not getattr(state, "last_prompt", ""):
            return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))
        matched = [path for path in candidates if _file_has_user_prompt(path, getattr(state, "last_prompt", ""))]
        if matched:
            return max(matched, key=lambda path: (path.stat().st_mtime_ns, path.name))
        return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))
    return find_latest_transcript(root)


def resolve_session_transcript_path(root: Path, state: object) -> Path | None:
    candidates = _iter_state_transcript_paths(root, state, allow_global_fallback=False)
    if not candidates:
        return None
    if not getattr(state, "last_prompt", ""):
        return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))
    matched = [path for path in candidates if _file_has_user_prompt(path, getattr(state, "last_prompt", ""))]
    if matched:
        return max(matched, key=lambda path: (path.stat().st_mtime_ns, path.name))
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))


def resolve_status_transcript_path(
    root: Path,
    state: object | None,
    explicit_transcript: Path | None = None,
) -> tuple[Path | None, bool]:
    if explicit_transcript is not None:
        return explicit_transcript, False
    if state is None:
        return find_latest_transcript(root), False
    if not getattr(state, "last_prompt", ""):
        return resolve_transcript_path(root, state), False

    candidates = _iter_state_transcript_paths(root, state)
    matched = [path for path in candidates if _file_has_user_prompt(path, getattr(state, "last_prompt", ""))]
    if matched:
        return max(matched, key=lambda path: (path.stat().st_mtime_ns, path.name)), False
    return None, True


def project_transcript_dir(root: Path, cwd: Path) -> Path:
    encoded = str(cwd.absolute()).replace("/", "-").replace("_", "-").replace(".", "-")
    return root / "projects" / encoded


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


def _iter_state_transcript_paths(
    root: Path,
    state: object,
    allow_global_fallback: bool = True,
) -> list[Path]:
    cwd = getattr(state, "cwd", None)
    if cwd:
        project_dir = project_transcript_dir(root, Path(cwd))
        if project_dir.is_dir():
            paths = list(project_dir.rglob("*.jsonl"))
            session_id = getattr(state, "session_id", None)
            if session_id:
                return [path for path in paths if transcript_matches_session_id(path, session_id)]
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


def _event_content(event: dict) -> object:
    message = event.get("message")
    if isinstance(message, dict) and "content" in message:
        return message.get("content")
    return event.get("content")


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


def _is_internal_user_content(content: object) -> bool:
    text = _format_user_content(content)
    return text.startswith("당신은 Claude Code 세션 활동 요약 작성자입니다.") or text.startswith("Base directory for this skill:")


def _is_tool_result_content(content: object) -> bool:
    if isinstance(content, list):
        return any(isinstance(item, dict) and item.get("type") == "tool_result" for item in content)
    return False
