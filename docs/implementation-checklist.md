# Implementation Checklist

## 1. Source Model

- [x] 입력 전달은 `tmux load-buffer` + `paste-buffer` + `send-keys Enter`로 처리한다.
- [x] 응답 본문은 Claude Code transcript JSONL의 assistant `text` block에서 읽는다.
- [x] tool call은 transcript JSONL의 assistant `tool_use` block에서 읽는다.
- [x] tool result는 transcript JSONL의 user `tool_result` block에서 읽는다.
- [x] thinking은 transcript JSONL의 assistant `thinking` block에서 읽는다.
- [x] usage/token 정보는 transcript JSONL의 `usage` field가 있을 때만 표시한다.
- [ ] `status` 기본 판정을 transcript-first로 단순화한다.
- [ ] `tmux capture-pane` 기반 상태 판정은 fallback/debug 옵션으로 분리한다.

## 2. Session Identity

- [x] canonical `session_id` 소유자는 bridge로 정의한다.
- [x] 새 대화의 `session_id`는 UUID v4로 정의한다.
- [x] tmux session naming 규칙을 `ctc-csess-<session_id>`로 정의한다.
- [x] `:`는 tmux target separator라서 session name에서 제외한다.
- [x] high-level `stream`에서 `session_id`가 없으면 UUID를 생성한다.
- [x] high-level `stream` 첫 Claude Code 시작 시 `--session-id <session_id>`를 전달한다.
- [x] high-level `stream`에서 같은 `session_id`가 이미 active tmux session이면 재사용한다.
- [x] high-level `stream`에서 같은 `session_id`가 inactive이고 기존 state/transcript가 있으면 새 tmux session을 만들고 `--resume <session_id>`로 Claude Code를 실행한다.
- [x] `session_id -> tmux session` 매핑은 tmux session name에서 직접 복원한다.
- [ ] Claude transcript 내부 `sessionId`를 추출해서 `info` 출력에 포함한다.
- [x] high-level `stream` JSON 응답에 `session_id`를 포함한다.

## 3. CLI Contract

- [x] `start`: 새 tmux session 생성 또는 기존 session 재사용
- [x] `start`: `CLAUDE_CODE_OAUTH_TOKEN` source env를 새 tmux session에 주입
- [x] `start`/`launch`/`chat`: Claude Code command에 `--dangerously-skip-permissions` 기본 적용
- [x] `send`: tmux session에 prompt 전달
- [x] `status`: working/ready 상태 확인
- [x] `wait-ready`: 완료까지 대기
- [x] `events <session>`: session-scoped transcript event 조회
- [x] `answer <session>`: 최신 assistant text 조회
- [x] `answer <session> --tail N`: 최근 N개 assistant text 조회
- [x] `turn <session>`: 최신 turn의 user/thinking/tool/text 조회
- [x] `turn <session> --tail N`: 최근 N개 turn 조회
- [x] low-level `stream <tmux-session>`: 최신 turn을 JSONL로 streaming하고 완료 시 `done` 후 종료
- [x] high-level `stream [--session-id] --cwd <path> <prompt>`: UUID 생성/session 생성/재사용/resume/prompt 전송/turn stream
- [ ] internal `ensure [session-id] --cwd <path>`: UUID 생성 또는 session 생성/재사용/resume 단계 구현
- [ ] `ask [--session-id] --cwd <path> <prompt>`: streaming 없이 완료 후 answer/metrics 출력
- [ ] `list`: active controlled session 목록 조회
- [ ] `info <session-id>`: tmux/transcript/Claude session metadata 조회
- [x] `kill <session-id>`: 특정 session 종료
- [x] `reap --idle-seconds N`: 오래된 inactive session 정리

## 4. Machine-Readable Output

- [ ] 모든 service-facing command에 `--json` 옵션을 제공한다.
- [ ] `status --json` schema를 정의한다.
- [ ] `answer --json` schema를 정의한다.
- [ ] `turn --json` schema를 정의한다.
- [ ] `events --json`은 raw event와 normalized event 중 어떤 모드인지 명확히 분리한다.
- [ ] error response schema를 정의한다.
- [ ] exit code 규칙을 문서화한다.
- [x] client-provided `session_id` UUID validation을 구현한다.
- [x] existing state `cwd`와 request `cwd` mismatch를 `session_cwd_mismatch`로 fail closed한다.

