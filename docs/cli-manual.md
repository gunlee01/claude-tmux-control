# CLI Manual

[English](./cli-manual.md) | [한국어](./cli-manual.ko.md)

This manual describes `ctc`, the command-line bridge for controlling Claude Code interactive sessions through tmux.

## 1. Quick Start

### Install

```bash
pipx install git+https://github.com/gunlee01/claude-tmux-control.git
ctc --help
ctc --version
```

### Runtime Requirements

```bash
python3 --version
tmux -V
claude --version
```

If your terminal type is unsupported by tmux, force a portable value.

```bash
TERM=xterm-256color ctc --help
```

### High-level Stream

```bash
SESSION_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"

TERM=xterm-256color ctc stream \
  --cwd "$PWD" \
  --session-id "$SESSION_ID" \
  "Explain this repository"
```

### Low-level Debug Session

```bash
ctc start work --cwd "$PWD"
ctc send work "Summarize the current directory"
ctc answer work --wait --timeout 120
```

`work` is an arbitrary tmux session name. It is not a high-level bridge session id.

## 2. Mental Model

Claude Code is a terminal UI, not a plain stdin/stdout protocol.

Input is sent through tmux:

```text
tmux load-buffer
  -> tmux paste-buffer
  -> short submit delay
  -> tmux send-keys Enter
  -> retry Enter once if no user turn is recorded
```

`ctc` waits briefly between paste and submit so terminal UIs have time to finish accepting the pasted text before `Enter` is sent. If a high-level stream still cannot find the transcript, or sees no target user turn in the transcript while the terminal is not working, it sends one extra `Enter` as a submit retry. For prompts that contain embedded newlines, `ctc` uses bracketed `tmux paste-buffer -p` so the newline bytes remain input text instead of separate Enter key presses. Structured output comes from Claude Code transcript JSONL. The terminal screen is used only for readiness and fallback checks.

## 3. Session Rules

High-level session:

- public id: UUID `session_id`
- tmux name: `ctc-csess-<session_id>`
- state path: `~/.cache/claude-tmux-control/sessions/<session_id>.json`
- transcript path: under `~/.claude/projects/<encoded-cwd>/`

If no state exists, `ctc stream` starts Claude Code with `--session-id <session_id>`. If state/transcript exists but tmux is gone, it starts Claude Code with `--resume <session_id>`.

In both cases the prompt is not passed as a Claude Code command argument. The bridge waits for the Claude Code TUI to become ready, then submits the prompt through tmux `load-buffer`, `paste-buffer`, a short submit delay, and `send-keys Enter`. Active tmux sessions use the same tmux input path. Multi-line prompts use bracketed `paste-buffer -p` so embedded newlines are submitted as one user turn. If the prompt never anchors in the transcript, or the transcript does not appear, and the terminal is not working or waiting for confirmation, high-level streams retry submit once with another `Enter`.

The same `session_id` cannot be reused with a different `cwd`.

## 4. Command Reference

### `stream`

High-level one-turn streaming:

```bash
ctc stream --cwd PATH [--session-id UUID] [--model MODEL] [--claude-args "ARGS"] [--timeout SECONDS] PROMPT
```

Attach to the active turn without sending a new prompt:

```bash
ctc stream --attach --session-id UUID
```

The final `metrics` event is scoped to the user-visible turn. If Claude Code records multiple internal API calls while handling one prompt, `metrics.usage` sums the available input, cache read, cache write, and output token fields across those call events. `metrics.usage.api_call_count` reports the deduplicated number of usage-bearing internal API calls. If a transcript `result.total_cost_usd` is present, `metrics.cost.turn_usd` uses that authoritative Claude CLI total; otherwise the CLI estimates cost from the aggregated usage and `claude_pricing.json`.

Important options:

| Option | Meaning |
| --- | --- |
| `--cwd PATH` | project directory; enables high-level mode |
| `--session-id UUID` | bridge session id |
| `--state-dir PATH` | bridge state directory |
| `--root PATH` | Claude config/transcript root |
| `--interval N` | transcript polling interval, default `2.0` |
| `--timeout N` | max wait time |
| `--tool-result-limit N` | truncate tool result previews |
| `--env-file PATH` | read extra environment for newly created tmux sessions |
| `--env NAME` | copy one named variable from the current `ctc` process env |
| `--model MODEL` | pass a Claude model to a newly launched Claude Code process |
| `--claude-args "ARGS"` | trusted extra Claude Code CLI arguments, parsed without shell execution |

`--model` and `--claude-args` apply only when the bridge launches a new Claude Code process. Existing tmux sessions keep their original process options.

Quote `--claude-args` as one shell argument:

```bash
ctc stream --cwd "$PWD" --model opus --claude-args "--add-dir ../shared" "hello"
```

### `ask`

Runs one high-level turn and prints final JSON instead of streaming progress.

```bash
ctc ask --cwd PATH [--session-id UUID] [--model MODEL] [--claude-args "ARGS"] PROMPT
```

### `cancel`

Sends Escape to the Claude Code pane for a high-level session.

```bash
ctc cancel UUID
```

Then attach or replay to receive final `done`/`metrics`.

### `last` / `replay`

Replays recent high-level turn events.

```bash
ctc last UUID --last 1
ctc replay UUID --last 5
```

If the last turn is active, these commands attach to it and stream through completion.

### `info`

```bash
ctc info UUID --json
```

Prints state metadata, tmux activity, transcript path, completed turn count, cumulative usage/cost fields, and `active_turn_recovery` guidance when an active turn is present.

Completed turn state is bounded to the latest 200 completed turns. The reported `usage_totals` and `cost_totals` are recomputed from that retained state window, not by rereading the full transcript JSONL.

### `list`

### `stats`

```bash
ctc stats UUID --json
ctc stats --transcript PATH --json
```

Prints machine-readable transcript statistics: model, normalized usage, context when present, event count, read offset, and estimated cost when usage/model pricing is available.

```bash
ctc list --json
```

Lists controlled high-level sessions from state files and active tmux sessions.

### `reap`

```bash
ctc reap --idle-seconds 1800 --prefix ctc-csess- --dry-run
ctc reap --idle-seconds 1800 --prefix ctc-csess-
```

Kills idle controlled tmux sessions. For high-level sessions with a stale active turn, `reap` can finalize the turn first when both transcript and tmux screen are ready. `--dry-run` is side-effect free: it reports sessions that would be killed after that recovery check without writing state or killing sessions.

### `kill`

```bash
ctc kill "ctc-csess-$SESSION_ID"
```

Terminates one tmux session by tmux session name.

### Low-level Commands

```bash
ctc start TMUX_SESSION --cwd PATH
ctc launch TMUX_SESSION --model opus
ctc send TMUX_SESSION PROMPT
ctc answer TMUX_SESSION --wait
ctc turn TMUX_SESSION --tail 3
ctc events TMUX_SESSION --json --tail 50
ctc capture TMUX_SESSION
ctc watch TMUX_SESSION
ctc follow TMUX_SESSION --append screen.log
```

Do not use low-level `start` to create normal high-level web sessions.

## 5. Authentication And Accounts

The bridge can pass OAuth tokens to newly created Claude Code tmux sessions.

```bash
CLAUDE_CODE_OAUTH_TOKEN="$TOKEN" ctc stream --cwd "$PWD" "hello"
```

Use `--oauth-token-env` when the source env var has another name.

```bash
ACCOUNT_A_TOKEN="..." \
ctc stream --cwd "$PWD" --oauth-token-env ACCOUNT_A_TOKEN "hello"
```

Do not print or log token values.

### Extra Claude Environment

Commands that create Claude Code tmux sessions (`stream`, `ask`, `start`, and `chat`) can inject additional environment variables.

If no `--env-file` is provided and `<cwd>/.ctc.env` exists, the bridge reads it for newly created tmux sessions.

```env
SERVICE_BASE_URL=https://api.example.test
```

