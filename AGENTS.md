# AGENTS.md

## Project Map

Canonical project name: `claude-tmux-control`

This repository provides a Python CLI bridge for controlling Claude Code interactive sessions from external programs.

Primary flow:

```text
web/backend process
  -> ctc stream/ask/replay/info
    -> tmux session
      -> Claude Code interactive CLI
    <- Claude transcript JSONL
  <- JSONL events / done / metrics / state
```

The installed CLI entrypoints are:

- `ctc`
- `claude-tmux-control`

## Where To Look

Use this section as the project map before editing.

| Need | Primary file |
| --- | --- |
| Main CLI implementation | `claude_tmux_control.py` |
| CLI command reference | `docs/cli-manual.md` |
| Web/backend integration contract | `docs/web-chat-integration-guide.md` |
| Quick web client setup | `docs/quickstart-web-client.md` |
| Operations, cleanup, recovery | `docs/operations.md` |
| State schema, offsets, locks, replay model | `docs/local-storage-plan.md` |
| Security, tokens, permission mode, Docker safety | `docs/security.md` |
| Docker image and entrypoint behavior | `docs/docker.md` |
| Product direction and design decisions | `docs/PRD.md` |
| Completed/future work checklist | `docs/implementation-checklist.md` |
| Test and smoke scenario guide | `docs/test-scenarios.md` |
| Documentation index | `docs/README.md` |
| Interactive web integration guide | `docs/web-chat-integration-guide.html` |
| Web chat app flow visual guide | `docs/web-chat-app-flow.html` |
| Pricing table for cost estimates | `claude_pricing.json` |
| Public integration examples | `examples/` |
| Docker runtime image | `docker/Dockerfile`, `docker/entrypoint.sh` |
| Minimal stream wrapper | `scripts/stream_question.py` |
| Backend-style client smoke helper | `scripts/web_chat_client.py` |
| Markdown link/language-pair validation | `scripts/check_docs.py` |
| Main behavior tests | `tests/test_claude_tmux_control.py` |
| Script tests | `tests/test_stream_question_script.py`, `tests/test_web_chat_client_script.py`, `tests/test_check_docs_script.py` |

Generated packaging/build outputs such as `build/` and `claude_tmux_control.egg-info/` are not primary source files.

## Command Surfaces

High-level web/client commands use a bridge `session_id` UUID:

```bash
ctc stream --cwd PATH [--session-id UUID] [--model MODEL] [--claude-args "ARGS"] PROMPT
ctc stream --attach --session-id UUID
ctc ask --cwd PATH [--session-id UUID] [--model MODEL] [--claude-args "ARGS"] PROMPT
ctc cancel UUID
ctc last UUID --last N
ctc replay UUID --last N
ctc info UUID --json
ctc list --json
```

Low-level tmux/debug commands use a tmux session name:

```bash
ctc start TMUX_SESSION --cwd PATH
ctc launch TMUX_SESSION
ctc send TMUX_SESSION PROMPT
ctc answer TMUX_SESSION
ctc turn TMUX_SESSION
ctc events TMUX_SESSION
ctc status TMUX_SESSION
ctc wait-ready TMUX_SESSION
ctc capture TMUX_SESSION
ctc watch TMUX_SESSION
ctc follow TMUX_SESSION --append screen.log
ctc chat TMUX_SESSION --cwd PATH
```

Operational commands:

```bash
ctc kill TMUX_SESSION
ctc reap --idle-seconds N --prefix ctc-csess- --dry-run
ctc reap --idle-seconds N --prefix ctc-csess-
```

Do not pass `ctc-csess-$SESSION_ID` to low-level `start` for normal web sessions. Use high-level `stream --session-id "$SESSION_ID" --cwd ...` to create, reuse, or resume web sessions.

## Core Invariants

### Input

Claude Code is a terminal UI. Send prompts through tmux, not raw stdin:

```text
tmux load-buffer
tmux paste-buffer
tmux send-keys Enter
```

### Output

