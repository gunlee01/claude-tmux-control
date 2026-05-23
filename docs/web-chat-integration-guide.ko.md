# Web Chat Integration Guide

[English](./web-chat-integration-guide.md) | [한국어](./web-chat-integration-guide.ko.md)

이 문서는 웹 서버나 다른 프로그램이 `claude_tmux_control.py`를 호출해서 ChatGPT 페이지처럼 Claude Code와 대화형 UI를 만드는 방법을 정의합니다.

목표는 한 웹 대화창이 하나의 Claude Code session을 계속 재사용하고, 각 turn의 진행 이벤트와 최종 답변, usage/cost 정보를 UI에 보여주는 것입니다.

## 1. Integration Contract

### 핵심 원칙

한 웹 대화창은 하나의 bridge `session_id`를 가집니다.

운영 웹앱에서는 앱 서버가 UUID를 먼저 생성해 `--session-id`로 넘기는 방식을 권장합니다.

첫 메시지에서 `--session-id`를 생략해도 됩니다.

생략하면 CLI가 새 UUID를 만들고, Claude Code 첫 실행에 같은 UUID를 `--session-id`로 전달합니다. 클라이언트는 첫 stream event의 `session_id`를 저장해서 이후 요청에 다시 넘깁니다.

클라이언트가 이후 요청에 `session_id`를 보내면 bridge는 같은 tmux session을 재사용합니다.

tmux session이 없고 기존 state 또는 matching transcript가 있으면 Claude Code를 `--resume <session_id>`로 다시 실행합니다.

기존 state/transcript가 없는 새 client-provided `session_id`라면 `--session-id <session_id>`로 새 Claude Code session을 시작합니다.

권장 규칙:

```text
session_id:   <client-or-cli-generated-uuid>
tmux_session: ctc-csess-<session_id>
```

예:

```text
session_id   = 550e8400-e29b-41d4-a716-446655440000
tmux_session = ctc-csess-550e8400-e29b-41d4-a716-446655440000
```

`:`는 tmux target 문법에서 `session:window.pane` 구분자로 쓰이므로 session name에 넣지 않습니다.

session id 형식:

```text
canonical hyphenated UUID string
```

클라이언트가 `session_id`를 보내도 bridge는 UUID 형식을 검증해야 합니다.

UUID가 아니면 state path나 tmux session name에 사용하지 않고 요청을 거절합니다.

CLI가 `session_id`를 생성할 때는 UUID v4를 사용합니다.

대문자 UUID 입력은 허용되며 bridge 내부에서는 lowercase canonical form으로 정규화합니다.

클라이언트가 직접 생성할 때도 UUID v4를 권장하지만, bridge는 version 4 여부까지 강제하지 않습니다.

기존 state가 있는 `session_id`에 다른 `cwd`가 들어오면 canonical path 기준으로 비교한 뒤 `session_cwd_mismatch`로 fail closed합니다.

### Command Boundary

웹 서버가 직접 호출하는 high-level 계약은 아래 명령입니다.

| 목적 | CLI 명령 | 인자 의미 |
| --- | --- | --- |
| 대화 한 턴 실행 | `stream --cwd <path> [--session-id <uuid>] [--model MODEL] [--claude-args "ARGS"] "<prompt>"` | bridge `session_id` |
| 진행 중 turn 재연결 | `stream --attach --session-id <uuid>` | bridge `session_id` |
| 진행 중 turn 취소 | `cancel <uuid>` | bridge `session_id` |
| 완료된 turn replay | `last <uuid> --last <n>` 또는 `replay <uuid> --last <n>` | bridge `session_id` |
| 최종 결과만 실행 | `ask --cwd <path> [--session-id <uuid>] [--model MODEL] [--claude-args "ARGS"] "<prompt>"` | bridge `session_id` |
| session metadata 조회 | `info <uuid> --json` | bridge `session_id` |
| session 목록 조회 | `list --json` | high-level controlled sessions |
| 오래된 web process 정리 | `reap --idle-seconds <n> --prefix ctc-csess-` | high-level web tmux prefix |

