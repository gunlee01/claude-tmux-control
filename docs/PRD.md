# PRD: Claude Code tmux Session Bridge CLI

## Problem Statement

외부 프로그램이 Claude Code를 interactive CLI 그대로 실행하고 제어하려면 안정적인 bridge가 필요하다.

`claude -p` 같은 one-shot 호출은 Claude Code의 실제 terminal UI, tool call, permission prompt, session continuity를 그대로 활용하기 어렵다. 반대로 사람이 쓰는 Claude Code CLI는 `tmux` 안에서 안정적으로 실행할 수 있지만, 웹 서버나 다른 클라이언트가 입력 전달, 응답 수집, 상태 확인, 세션 재사용을 하려면 별도 제어 계층이 필요하다.

현재 CLI는 `tmux` 기반 실행, 입력 전달, 화면 capture, transcript JSONL 기반 상태/응답 조회를 제공한다. 다음 단계는 이것을 여러 외부 클라이언트가 사용할 수 있는 session-oriented bridge CLI로 확장하는 것이다.

## Solution

CLI는 Claude Code를 `tmux` session 안에서 실행하고, 외부 프로그램은 이 CLI를 호출해 다음 작업을 수행한다.

- Claude Code session 생성 또는 재사용
- bridge session id 기준으로 tmux session 조회
- 입력 prompt 전달
- 응답 진행 상태 확인
- 최신 답변 본문 조회
- 최신 turn 조회
- tool call, tool result, thinking, token usage 조회
- 최신 turn을 JSONL로 streaming하고 완료 시 종료
- 오래된 inactive session 종료

입력은 `tmux load-buffer` + `paste-buffer` + `send-keys Enter`로 전달한다.

Claude Code process는 기본적으로 `--dangerously-skip-permissions`를 붙여 실행한다. 이 bridge는 외부 서비스가 session을 소유하는 구조이므로 Claude Code의 interactive permission prompt에 의존하지 않는다.

OAuth token이 필요한 경우 호출 프로세스가 token을 환경변수로 제공하고, bridge는 새 tmux session을 만들 때 그 값을 `CLAUDE_CODE_OAUTH_TOKEN`으로 전달한다. 계정별 token은 호출 시점에 다른 source env를 선택해서 동적으로 바꾼다.

출력/상태는 두 원본을 조합한다.

- `tmux capture-pane`: 사람이 보는 화면과 prompt 상태 확인
- Claude Code transcript JSONL: assistant text, thinking, tool_use, tool_result, usage, timestamp 확인

운영 웹앱은 UUID 기반 `session_id`를 먼저 생성해 bridge에 넘기는 방식을 권장한다. 클라이언트가 생략하면 CLI가 UUID를 생성하고 Claude Code 첫 실행에 같은 값을 `--session-id <session_id>`로 전달한다. tmux session name은 `ctc-csess-<session_id>` 규칙을 사용한다.

클라이언트가 이후 요청에 같은 `session_id`를 보내면 bridge는 같은 tmux session을 재사용한다. tmux session이 이미 종료되어 있고 기존 state 또는 matching transcript가 있으면 같은 서버의 로컬 Claude transcript를 기준으로 `claude --resume <session_id>`를 실행해 복구한다. 기존 state/transcript가 없는 client-provided id라면 `claude --session-id <session_id>`로 새 session을 시작한다.

## User Stories