Use Claude Code transcript JSONL as the primary structured source for:

- assistant text
- thinking
- tool_use
- tool_result
- timestamps
- model, usage, context fields when present

Use `tmux capture-pane` only for rendered-screen state, prompt readiness, debugging, and fallback checks.

Do not parse final answers from rendered terminal text when transcript data is available.

### Session Identity

- The bridge owns the canonical high-level `session_id`.
- Client-provided `session_id` must be a UUID before it is used in paths or tmux names.
- High-level tmux session name is `ctc-csess-<session_id>`.
- New Claude Code sessions start with `--session-id <session_id>`.
- If state/transcript exists but tmux is inactive, restart Claude Code with `--resume <session_id>`.
- Fail closed with `session_cwd_mismatch` if an existing session state has a different canonical `cwd`.
- Do not use `:` in tmux session names.

### Completion

Do not treat the visible prompt glyph alone as completion.

A turn is complete only when:

- the target user turn has final assistant text, or a recognized cancelled-tool terminal state,
- the latest meaningful transcript event is not `thinking`, `tool_use`, or `tool_result`,
- the tmux screen is ready,
- and the ready state remains stable for the configured idle window.

Subagent flows such as `Task` tool calls are not complete when the tool result arrives. Wait for final assistant text after the tool result unless handling a recognized cancelled-tool terminal state.

### Streaming And Replay

- High-level `stream` emits line-delimited JSON events.
- Normalized progress events include stable `session_id`, `turn_id`, deterministic `event_id`, source offsets, and block ordinal metadata.
- `done` is answer-completion state only; usage/cost summary belongs in a separate `metrics` event after `done`.
- Delivery is at-least-once until an explicit client acknowledgement protocol exists.
- Reconnect/takeover should replay from conservative `replay_start_offset`.
- `last_stdout_flushed_offset` is diagnostic only. It is not a delivery acknowledgement.
- `timeout` and `failed` do not automatically mean the session is ready for another prompt. Keep or inspect `active_turn` until attach/inspect/kill confirms Claude is no longer processing.

### Local State

High-level state lives under:

```text
~/.cache/claude-tmux-control/
```

For state schema, cursor meanings, locks, stale owner recovery, transcript rotation, and replay semantics, follow `docs/local-storage-plan.md`.

## Claude Launch Contract

The bridge always launches the fixed `claude` executable. It does not accept an arbitrary shell command.

Supported launch options:

- `--model MODEL`: common model selection.
- `--claude-args "ARGS"`: trusted extra Claude Code CLI arguments, parsed without shell execution.

Example:

```bash
ctc stream --cwd "$PWD" --model opus --claude-args "--add-dir ../shared" "hello"
```

`--model` and `--claude-args` apply only when the bridge starts or resumes a Claude Code process. Existing tmux sessions keep their original model and launch arguments.

Low-level `stream <tmux-session>` reads an already-running turn. It must not silently accept Claude launch options.

Claude Code launch defaults to:

```text
--dangerously-skip-permissions
```

This is a high-risk default. It avoids interactive approval prompts for service flows, but lets Claude Code run tools without per-action confirmation. Use it only in controlled environments such as Docker, isolated servers, restricted project directories, or dedicated service users.

To change permission behavior, pass a Claude Code permission option through trusted `--claude-args`:

```bash
ctc stream --cwd "$PWD" --claude-args "--permission-mode plan" "hello"
```

Do not add `--dangerously-skip-permissions` when `--claude-args` already contains `--permission-mode ...` or `--dangerously-skip-permissions`.

Do not expose raw `--claude-args` input to untrusted browser clients. Prefer a backend-controlled setting or safe enum such as `permissionMode=plan`, then map it server-side.

## Environment And Secrets

- `--oauth-token-env ENV` selects the source env var whose value is passed as `CLAUDE_CODE_OAUTH_TOKEN`.
- Additional Claude-side env can come from `<cwd>/.ctc.env`, explicit `--env-file PATH`, or whitelisted `--env NAME`.
- Env injection applies only when a new tmux session is created. Existing sessions keep their original environment.
- `CLAUDE_CODE_OAUTH_TOKEN` is reserved for `--oauth-token-env`; `.ctc.env` and `--env` must not set it.
- Never print token values in logs, docs examples, tests, errors, screenshots, or commits.

