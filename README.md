# claude-tmux-control

[English](./README.md) | [한국어](./README.ko.md)

[![Latest release](https://img.shields.io/github/v/release/gunlee01/claude-tmux-control?sort=semver)](https://github.com/gunlee01/claude-tmux-control/releases/latest)

`claude-tmux-control` is a tmux-based Claude Code control CLI and interactive streaming alternative to `claude -p` for persistent Claude Code sessions, JSONL streaming, web chat integration, and backend automation.

Instead of starting a new one-shot `claude -p` process for every request, it keeps an interactive Claude Code session alive so a web chat app, backend service, or automation can continue the same conversation.

## Command Model

Use the high-level API for web clients and external programs.

| Purpose | Command | Id meaning |
| --- | --- | --- |
| Run and stream one turn | `ctc stream --cwd PATH [--session-id UUID] PROMPT` | bridge session UUID |
| Reattach to an active turn | `ctc stream --attach --session-id UUID` | bridge session UUID |
| Cancel an active turn | `ctc cancel UUID` | bridge session UUID |
| Replay completed turns | `ctc last UUID --last N` or `ctc replay UUID --last N` | bridge session UUID |
| Inspect one session | `ctc info UUID --json` | bridge session UUID |
| List sessions | `ctc list --json` | high-level bridge sessions |
| Reap idle web sessions | `ctc reap --idle-seconds N --prefix ctc-csess-` | high-level web tmux prefix |

Low-level tmux commands are for manual debugging and smoke tests, not for the web-client contract.

| Purpose | Command | Id meaning |
| --- | --- | --- |
| Start a named tmux session | `ctc start TMUX_SESSION` | arbitrary tmux session name, such as `work` |
| Send a prompt manually | `ctc send TMUX_SESSION PROMPT` | tmux session name |
| Inspect low-level output | `ctc answer/turn/events TMUX_SESSION` | tmux session name |

`ctc start work` is valid, but `work` is a tmux session name, not a bridge `session_id`.

Web chat apps normally call only `ctc stream --cwd ... [--session-id ...] ...`.

## Requirements

- Python 3.10+
- `tmux`
- Claude Code CLI (`claude`)
- Claude Code auth, or `CLAUDE_CODE_OAUTH_TOKEN`

If a server terminal type causes tmux errors, run with `TERM=xterm-256color`.

```bash
TERM=xterm-256color ctc --help
```

## Installation

`pipx` is recommended.

```bash
pipx install git+https://github.com/gunlee01/claude-tmux-control.git
ctc --help
ctc --version
```

The package installs two console commands.

```bash
ctc --help
claude-tmux-control --help
```

You can also install into a virtual environment.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install git+https://github.com/gunlee01/claude-tmux-control.git
ctc --help
ctc --version
```

## Docker

A Docker image can include Python, `tmux`, Claude Code CLI, and `ctc`.

```bash
docker build -t claude-tmux-control -f docker/Dockerfile .
```

```bash
docker run --rm -it \
  -e CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  -v "$PWD":/repo \
  -w /repo \
  claude-tmux-control \
  ctc stream --cwd /repo "Summarize this project"
```

The Docker entrypoint pre-seeds Claude Code onboarding/trust settings and runs a `claude -p` preflight to cache managed settings before the interactive tmux session starts.

See [Docker Guide](./docs/docker.md) for details.

## License

This project is licensed under the [Apache License 2.0](./LICENSE).

Claude Code itself is distributed separately by Anthropic and is subject to its own license and terms.

## Stream As A `claude -p` Alternative

Run one prompt and receive JSONL stream events.

```bash
SESSION_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"

TERM=xterm-256color ctc stream \
  --cwd "$PWD" \
  --session-id "$SESSION_ID" \
  "Explain the project structure"
```

If `--session-id` is omitted, the CLI creates a UUID and includes it in every stream event. Clients should store that `session_id` and send it again for the next turn.

Common event order:

```text
user
thinking
tool_use
tool_result
assistant_text
done
metrics
```

The final answer for a normal completed turn is in `done.answer`.

Cancelled or interrupted tool turns may finish without final assistant text. Treat the turn as complete when `done` and `metrics` arrive.

`metrics` is emitted after `done` and includes elapsed time, model, token usage, and estimated cost when available. `metrics.context` is included only when Claude Code transcript data provides context fields.

Send the next message in the same chat by reusing the same `SESSION_ID`.

```bash
TERM=xterm-256color ctc stream \
  --cwd "$PWD" \
  --session-id "$SESSION_ID" \
  "Now summarize that more briefly"
```

Internally, the bridge creates or reuses a tmux session named `ctc-csess-<SESSION_ID>`.

## Web Client Flow

```text
app server
  -> ctc stream --cwd <project> --session-id <uuid> "<prompt>"
  -> read stdout JSONL line by line
  -> relay events to browser by SSE/WebSocket
  -> enable input after done
  -> store/display metrics
```

If a previous turn is still active, new prompts are rejected with exit code `5` and JSON on stderr.

```json
{"event":"error","error":"turn_in_progress"}
```

Clients should either queue the new message or attach to the active turn.

```bash
TERM=xterm-256color ctc stream --attach --session-id "$SESSION_ID" --timeout 300
```

To cancel the active turn, send Escape, stop the tmux session, and clear the bridge
`active_turn` so the next prompt can resume with the same `session_id`.

```bash
TERM=xterm-256color ctc cancel "$SESSION_ID"
```

`--reset` is kept as a compatibility alias for the same behavior.

When a high-level `stream` reaches its `--timeout`, `ctc` treats that timeout as a cancellation boundary: it emits a `timeout` event, sends Escape, stops the tmux session, records the turn as timed out in `last_turn`, and clears `active_turn` if cleanup succeeds so the next prompt can be sent through the resume path.

```bash
TERM=xterm-256color ctc cancel "$SESSION_ID" --reset
```

## Session Cleanup

`ctc stream` creates tmux sessions that do not disappear automatically.

Run `reap` periodically in long-running servers.

```bash
TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc-csess- --dry-run
TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc-csess-
```

`reap` is a one-shot scan, not a daemon. Use cron, systemd timer, or an app scheduler.

If `reap` kills the tmux session, the Claude Code process inside it is killed too. The Claude conversation can still be resumed later with the same `SESSION_ID` when state/transcript data exists.

## Authentication

Pass OAuth tokens at runtime through environment variables. Do not commit tokens.

```bash
CLAUDE_CODE_OAUTH_TOKEN="$TOKEN" \
TERM=xterm-256color ctc stream --cwd "$PWD" "hello"
```

`--oauth-token-env` selects the source env var when needed.

Additional Claude-side environment can come from a project env file or an explicit whitelist. By default, high-level commands read `<cwd>/.ctc.env` when it exists; use `--env-file PATH` to choose another file and `--env NAME` to copy a named variable from the current `ctc` process environment.

```bash
SERVICE_API_KEY="$TOKEN" \
TERM=xterm-256color ctc stream \
  --cwd "$PWD" \
  --env SERVICE_API_KEY \
  "hello"
```

Environment injection applies only when a new tmux session is created. Existing sessions keep the environment they started with. `CLAUDE_CODE_OAUTH_TOKEN` is reserved for `--oauth-token-env` and is rejected in `.ctc.env` or `--env`.

## Claude Launch Options

The bridge always launches the fixed `claude` executable. Use `--model MODEL` for model selection and `--claude-args "ARGS"` for trusted extra Claude Code CLI arguments.

```bash
TERM=xterm-256color ctc stream \
  --cwd "$PWD" \
  --model opus \
  --claude-args "--add-dir ../shared" \
  "hello"
```

These options apply only when a new Claude Code process is launched. Existing tmux sessions keep their original model and arguments.

Claude Code launches with `--dangerously-skip-permissions` by default. This is convenient for non-interactive service flows, but it lets Claude Code run tools without per-action approval. Run it only in a controlled project directory, container, or dedicated service user.

To change the default permission behavior, pass a Claude Code permission option through trusted `--claude-args`:

```bash
TERM=xterm-256color ctc stream \
  --cwd "$PWD" \
  --claude-args "--permission-mode plan" \
  "hello"
```

The bridge does not add `--dangerously-skip-permissions` when `--claude-args` already contains `--permission-mode ...` or `--dangerously-skip-permissions`.

See [Security Guide](./docs/security.md) for token, transcript, Docker, and `--dangerously-skip-permissions` guidance.

## Examples

Runnable examples are in [examples](./examples/README.md).

- `shell-stream.sh`: one-turn shell wrapper around `ctc stream`
- `web-client-minimal.py`: minimal backend-style stdout JSONL consumer
- `docker-compose.yml`: persistent Claude config and bridge state volumes

## Useful Commands

```bash
ctc list --json
ctc info "$SESSION_ID" --json
ctc replay "$SESSION_ID" --last 1
ctc reap --idle-seconds 1800 --prefix ctc-csess- --dry-run
```

## Low-level Debug Commands

These commands are for manually debugging separate tmux sessions. Do not use `ctc-csess-$SESSION_ID` with `ctc start` for normal web sessions.

```bash
ctc start work --cwd "$PWD"
ctc send work "Summarize the current directory"
ctc answer work --wait --timeout 120
ctc turn work --tail 3
ctc events work --json --tail 50
```

## Limitations

This project depends on current Claude Code implementation details:

- transcript JSONL path and schema
- `tool_use`, `tool_result`, `usage`, and `context` event fields
- terminal prompt/readiness rendering
- Claude Code options and env vars such as `--session-id`, `--resume`, and `CLAUDE_CODE_OAUTH_TOKEN`

This is not an official stable SDK protocol for Claude Code. Run smoke tests after Claude Code updates.

## Documentation

- [Docs Index](./docs/README.md)
- [CLI Manual](./docs/cli-manual.md)
- [Web Client Quickstart](./docs/quickstart-web-client.md)
- [Web Chat Integration Guide](./docs/web-chat-integration-guide.md)
- [Operations Guide](./docs/operations.md)
- [Docker Guide](./docs/docker.md)
- [Security Guide](./docs/security.md)
- [Release Guide](./docs/release.md)
