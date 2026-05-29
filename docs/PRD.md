# PRD: Claude Code tmux Session Bridge CLI

[English](./PRD.md) | [한국어](./PRD.ko.md)

## Problem Statement

External programs need a way to use Claude Code like a long-lived chat session. `claude -p` is simple but one-shot. It does not preserve the same interactive TUI session in the way a web chat product expects.

## Solution

`claude-tmux-control` runs Claude Code inside tmux, sends input through the terminal pane, and reads structured output from Claude Code transcript JSONL.

```text
client/web
  -> ctc stream
    -> tmux session
      -> Claude Code interactive CLI
    <- Claude transcript JSONL
  <- JSONL stream / done / metrics
```

## User Stories

- As a web app, I can start a Claude Code session for one conversation.
- As a user, I can send multiple turns to the same conversation.
- As a backend, I can stream text, tool calls, and final metrics to the browser.
- As an operator, I can reap idle tmux sessions without losing the Claude conversation transcript.
- As a client, I can reconnect to an active turn or replay completed turns.

## Implementation Decisions

- Use tmux for input because Claude Code is a terminal UI.
- Use transcript JSONL as the primary output source.
- Use `ctc-csess-<session_id>` as the tmux naming convention.
- Use UUID session ids for high-level web sessions.
- Use local state under `~/.cache/claude-tmux-control/` for locks, cursors, active turns, and metrics totals.
- Emit JSONL events from `stream`.
- Emit `done` before final `metrics`.
- Do not estimate context size when Claude Code transcript does not provide it.

## CLI Contract

Primary web-facing commands:

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

Low-level commands remain available for debugging only.

## Status Detection Model

A turn is complete only when:

- the target user turn is anchored in transcript,
- meaningful transcript events no longer indicate thinking/tool work,
- the tmux screen looks ready,
- readiness remains stable for the idle window, default `3.5` seconds.

The visible prompt alone is not enough.

## Local Storage

State should preserve:

- session id and tmux session name,
- canonical cwd,
- transcript path and file identity,
- active turn metadata,
- transcript offsets,
- completed turn records,
- usage and cost totals,
- lock generation.

## Testing Decisions

- Unit tests cover command construction, transcript parsing, stream event normalization, replay, status, kill, and reap.
- CI verifies Docker image build and CLI startup. Authenticated Docker stream smoke should be run separately to verify first-run preseed and managed settings preflight.
- Real Claude Code integration tests should focus on stream, reconnect, cancel, kill, and reap scenarios.

## Out of Scope

- Multi-server distributed session routing.
- A long-running daemon protocol.
- Direct token issuance.
- A stable replacement for an official Claude Code SDK.