1. As a web client, I want to create a Claude Code session, so that I can start an interactive coding conversation from a browser.
2. As a web client, I want to send user input to an existing Claude session id, so that the same Claude Code context is reused.
3. As a web client, I want the bridge to create a new tmux session when the requested session id is inactive, so that requests can recover automatically.
4. As a backend service, I want to list active Claude sessions, so that I can show users which sessions are running.
5. As a backend service, I want to know whether a session is active in tmux, so that I can decide between reuse and recreate.
6. As a backend service, I want to read the latest assistant answer only, so that I can render a clean chat response.
7. As a backend service, I want to read the latest full turn, so that I can show thinking, tool calls, tool results, and final text.
8. As a backend service, I want to follow turn updates while Claude is working, so that I can stream progress to a UI.
9. As a backend service, I want to inspect tool_use events, so that I can show which command or tool Claude is running.
10. As a backend service, I want to inspect tool_result events, so that I can show command output or failures.
11. As a backend service, I want timestamps for events, so that I can show elapsed time and ordering.
12. As a backend service, I want token usage when available, so that I can show cost/context indicators.
13. As a backend service, I want context usage when available, so that I can warn about long sessions.
14. As a backend service, I want a reliable done/working signal, so that I do not send another user input while Claude is still processing.
15. As a backend service, I want Claude Code launched without interactive permission prompts, so that requests do not block on dynamic approval.
16. As an operator, I want to terminate idle sessions, so that Claude Code sessions do not consume resources forever.
17. As an operator, I want to configure idle timeout, so that different deployments can tune session lifetime.
18. As an operator, I want to terminate a specific session id, so that stuck sessions can be cleaned up.
19. As an operator, I want to avoid global transcript confusion, so that one web user cannot see another session's transcript.
20. As a developer, I want the CLI to work on headless Linux servers, so that it can run behind a web service.
21. As a developer, I want the CLI to keep a minimal dependency footprint, so that deployment is easy.
22. As a developer, I want predictable JSON output modes, so that another program can parse status, answer, and turn data.
23. As a developer, I want human-readable output modes, so that I can debug from shell.
24. As a developer, I want tests around transcript selection and status detection, so that schema drift does not silently break the bridge.

## Implementation Decisions

- Use `tmux` as the terminal execution layer.
- Use bridge `session_id` as the primary external session key.
- Derive tmux session names from bridge `session_id`.
- The app server should usually create and persist the canonical `session_id` for each web conversation.
- Generate `session_id` as UUID v4 when the client does not provide one.
- Validate client-provided `session_id` as a canonical hyphenated UUID string before using it in a path, state filename, or tmux session name.
- Normalize accepted uppercase UUID input to lowercase canonical form.
- Recommend UUID v4 for client-provided ids, but do not require version 4.
- If existing state has a different canonical `cwd` from the request, fail closed with `session_cwd_mismatch`.
- Pass the generated id to first-run Claude Code as `--session-id <session_id>`.
- Reuse tmux session name `ctc-csess-<session_id>`.
- If known state or a matching transcript exists and the tmux session is inactive, restart Claude Code with `--resume <session_id>`.
- If the client provides a UUID that has no known state/transcript yet, launch Claude Code with `--session-id <session_id>`.
- Avoid `:` in tmux session names because tmux uses it as a target separator.
- Treat `tmux has-session` as the source of truth for whether a session is currently active.
- Treat Claude Code transcript JSONL as the structured source for answer text, thinking, tool_use, tool_result, usage, and timestamps.
- Treat `tmux capture-pane` as the rendered-screen source for prompt and visual status.
- Combine transcript state and rendered screen state for completion detection.
- Launch Claude Code with `--dangerously-skip-permissions` by default.
- Pass caller-provided OAuth tokens into new tmux sessions as `CLAUDE_CODE_OAUTH_TOKEN`.
- Select account tokens at process invocation time, for example by setting different source env variables per request.
- Store lightweight local session state only for operational hints such as last prompt and cwd.
- Use local state for transcript cursoring, turn anchors, locks, usage totals, and efficient streaming.
- Do not rely on local state as the authoritative session mapping; derive tmux session name from bridge `session_id`.
- Store transcript path/file identity and offsets to avoid repeatedly reading the whole JSONL file.
- Capture transcript baseline before any send/start/resume action.
- Anchor a new turn by `before_send_transcript.offset` or `before_send_wall_time_utc` first, using prompt matching only as unambiguous fallback.
- Split locking into short `send_lock`, state-write lock, and durable `active_turn`; do not rely on one whole-stream lock.
- Store state `generation`, writer metadata, active turn owner pid/host, heartbeat, and stream epoch to make reconnect/takeover deterministic.
- Treat stream delivery as at-least-once until a future client acknowledgement protocol exists. Every streamed event must have stable `turn_id`, deterministic `event_id`, source start/end offset, and block ordinal metadata so clients can deduplicate replay.
- Support attach/reconnect semantics before allowing web clients to reconnect to an in-progress turn.
- Default high-level web stream polling interval is 2.0 seconds.
- Resolve transcript by cwd-specific Claude project directory, pinned file identity, and Claude transcript `sessionId` when available. Prompt matching is only a diagnostic fallback and must fail closed if ambiguous.
- Ignore internal Claude Code hook prompts such as session-summary prompts when extracting user turns.
- Add machine-readable output options for service integration.
- Add commands for session lifecycle:
  - create/reuse by session id
  - list active sessions
  - inspect status
  - send input
  - wait until ready
  - get latest answer
  - get latest turn
  - terminate session
  - reap idle sessions
