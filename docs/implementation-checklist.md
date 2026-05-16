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
- [ ] `session_id`가 없으면 UUID를 생성하는 `ensure` 흐름을 구현한다.
- [ ] 첫 Claude Code 시작 시 `--session-id <session_id>`를 전달한다.
- [ ] 같은 `session_id`가 이미 active tmux session이면 재사용한다.
- [ ] 같은 `session_id`가 inactive면 새 tmux session을 만들고 `--resume <session_id>`로 Claude Code를 실행한다.
- [ ] `session_id -> tmux session` 매핑은 tmux session name에서 직접 복원한다.
- [ ] Claude transcript 내부 `sessionId`를 추출해서 `info` 출력에 포함한다.
- [ ] 모든 service-facing JSON 응답에 `session_id`를 포함한다.

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
- [x] `stream <session>`: 최신 turn을 JSONL로 streaming하고 완료 시 `done` 후 종료
- [ ] `ensure [session-id] --cwd <path>`: UUID 생성 또는 session 생성/재사용/resume
- [ ] `ask <session-id> <prompt>`: ensure + send + optional wait + answer/turn 출력
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

## 5. Lifecycle And Idle Cleanup

- [x] session activity timestamp 기준을 정의한다.
- [ ] transcript latest timestamp를 activity source로 사용한다.
- [ ] transcript가 아직 없으면 tmux session creation time 또는 local state timestamp를 사용한다.
- [x] `reap`이 controlled session만 종료하도록 prefix guard를 둔다.
- [ ] `kill`은 uncontrolled tmux session을 실수로 종료하지 않도록 guard한다.
- [ ] stuck/unknown session 처리 정책을 정의한다.

## 6. Transcript Resolution

- [x] cwd-specific Claude project transcript directory를 우선 탐색한다.
- [x] 마지막 prompt가 user event에 들어있는 transcript를 우선 선택한다.
- [x] tool output 파일에 prompt 문자열이 들어간 경우를 transcript 후보에서 배제한다.
- [x] internal session-summary prompt를 `answer`/`turn`에서 제외한다.
- [ ] transcript 내부 Claude `sessionId`를 추출하고 CLI session id와 함께 보여준다.
- [ ] transcript가 rotate/new file로 바뀌는 경우 follow mode가 새 파일로 넘어가도록 한다.

## 7. Status Detection

- [x] prompt 전송 직후 transcript race window를 `working`으로 처리한다.
- [x] latest user only 상태를 `working`으로 처리한다.
- [x] latest assistant thinking 상태를 `working`으로 처리한다.
- [x] latest tool_use/tool_result 상태를 `working`으로 처리한다.
- [x] latest assistant text 상태를 `ready` 후보로 처리한다.
- [ ] `ready` 판정에서 tmux screen dependency를 제거하거나 fallback으로 낮춘다.
- [ ] confirmation/permission 상태를 transcript와 screen 양쪽에서 검출한다.
- [ ] `starting`, `inactive`, `unknown` 상태를 명확히 분리한다.

## 8. External Integration Readiness

- [x] 웹 서버가 CLI를 호출할 때 필요한 command examples를 문서화한다.
- [x] CLI manual에 quick start, command reference, integration recipe, troubleshooting을 문서화한다.
- [x] ChatGPT-style 웹 채팅 연동 가이드를 별도 문서로 작성한다.
- [x] OAuth token을 호출 프로세스 env로 받고 `CLAUDE_CODE_OAUTH_TOKEN`으로 Claude Code에 전달한다.
- [x] 복수 계정 token은 `--oauth-token-env <SOURCE_ENV>`로 호출 시점에 선택한다.
- [x] Claude Code를 기본적으로 `--dangerously-skip-permissions`로 실행해 dynamic approval prompt를 피한다.
- [ ] concurrent requests가 같은 session에 동시에 prompt를 보내는 경우의 lock 정책을 정의한다.
- [ ] session별 send lock을 구현한다.
- [x] streaming UI를 위한 `stream <session>` JSONL 출력 포맷을 정의하고 구현한다.
- [ ] `stream` 완료 후 별도 `metrics` event로 elapsed/model/usage/context/cost summary를 출력한다.
- [ ] final metrics usage에는 input/output/cache_read/cache_write tokens를 포함한다.
- [ ] final metrics cost에는 estimated turn USD와 estimated session cumulative USD를 포함한다.
- [ ] raw transcript에서 model/usage/context를 추출하는 machine-readable stats 명령을 추가한다.
- [ ] 중간 metrics는 transcript event에 usage/context/model이 있는 경우에만 best-effort 옵션으로 제공한다.
- [ ] long-running command timeout 정책을 정의한다.
- [x] Linux server에서 `TERM=xterm-256color` fallback 사용법을 문서화한다.

## 9. Testing

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
- [ ] `ensure` command test
- [ ] `ask` command test
- [ ] `list/info` command test
- [x] `kill/reap` command test
- [ ] JSON output contract test
- [x] idle cleanup test
