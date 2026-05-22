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
- Commit messages for this repository must be written in English.
- Append the required AI co-author attribution line to AI-generated commits.
- Keep primary Markdown docs in English: `README.md` and `docs/*.md`.
- Preserve Korean documentation in matching `.ko.md` files and keep the top language switch links working both ways.

## Tech Stack

- Python 3.10+
- Standard library only unless there is a clear reason to add a dependency.
- `tmux` as the terminal execution layer.
- Claude Code CLI (`claude`) as the controlled interactive process.
- `unittest` for tests.
- Packaging is defined in `pyproject.toml`.
- The installed CLI entrypoints are `ctc` and `claude-tmux-control`.
- Keep developer-only setup notes in `AGENTS.md` or docs under `docs/`; keep `README.md` focused on installation and user-facing usage.

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
- `docs/local-storage-plan.md`
  - Planned local state schema for transcript cursors, turn anchors, locks, and efficient streaming.
- `docs/docker.md`
  - Docker image build, Claude Code first-run preseed, managed settings preflight, container 운영 절차.
- `docs/security.md`
  - Token handling, dangerous permission mode, transcript/state sensitivity, and Docker safety.
- `docs/release.md`
  - Release checklist for GitHub tags, PyPI readiness, and Docker registry readiness.
- `scripts/check_docs.py`
  - Markdown language switch, translation pair, and local link validation.
- `examples/`
  - Public integration examples. Keep them minimal and stdout-JSONL oriented.
- `docker/Dockerfile`
  - Runtime image for `ctc`, `tmux`, and Claude Code CLI.
- `docker/entrypoint.sh`
  - Container startup preseed for Claude Code onboarding/trust/bypass prompts and managed settings preflight.
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

### Local Storage And Cursoring

Follow `docs/local-storage-plan.md` for high-level web chat work.

High-level stream should not repeatedly read the full transcript after the target turn is anchored. It should store session state, transcript path/file identity, offsets, turn cursor, prompt hash, emitted offset, usage totals, and lock state under `~/.cache/claude-tmux-control/`.

Capture transcript baseline before any send/start/resume action.

If no transcript exists before first launch, record an explicit `no_transcript_baseline` plus the pre-send wall-clock timestamp. Do not pretend offset `0` is a real captured transcript.

For each new turn, establish the target turn by offset/time first, with prompt matching only as unambiguous fallback:

1. stored current turn cursor
2. first user event after `before_send_transcript.offset`
3. first user event after `before_send_wall_time_utc`
4. latest matching user prompt hash/text, fail closed if ambiguous

Keep cursor meanings separate:

- `transcript.scan_offset`
- `turn.anchor_start_offset`
- `turn.anchor_end_offset`
- `turn.replay_start_offset`
- `turn.read_offset`
- `turn.last_stdout_flushed_offset`
- `turn.completed_offset`

Use a short `send_lock` for ensure/start/resume/send. Use a separate state-write lock with `generation` compare/update for state mutations.

Use durable `active_turn` state to prevent new prompts while Claude is still working. `active_turn` must include owner pid/host, heartbeat, stream epoch, and enough cursor metadata to resume or take over deterministically.

A reconnect should attach to the active turn rather than sending a new prompt. Takeover is allowed only when the recorded owner process is gone or the heartbeat lease is stale.

Default high-level web stream polling interval should be `2.0` seconds unless the caller overrides it.

### Completion

Do not treat the visible prompt glyph alone as completion.

A turn is complete only when:

- the target user turn has final assistant text,
- the latest meaningful transcript event is not `thinking`, `tool_use`, or `tool_result`,
- the rendered tmux screen is ready,
- and the ready state remains stable for the configured idle window.

Subagent flows such as `Task` tool calls are not complete when the tool result arrives. Wait for the final assistant text after the tool result.

### Streaming

The target service-facing `stream [--session-id <id>] --cwd <path> <prompt...>` command is the primary web chat API. It should ensure/reuse/resume the Claude Code session, send the prompt for exactly one turn, emit progress JSONL, then emit `done` and final `metrics`.

The existing low-level `stream <tmux-session>` behavior reads an already-sent turn from a tmux-backed Claude Code session. Keep this compatibility clear when changing CLI contracts.

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

For service-facing chat flows, `done` is answer-completion only. Do not put usage/context/cost summary fields in `done`.

Emit final turn metrics after `done` as a separate `metrics` event for the same `turn_id`. Current final metrics include elapsed_ms, model, input tokens, cache read tokens, cache write tokens, output tokens, context fields when available, estimated turn USD, and estimated session cumulative USD from completed turn records when model/usage can be resolved through `claude_pricing.json`. Mid-stream metrics are optional best-effort only when the transcript already contains usage/context/model before the turn is done.

Treat stream delivery as at-least-once until an explicit client acknowledgement protocol exists. Reconnect/takeover should replay from conservative `replay_start_offset`, not from stdout flush state. `last_stdout_flushed_offset` is diagnostic only.

Every normalized progress/done/metrics event should include stable `turn_id`, deterministic `event_id`, source start/end offsets, and block ordinal metadata so clients can deduplicate replay after reconnect or crash recovery.

`timeout` and `failed` do not mean the session is ready for another prompt. Keep `active_turn` until attach/inspect/kill confirms Claude is no longer processing the prior turn.

External programs should consume stream output line-by-line as JSON.

### Session Lifecycle

The bridge owns the canonical web-facing `session_id`.

Target session identity model:

- Generate a UUID v4 when the client does not provide `session_id`.
- Validate client-provided `session_id` as UUID before using it in state paths or tmux session names.
- Fail closed with `session_cwd_mismatch` if existing session state has a different canonical `cwd` from the request.
- Use tmux session name `ctc-csess-<session_id>`.
- Start new Claude Code sessions with `--session-id <session_id>`.
- If known state or a matching transcript exists but tmux is inactive, restart Claude Code with `--resume <session_id>`.
- If a client provides a UUID that has no known state/transcript yet, start Claude Code with `--session-id <session_id>`.
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
- Update `docs/local-storage-plan.md` when changing state schema, cursoring, transcript rotation handling, lock semantics, or polling behavior.
- Update `docs/docker.md` whenever Docker image build, `docker/entrypoint.sh`, Claude Code first-run preseed, managed settings preflight, auth env handling, or container cleanup behavior changes.
- Update `docs/security.md` whenever auth handling, token exposure risk, state/transcript storage, permission mode, or Docker secret behavior changes.
- Update `docs/release.md` whenever packaging, CI, release, Docker registry, or versioning behavior changes.
- Keep English docs and matching `.ko.md` docs in sync for user-facing Markdown.
- Run `python scripts/check_docs.py` after documentation changes.
- If a behavior affects product direction, update `docs/PRD.md`.

## Current Known Gaps

Tracked in `docs/implementation-checklist.md`.

Important future areas:

- internal `ensure [session-id]`
- JSON output contracts for service-facing commands
- state-write lock generation conflict retry
- machine-readable stats command
- more explicit `starting`, `inactive`, and `unknown` states

Do not quietly implement broad future work while doing a narrow fix. Mention it and keep the requested change focused.
