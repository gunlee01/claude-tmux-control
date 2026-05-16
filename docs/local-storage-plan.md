# Local Storage Plan

이 문서는 high-level `stream [--session-id] --cwd <path> <prompt>`를 안정적으로 구현하기 위한 로컬 스토리지 계획입니다.

목표는 두 가지입니다.

- JSONL transcript를 매번 전체 재읽지 않고 offset 기반으로 tailing한다.
- 다음 stream turn에서 어디부터 읽어야 하는지 명확하게 판단한다.

## 1. Current Problem

현재 low-level stream은 단순합니다.

```text
loop every interval:
  read whole transcript JSONL
  parse all events
  find target turn by last_prompt
  emit events after emitted_event_count
  inspect completion
```

장점은 구현이 쉽다는 것입니다.

하지만 웹 채팅 서버에서는 문제가 됩니다.

- transcript가 커질수록 매 polling마다 전체 파일을 다시 읽습니다.
- 같은 prompt가 반복되면 prompt text matching만으로 target turn을 찾기 애매합니다.
- 다음 turn에서 어디부터 읽을지 판단하려면 이전 stream cursor가 필요합니다.
- 여러 session을 동시에 stream하면 IO와 CPU가 커집니다.

## 2. Storage Location

기존 state dir를 유지합니다.

```text
~/.cache/claude-tmux-control/
```

session별 파일:

```text
~/.cache/claude-tmux-control/sessions/<session_id>.json
```

`session_id`는 UUID 형식만 허용합니다.

클라이언트가 제공한 값도 path나 tmux session name에 쓰기 전에 UUID로 검증합니다.

기존 state가 있으면 요청 `cwd`를 canonical path로 정규화한 뒤 state의 canonical `cwd`와 비교합니다.

다르면 `session_cwd_mismatch`로 fail closed합니다.

lock 파일:

```text
~/.cache/claude-tmux-control/locks/<session_id>.lock
```

atomic write:

```text
write temp file -> fsync best effort -> rename
```

초기 구현은 표준 라이브러리만 사용합니다.

SQLite는 여러 프로세스 동시성이나 query가 필요해질 때 도입합니다.

지원 전제:

- Linux/macOS local filesystem
- `tmux` 사용 가능 환경
- cross-host/NFS lock 보장은 하지 않음
- file identity는 가능하면 `(st_dev, st_ino, st_size, st_mtime_ns)`를 사용

## 3. Session State Schema

