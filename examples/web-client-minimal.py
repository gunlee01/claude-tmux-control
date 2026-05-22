#!/usr/bin/env python3
"""Minimal backend-style client for ctc stream."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one ctc stream turn and print routed events.")
    parser.add_argument("prompt", nargs="+", help="Prompt text to send to Claude Code.")
    parser.add_argument("--cwd", required=True, help="Project directory for Claude Code.")
    parser.add_argument("--session-id", default=None, help="Bridge session UUID. Generated if omitted.")
    parser.add_argument("--ctc", default="ctc", help="ctc executable path.")
    parser.add_argument("--timeout", type=int, default=300, help="Turn timeout in seconds.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    session_id = args.session_id or str(uuid.uuid4())
    prompt = " ".join(args.prompt)

    cmd = [
        args.ctc,
        "stream",
        "--cwd",
        args.cwd,
        "--session-id",
        session_id,
        "--timeout",
        str(args.timeout),
        prompt,
    ]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    assert process.stdout is not None
    for line in process.stdout:
        event = json.loads(line)
        event_type = event.get("event")
        if event_type == "assistant_text":
            print(event.get("text", ""), end="", flush=True)
        elif event_type == "tool_use":
            print(f"\n[tool_use] {event.get('name', '')}", flush=True)
        elif event_type == "tool_result":
            print("\n[tool_result]", flush=True)
        elif event_type == "done":
            print("\n[done]", flush=True)
        elif event_type == "metrics":
            print(f"[metrics] {json.dumps(event, ensure_ascii=False)}", flush=True)

    stderr = process.stderr.read() if process.stderr is not None else ""
    return_code = process.wait()
    if return_code != 0:
        print(stderr, file=sys.stderr, end="")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