## 5. Local Storage And Cursoring

- [x] local storage plan 문서를 작성한다.
- [x] high-level stream용 session state schema v1을 구현한다.
- [x] state 저장 위치를 `~/.cache/claude-tmux-control/sessions/<session_id>.json`로 정리한다.
- [x] atomic JSON write helper를 구현한다.
- [x] state generation/writer_pid/updated_at metadata를 구현한다.
- [ ] state-write lock과 generation compare/update retry를 구현한다.
- [x] 짧은 `send_lock` file을 구현한다.
- [x] durable `active_turn` state를 구현한다.
- [x] `active_turn`에 owner_pid/owner_hostname/heartbeat_at/stream_epoch를 저장한다.
- [x] stale owner heartbeat recovery 정책을 구현한다.
- [ ] `stream --attach --session-id <id>` reconnect mode를 정의하고 구현한다.
- [x] high-level stream 기본 polling interval을 2.0초로 둔다.
- [x] transcript file identity(path/st_dev/st_ino/size/mtime_ns)를 state에 저장한다.
- [x] transcript offset 기반 tail reader를 구현한다.
- [x] cursor 필드를 `anchor_start_offset`, `anchor_end_offset`, `replay_start_offset`, `read_offset`, `last_stdout_flushed_offset`, `completed_offset`로 분리한다.
- [x] prompt hash와 prompt preview를 state에 저장한다.
- [x] send/start/resume 전에 transcript baseline을 캡처한다.
- [x] transcript가 없는 첫 실행은 `no_transcript_baseline`과 pre-send wall-clock timestamp를 저장한다.
- [x] 다음 turn 시작점을 before_send_transcript.offset 우선으로 찾는다.
- [ ] offset 실패 시 before_send_wall_time_utc로 user event를 찾는다.
- [ ] time fallback 실패 시 prompt hash/text matching을 사용하되 ambiguity면 fail closed한다.
- [x] transcript rotation/truncation을 감지하고 재탐색한다.
- [ ] missing sessionId rotation recovery matrix를 구현한다.
- [ ] completed turn metrics를 turn_id/source offset 기준으로 저장한다.
- [ ] session cumulative usage/cost totals는 completed turn records에서 재계산한다.
- [x] streamed event에 stable `turn_id`, `event_id`, `source_offset`, `source_end_offset`, `block_index`를 포함한다.
- [x] crash/reconnect replay는 at-least-once로 처리하고 client dedupe key를 문서화한다.
- [x] `done` event에는 answer/completion 정보만 담고 usage/context/cost를 넣지 않는다.
- [x] `metrics` event는 `done` 직후 같은 `turn_id`로 별도 출력한다.
- [x] `timeout`/`failed` 후 stale recovery 전까지 새 prompt를 막는다.

## 6. Lifecycle And Idle Cleanup

- [x] session activity timestamp 기준을 정의한다.
- [ ] transcript latest timestamp를 activity source로 사용한다.
- [ ] transcript가 아직 없으면 tmux session creation time 또는 local state timestamp를 사용한다.
- [x] `reap`이 controlled session만 종료하도록 prefix guard를 둔다.
- [ ] `kill`은 uncontrolled tmux session을 실수로 종료하지 않도록 guard한다.
- [ ] stuck/unknown session 처리 정책을 정의한다.

## 7. Transcript Resolution

- [x] cwd-specific Claude project transcript directory를 우선 탐색한다.
- [x] 마지막 prompt가 user event에 들어있는 transcript를 우선 선택한다.
- [x] tool output 파일에 prompt 문자열이 들어간 경우를 transcript 후보에서 배제한다.
- [x] internal session-summary prompt를 `answer`/`turn`에서 제외한다.
- [ ] transcript 내부 Claude `sessionId`를 추출하고 CLI session id와 함께 보여준다.
- [ ] transcript가 rotate/new file로 바뀌는 경우 follow mode가 새 파일로 넘어가도록 한다.

## 8. Status Detection