아래 명령은 사람이 직접 tmux session을 다루는 low-level debug/smoke 전용입니다.

| 목적 | CLI 명령 | 인자 의미 |
| --- | --- | --- |
| 직접 tmux session 시작 | `start <tmux_session>` | tmux session name. 예: `work` |
| 직접 prompt 입력 | `send <tmux_session> "<prompt>"` | tmux session name |
| raw event 조회 | `events <tmux_session> --json` | tmux session name |
| 최종 답변만 조회 | `answer <tmux_session>` | tmux session name |
| 전체 turn 조회 | `turn <tmux_session>` | tmux session name |
| 특정 process 종료 | `kill <tmux_session>` | tmux session name |

웹 클라이언트는 `answer "$SESSION_ID"`나 `kill "$SESSION_ID"`를 호출하지 않습니다.

웹 UI의 답변 본문은 `stream`의 `done.answer` 또는 `ask`의 `ask_result.answer`를 사용합니다. 취소되었거나 tool 실행 중 interrupt된 turn은 final answer 없이 `done`/`metrics`만 올 수 있습니다.

`kill <tmux_session>`은 process stop입니다. high-level bridge state나 Claude transcript를 지우는 대화 삭제 명령이 아닙니다.

## 2. Primary Stream Flow

웹 서버가 주로 사용할 고수준 명령은 `stream`입니다.

`stream`은 prompt를 받아서 한 대화 turn을 실행하고, 진행 이벤트를 JSONL로 출력한 뒤 `done`과 final `metrics`까지 내보냅니다.

새 대화:

```bash
TERM=xterm-256color \
ctc stream \
  --cwd "$PROJECT_DIR" \
  "$USER_PROMPT"
```

기존 대화:

```bash
TERM=xterm-256color \
ctc stream \
  --session-id "$SESSION_ID" \
  --cwd "$PROJECT_DIR" \
  "$USER_PROMPT"
```

Claude Code 실행 파일은 항상 `claude`입니다.

model 선택은 `--model MODEL`을 씁니다.

신뢰된 추가 Claude Code option은 `--claude-args "ARGS"`로 전달합니다.

```bash
TERM=xterm-256color \
ctc stream \
  --session-id "$SESSION_ID" \
  --cwd "$PROJECT_DIR" \
  --model opus \
  --claude-args "--add-dir ../shared" \
  "$USER_PROMPT"
```

이 옵션들은 bridge가 Claude Code process를 새로 만들거나 resume할 때만 적용됩니다.

tmux session이 이미 있으면 기존 process의 model/argument가 유지됩니다.

`--claude-args`는 운영자가 통제하는 값으로만 쓰고, 신뢰할 수 없는 browser client에 raw argument 입력창으로 노출하지 마세요.

### Permission Mode

새 Claude Code process는 기본적으로 `--dangerously-skip-permissions`로 실행됩니다.

이 기본값은 non-interactive service flow에는 맞지만 위험합니다. Claude Code가 action별 승인 없이 tool을 실행할 수 있습니다.

client/backend가 이 동작을 바꾸려면 Claude process 시작 시점에 신뢰된 `--claude-args`로 Claude Code permission option을 전달합니다.

```bash
TERM=xterm-256color \
ctc stream \
  --session-id "$SESSION_ID" \
  --cwd "$PROJECT_DIR" \
  --claude-args "--permission-mode plan" \
  "$USER_PROMPT"
```

제품에서 사용자에게 노출해야 한다면 `permissionMode=plan` 같은 application-level setting으로 받고, 서버에서 `--claude-args "--permission-mode ..."`로 매핑하세요.

browser에서 받은 임의 문자열을 `--claude-args`에 그대로 넣지 마세요.

계정별 OAuth token을 써야 하면 요청 처리 프로세스에서 source env를 선택합니다.

