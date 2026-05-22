# Web Client Quickstart

[English](./quickstart-web-client.md) | [한국어](./quickstart-web-client.ko.md)

This is the shortest path for a backend or web app to expose Claude Code as a chat-like UI through `ctc stream`.

For command details, see [CLI Manual](./cli-manual.md).

## 1. Principles

- Use high-level `ctc stream --cwd PATH [--session-id UUID] PROMPT`.
- Treat stdout as JSONL and read line by line.
- Store `session_id` per chat conversation.
- Disable the input box until `done` and `metrics` arrive.
- Use `stream --attach` or `last` to reconnect to active turns.
- Use `reap` periodically to clean idle tmux sessions.

## 2. Start A New Conversation

The server may generate a UUID before the first call.

```bash
SESSION_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"

TERM=xterm-256color ctc stream \
  --cwd "$PROJECT_DIR" \
  --session-id "$SESSION_ID" \
  "Explain this repository"
```

If `--session-id` is omitted, read it from the first JSONL event and store it.

## 3. Stream Events

Expected event types:

| Event | Meaning |
| --- | --- |
| `user` | target user turn anchor |
| `thinking` | Claude Code thinking/progress block |
| `tool_use` | tool invocation metadata |
| `tool_result` | tool result preview |
| `assistant_text` | answer text block |
| `done` | answer turn completed |
| `metrics` | final elapsed/model/token/cost metrics |

Normal UI behavior:

```text
user submits prompt
  -> server starts ctc stream
  -> browser receives JSONL events through SSE/WebSocket
  -> append assistant_text/tool events to the message
  -> on done, mark answer complete
  -> on metrics, show usage/cost and enable input
```

## 4. Send The Next Turn

Use the same `SESSION_ID`.

```bash
TERM=xterm-256color ctc stream \
  --cwd "$PROJECT_DIR" \
  --session-id "$SESSION_ID" \
  "$NEXT_PROMPT"
```

If the tmux session still exists, it is reused. If it was reaped but state/transcript remains, `ctc` starts a new tmux session and resumes Claude Code.

## 5. Disconnect And Reconnect

If the browser disconnects while the turn is active, do not send the prompt again. Attach to the active turn.

```bash
TERM=xterm-256color ctc stream --attach --session-id "$SESSION_ID" --timeout 300
```

To replay completed turns:

```bash
TERM=xterm-256color ctc last "$SESSION_ID" --last 1
TERM=xterm-256color ctc replay "$SESSION_ID" --last 5
```

If the last turn is still active, `last`/`replay` attach to it and stream through completion.

## 6. Busy Session Handling

A new prompt during an active turn fails with exit code `5` and stderr JSON.

```json
{"event":"error","error":"turn_in_progress"}
```

Client options:

- keep the new message queued,
- disable input and attach to the active turn,
- let the user cancel the active turn.

## 7. Cancel

```bash
TERM=xterm-256color ctc cancel "$SESSION_ID"
TERM=xterm-256color ctc last "$SESSION_ID" --last 1
```

`cancel` only sends Escape. Use `last` or `stream --attach` to receive the final `done`/`metrics` state.

## 8. Cleanup

```bash
TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc-csess- --dry-run
TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc-csess-
```

See [Operations Guide](./operations.md) for production policy.

## 9. Minimal Server Checklist

- [ ] create or store one UUID per conversation.
- [ ] call `ctc stream` for each user turn.
- [ ] read stdout JSONL line by line.
- [ ] handle `turn_in_progress` explicitly.
- [ ] persist `done.answer` and `metrics`.
- [ ] support reconnect through `stream --attach` or `last`.
- [ ] run `reap` periodically.
- [ ] redact tokens in logs.