- Idle tracking should be based on last observed transcript or tmux activity timestamp, not only process age.

## Proposed CLI Contract

Existing commands remain:

- `start`
- `launch`
- `send`
- `status`
- `wait-ready`
- `events`
- `answer`
- `turn`
- `stream`
- `capture`
- `follow`
- `kill`
- `reap`

High-level `stream [--session-id <id>] --cwd <path> <prompt...>` contract:

- Emits one JSON object per line.
- Accepts the user prompt for one conversation turn.
- Generates a UUID `session_id` when omitted.
- Uses tmux session name `ctc-csess-<session_id>` internally.
- If tmux session is inactive and this is a new session, launches Claude Code with `--session-id <session_id> <prompt>`.
- If tmux session is inactive and known state or a matching transcript exists, launches Claude Code with `--resume <session_id> <prompt>`.
- If tmux session is active, sends the prompt to the existing Claude Code process.
- Emits normalized events: `user`, `thinking`, `tool_use`, `tool_result`, `assistant_text`.
- Emits `done` and exits `0` only after the target turn is complete.
- Does not treat `tool_use`, `tool_result`, or thinking-only assistant events as complete.
- Does not complete a subagent flow until the `Task` tool result is followed by final assistant text.
- Combines transcript readiness with tmux screen readiness and a short idle window.
- Emits final turn metrics after `done` as a separate `metrics` event.
- Provides `cancel <session_id>` to send `Escape` to the underlying Claude Code tmux pane without sending a new prompt.
- Provides `last <session_id> --last N` / `replay <session_id> --last N` to re-emit completed turn JSONL events for clients that disconnected after a turn finished.
- If the newest turn is still active, `last`/`replay` attaches to the active turn and streams until completion without sending a new prompt.
- If Claude Code records a cancelled tool use as `User rejected tool use` plus `[Request interrupted by user ...]`, treat it as a completed cancelled turn and emit final `done`/`metrics` even when no final assistant text exists.
- Current final metrics include model, input tokens, cache read tokens, cache write tokens, output tokens, context fields only when present in the transcript, and pricing-table-based estimated turn cost when model/usage can be resolved.
- Do not estimate or synthesize `metrics.context` from token usage when transcript context fields are absent.
- Elapsed time and estimated session cumulative cost are emitted by the CLI in the final `metrics` event when the turn reaches `done`.
- Mid-stream metrics are optional best-effort events only when new transcript events already contain usage/context/model fields.

Proposed additions and high-level commands:

- `stream [--session-id <id>] --cwd <path> <prompt...>`
  - Primary service-facing command for web chat.
  - Ensures/reuses/resumes the session, sends the prompt, streams one full turn, then emits final metrics.
- internal `ensure <session-id> --cwd <path>`
  - If `session-id` is omitted, generate a UUID v4.
  - If `session-id` is provided, validate it as a canonical hyphenated UUID string and normalize accepted uppercase input to lowercase. UUID v4 is recommended but not required.
  - Use tmux session name `ctc-csess-<session-id>`.
  - If the tmux session exists, reuse it.
  - If this is a new session, create tmux session and launch Claude Code with `--session-id <session-id>`.
  - If this is a known session but tmux is inactive, create tmux session and launch Claude Code with `--resume <session-id>`.
  - Output resolved session metadata.