```bash
ACCOUNT_A_TOKEN="$TOKEN" \
TERM=xterm-256color \
ctc stream \
  --session-id "$SESSION_ID" \
  --cwd "$PROJECT_DIR" \
  --oauth-token-env ACCOUNT_A_TOKEN \
  "$USER_PROMPT"
```

Claude Code에 project별 secret이 필요하면 secret 값을 command argument로 직접 넣지 말고 `<project>/.ctc.env` 또는 `--env NAME`을 사용합니다.

```bash
SERVICE_API_KEY="..." \
ctc stream \
  --cwd "$PROJECT_DIR" \
  --env SERVICE_API_KEY \
  "$USER_PROMPT"
```

`<project>/.ctc.env`가 있고 명시적 `--env-file`이 없으면 `ctc`는 새 tmux session 생성 시 이 파일을 읽습니다. 이미 실행 중인 session에는 env 변경이 반영되지 않으므로, 변경된 env를 쓰려면 tmux session을 종료하거나 reap한 뒤 다시 시작해야 합니다.

`CLAUDE_CODE_OAUTH_TOKEN`은 `--oauth-token-env` 전용이며 `.ctc.env`와 `--env`에서는 설정할 수 없습니다.

고수준 `stream` 내부 동작:

```text
if session_id is empty:
  generate UUID
  tmux_session = ctc-csess-<uuid>
  start Claude Code with --session-id <uuid> "<prompt>"

else if tmux ctc-csess-<session_id> exists:
  send "<prompt>" to the active Claude Code process

else:
  tmux_session = ctc-csess-<session_id>
  if known state or matching transcript exists:
    start Claude Code with --resume <session_id> "<prompt>"
  else:
    start Claude Code with --session-id <session_id> "<prompt>"
```

stream은 로컬 session state를 사용해서 transcript cursor를 관리합니다.

저장 위치:

```text
~/.cache/claude-tmux-control/sessions/<session_id>.json
```

새 turn의 시작점은 다음 순서로 찾습니다.

```text
1. 저장된 current turn cursor
2. prompt 전송 직전 transcript offset 이후의 첫 user event
3. prompt 전송 직전 timestamp 이후의 첫 user event
4. prompt hash/text matching fallback
```

중요한 제약:

- transcript baseline은 prompt를 보내기 전에 캡처합니다.
- offset cursor는 `scan_offset`, `replay_start_offset`, `read_offset`, `last_stdout_flushed_offset`처럼 역할별로 분리합니다.
- 같은 `session_id`에 진행 중인 turn이 있으면 새 prompt를 보내지 않습니다.
- 재연결은 새 prompt 전송이 아니라 active turn에 attach하는 방식으로 처리합니다.

turn이 anchor되면 이후 polling은 transcript 전체를 다시 읽지 않고 저장된 offset 이후의 새 JSONL line만 읽는 방향입니다.

웹용 high-level stream의 기본 polling interval은 `2.0`초로 둡니다.

현재 CLI에는 이 고수준 `stream`이 구현되어 있습니다.

`start -> send -> stream <tmux_session>` 조합은 low-level smoke/debug 전용으로 사용합니다.

이때 `TMUX_SESSION=ctc-csess-$SESSION_ID`는 내부 구현 detail이며, 최종 클라이언트 계약에는 노출하지 않습니다.

내부 fallback 예시는 CLI manual을 봅니다.

## 3. One Turn Flow

한 사용자 입력 turn은 `send_lock`과 `active_turn`으로 보호합니다.

같은 `tmux_session`에 동시에 두 prompt를 보내면 transcript와 완료 판정이 꼬일 수 있습니다.

권장 흐름:

```text
POST /conversations/:id/messages
  -> run high-level stream with optional request.session_id and prompt
  -> bridge acquires short send_lock
  -> bridge creates/reuses/resumes Claude Code session
  -> bridge captures transcript baseline before send/start/resume
  -> bridge persists active_turn with owner and heartbeat
  -> bridge sends prompt or passes it as initial Claude command argument
  -> bridge releases send_lock
  -> stream owner tails transcript from stored offset
  -> bridge streams events until done
  -> include session_id in every client event
  -> emit done, then emit separate final metrics
  -> clear active_turn only after the turn is confirmed complete
```

CLI 예:

```bash
ctc stream --session-id "$SESSION_ID" --cwd "$PROJECT_DIR" "$USER_PROMPT"
```

`wait-ready`가 실패하면 정책을 정해야 합니다.

| 상태 | 권장 처리 |
| --- | --- |
| 아직 working | 새 입력 거절 또는 현재 stream에 attach |
| 사용자가 취소 요청 | `cancel <session_id>` 호출 후 `last --last 1` 또는 `stream --attach`로 완료까지 수신 |
| timeout/interrupted | UI에 "아직 처리 중" 표시. 다음 prompt 요청 시 CLI가 이전 turn의 완료 여부를 먼저 검사 |
| needs confirmation | session 재시작 또는 운영자 확인 |
| inactive | `stream --session-id ...`가 내부에서 `--resume`으로 재생성 |

`send_lock`은 prompt 전송 구간만 보호하는 짧은 lock입니다.

긴 stream 동안 새 prompt를 막는 것은 `active_turn` state입니다.

브라우저가 끊겼다가 다시 붙으면 새 prompt를 보내지 않고 `active_turn`에 attach합니다.

사용자가 진행 중인 응답을 취소하면 `cancel`을 호출합니다.

```bash
ctc cancel "$SESSION_ID"
ctc last "$SESSION_ID" --last 1
```

`cancel`은 내부 tmux session에 `Escape` key를 보낸 뒤 JSON을 반환합니다. 이 명령은 완료 판정을 하지 않습니다.

취소 뒤에도 `active_turn`은 남아 있을 수 있습니다. 클라이언트는 `last --last 1` 또는 `stream --attach --session-id "$SESSION_ID"`로 이어서 `done`/`metrics`까지 받아야 합니다.

Claude Code가 tool 실행 중 취소되면 transcript에 `User rejected tool use`와 `[Request interrupted by user for tool use]`가 남을 수 있습니다. CLI는 이 패턴을 취소 완료로 처리합니다. 이때 final assistant text가 없을 수 있으므로 `done.answer`는 비어 있을 수 있지만, `done`/`metrics`가 오면 입력창을 다시 열 수 있습니다.

attach는 완료된 과거 turn 조회가 아닙니다.

성공하려면 `--session-id`가 필요하고, 해당 session에 `active_turn`이 남아 있어야 하며, `active_turn.stream_state`가 `active`, `timeout`, `interrupted` 중 하나여야 합니다. 내부 tmux session과 transcript도 찾을 수 있어야 합니다.

이미 완료되어 `active_turn`이 정리된 turn에는 attach할 수 없습니다. 완료된 마지막 답변은 앱 서버가 저장한 `done.answer` 또는 `info`의 `last_turn`/state를 사용합니다. 취소된 turn처럼 final answer가 없을 수 있으므로, 완료 판단은 `done`/`metrics` 도착 여부로 분리합니다.

완료된 turn의 JSONL event를 CLI에서 다시 받아야 하면 `replay`를 사용합니다.

```bash
ctc replay "$SESSION_ID" --last 1
ctc replay "$SESSION_ID" --last 5
ctc last "$SESSION_ID" --last 1
```

`replay`는 완료된 turn의 `user`/`tool_use`/`tool_result`/`assistant_text`/`done`/`metrics`를 같은 JSONL 형식으로 다시 출력합니다.

마지막 turn이 아직 진행 중이면 `last`/`replay`는 완료된 이전 turn을 먼저 replay한 뒤, 현재 `active_turn`에는 attach해서 완료될 때까지 stream합니다.