```json
{
  "schema_version": 1,
  "generation": 17,
  "updated_at": "2026-05-16T12:03:12.000Z",
  "writer_pid": 4242,
  "writer_hostname": "server-a",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "tmux_session": "ctc-csess-550e8400-e29b-41d4-a716-446655440000",
  "cwd": "/srv/projects/app",
  "created_at": "2026-05-16T12:00:00.000Z",
  "claude": {
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "model": "claude-sonnet-4-5-20250929"
  },
  "transcript": {
    "path": "/home/app/.claude/projects/.../550e8400.jsonl",
    "st_dev": 16777220,
    "st_ino": 123456,
    "size": 82731,
    "mtime_ns": 1778910000000000000,
    "scan_offset": 82731,
    "last_event_timestamp": "2026-05-16T12:03:11.000Z"
  },
  "active_turn": {
    "turn_id": "20260516T120300Z-7f3a",
    "claude_state": "working",
    "stream_state": "active",
    "owner_pid": 4242,
    "owner_hostname": "server-a",
    "owner_process_start": "2026-05-16T12:02:58.000Z",
    "stream_epoch": 3,
    "heartbeat_at": "2026-05-16T12:03:04.000Z",
    "prompt_hash": "sha256:...",
    "prompt_preview": "Finish this PR",
    "started_at": "2026-05-16T12:03:00.000Z",
    "before_send_wall_time_utc": "2026-05-16T12:02:59.900Z",
    "before_send_transcript": {
      "path": "/home/app/.claude/projects/.../550e8400.jsonl",
      "st_dev": 16777220,
      "st_ino": 123456,
      "offset": 80120,
      "mtime_ns": 1778909999900000000
    },
    "no_transcript_baseline": false,
    "anchor_start_offset": 80120,
    "anchor_end_offset": 80410,
    "replay_start_offset": 80410,
    "read_offset": 82731,
    "last_stdout_flushed_offset": 82731,
    "completed_offset": 82731,
    "anchor_strategy": "after_offset"
  },
  "last_turn": {
    "turn_id": "20260516T115800Z-2d91",
    "claude_state": "ready",
    "stream_state": "done",
    "completed_at": "2026-05-16T11:58:33.000Z"
  },
  "completed_turns": [
    {
      "turn_id": "20260516T120300Z-7f3a",
      "source_event_offsets": [82100, 82731],
      "model": "claude-sonnet-4-5-20250929",
      "usage": {
        "input_tokens": 12000,
        "cache_read_tokens": 8000,
        "cache_write_tokens": 500,
        "output_tokens": 1400
      },
      "estimated_turn_usd": 0.0572
    }
  ],
  "usage_totals": {
    "input_tokens": 12000,
    "cache_read_tokens": 8000,
    "cache_write_tokens": 500,
    "output_tokens": 1400,
    "estimated_session_usd": 0.2516,
    "pricing_version": "anthropic-2026-05-16"
  }
}
```

## 4. What To Store

### Stable Identity

- `session_id`
- `tmux_session`
- `cwd`
- Claude transcript `sessionId` when observed

### Transcript Cursor

- transcript file path
- file identity: `st_dev`, `st_ino`, `st_size`, `st_mtime_ns`
- file size
- `scan_offset`: last byte safely parsed from the current file
- last event timestamp

### Turn Cursor

- generated `turn_id`
- `claude_state`: `starting`, `working`, `ready`, `unknown`
- `stream_state`: `active`, `detached`, `timeout`, `failed`, `done`
- owner lease fields: `owner_pid`, `owner_hostname`, `owner_process_start`, `stream_epoch`, `heartbeat_at`
- prompt hash
- prompt preview for debugging
- `before_send_wall_time_utc`: wall-clock UTC timestamp for transcript event comparison
- `before_send_monotonic`: process-local value for timeout/elapsed calculation only, not persisted as event comparison key
- `before_send_transcript`: transcript identity and offset captured before send/start/resume
- `anchor_start_offset`: byte offset of the anchored user event
- `anchor_end_offset`: byte offset immediately after the anchored user event
- `replay_start_offset`: conservative replay lower bound for reconnect/takeover in v1
- `read_offset`: next byte to scan for this turn
- `last_stdout_flushed_offset`: highest event end offset written to stdout and flushed
- `completed_offset`: offset of the final event used to mark done
- `anchor_strategy`: `after_offset`, `after_time`, `prompt_fallback`

Do not decide whether a new prompt is allowed from `stream_state` alone.

New prompts are allowed only when `claude_state` is `ready`, or when the tmux/Claude process is confirmed inactive and resume is safe.

`stream_state = timeout` means the streaming process timed out. It does not prove Claude Code stopped working.

### No-Transcript Baseline

For first launch, a transcript may not exist before sending the prompt.

Represent that explicitly:

```json
{
  "before_send_transcript": null,
  "no_transcript_baseline": true,
  "before_send_wall_time_utc": "2026-05-16T12:02:59.900Z"
}
```

Do not synthesize `before_send_transcript.path` or `offset` from a transcript discovered after send/start/resume.

### Usage State

- last turn usage summary
- completed turn metrics keyed by `turn_id` and source event offsets
- session cumulative token totals recomputed from completed turn metrics
- estimated session cumulative cost recomputed from completed turn metrics
- pricing version used by calculation