## Language And Documentation

- User-facing answers should be in Korean.
- Keep explanations concise and conclusion-first.
- Code, commands, paths, and option names stay in English.
- Commit messages for this repository must be written in English. This project-specific rule overrides the global Korean commit-message default for this repository.
- Append the required AI co-author attribution line to AI-generated commits:

```text
🤖 Co-authored with AI agent
```

- Keep primary Markdown docs in English: `README.md` and `docs/*.md`.
- Preserve Korean documentation in matching `.ko.md` files and keep top language switch links working both ways.
- Keep developer-only setup notes in `AGENTS.md` or docs under `docs/`; keep `README.md` focused on installation and user-facing usage.

## Editing Checklist

Keep changes surgical and source-compatible:

- Prefer existing patterns in `claude_tmux_control.py`.
- Standard library only unless there is a clear reason to add a dependency.
- Prefer structured parsing over string scraping.
- Preserve transcript schema tolerance. Claude Code JSONL fields may vary by version.
- Keep stdout/stderr and JSON event contracts stable for external programs.

## Versioning And Commits

`pyproject.toml` is the source of truth for the package version. The runtime
`ctc --version` output must match that version.

This project is still pre-1.0. Do not bump to `1.0.0` unless the command
surface, JSONL event contract, and state schema are explicitly declared stable.

For changes in the `0.x` line:

- Patch: compatible fixes, docs corrections, test-only changes, and packaging fixes.
- Minor: new commands, new flags, new output fields, state schema changes, or behavior changes that clients may notice.
- Breaking changes still use a `0.x` minor bump until stabilization, for example `0.2.0 -> 0.3.0`.

When a change affects behavior, commands, packaging, or release-facing docs,
bump `pyproject.toml` in the same change before committing. Update
`docs/release.md` and `docs/release.ko.md` when policy or release procedure
changes.

After verification, commit the completed change unless the user explicitly asks
not to. Commit messages must remain English for this repository and include the
required AI co-author attribution line.

Update docs when behavior changes:

| Change | Docs to check |
| --- | --- |
| CLI flags, exit codes, command behavior | `docs/cli-manual.md`, matching `.ko.md` |
| Web stream events, session lifecycle, client contract | `docs/web-chat-integration-guide.md`, `.ko.md`, `.html` |
| State schema, cursoring, locks, replay | `docs/local-storage-plan.md`, `.ko.md` |
| Auth, env, permission mode, token risk | `docs/security.md`, `.ko.md` |
| Docker image, entrypoint, container auth | `docs/docker.md`, `.ko.md` |
| Release, packaging, CI, versioning | `docs/release.md`, `.ko.md` |
| Product direction or major contract decision | `docs/PRD.md`, `.ko.md` |
| Completed/future work tracking | `docs/implementation-checklist.md`, `.ko.md` |
| Smoke scenarios or client harness | `docs/test-scenarios.md`, `.ko.md` |

Run `python3 scripts/check_docs.py` after documentation changes.

## Testing Rules

Use TDD for behavior changes.

Minimum local verification:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
python3 -m py_compile claude_tmux_control.py scripts/stream_question.py scripts/web_chat_client.py scripts/check_docs.py
python3 scripts/check_docs.py
```

For tmux lifecycle changes, add fake-runner tests. When feasible, add a harmless smoke test with a short-lived tmux session.

Clean generated bytecode before finishing:

```bash
rm -rf __pycache__ tests/__pycache__ scripts/__pycache__
```

## Current Known Gaps

Tracked in `docs/implementation-checklist.md`; keep that checklist as the source of truth instead of duplicating a separate gap list here.

Do not quietly implement broad future work while doing a narrow fix. Mention it and keep the requested change focused.