예를 들어 `ctc last "$SESSION_ID" --last 2`에서 마지막 turn이 진행 중이면, 최근 완료 turn 1개를 먼저 JSONL로 replay하고 마지막 active turn을 이어서 `done`/`metrics`까지 출력합니다.

`timeout`이나 `failed`는 "입력 가능" 신호가 아닙니다.

이 경우 `active_turn`을 유지합니다.

다음 `stream --cwd ... --session-id ... "$PROMPT"` 요청이 들어오면 CLI는 먼저 tmux 화면과 transcript를 검사합니다.

이전 turn이 `tmux ready + transcript ready`로 확인되면 이전 turn을 state에 `done/metrics`로 finalize한 뒤 새 prompt를 전송합니다.

확인되지 않으면 기존처럼 `turn_in_progress`로 중복 입력을 막습니다.

`ensure`는 사용자가 주로 직접 호출할 명령이 아니라, `stream` 내부에서 수행하는 session 보장 단계입니다.

`ask`는 stream이 필요 없을 때 쓰는 non-streaming convenience 명령입니다.

`ask`는 전체 turn이 끝난 뒤 최종 answer와 metrics를 한 번에 반환합니다.

## 4. Stream Handling

`stream`은 JSONL을 stdout으로 출력합니다.

앱 서버는 child process stdout을 line-by-line으로 읽고, 각 줄을 JSON으로 parse합니다.

```bash
ctc stream --session-id "$SESSION_ID" --cwd "$PROJECT_DIR" "$USER_PROMPT"
```

대표 event:

| event | 의미 | UI 표시 |
| --- | --- | --- |
| `user` | 이번 turn의 user prompt | 보통 이미 표시했으므로 무시 가능 |
| `thinking` | Claude thinking block | 접힌 reasoning/progress 영역. text가 없으면 metadata만 표시 |
| `tool_use` | tool call 시작 | "도구 실행 중" 카드 |
| `tool_result` | tool call 결과 | tool output 또는 error |
| `assistant_text` | assistant 답변 text block | 답변 영역에 append |
| `done` | turn 완료 | 입력창 재활성화 |
| `metrics` | turn final metrics | token/cost 패널 갱신. transcript에 context 정보가 있으면 context도 갱신 |
| `timeout` | 완료 전 timeout | 처리 중/재시도 UI |

예:

```json
{"event":"thinking","session_id":"550e8400-e29b-41d4-a716-446655440000","turn_id":"turn_20260516_0001","event_id":"turn_20260516_0001:dev1-ino2:00001-00009:block0:thinking","source_offset":1,"source_end_offset":9,"block_index":0,"timestamp":"2026-05-16T12:00:00.000Z","text":"","text_available":false,"has_signature":true}
{"event":"tool_use","session_id":"550e8400-e29b-41d4-a716-446655440000","turn_id":"turn_20260516_0001","event_id":"turn_20260516_0001:dev1-ino2:00010-00080:block0:tool_use","source_offset":10,"source_end_offset":80,"block_index":0,"timestamp":"2026-05-16T12:00:01.000Z","id":"toolu_...","name":"Bash","input":{"command":"ls"}}
{"event":"tool_result","session_id":"550e8400-e29b-41d4-a716-446655440000","turn_id":"turn_20260516_0001","event_id":"turn_20260516_0001:dev1-ino2:00081-00130:block0:tool_result","source_offset":81,"source_end_offset":130,"block_index":0,"timestamp":"2026-05-16T12:00:02.000Z","tool_use_id":"toolu_...","text":"README.md\nlong output...","text_truncated":true,"text_full_length":2048}
{"event":"assistant_text","session_id":"550e8400-e29b-41d4-a716-446655440000","turn_id":"turn_20260516_0001","event_id":"turn_20260516_0001:dev1-ino2:00131-00220:block0:assistant_text","source_offset":131,"source_end_offset":220,"block_index":0,"timestamp":"2026-05-16T12:00:03.000Z","text":"현재 디렉터리는..."}
{"event":"done","session_id":"550e8400-e29b-41d4-a716-446655440000","turn_id":"turn_20260516_0001","event_id":"turn_20260516_0001:done:220","state":"ready","reason":"prompt visible; transcript ready","answer":"현재 디렉터리는..."}
{"event":"metrics","session_id":"550e8400-e29b-41d4-a716-446655440000","turn_id":"turn_20260516_0001","event_id":"turn_20260516_0001:metrics:220","source_offset":220,"source_end_offset":220,"block_index":-1,"scope":"turn_final","usage":{"input_tokens":12000,"cache_read_tokens":8000,"cache_write_tokens":500,"output_tokens":1400}}
```