- `ask [--session-id <id>] --cwd <path> <prompt...>`
  - Ensure active session.
  - Send prompt.
  - Wait until the full turn is done and return final answer plus metrics without streaming progress events.
- `list`
  - List active tmux sessions controlled by this CLI.
- `info <session-id>`
  - Print tmux session name, active status, cwd, transcript path, latest Claude transcript sessionId when available.
Implemented lifecycle commands:

- `kill <session-id>`
  - Terminate a specific tmux session.
- `reap --idle-seconds <n>`
  - Terminate controlled sessions with no observed activity after timeout.
  - Default controlled session prefix is `ctc-`.
  - Sessions with a working transcript state are skipped.

Suggested machine-readable output:

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "tmux_session": "ctc-csess-550e8400-e29b-41d4-a716-446655440000",
  "active": true,
  "state": "working",
  "transcript_path": "...",
  "claude_session_id": "550e8400-e29b-41d4-a716-446655440000",
  "latest_event_timestamp": "2026-05-15T08:34:25.005Z"
}
```

Current final metrics event:

```json
{
  "event": "metrics",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "turn_id": "turn_20260516_0001",
  "event_id": "turn_20260516_0001:metrics:82300",
  "source_offset": 82300,
  "source_end_offset": 82300,
  "block_index": -1,
  "scope": "turn_final",
  "model": "claude-sonnet-4-5-20250929",
  "usage": {
    "input_tokens": 12000,
    "cache_read_tokens": 8000,
    "cache_write_tokens": 500,
    "output_tokens": 1400
  },
  "cost": {
    "estimated": true,
    "currency": "USD",
    "pricing_version": "anthropic-2026-05-18",
    "model": "claude-sonnet-4.6",
    "model_match": "exact",
    "cache_write_ttl": "1h",
    "turn_usd": 0.0624
  }
}
```

Synthetic `done` and final `metrics` ids are deterministic:

```text
<turn_id>:done:<completed_offset>
<turn_id>:metrics:<completed_offset>
```

They use `completed_offset` as both `source_offset` and `source_end_offset`, with `block_index = -1`.

## Status Detection Model

The bridge should expose these states:

- `starting`: tmux session exists but Claude Code transcript has not appeared yet.
- `ready`: latest user turn has completed assistant text and the rendered screen is input-ready.
- `working`: latest user turn has no completed assistant text yet, or the latest transcript event is user/tool_use/tool_result/thinking.
- `needs_confirmation`: rendered screen or transcript indicates permission/confirmation is needed.
- `inactive`: tmux session is missing.
- `unknown`: available evidence is insufficient.

Completion should not be decided from the `❯` prompt glyph alone. Claude Code can show that glyph while a response is still being recorded. The transcript must be consulted.

## Transcript Source Notes

Claude Code writes JSONL records under directories such as:

- `~/.claude/projects/<encoded-cwd>/*.jsonl`
- `~/.claude/transcripts/*.jsonl`

The useful event shapes include:

- user prompt
- assistant thinking
- assistant tool_use
- user tool_result
- assistant text
- attachment
- system metadata
- usage data
- sessionId, cwd, version, gitBranch when available

Transcript writes are asynchronous. Immediately after sending a prompt, the prompt may not yet be present in the file. During that race window the bridge should report `working`, not `ready`.

## Local Storage And Cursoring

High-level web chat uses a small local JSON state store under:

```text
~/.cache/claude-tmux-control/
```

The state store is used for:

- session metadata
- tmux session name derived from `session_id`
- transcript path/file identity
- transcript read offset
- current turn start/current/emitted offsets
- prompt hash and preview
- short send lock metadata
- durable active turn state
- usage totals and estimated session cumulative cost
- state generation and writer metadata
- active turn owner, heartbeat, takeover epoch, and failure state

This storage is not an external mapping database.

The canonical identity remains the bridge `session_id`, and tmux session name is derived as:

```text
ctc-csess-<session_id>
```

For each new high-level `stream` call, the bridge records transcript file identity, offset, and wall-clock timestamp before sending the prompt. It then finds the current turn in this order:

```text
1. existing turn cursor in state
2. first user event after before_send_transcript.offset
3. first user event after before_send_wall_time_utc
4. latest user event matching prompt hash/text, only if unambiguous
```

If no transcript exists before first launch, the bridge records an explicit `no_transcript_baseline` with the pre-send wall-clock timestamp. That fallback may only select events after that timestamp.

After the target turn is anchored, the stream loop should read only new JSONL bytes from the last stored offset.

Transcript rotation or truncation is detected by file identity or file size moving backwards, then the transcript is resolved again. If the transcript lacks Claude `sessionId`, recovery must prove the file lineage belongs to the active bridge session or fail closed with an ambiguous transcript error.

Cursor fields must stay separate:

```text
transcript.scan_offset
turn.anchor_start_offset
turn.anchor_end_offset
turn.replay_start_offset
turn.read_offset
turn.last_stdout_flushed_offset
turn.completed_offset
```

If reconnect happens in v1, replay from conservative `replay_start_offset`.

`last_stdout_flushed_offset` is diagnostic only because stdout flush does not prove that the app server or browser received the event.

Only a future acknowledgement protocol may advance replay past the conservative anchor.

State writes use a dedicated state-write lock plus `generation` compare/update. `send_lock` only covers ensure/start/resume/send. The stream owner keeps `active_turn.heartbeat_at` fresh; reconnect may attach read-only, and takeover is allowed only when the owner process is gone or the heartbeat lease is stale.

`timeout` and `failed` events do not mean the session is ready for another prompt. They leave `active_turn` in place with `claude_state` as `working` or `unknown` until attach/inspect/kill confirms the prior turn ended.

`done` is the answer-completion event. It must not carry usage/context/cost summary fields. Final usage and cost are emitted immediately after `done` as a separate `metrics` event for the same `turn_id`.

## Testing Decisions

- Test `tmux` command construction with a fake runner.
- Test transcript path resolution using temporary JSONL files.
- Test that session-scoped transcript selection does not accidentally pick another active Claude session.
- Test that prompt matching looks at user events, not arbitrary tool output files containing the prompt string.
- Test that internal session-summary prompts are ignored for `answer` and `turn`.
- Test status transitions:
  - prompt sent but not recorded yet -> `working`
  - latest user only -> `working`
  - latest assistant thinking -> `working`
  - latest tool_use/tool_result -> `working`
  - latest assistant text + screen ready -> `ready`
  - confirmation screen -> `needs_confirmation`
- Test `answer --tail N` and `turn --tail N`.
- Test state generation conflict handling and writer metadata preservation.
- Test active turn heartbeat, stale owner takeover, and attach without prompt send.
- Test at-least-once replay using stable `event_id` and source offsets.
- Test that `timeout` or `failed` does not allow a new prompt until the prior turn is resolved.
- Test that `done` and `metrics` are separate events with the same `turn_id`.
- Add smoke tests using a harmless shell command in tmux.

## Out of Scope

- Replacing Claude Code with a direct API client.
- Parsing every possible Claude Code UI glyph perfectly.
- Guaranteeing compatibility with future private transcript schema changes.
- Multi-user authentication and authorization in the CLI itself.
- Multi-server routing, transcript replication, and cross-host resume. The first version assumes one server owns local tmux sessions and Claude transcripts.
- Web server implementation. This PRD covers the CLI bridge needed by a web server or another program.

## Further Notes

The long-term integration should treat this CLI as a process boundary. A web server can call the CLI and parse JSON output first, then later replace the shell boundary with an internal Python module or daemon if performance requires it.

The main reliability risk is transcript selection. The safest path is to combine:

- app-server-provided canonical UUID or CLI-generated UUID v4 session id
- tmux session name `ctc-csess-<session_id>`
- cwd-specific Claude project transcript directory
- transcript file identity and offset/time baseline captured before send/start/resume
- Claude transcript `sessionId` verification when available
- prompt matching only as an unambiguous diagnostic fallback
