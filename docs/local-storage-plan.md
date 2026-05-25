# Local Storage Plan

[English](./local-storage-plan.md) | [한국어](./local-storage-plan.ko.md)

This plan defines how `ctc` stores enough local state to stream efficiently, reconnect safely, and avoid rereading full Claude Code transcripts on every poll.

## 1. Current Problem

A service-facing stream must know:

- which Claude Code transcript belongs to the session,
- where the current turn starts,
- which events have already been emitted,
- whether another process is already streaming/sending,
- whether a previous turn is still active.

Without local state, reconnect and crash recovery become ambiguous.

## 2. Storage Location

Default state directory:

```text
~/.cache/claude-tmux-control/
```

Suggested layout:

```text
sessions/<session_id>.json
locks/<session_id>.lock
```

## 3. Session State Schema

Core fields:

```json
{
  "schema_version": 1,
  "session_id": "uuid",
  "tmux_session": "ctc-csess-uuid",
  "cwd": "/canonical/project/path",
  "transcript": {
    "path": "...jsonl",
    "device": 1,
    "inode": 2,
    "size": 1000,
    "mtime_ns": 123
  },
  "active_turn": null,
  "completed_turns": [],
  "usage_totals": {},
  "cost_totals": {}
}
```

`active_turn` should include turn id, owner pid/host, heartbeat, stream epoch, send time, prompt hash, transcript baseline, read offsets, and Claude state.

## 4. What To Store

### Stable Identity

- `session_id`
- `tmux_session`
- canonical `cwd`
- Claude transcript session id when known

### Transcript Cursor

- transcript path
- file identity
- last scan offset
- replacement/truncation detection metadata

### Turn Cursor

- `turn_id`
- anchor start/end offsets
- replay start offset
- read offset
- completed offset

### No-transcript Baseline

If no transcript exists before first launch, store an explicit no-transcript baseline plus pre-send wall-clock timestamp. Do not pretend offset `0` is a real captured baseline.

### Usage State

- per-turn usage
- cumulative token totals from retained completed turn records
- estimated turn/session cost from retained completed turn records
- pricing table version/source

`completed_turns` is a bounded state history. The CLI keeps at most the latest 200 completed turn records, and `usage_totals` / `cost_totals` are recomputed from that retained window rather than by rereading the entire transcript JSONL.

## 5. High-level Stream Algorithm

```text
acquire send lock
  validate session id and cwd
  recover stale active turn if safe
  resolve transcript baseline
  create active_turn
  start/reuse/resume tmux session
  send prompt to an active session or start Claude without prompt argv
release send lock

if a new tmux session was started:
  wait for the Claude Code prompt
  submit the prompt via tmux paste+Enter

poll transcript from baseline
  anchor target user turn
  emit new events
  update read offsets
  wait for transcript and screen readiness
  emit done
  emit metrics
  mark active_turn ready/clear
```

## 6. Where To Start Reading

Priority:

1. stored current turn cursor,
2. first user event after `before_send_transcript.offset`,
3. first user event after `before_send_wall_time_utc`,
4. latest unambiguous prompt hash/text match.

Fail closed when the anchor is ambiguous.

## 7. Rotation And Truncation

Detect transcript replacement through file identity and size. If a transcript changes unexpectedly, replay from a conservative baseline and rely on `event_id` deduplication.

## 8. Locking

Use a short send lock for start/resume/send. Use state generation compare/update for state writes.

`active_turn` prevents concurrent prompts. Takeover is allowed only when the owner process is gone or heartbeat is stale.

## 9. Stream Delivery Semantics

Delivery is at-least-once. Clients must deduplicate by `event_id`.

`last_stdout_flushed_offset` is diagnostic only, not an acknowledgement boundary.

## 10. Metrics Ownership

`done` means answer completion. Final usage/cost appears in a separate `metrics` event.

Do not place usage/cost summary fields inside `done`.

## 11. Compatibility

Low-level commands can still read transcript and screen state directly, but high-level web sessions should rely on bridge state and cursors.

## 12. Implementation Steps

- [x] create session state files.
- [x] store active turn metadata.
- [x] stream by offsets.
- [x] replay completed turns.
- [x] recover stale active turns.
- [ ] add explicit client ack protocol.