`tool_result.text`는 기본 100자 preview입니다. 더 길게 보고 싶으면 `stream --tool-result-limit 240`처럼 조정합니다. 음수는 축약을 끕니다.

고수준 `stream`의 stdout JSONL event에는 `session_id`가 포함됩니다.

low-level `stream <tmux_session>`은 기존 디버깅용 명령이므로 웹 클라이언트에는 고수준 `stream --session-id ... --cwd ...`를 사용합니다.

### UI 상태 전환

```text
idle
  -> sending
  -> streaming
       -> thinking/tool_use/tool_result/assistant_text...
  -> done
  -> idle
```

`done`을 받기 전에는 같은 session에 새 prompt를 보내지 않습니다.

`tool_result`만 보고 완료로 판단하면 안 됩니다.

Claude Code는 tool result 이후에 최종 assistant text를 이어서 쓸 수 있습니다.

## 5. Metrics Per Turn

각 turn에서 보여줄 수 있는 값은 두 종류로 나눕니다.

| 값 | 권장 source | 현재 CLI 지원 |
| --- | --- | --- |
| model | final `metrics.model` | 지원 |
| elapsed time | final `metrics.elapsed_ms` | 지원 |
| input tokens | final `metrics.usage.input_tokens` | 지원 |
| cache write tokens | final `metrics.usage.cache_write_tokens` | 지원 |
| cache read tokens | final `metrics.usage.cache_read_tokens` | 지원 |
| output tokens | final `metrics.usage.output_tokens` | 지원 |
| context size | final `metrics.context` | transcript에 있으면 지원. 없으면 추정하지 않고 생략 |
| turn cost | final `metrics.cost.turn_usd` | pricing table 기준 추정 |
| session cumulative cost | final `metrics.cost.session_usd` 또는 `info.cost_totals.session_usd` | completed turn records 기준 |

### Elapsed Time

CLI가 turn 시작부터 완료까지의 `elapsed_ms`를 final `metrics`에 넣습니다.

```text
done
metrics.elapsed_ms
```

### Token And Context

`stream`은 UI용 normalized event이며, final `metrics`는 통계용 필드를 함께 제공합니다.

usage/context 필드는 Claude Code transcript schema에 따라 위치와 이름이 달라질 수 있으므로, CLI가 최신 turn의 raw event를 읽어 normalized `usage`/`context`로 변환합니다.

현재 실측한 Claude Code transcript에서는 `usage`는 나오지만 `context`, `context_window`, `context_usage`는 나오지 않는 경우가 일반적입니다.

context 정보가 없으면 CLI는 `metrics.context`를 생략합니다. `usage` 값을 더해서 context 추정치를 넣지는 않습니다.

별도 raw 분석이 필요하면 `events --json` 또는 향후 `stats` 명령을 추가합니다.

앱 서버는 최신 user turn 이후의 raw event를 보고 다음 위치를 우선 탐색합니다.

```text
event.usage
event.message.usage
event.response.usage

event.model
event.message.model
event.response.model

event.context
event.context_window
event.context_usage
```

자주 기대할 수 있는 usage key:

```text
input_tokens
output_tokens
cache_creation_input_tokens
cache_read_input_tokens
```

client-facing 이름은 다음처럼 normalize합니다.