Use `--env-file PATH` to select another file. Use `--env NAME` to copy a specific variable from the current `ctc` process environment without putting the value in shell history.

```bash
SERVICE_API_KEY="..." \
ctc stream --cwd "$PWD" --env SERVICE_API_KEY "hello"
```

Environment injection applies only when a new tmux session is created. Existing sessions keep their original environment.

`CLAUDE_CODE_OAUTH_TOKEN` is reserved for `--oauth-token-env`; `.ctc.env` and `--env` cannot set it.

## 6. Claude Launch Arguments

The bridge always launches the fixed `claude` executable. It does not accept an arbitrary shell command.

Use `--model MODEL` for the common model selection case. Use `--claude-args "ARGS"` for trusted extra Claude Code CLI arguments.

```bash
ctc start work --cwd "$PWD" --model opus
ctc stream --cwd "$PWD" --claude-args "--add-dir ../shared" "hello"
ctc stream --cwd "$PWD" --claude-args "--permission-mode plan" "hello"
```

`--claude-args` is parsed with shell-like quoting, but it is not executed by a shell. Keep it operator-controlled; do not expose a raw text box for untrusted clients.

## 7. Permission Mode

Claude Code launch commands default to `--dangerously-skip-permissions`. The bridge does not duplicate the flag when another permission override is already present.

This is a high-risk default. It avoids interactive approval prompts for service flows, but it also allows Claude Code to run tools without per-action confirmation.

Use this default only in controlled environments such as Docker, isolated servers, restricted project directories, or dedicated service users.

Clients can change permission behavior by passing a Claude Code permission option through trusted `--claude-args`. Expose this as a backend-controlled setting or a safe enum, not as an arbitrary browser text field.

Examples:

```bash
ctc start work --model opus
# launches: claude --model opus --dangerously-skip-permissions

ctc start work --claude-args "--permission-mode plan"
# launches: claude --permission-mode plan

ctc stream --cwd "$PWD" --claude-args "--permission-mode plan" "hello"
# launches without adding --dangerously-skip-permissions
```

## 8. Transcript Resolution

The transcript JSONL is the primary structured source. The bridge resolves transcript path from:

1. explicit `--transcript`,
2. stored bridge state,
3. high-level session id and cwd,
4. latest matching low-level session state.

After a turn is anchored, streaming reads from stored offsets instead of repeatedly scanning the full transcript.

## 9. Exit Codes

| Code | Meaning |
| --- | --- |
| `0` | success |
| `2` | request/session/transcript error |
| `3` | timeout or readiness failure |
| `4` | no replayable answer/turn |
| `5` | high-level runtime state error such as `turn_in_progress` |
| `127` | missing `tmux` or `claude` |
| `130` | client interrupt |

## 10. Troubleshooting

### `missing or unsuitable terminal`

```bash
TERM=xterm-256color ctc stream --cwd "$PWD" "hello"
```

### `turn_in_progress`

The previous turn is still active or not safely finalized. Use:

```bash
ctc stream --attach --session-id "$SESSION_ID"
ctc last "$SESSION_ID" --last 1
ctc cancel "$SESSION_ID"
```

Use `ctc info "$SESSION_ID" --json` to inspect `active_turn_recovery`:

| State | Recommended action |
| --- | --- |
| `active` | wait, attach, queue, or cancel |
| `timeout` / `interrupted` | attach or retry with the same session before sending a new prompt |
| `failed` | inspect or kill before sending a new prompt |

### Events look like another session

Use high-level `info` or pass an explicit transcript path for low-level inspection.

```bash
ctc info "$SESSION_ID" --json
ctc events --transcript /path/to/transcript.jsonl --json --tail 50
```

### Need screen text

```bash
ctc capture "ctc-csess-$SESSION_ID" --height 120
```

## 11. Current Gaps

- stream delivery is at-least-once until an explicit client acknowledgement protocol exists.
- mid-stream metrics are best-effort only.
- exact context size is available only when Claude Code transcript includes it.
- Claude Code transcript schema and TUI behavior may change across versions.
