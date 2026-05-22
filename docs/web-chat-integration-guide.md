# Web Chat Integration Guide

[English](./web-chat-integration-guide.md) | [한국어](./web-chat-integration-guide.ko.md)

This document defines how a web/backend service should use `ctc` to expose Claude Code interactive sessions as chat conversations.

## 1. Integration Contract

The primary command is:

```bash
ctc stream --cwd <project_dir> --session-id <uuid> "<prompt>"
```

The process emits JSONL on stdout. Each line is one event. The backend relays events to the browser through SSE, WebSocket, or another streaming transport.

Use one `session_id` per chat conversation. The bridge maps it to `ctc-csess-<session_id>` internally.

## 2. Command Boundary

High-level web/client commands:

```bash
ctc stream --cwd PATH [--session-id UUID] PROMPT
ctc stream --attach --session-id UUID
ctc ask --cwd PATH [--session-id UUID] PROMPT
ctc cancel UUID
ctc last UUID --last N
ctc replay UUID --last N
ctc info UUID --json
ctc list --json
ctc reap --idle-seconds N --prefix ctc-csess-
```

Low-level tmux commands are for manual debugging and should not be the normal web integration surface.

## 3. One Turn Flow

```text
browser
  -> POST /chat/:conversation/messages
backend
  -> spawn ctc stream --cwd <project> --session-id <uuid> <prompt>
  -> read stdout JSONL
  -> relay events to browser
  -> wait for done and metrics
  -> persist answer/metrics
browser
  -> enables next input
```

Do not send a second prompt until the previous turn reaches `done`/`metrics` or is explicitly cancelled and finalized.

### Runtime Environment

When Claude Code needs project-specific secrets, prefer `<project>/.ctc.env` or `--env NAME` over putting secret values in command arguments.

```bash
ZETTA_API_KEY="..." \
ctc stream \
  --cwd "$PROJECT_DIR" \
  --env ZETTA_API_KEY \
  "$USER_PROMPT"
```

If `<project>/.ctc.env` exists and no explicit `--env-file` is passed, `ctc` reads it when creating a new tmux session. Environment changes do not affect already running sessions; stop/reap the tmux session before expecting updated env values.

`CLAUDE_CODE_OAUTH_TOKEN` is reserved for `--oauth-token-env` and cannot be set through `.ctc.env` or `--env`.

## 4. Event Handling

| Event | UI behavior |
| --- | --- |
| `user` | record user turn anchor |
| `thinking` | show progress if desired |
| `tool_use` | show tool call name/input summary |
| `tool_result` | show truncated tool result preview |
| `assistant_text` | append answer text |
| `done` | mark answer complete; use `done.answer` as final text when present |
| `metrics` | display elapsed time, model, token usage, cost estimate |

Events include stable metadata such as `session_id`, `turn_id`, `event_id`, source offsets, and block index. Clients should deduplicate by `event_id` when replaying after reconnect.

## 5. Metrics

`metrics` is emitted after `done`.

Typical fields:

```json
{
  "event": "metrics",
  "elapsed_ms": 8042,
  "model": "claude-sonnet-4-6",
  "usage": {
    "input_tokens": 3,
    "cache_read_tokens": 12030,
    "cache_write_tokens": 5526,
    "output_tokens": 11
  },
  "cost": {
    "estimated": true,
    "currency": "USD",
    "turn_usd": 0.036939,
    "session_usd": 0.036939
  }
}
```

`context` appears only when Claude Code transcript events provide context fields. The CLI does not fabricate context size from billing tokens.

Cost is an estimate from `claude_pricing.json` and the model/usage data present in the transcript.

## 6. Session Ownership

The client owns the web-facing `session_id`.

Rules:

- use UUID values only.
- use the same UUID for one chat conversation.
- reject reuse with a different `cwd`.
- do not expose low-level tmux session names to normal clients.
- if tmux is missing but state/transcript remains, the bridge can resume with the same session id.

## 7. Reconnect And Replay

If a client disconnects mid-turn:

```bash
ctc stream --attach --session-id "$SESSION_ID" --timeout 300
```

If the turn is already complete:

```bash
ctc last "$SESSION_ID" --last 1
ctc replay "$SESSION_ID" --last 5
```

If the last turn is still active, `last`/`replay` attach instead of sending a new prompt.

## 8. Error Handling

| Error | Meaning | Client action |
| --- | --- | --- |
| `turn_in_progress` | previous turn is still active | attach, queue, or cancel |
| `session_cwd_mismatch` | same session id used with another cwd | reject and create a new conversation |
| no transcript | Claude Code did not produce transcript before timeout | show processing/retry state |
| timeout | completion not confirmed | attach/retry; do not assume ready |

Exit codes:

| Code | Meaning |
| --- | --- |
| `2` | request/session/transcript error |
| `3` | timeout or readiness failure |
| `5` | high-level runtime state error such as `turn_in_progress` |
| `127` | missing runtime dependency |
| `130` | client interrupt |

## 9. Cleanup

Run `reap` on a schedule.

```bash
ctc reap --idle-seconds 1800 --prefix ctc-csess- --dry-run
ctc reap --idle-seconds 1800 --prefix ctc-csess-
```

`reap` kills idle tmux sessions but does not delete the Claude conversation transcript. Later requests can resume when enough state remains.

## 10. Current Gaps

- stream delivery is at-least-once; clients should deduplicate by `event_id`.
- metrics are final-turn metrics, not guaranteed mid-stream telemetry.
- context size is available only when Claude Code transcript includes it.
- this integration depends on Claude Code transcript and TUI behavior.