```text
input_tokens       <- input_tokens
output_tokens      <- output_tokens
cache_read_tokens  <- cache_read_input_tokens
cache_write_tokens <- cache_creation_input_tokens
model              <- event/message/response model
```

주의:

- Claude Code 버전별로 transcript schema가 바뀔 수 있습니다.
- 모든 event에 usage가 있는 것은 아닙니다.
- usage가 여러 event에 나뉘어 있으면 최신 assistant/result event의 값을 우선 사용하거나, 앱 정책에 맞게 합산합니다.
- 현재 CLI는 `claude_pricing.json`을 기준으로 turn cost를 계산합니다.

### Metrics Delivery Timing

기본 정책은 turn 완료 후 final metrics만 클라이언트에 전달하는 것입니다.

`stream`에서 `done`을 받은 직후 별도 `metrics` event를 보냅니다.

```json
{
  "event": "metrics",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "turn_id": "turn_20260516_0001",
  "event_id": "turn_20260516_0001:metrics:220",
  "source_offset": 220,
  "source_end_offset": 220,
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
    "turn_usd": 0.0624,
    "session_usd": 0.2516
  }
}
```

final `metrics`는 replay될 수 있으므로 `turn_id`와 deterministic `event_id`로 dedupe합니다.

`done`과 `metrics`의 synthetic `event_id`는 `<turn_id>:done:<completed_offset>`, `<turn_id>:metrics:<completed_offset>` 형식을 사용합니다.

중간 metrics는 아직 기본 CLI 출력이 아닙니다.

구조상 `stream` loop는 transcript에 새 event가 생길 때마다 읽으므로, 새 raw event에 `usage`/`context`/`model`이 포함되어 있으면 향후 `scope: "turn_partial"` event를 best-effort로 추가할 수 있습니다.

다만 Claude Code가 usage/context를 언제 기록하는지는 버전과 event 종류에 따라 달라질 수 있습니다.

그래서 중간 metrics는 기본 UI 계약으로 삼지 않고, 필요하면 best-effort 옵션으로 둡니다.

### Cost

turn cost는 CLI가 `claude_pricing.json`으로 계산합니다.

session cumulative cost는 CLI가 completed turn records에서 재계산해 final `metrics.cost.session_usd`와 `info.cost_totals.session_usd`로 제공합니다.

필요한 값:

```text
model
input_tokens
cache_creation_input_tokens
cache_read_input_tokens
output_tokens
pricing table version
```

가격표는 `claude_pricing.json`에 분리되어 있습니다.

모델 버전이 정확히 매칭되지 않으면 CLI는 family별 최신 버전 단가를 사용하고 `model_match: "family_latest"`를 표시합니다.

## 6. Session Id Ownership

bridge `session_id`, tmux session name, Claude transcript 내부 `sessionId`는 같은 UUID를 중심으로 맞추는 것이 목표입니다.

| 이름 | 의미 |
| --- | --- |
| `session_id` | 앱 서버가 생성해 넘기거나 CLI가 생략 시 생성하는 UUID. 클라이언트는 이후 요청에 같은 값을 다시 보냄 |
| `tmux_session` | `ctc-csess-<session_id>` 형식의 tmux session name |
| Claude `sessionId` | Claude Code에 `--session-id <session_id>`로 지정한 내부 session id |

첫 응답부터 클라이언트에는 `session_id`를 포함해서 내려줍니다.

