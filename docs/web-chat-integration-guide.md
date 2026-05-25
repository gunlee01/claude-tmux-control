# Web Chat Integration Guide

[English](./web-chat-integration-guide.md) | [한국어](./web-chat-integration-guide.ko.md)

This document defines how a web/backend service should use `ctc` to expose Claude Code interactive sessions as chat conversations.

## 1. Integration Contract

The primary command is:

```bash
ctc stream --cwd <project_dir> --session-id <uuid> [--model MODEL] [--claude-args "ARGS"] "<prompt>"
```

The process emits JSONL on stdout. Each line is one event. The backend relays events to the browser through SSE, WebSocket, or another streaming transport.

Use one `session_id` per chat conversation. The bridge maps it to `ctc-csess-<session_id>` internally.

## 2. Command Boundary

High-level web/client commands:

```bash
ctc stream --cwd PATH [--session-id UUID] [--model MODEL] [--claude-args "ARGS"] PROMPT
ctc stream --attach --session-id UUID
ctc ask --cwd PATH [--session-id UUID] [--model MODEL] [--claude-args "ARGS"] PROMPT
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
  -> ctc starts/resumes Claude Code without prompt argv when tmux is missing
  -> ctc submits the prompt through tmux paste+Enter after the TUI is ready
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
SERVICE_API_KEY="..." \
ctc stream \
  --cwd "$PROJECT_DIR" \
  --env SERVICE_API_KEY \
  "$USER_PROMPT"
```

If `<project>/.ctc.env` exists and no explicit `--env-file` is passed, `ctc` reads it when creating a new tmux session. Environment changes do not affect already running sessions; stop/reap the tmux session before expecting updated env values.

`CLAUDE_CODE_OAUTH_TOKEN` is reserved for `--oauth-token-env` and cannot be set through `.ctc.env` or `--env`.

### Claude Launch Arguments

The bridge always launches the fixed `claude` executable. Use `--model MODEL` for model selection and `--claude-args "ARGS"` for trusted extra Claude Code CLI arguments.

```bash
TERM=xterm-256color \
ctc stream \
  --session-id "$SESSION_ID" \
  --cwd "$PROJECT_DIR" \
  --model opus \
  --claude-args "--add-dir ../shared" \
  "$USER_PROMPT"
```

These options apply only when the bridge creates or resumes a Claude Code process. If the tmux session already exists, the running process keeps its original model and arguments.

When creating or resuming a tmux session, the bridge launches Claude Code with only the launch/session arguments, such as `--session-id <uuid>` or `--resume <uuid>`. It does not pass the user prompt as a Claude Code argv value. After the TUI is ready, it submits the prompt through tmux `load-buffer`, `paste-buffer`, and `send-keys Enter`. Prompts containing embedded newlines use bracketed `paste-buffer -p` to prevent those newlines from being interpreted as separate Enter key presses.

Keep `--claude-args` operator-controlled. Do not expose arbitrary raw arguments to untrusted browser clients.

### Permission Mode

New Claude Code processes launch with `--dangerously-skip-permissions` by default. This is intentionally non-interactive, but it is also high-risk because Claude Code can run tools without per-action approval.

A client/backend can change this only at Claude process launch time by passing a Claude Code permission option through trusted `--claude-args`:

```bash
TERM=xterm-256color \
ctc stream \
  --session-id "$SESSION_ID" \
  --cwd "$PROJECT_DIR" \
  --claude-args "--permission-mode plan" \
  "$USER_PROMPT"
```

If your product exposes this to users, expose a safe application-level setting such as `permissionMode=plan` and map it server-side. Do not pass arbitrary browser-provided strings into `--claude-args`.

When starting or resuming a high-level stream session, `ctc` pre-seeds the requested `--cwd` as a Claude Code trusted project before launching Claude Code. It updates `~/.claude.json` and the effective `CLAUDE_CONFIG_DIR/settings.json` without replacing existing unrelated fields.

## 4. Event Handling

| Event | UI behavior |
| --- | --- |
| `user` | record user turn anchor |
| `thinking` | show progress if desired |
| `tool_use` | show tool call name/input summary |
| `tool_result` | show truncated tool result preview |
| `assistant_text` | append answer text |
| `done` | mark answer complete; use `done.answer` as final text when present |
| `metrics` | display elapsed time, model, user-turn token usage, and cost |

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
    "output_tokens": 11,
    "api_call_count": 2
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

`usage` is scoped to the user-visible turn. If Claude Code records multiple internal API calls for one prompt, the CLI sums the available input, cache read, cache write, and output token fields across those call events. `usage.api_call_count` is the deduplicated count of usage-bearing internal API calls included in that final usage summary.

Cost uses `result.total_cost_usd` from the Claude Code transcript when present. If that field is unavailable, cost is an estimate from `claude_pricing.json` and the aggregated turn usage.

`session_usd` is based on completed turn records retained in bridge state. The CLI keeps the latest 200 completed turn records, so long-running sessions should treat `metrics.cost.session_usd` and `info.cost_totals` as the retained-window cumulative total.

## 6. Session Ownership

The client owns the web-facing `session_id`.

Rules:

- use UUID values only.
- use the same UUID for one chat conversation.
- reject reuse with a different `cwd`.
- do not expose low-level tmux session names to normal clients.
- if tmux is missing but state/transcript remains, the bridge can resume with the same session id and then submit the new prompt through tmux input.

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

`ctc info "$SESSION_ID" --json` includes `active_turn_recovery` when an active turn is present. Treat `active`, `timeout`, and `interrupted` as not ready for a new prompt; attach/retry/cancel first. Treat `failed` as requiring inspect or kill before sending another prompt.

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

`reap` kills idle tmux sessions but does not delete the Claude conversation transcript. Later requests can resume when enough state remains. On resume, `ctc` starts Claude Code with `--resume <session_id>` without a prompt argv, waits for the TUI prompt, and then submits the new user prompt through tmux paste+Enter. Multi-line prompts use bracketed paste.

For high-level sessions, `reap` can repair a stale `active_turn` before killing an idle tmux session when the transcript and tmux screen both show the turn is complete. `--dry-run` only reports this outcome and does not write state.

## 10. Current Gaps

- stream delivery is at-least-once; clients should deduplicate by `event_id`.
- metrics are final-turn metrics, not guaranteed mid-stream telemetry.
- context size is available only when Claude Code transcript includes it.
- this integration depends on Claude Code transcript and TUI behavior.