Do not increment cumulative usage blindly while streaming.

If a stream reconnects or replays events, metrics must be idempotent by `turn_id` plus source offset/event identity.

## 5. High-Level Stream Algorithm

### New Request

```text
input: optional session_id, cwd, prompt

1. if session_id is empty:
     generate UUID
   else:
     validate UUID format

2. derive tmux_session = ctc-csess-<session_id>

3. acquire short send_lock for session_id

4. load state file if present
     if present and canonical cwd differs from request cwd:
       fail with session_cwd_mismatch

5. if state.active_turn exists and state.active_turn.claude_state is not ready:
     fail with turn_in_progress unless attach mode is requested

6. resolve current transcript path before sending if possible:
     prefer state.transcript.path if still valid
     else search cwd-specific Claude project transcripts

7. capture baseline before any send/start/resume action:
     before_send_transcript.path
     before_send_transcript.file_identity
     before_send_transcript.offset = current file size if transcript exists
     before_send_wall_time_utc = current UTC wall time
     before_send_monotonic = current monotonic time

8. persist pending active_turn:
     turn_id
     claude_state = starting
     stream_state = active
     owner_pid / owner_hostname / owner_process_start
     stream_epoch
     heartbeat_at
     prompt_hash
     prompt_preview
     before_send_transcript
     before_send_wall_time_utc
     replay_start_offset = before_send_transcript.offset when transcript exists, otherwise null
     read_offset = before_send_transcript.offset when transcript exists, otherwise 0
     last_stdout_flushed_offset = before_send_transcript.offset when transcript exists, otherwise 0

9. inspect tmux:
     if active:
       send prompt to existing Claude Code process
     else if state exists or matching transcript exists:
       start tmux with claude --resume <session_id> "<prompt>"
     else:
       start tmux with claude --session-id <session_id> "<prompt>"

10. release send_lock after prompt is accepted or launch command is started

11. establish turn start:
     wait for user event after before_send_transcript.offset
     or after before_send_wall_time_utc when no prior transcript existed
     fallback to prompt_hash/user prompt match only if needed and unambiguous

12. persist turn cursor:
     turn.anchor_start_offset = user event offset
     turn.anchor_end_offset = user event end offset
     turn.replay_start_offset = user event end offset
     turn.read_offset = user event end offset
     turn.claude_state = working
     turn.stream_state = active

13. stream from cursor until done

14. write final metrics and status

15. after done:
     set claude_state = ready
     set stream_state = done
     move active_turn to last_turn
     clear active_turn

16. after timeout/failure:
     set stream_state = timeout or failed
     set claude_state = working or unknown
     keep active_turn until attach/inspect/kill confirms completion or inactivity
```

### Polling Loop

Default web interval:

```text
2.0 seconds
```

Loop:

```text
1. open transcript file
2. if file identity changed or size < read_offset:
     handle rotation by resolving transcript again
3. seek(read_offset)
4. read new lines only
5. parse JSON objects
6. append to in-memory current turn state
7. assign stable `event_id` and `source_offset` to normalized events
8. emit normalized events
9. after stdout write+flush, update last_stdout_flushed_offset under state-write lock
10. after parse, update transcript.scan_offset and turn.read_offset under state-write lock
11. update active_turn owner heartbeat
12. inspect completion
13. sleep interval
```

Completion still requires:

```text
final assistant text exists
latest meaningful transcript event is not thinking/tool_use/tool_result
tmux screen is ready or transcript-only completion policy is explicitly enabled
ready state remains stable for idle window
```

## 6. Where To Start Reading

Priority order:

```text
1. active_turn.anchor_start_offset from current state
2. first user event after before_send_transcript.offset in the same transcript file identity
3. first user event after before_send_wall_time_utc
4. latest user event matching prompt_hash/prompt text
```

After the first user event for the new prompt is found, that event becomes the turn anchor.