클라이언트는 같은 대화를 이어가고 싶으면 다음 요청에 이 값을 그대로 보냅니다.

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": "다음 작업 계속해줘"
}
```

검증용으로는 `info <session_id> --json`에서 bridge `session_id`, tmux session name, Claude transcript `sessionId`, transcript path를 한 번에 확인할 수 있습니다.

멀티서버는 현재 고려하지 않습니다.

`--resume <session_id>`는 같은 서버의 로컬 Claude transcript를 기준으로 복구합니다.

## 7. Backend Pseudocode

```python
def send_turn(session_id, project_dir, prompt, account_env):
    started_at = monotonic()

    command = [
        "ctc",
        "stream",
        "--cwd",
        project_dir,
        "--oauth-token-env",
        account_env,
    ]
    if session_id:
        command += ["--session-id", session_id]
    command.append(prompt)

    for event in stream_jsonl(command):
        session_id = event["session_id"]
        if event["event"] == "metrics":
            assert "elapsed_ms" in event
            assert "cost" in event
        push_to_client(session_id, event)
        if event["event"] == "done":
            answer = event["answer"]

    return answer
```

## 8. Session Cleanup

오래된 session은 별도 scheduler에서 `reap`을 주기적으로 실행합니다.

예:

```bash
ctc reap --idle-seconds 1800 --prefix ctc-csess- --dry-run
ctc reap --idle-seconds 1800 --prefix ctc-csess-
```

현재 `reap`은 daemon이 아닙니다.

한 번 scan하고 종료합니다.

high-level `active_turn`이 남아 있고 `ready`가 아니면 `reap`은 보수적으로 skip합니다.

`timeout`이나 `interrupted`는 입력 가능 또는 정리 가능 신호가 아닙니다. `attach`, 같은 `session_id` 재시도, 또는 운영자 판단에 따른 `kill`로 별도 처리합니다.

web session만 정리하려면 `ctc-csess-` prefix를 권장합니다. `ctc-` prefix는 controlled 전체 정리용이라 low-level `ctc-*` session도 포함될 수 있습니다.

cron, systemd timer, app scheduler 중 하나에서 호출합니다.

## 9. Error Handling

| 상황 | CLI signal | 앱 처리 |
| --- | --- | --- |
| `tmux` 없음 | exit `127` | 설치 안내, 서버 misconfig |
| `claude` 없음 | exit `127` | Claude Code 설치 안내 |
| invalid `session_id` 또는 cwd mismatch | exit `2` | 요청 오류로 표시. 같은 대화창에 다른 cwd를 연결하지 않음 |
| `turn_in_progress` 등 high-level state error | exit `5`, stderr JSON | 새 입력을 큐에 넣거나 `stream --attach`로 기존 turn에 재연결 |
| cancel 성공 | exit `0`, stdout JSON `event:cancel` | UI를 cancelling 상태로 전환하고 `last`/`attach`로 완료 대기 |
| cancel 대상 tmux 없음 | exit `2`, stderr JSON `tmux_session_missing` | 이미 종료된 session으로 표시하거나 같은 session_id로 다음 stream 시 resume |
| transcript 없음 | exit `2` | starting 상태로 재시도 |
| stream timeout | exit `3`, `timeout` event | UI에 계속 처리 중 표시 |
| stream interrupted | exit `130` | 연결 끊김으로 표시. 같은 session_id로 attach 또는 다음 prompt 시 자동 완료 검사 |
| 이전 timeout turn 완료됨 | 새 요청 안에서 state-only finalize | 이전 answer/metrics는 `info`/state에서 확인. 새 요청 stdout에는 새 turn event만 출력 |
| replay 대상 turn 없음 | exit `4` | 저장된 이벤트가 없음을 표시 |
| session 없음 | exit `2` | `start` 후 재시도 |

앱 서버는 stderr를 운영 로그에 남기되, token 값은 절대 로그에 남기지 않습니다.

## 10. Current Gaps

웹 채팅 제품 관점에서 아직 남은 CLI 개선점입니다.

- `ensure`: 고수준 `stream` 내부에서 쓰는 session 보장 단계로 유지
- low-level `status`, `answer`, `turn`의 machine-readable JSON mode가 필요하면 별도 web-facing contract로 정의
- `status --json`, `answer --json`, `turn --json`
- state-write lock과 generation compare/update retry 강화
- transcript rotation follow 고도화
- machine-readable stats command

이 gap이 구현되면 이 문서를 같이 갱신해야 합니다.
