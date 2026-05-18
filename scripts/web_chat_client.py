#!/usr/bin/env python3
"""Client-style runner for one high-level claude-tmux-control stream turn."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Sequence


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one web-chat style ctc stream turn and log JSONL events.")
    parser.add_argument("--ctc", default="./claude_tmux_control.py", help="path to claude_tmux_control.py")
    parser.add_argument("--cwd", required=True, help="project cwd passed to ctc stream")
    parser.add_argument("--prompt", required=True, help="user prompt for one turn")
    parser.add_argument("--session-id", help="existing or new high-level session UUID")
    parser.add_argument("--oauth-token-env", help="source env var for Claude OAuth token")
    parser.add_argument("--state-dir", help="ctc state directory")
    parser.add_argument("--root", help="Claude transcript root")
    parser.add_argument("--log", type=Path, required=True, help="JSONL log path")
    parser.add_argument("--timeout", type=float, default=180.0, help="ctc stream timeout seconds")
    parser.add_argument("--interval", type=float, default=2.0, help="ctc stream polling interval seconds")
    parser.add_argument("--expect-answer", help="exact expected final answer after stripping whitespace")
    return parser.parse_args(argv)


def build_stream_command(args: argparse.Namespace) -> list[str]:
    command = [
        args.ctc,
        "stream",
        "--cwd",
        args.cwd,
        "--timeout",
        str(args.timeout),
        "--interval",
        str(args.interval),
    ]
    if args.session_id:
        command.extend(["--session-id", args.session_id])
    if args.oauth_token_env:
        command.extend(["--oauth-token-env", args.oauth_token_env])
    if args.state_dir:
        command.extend(["--state-dir", args.state_dir])
    if args.root:
        command.extend(["--root", args.root])
    command.append(args.prompt)
    return command


def parse_jsonl_line(line: str) -> dict | None:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return {"event": "client_parse_error", "raw": stripped}
    return payload if isinstance(payload, dict) else {"event": "client_parse_error", "raw": stripped}


def summarize_events(events: Sequence[dict], returncode: int | None, expected_answer: str | None = None) -> dict:
    done = next((event for event in reversed(events) if event.get("event") == "done"), None)
    metrics = next((event for event in reversed(events) if event.get("event") == "metrics"), None)
    validation_errors = validate_event_order(events)
    answer = done.get("answer") if isinstance(done, dict) else None
    if expected_answer is not None and (answer or "").strip() != expected_answer:
        validation_errors.append("answer_mismatch")
    if returncode not in (0, None):
        validation_errors.append(f"process_failed:{returncode}")
    return {
        "record": "summary",
        "timestamp": utc_timestamp(),
        "returncode": returncode,
        "ok": not validation_errors,
        "session_id": _latest_field(events, "session_id"),
        "turn_id": _latest_field(events, "turn_id"),
        "answer": answer,
        "metrics": metrics,
        "events_seen": len(events),
        "validation_errors": validation_errors,
    }


def validate_event_order(events: Sequence[dict]) -> list[str]:
    event_names = [str(event.get("event") or "") for event in events]
    errors: list[str] = []
    if "done" not in event_names:
        errors.append("missing_done")
    if "metrics" not in event_names:
        errors.append("missing_metrics")
    if "done" in event_names and "metrics" in event_names and event_names.index("metrics") < event_names.index("done"):
        errors.append("metrics_before_done")
    return errors


def write_log(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_client(args: argparse.Namespace) -> int:
    command = build_stream_command(args)
    request = {
        "record": "request",
        "timestamp": utc_timestamp(),
        "command": command,
        "cwd": args.cwd,
        "session_id": args.session_id,
        "prompt": args.prompt,
    }
    write_log(args.log, request)

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    events: list[dict] = []
    assert process.stdout is not None
    for line in process.stdout:
        payload = parse_jsonl_line(line)
        if payload is None:
            continue
        events.append(payload)
        write_log(args.log, {"record": "event", "timestamp": utc_timestamp(), "payload": payload})
        print(json.dumps(payload, ensure_ascii=False), flush=True)
    stderr = process.stderr.read() if process.stderr is not None else ""
    returncode = process.wait()
    if stderr:
        write_log(args.log, {"record": "stderr", "timestamp": utc_timestamp(), "text": stderr})
    summary = summarize_events(events, returncode, args.expect_answer)
    write_log(args.log, summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if returncode != 0:
        return 3
    return 0 if summary["ok"] else 4


def _latest_field(events: Iterable[dict], field: str) -> object:
    for event in reversed(list(events)):
        value = event.get(field)
        if value is not None:
            return value
    return None


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main(argv: Sequence[str] | None = None) -> int:
    return run_client(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