The stream should only emit events belonging to that anchored turn.

This avoids relying only on prompt text matching.

Prompt fallback is non-authoritative.

Rules:

- only consider user events after the pre-send wall time
- normalize transcript user content before hashing
- ignore tool_result user events and known internal hook prompts
- if multiple candidate user events remain, fail closed with `unknown_transcript_ambiguous`
- record `anchor_strategy = "prompt_fallback"` in state for diagnostics

## 7. Rotation And Truncation

Detect rotation when:

```text
stored path missing
stored inode changed
current file size < stored offset
no new events but newer matching transcript exists for same cwd/session_id
```

Recovery:

```text
1. resolve transcript path again
2. if expected Claude sessionId is known:
     require candidate transcript to contain the same sessionId before switching
3. if expected Claude sessionId is not known:
     require transcript lineage from the previously stored path/file identity
     or require bridge session id in transcript metadata
     or require proof there are no other controlled sessions for the same cwd in the relevant time window
     then restrict candidates to expected cwd
     require file created/modified after pre-send baseline
     require first user event after baseline
4. if multiple candidates remain:
     fail with unknown_transcript_ambiguous
5. if no additional binding exists while sessionId is unknown:
     fail with unknown_transcript_ambiguous even if there is only one candidate
6. if current turn not done:
     find turn by offset/time first, prompt fallback only if unambiguous
7. continue streaming
```

Pin the first observed Claude transcript `sessionId` into state.

If a later transcript has a different `sessionId`, treat it as a mismatch and do not switch silently.

## 8. Locking

One active high-level turn per `session_id`, but do not hold one whole-stream lock for the full response.

Use two concepts:

```text
send_lock:
  short lock around ensure/start/resume/send and initial active_turn creation

active_turn:
  durable state that prevents a new prompt until the current turn is complete
```

Initial implementation:

```text
open send lock file with O_CREAT | O_EXCL
write pid, hostname, process start time when available, boot id when available, bridge version, session_id, turn_id, started_at, heartbeat_at
stale lock timeout is configurable but not sufficient by itself
```

If lock exists:

- If `active_turn.claude_state` is `working`, default behavior is `turn_in_progress`.
- `stream --attach --session-id <id>` can reconnect to the active turn without sending a new prompt.
- Breaking a stale lock requires stale heartbeat and no active matching process.
- Even after breaking a stale lock, do not send a new prompt when state says the current turn is still `working`.

Active turn owner lease:

- active stream owner writes `owner_pid`, `owner_hostname`, `owner_process_start`, `stream_epoch`, and `heartbeat_at`
- owner updates `heartbeat_at` while streaming
- attach mode is read-only by default and must not update cursor ownership
- takeover is allowed only when owner heartbeat is stale and owner process identity is no longer active
- takeover increments `stream_epoch`, becomes the new owner, and replays from `replay_start_offset`
- takeover must not send a prompt

State write policy:

- all state writers hold a state-write lock
- `send_lock` is only for prompt/start/resume critical section
- readers tolerate stale state
- state includes `generation`, `updated_at`, and `writer_pid`
- writers read current state under lock, verify expected `generation`, merge or retry, then write `generation + 1`
- atomic write uses temp file in same directory, fsync file best effort, rename, and fsync directory best effort
- `reap` must not kill or rewrite a `working` session unless tmux/process inactivity is also verified

## 9. Stream Delivery Semantics

Until a client acknowledgement protocol exists, stream delivery is at-least-once.

`last_stdout_flushed_offset` only means the bridge wrote bytes to its stdout pipe and flushed them.

It does not prove the app server, browser, or user received the event.

Therefore v1 reconnect/takeover replays from conservative `replay_start_offset`, normally the anchored user event end offset or the pre-send baseline offset.

This may duplicate progress events, but must not skip final answer, `done`, or `metrics`.

Every client-facing event must include stable identity:

```json
{
  "event": "assistant_text",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "turn_id": "20260516T120300Z-7f3a",
  "event_id": "20260516T120300Z-7f3a:dev16777220-ino123456:0000000000082100-0000000000082300:block0:assistant_text",
  "source_offset": 82100,
  "source_end_offset": 82300,
  "block_index": 0,
  "text": "..."
}
```

Rules:

- clients must deduplicate replayed events by `event_id`
- `done` and `metrics` also include `turn_id` and `event_id`
- progress event ids are derived from `turn_id`, transcript file identity, source start offset, source end offset, content block ordinal, and normalized event type
- if one transcript record produces multiple normalized events, the block ordinal must differ
- synthetic completion event ids are deterministic:
  - `<turn_id>:done:<completed_offset>`
  - `<turn_id>:metrics:<completed_offset>`
- `metrics` uses `scope: "turn_final"` and is emitted once per completed turn, but may replay after reconnect
- mid-stream metrics use `scope: "turn_partial"` and are not cumulative
- future acknowledgement support may advance a separate acknowledged offset

## 10. Metrics Ownership

Authoritative event contract:

- `done` contains completion state and final answer only
- final usage/context/cost is delivered in a separate `metrics` event after `done`
- `done` must not contain final usage/context summary
- if the bridge has pricing config, it may emit estimated cost
- if the app server owns pricing, it must enrich the `metrics` event before forwarding it to the browser
- elapsed time must be set before forwarding the final `metrics` event

Do not double-count metrics when replaying a `metrics` event.

## 11. Compatibility With Existing Low-Level Commands

Keep current low-level commands for debugging:

- `start <tmux-session>`
- `send <tmux-session>`
- `stream <tmux-session>`
- `events <tmux-session>`

But web-facing docs and examples should use:

```bash
./claude_tmux_control.py stream --session-id "$SESSION_ID" --cwd "$PROJECT_DIR" "$PROMPT"
```

Low-level commands may continue to use older state fields during transition.

High-level commands should use the new session state schema.

## 12. Metrics And Cost Idempotency

Usage fields can appear in different transcript events and may be repeated.

Rules:

- derive turn metrics from events within the anchored turn only
- store completed turn metrics keyed by `turn_id`
- store source event offsets or stable event ids when available
- recompute session cumulative totals from completed turn records
- never add the same completed turn twice after reconnect/replay
- if usage semantics are ambiguous, mark metrics as estimated or incomplete
- pricing table/version should be injected by caller or configured externally; do not hardcode long-lived prices

## 13. Clock Semantics

Use two clocks:

```text
before_send_wall_time_utc:
  ISO-8601 UTC timestamp for transcript timestamp comparison

before_send_monotonic:
  process-local monotonic value for timeout and elapsed calculations
```

Do not compare transcript timestamps to monotonic values.

Do not persist monotonic values as cross-process event comparison keys.

## 14. Implementation Steps

1. Add state schema dataclasses and atomic JSON read/write helpers.
2. Add session id normalization and tmux session derivation.
3. Add state generation and writer metadata.
4. Add short `send_lock` helper.
5. Add state-write lock.
6. Add durable `active_turn` owner lease and heartbeat.
7. Add `stream --attach` and takeover behavior.
8. Add transcript cursor reader that can `seek(offset)` and parse only new JSONL lines.
9. Capture transcript baseline before any send/start/resume action.
10. Add turn anchor detection from offset/time/prompt fallback with ambiguity failure.
11. Add high-level `stream [--session-id] --cwd <path> <prompt>` command.
12. Add stable client event ids and at-least-once replay contract.
13. Add final `metrics` event and idempotent session cumulative usage update.
14. Add rotation/truncation detection and candidate ambiguity handling.
15. Add tests for repeated prompt, pre-send baseline race, offset-based turn selection, rotation, attach/reconnect, crash/replay, stale locks, and metrics deduplication.