- [x] prompt 전송 직후 transcript race window를 `working`으로 처리한다.
- [x] latest user only 상태를 `working`으로 처리한다.
- [x] latest assistant thinking 상태를 `working`으로 처리한다.
- [x] latest tool_use/tool_result 상태를 `working`으로 처리한다.
- [x] latest assistant text 상태를 `ready` 후보로 처리한다.
- [ ] `ready` 판정에서 tmux screen dependency를 제거하거나 fallback으로 낮춘다.
- [ ] confirmation/permission 상태를 transcript와 screen 양쪽에서 검출한다.
- [ ] `starting`, `inactive`, `unknown` 상태를 명확히 분리한다.

## 9. External Integration Readiness

- [x] 웹 서버가 CLI를 호출할 때 필요한 command examples를 문서화한다.
- [x] CLI manual에 quick start, command reference, integration recipe, troubleshooting을 문서화한다.
- [x] ChatGPT-style 웹 채팅 연동 가이드를 별도 문서로 작성한다.
- [x] OAuth token을 호출 프로세스 env로 받고 `CLAUDE_CODE_OAUTH_TOKEN`으로 Claude Code에 전달한다.
- [x] 복수 계정 token은 `--oauth-token-env <SOURCE_ENV>`로 호출 시점에 선택한다.
- [x] Claude Code를 기본적으로 `--dangerously-skip-permissions`로 실행해 dynamic approval prompt를 피한다.
- [x] concurrent requests가 같은 session에 동시에 prompt를 보내는 경우의 lock 정책을 문서화한다.
- [x] session별 send lock을 구현한다.
- [x] streaming UI를 위한 low-level `stream <tmux-session>` JSONL 출력 포맷을 정의하고 구현한다.
- [x] 웹 채팅 주력 high-level `stream [--session-id] --cwd <path> <prompt>` 계약을 구현한다.
- [x] `stream` 완료 후 별도 `metrics` event로 model/usage/context와 cost unavailable marker를 출력한다.
- [x] final metrics usage에는 input/output/cache_read/cache_write tokens를 포함한다.
- [ ] final metrics elapsed_ms를 CLI에서 직접 출력한다.
- [ ] final metrics cost에는 estimated turn USD와 estimated session cumulative USD를 포함한다.
- [ ] raw transcript에서 model/usage/context를 추출하는 machine-readable stats 명령을 추가한다.
- [ ] 중간 metrics는 transcript event에 usage/context/model이 있는 경우에만 best-effort 옵션으로 제공한다.
- [ ] long-running command timeout 정책을 정의한다.
- [x] Linux server에서 `TERM=xterm-256color` fallback 사용법을 문서화한다.

## 10. Testing

- [x] fake runner 기반 tmux command construction test
- [x] transcript path resolution test
- [x] session-scoped event lookup test
- [x] answer extraction test
- [x] turn formatting test
- [x] stream JSONL normalization test
- [x] stream completion test: final assistant text + ready screen + idle window
- [x] stream subagent wait test: `tool_result` 이후 final text 전에는 종료하지 않음
- [x] internal prompt ignore test
- [x] status race handling test
- [x] high-level `stream` command test
- [ ] internal `ensure` flow test
- [x] offset 기반 tail reader test
- [x] baseline이 send/start/resume 전에 캡처되는 race test
- [x] repeated prompt가 있어도 before_send_transcript.offset 기준으로 target turn을 잡는 test
- [ ] ambiguous prompt fallback fail-closed test
- [x] transcript rotation/truncation recovery test
- [x] rotation candidate가 sessionId를 아직 포함하지 않는 경우의 fallback test
- [x] short `send_lock` stale recovery test
- [x] active_turn blocks second prompt test
- [ ] state generation conflict retry test
- [x] stale owner heartbeat recovery test
- [ ] attach/reconnect without sending prompt test
- [ ] crash after parse before emit replay test
- [ ] crash after emit before state write replay/dedup policy test
- [x] stable event_id/source_offset/source_end_offset/block_index client dedupe test
- [ ] metrics deduplication by turn_id/source offset test
- [x] done and metrics event separation test
- [x] timeout/failed keeps active_turn until stale recovery test
- [ ] `ask` command test
- [ ] `list/info` command test
- [x] `kill/reap` command test
- [ ] JSON output contract test
- [x] idle cleanup test
