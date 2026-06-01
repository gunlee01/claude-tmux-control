# Implementation Checklist

[English](./implementation-checklist.md) | [한국어](./implementation-checklist.ko.md)

## 1. Source Model

- [x] Use Claude Code transcript JSONL as primary structured output.
- [x] Use tmux screen capture only for readiness/debugging fallback.
- [x] Normalize assistant text, thinking, tool use, tool result, done, and metrics events.

## 2. Session Identity

- [x] Use UUID high-level session ids.
- [x] Map high-level sessions to `ctc-csess-<session_id>` tmux names.
- [x] Validate `session_id` before using it in paths or tmux names.
- [x] Reject cwd mismatch for existing sessions.

## 3. CLI Contract

- [x] Add high-level `stream`.
- [x] Add `ask`.
- [x] Add `cancel`.
- [x] Add `last`/`replay`.
- [x] Add `info`/`list`.
- [x] Keep low-level tmux debug commands.

## 4. Machine-readable Output

- [x] Emit JSONL stream events.
- [x] Include `session_id`, `turn_id`, `event_id`, source offsets, and block index.
- [x] Emit `done` before `metrics`.
- [x] Emit final token/cost metrics when transcript usage exists.

## 5. Local Storage And Cursoring

- [x] Store bridge state under `~/.cache/claude-tmux-control/`.
- [x] Capture transcript baseline before send/start/resume.
- [x] Use a short send lock for start/resume/send.
- [x] Use a state-write lock with generation compare/update for state writes.
- [x] Track active turn state and heartbeat.
- [x] Track transcript offsets for replay and deduplication.
- [x] Recover stale active turns conservatively.

## 6. Lifecycle And Cleanup

- [x] Reuse existing tmux session when ready.
- [x] Resume with Claude Code when tmux is gone but state/transcript exists.
- [x] Add `reap` with `--dry-run`.
- [x] Skip active/working sessions during reap.
- [x] Add `kill` for explicit process stop.

## 7. Authentication And Permissions

- [x] Pass caller-provided OAuth token as `CLAUDE_CODE_OAUTH_TOKEN`.
- [x] Support `--oauth-token-env`.
- [x] Support project env injection with `.ctc.env`, `--env-file`, and `--env`.
- [x] Default Claude Code launch to `--dangerously-skip-permissions`.
- [x] Avoid duplicating permission override flags.
- [x] Keep Claude Code executable fixed to `claude` and pass trusted launch options through `--model` / `--effort` / `--claude-args`.
- [x] Preseed high-level `--cwd` as a Claude Code trusted project before launching a new Claude Code process.

## 8. Docker

- [x] Add Dockerfile with Python, tmux, Claude Code, and `ctc`.
- [x] Run as non-root user.
- [x] Add entrypoint preseed for onboarding, trust, and bypass warning.
- [x] Add `claude -p` preflight for managed settings cache.
- [x] Document Docker usage and limitations.

## 9. Testing

- [x] Unit tests for core CLI behavior.
- [x] Docker image build and CLI smoke in CI.
- [x] Scenario harness for stream/replay/cancel/kill/reap behavior.
- [x] Regression test for stale state writes not resurrecting a completed active turn.
- [x] Transcript compatibility fixtures for normalized stream event contracts.
- [x] Integration test for high-level trusted project preseed using an isolated temp Claude home/config.

## 10. Future Work

- [ ] Add explicit client acknowledgement protocol for exactly-once delivery.
- [x] Add machine-readable stats command.
- [x] Improve failed/timeout recovery UX with `active_turn_recovery` guidance.
- [x] Add transcript compatibility fixtures to track Claude Code transcript schema changes across versions.
- [ ] Add optional authenticated Docker stream smoke for managed-settings preflight and `ctc stream`.
