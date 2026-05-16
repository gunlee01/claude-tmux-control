# AGENTS.md

## Project

Working name: `cc-tmux-bridge`

This project is a Python CLI bridge for controlling Claude Code interactive sessions from external programs.

The bridge runs Claude Code inside `tmux`, sends user input through the terminal pane, and reads structured response state from Claude Code transcript JSONL files.

Primary use case:

```text
web/client process
  -> cc-tmux-bridge CLI
    -> tmux session
      -> Claude Code interactive CLI
    <- Claude transcript JSONL
  <- status / stream / answer / events
```

## Language And Style

- User-facing answers should be in Korean.
- Keep explanations concise and conclusion-first.
- Code, commands, paths, and option names stay in English.
- Prefer short, practical examples over long theory.
- Preserve existing Korean documentation style in `README.md` and `docs/*.md`.

## Tech Stack

- Python 3.10+
- Standard library only unless there is a clear reason to add a dependency.
- `tmux` as the terminal execution layer.
- Claude Code CLI (`claude`) as the controlled interactive process.
- `unittest` for tests.
- No package manager is currently required.

## Important Files

- `claude_tmux_control.py`
  - Main CLI implementation.
  - Keep this file self-contained unless a real module split becomes necessary.
- `scripts/stream_question.py`
  - Smoke-test wrapper that sends a prompt and prints stream events until completion.
- `tests/test_claude_tmux_control.py`
  - Main behavior tests for tmux command construction, transcript parsing, streaming, status, kill/reap.
- `tests/test_stream_question_script.py`
  - Tests for the stream wrapper.
- `docs/cli-manual.md`
  - Detailed command manual.
- `docs/web-chat-integration-guide.md`
  - Integration guide for web/backend programs that expose Claude Code sessions as chat conversations.
- `docs/web-chat-integration-guide.html`
  - Interactive browser version of the web chat integration guide.
- `docs/PRD.md`
  - Product direction and design decisions.
- `docs/implementation-checklist.md`
  - Active checklist and future work.

## Core Design Rules

### Input

Send input to Claude Code through `tmux`, not raw stdin:

```text
tmux load-buffer
tmux paste-buffer
tmux send-keys Enter
```

This is intentional because Claude Code is a terminal UI, not a plain stdin/stdout protocol.

### Output And State

Use Claude Code transcript JSONL as the primary structured source for:

- assistant text
- thinking
- tool_use
- tool_result
- timestamps
- usage/context fields when present

Use `tmux capture-pane` only for rendered-screen state, prompt readiness, debugging, and fallback checks.

Do not parse final answers from rendered terminal text when transcript data is available.

### Completion

Do not treat the visible prompt glyph alone as completion.

A turn is complete only when:

- the target user turn has final assistant text,
- the latest meaningful transcript event is not `thinking`, `tool_use`, or `tool_result`,
- the rendered tmux screen is ready,
- and the ready state remains stable for the configured idle window.

Subagent flows such as `Task` tool calls are not complete when the tool result arrives. Wait for the final assistant text after the tool result.

### Streaming

`stream <session>` emits JSONL to stdout.

Normalized event names:

- `user`
- `thinking`
- `tool_use`
- `tool_result`
- `assistant_text`
- `done`
- `metrics`
- `timeout`

The stream should terminate with `done` only when the answer is genuinely complete.

For service-facing chat flows, emit final turn metrics after `done` as a separate `metrics` event. Final metrics should include model, elapsed time, input tokens, cache read tokens, cache write tokens, output tokens, current context size when available, estimated turn USD, and estimated session cumulative USD. Mid-stream metrics are optional best-effort only when the transcript already contains usage/context/model before the turn is done.

External programs should consume stream output line-by-line as JSON.

### Session Lifecycle

The bridge owns the canonical web-facing `session_id`.

Target session identity model:

- Generate a UUID v4 when the client does not provide `session_id`.
- Use tmux session name `ctc-csess-<session_id>`.
- Start new Claude Code sessions with `--session-id <session_id>`.
- If a client provides `session_id` but tmux is inactive, restart Claude Code with `--resume <session_id>`.
- Do not use `:` in tmux session names because tmux uses it as a target separator.
- Include `session_id` in service-facing JSON events and final responses.

`reap` is not a daemon. It scans and exits.

Idle cleanup policy:

- default controlled prefix: `ctc-`
- state file mtime means last user input time
- sessions without state files are skipped
- sessions still considered `working` are skipped
- `--dry-run` must remain safe and side-effect free

Be conservative with session termination. Avoid killing unrelated tmux sessions.

## Authentication And Permissions

When an OAuth token is provided by the caller, pass it to new tmux sessions as:

```text
CLAUDE_CODE_OAUTH_TOKEN
```

`--oauth-token-env` selects the source environment variable at invocation time.

Claude Code launch commands default to:

```text
--dangerously-skip-permissions
```

Do not duplicate this flag if a permission override already exists.

`start`, `launch`, and `chat` allow unknown CLI arguments to pass through to the Claude Code command.

Example:

```bash
./claude_tmux_control.py start work --cwd "$PWD" --model opus --add-dir ../shared
```

Do not apply this passthrough behavior to service commands such as `status`, `events`, `answer`, `stream`, `kill`, or `reap`; unknown options there should remain errors.

Never print token values in logs, docs examples, tests, or errors.

## Testing Rules

Use TDD for behavior changes.

Run at minimum:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
python3 -m py_compile claude_tmux_control.py scripts/stream_question.py
```

For tmux lifecycle changes, add fake-runner tests and, when feasible, a harmless smoke test with a short-lived tmux session.

Clean generated bytecode before finishing:

```bash
rm -rf __pycache__ tests/__pycache__ scripts/__pycache__
```

## Editing Rules

- Keep changes surgical.
- Do not add dependencies unless the current standard-library approach is clearly insufficient.
- Prefer structured parsing over string scraping.
- Preserve transcript schema tolerance. Claude Code JSONL fields may vary by version.
- Keep command output stable for external programs.
- Update `docs/cli-manual.md` and `docs/implementation-checklist.md` when CLI behavior changes.
- Update `docs/web-chat-integration-guide.md` and `docs/web-chat-integration-guide.html` whenever a source change affects external chat integration, stream events, session lifecycle, token/context extraction, cost/usage reporting, or error/exit-code behavior.
- If a behavior affects product direction, update `docs/PRD.md`.

## Current Known Gaps

Tracked in `docs/implementation-checklist.md`.

Important future areas:

- `ensure [session-id]`
- `ask <session-id> <prompt>`
- `list`
- `info`
- JSON output contracts for service-facing commands
- session-level send locks
- transcript rotation handling
- more explicit `starting`, `inactive`, and `unknown` states

Do not quietly implement broad future work while doing a narrow fix. Mention it and keep the requested change focused.
