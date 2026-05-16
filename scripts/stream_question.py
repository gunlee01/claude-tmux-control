#!/usr/bin/env python3
"""Send one prompt to claude-tmux-control and print streamed events until done."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a prompt to a Claude Code tmux session and print stream events until completion.",
    )
    parser.add_argument("session", help="tmux session name")
    parser.add_argument("prompt", nargs="*", help="prompt text; reads stdin when omitted")
    parser.add_argument("--ctc", default=str(Path(__file__).resolve().parents[1] / "claude_tmux_control.py"))
    parser.add_argument("--timeout", type=float, default=300.0, help="maximum seconds to stream")
    parser.add_argument("--idle", type=float, default=2.0, help="ready state must remain stable this many seconds")
    parser.add_argument("--interval", type=float, default=0.5, help="seconds between stream checks")
    parser.add_argument("--raw-json", action="store_true", help="print raw JSONL stream instead of formatted text")
    return parser.parse_args(argv)


def build_commands(
    ctc: str,
    session: str,
    prompt: str,
    timeout: float,
    idle: float,
    interval: float,
) -> tuple[list[str], list[str]]:
    send_command = [ctc, "send", session, prompt]
    stream_command = [
        ctc,
        "stream",
        session,
        "--timeout",
        str(timeout),
        "--idle",
        str(idle),
        "--interval",
        str(interval),
    ]
    return send_command, stream_command


def format_event(event: dict) -> str | None:
    event_type = event.get("event")
    if event_type == "user":
        return f"[user] {event.get('text', '')}"
    if event_type == "thinking":
        return f"[thinking] {event.get('text', '')}"
    if event_type == "tool_use":
        name = event.get("name") or "tool"
        metadata = []
        if event.get("id"):
            metadata.append(f"id={event['id']}")
        if event.get("caller"):
            metadata.append(f"caller={event['caller']}")
        suffix = " " + " ".join(metadata) if metadata else ""
        tool_input = event.get("input")
        if tool_input:
            return f"[tool_use] {name}{suffix} {json.dumps(tool_input, ensure_ascii=False)}"
        return f"[tool_use] {name}{suffix}"
    if event_type == "tool_result":
        metadata = []
        if event.get("tool_use_id"):
            metadata.append(f"tool_use_id={event['tool_use_id']}")
        if event.get("is_error") is not None:
            metadata.append(f"error={str(bool(event['is_error'])).lower()}")
        suffix = " " + " ".join(metadata) if metadata else ""
        text = event.get("text") or event.get("result") or ""
        return f"[tool_result]{suffix}\n{text}"
    if event_type == "assistant_text":
        return str(event.get("text", ""))
    if event_type == "done":
        answer = event.get("answer")
        return f"\n[done]\n{answer}" if answer else "\n[done]"
    if event_type == "timeout":
        return f"[timeout] {event.get('reason', '')}"
    return json.dumps(event, ensure_ascii=False)


def run_stream(send_command: list[str], stream_command: list[str], raw_json: bool = False) -> int:
    subprocess.run(send_command, check=True)

    with subprocess.Popen(
        stream_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    ) as process:
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            if raw_json:
                print(line, flush=True)
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print(line, flush=True)
                continue
            formatted = format_event(event)
            if formatted:
                print(formatted, flush=True)

        stderr = process.stderr.read() if process.stderr is not None else ""
        returncode = process.wait()
        if stderr:
            print(stderr, end="", file=sys.stderr)
        return returncode


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    prompt = " ".join(args.prompt) if args.prompt else sys.stdin.read()
    if not prompt.strip():
        print("prompt is required", file=sys.stderr)
        return 2

    send_command, stream_command = build_commands(
        ctc=args.ctc,
        session=args.session,
        prompt=prompt,
        timeout=args.timeout,
        idle=args.idle,
        interval=args.interval,
    )
    return run_stream(send_command, stream_command, raw_json=args.raw_json)


if __name__ == "__main__":
    raise SystemExit(main())
