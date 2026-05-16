# Web Chat Integration Guide

이 문서는 웹 서버나 다른 프로그램이 `claude_tmux_control.py`를 호출해서 ChatGPT 페이지처럼 Claude Code와 대화형 UI를 만드는 방법을 정의합니다.

목표는 한 웹 대화창이 하나의 Claude Code session을 계속 재사용하고, 각 turn의 진행 이벤트와 최종 답변, usage/cost 정보를 UI에 보여주는 것입니다.

## 1. Integration Contract

### 핵심 원칙

한 웹 대화창은 하나의 bridge `session_id`를 가집니다.

`session_id`는 bridge가 UUID로 생성합니다.

클라이언트가 첫 메시지에 `session_id`를 보내지 않으면 bridge가 새 UUID를 만들고, Claude Code 첫 실행에 같은 UUID를 `--session-id`로 전달합니다.

클라이언트가 이후 요청에 `session_id`를 보내면 bridge는 같은 tmux session을 재사용하거나, tmux session이 없으면 Claude Code를 `--resume <session_id>`로 다시 실행합니다.

권장 규칙:

```text
session_id:   <bridge-generated-uuid>
tmux_session: ctc-csess-<session_id>
```

예:

```text
session_id   = 550e8400-e29b-41d4-a716-446655440000
tmux_session = ctc-csess-550e8400-e29b-41d4-a716-446655440000
```

`:`는 tmux target 문법에서 `session:window.pane` 구분자로 쓰이므로 session name에 넣지 않습니다.

권장 session id 형식:

```text
UUID v4
```

클라이언트가 `session_id`를 보내도 bridge는 UUID 형식을 검증해야 합니다.

UUID가 아니면 state path나 tmux session name에 사용하지 않고 요청을 거절합니다.

기존 state가 있는 `session_id`에 다른 `cwd`가 들어오면 canonical path 기준으로 비교한 뒤 `session_cwd_mismatch`로 fail closed합니다.

### 실행 원본

| 목적 | CLI 명령 | 원본 |
| --- | --- | --- |
| 대화 한 턴 실행 | `stream [--session-id] --cwd <path> "<prompt>"` target | session 생성/재사용/resume + prompt 입력 + JSONL stream |
| session 생성/재사용 | `ensure` internal step | `tmux new-session` + Claude `--session-id`/`--resume` |
| prompt 입력 | `send` low-level fallback | `tmux load-buffer` + `paste-buffer` |
| 진행 stream | `stream` | Claude Code transcript JSONL + tmux ready check |
| raw event 조회 | `events --json` | Claude Code transcript JSONL |
| 최종 답변만 조회 | `answer` | Claude Code transcript JSONL |
| 전체 turn 조회 | `turn` | Claude Code transcript JSONL |
| session 종료 | `kill` | `tmux kill-session` |
| 오래된 session 정리 | `reap` | tmux session + local state |

## 2. Primary Stream Flow

웹 서버가 주로 사용할 고수준 명령은 `stream`입니다.

`stream`은 prompt를 받아서 한 대화 turn을 실행하고, 진행 이벤트를 JSONL로 출력한 뒤 `done`과 final `metrics`까지 내보내는 명령이어야 합니다.

새 대화:

```bash
TERM=xterm-256color \
./claude_tmux_control.py stream \
  --cwd "$PROJECT_DIR" \
  "$USER_PROMPT"
```

기존 대화:

```bash
TERM=xterm-256color \
./claude_tmux_control.py stream \
  --session-id "$SESSION_ID" \
  --cwd "$PROJECT_DIR" \
  "$USER_PROMPT"
```

Claude Code 옵션을 그대로 넘겨야 하면 `--` 뒤에 둡니다.

```bash
TERM=xterm-256color \
./claude_tmux_control.py stream \
  --session-id "$SESSION_ID" \
  --cwd "$PROJECT_DIR" \
  "$USER_PROMPT" \
  -- --model opus --add-dir ../shared
```

계정별 OAuth token을 써야 하면 요청 처리 프로세스에서 source env를 선택합니다.

```bash
ACCOUNT_A_TOKEN="$TOKEN" \
TERM=xterm-256color \
./claude_tmux_control.py stream \
  --session-id "$SESSION_ID" \
  --cwd "$PROJECT_DIR" \
  --oauth-token-env ACCOUNT_A_TOKEN \
  "$USER_PROMPT"
```

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
  start Claude Code with --resume <session_id> "<prompt>"
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

현재 CLI에는 이 고수준 `stream`이 아직 없고, 구현된 `stream <tmux_session>`은 low-level read stream입니다.

구현 전까지 `start -> send -> stream` 조합은 smoke/debug 전용으로만 사용합니다.

production fallback으로 쓰려면 앱 서버가 같은 `send_lock`, `active_turn`, owner heartbeat, conservative replay cursor 계약을 직접 구현해야 합니다.

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
./claude_tmux_control.py stream --session-id "$SESSION_ID" --cwd "$PROJECT_DIR" "$USER_PROMPT"
```

`wait-ready`가 실패하면 정책을 정해야 합니다.

| 상태 | 권장 처리 |
| --- | --- |
| 아직 working | 새 입력 거절 또는 현재 stream에 attach |
| timeout | UI에 "아직 처리 중" 표시 |
| needs confirmation | session 재시작 또는 운영자 확인 |
| inactive | `stream --session-id ...`가 내부에서 `--resume`으로 재생성 |

`send_lock`은 prompt 전송 구간만 보호하는 짧은 lock입니다.

긴 stream 동안 새 prompt를 막는 것은 `active_turn` state입니다.

브라우저가 끊겼다가 다시 붙으면 새 prompt를 보내지 않고 `active_turn`에 attach합니다.

`timeout`이나 `failed`는 "입력 가능" 신호가 아닙니다.

이 경우 `active_turn`을 유지하고, attach/inspect/kill로 이전 turn이 끝났는지 확인한 뒤 다음 입력을 허용합니다.

`ensure`는 사용자가 주로 직접 호출할 명령이 아니라, `stream` 내부에서 수행하는 session 보장 단계입니다.

`ask`는 stream이 필요 없을 때 쓰는 non-streaming convenience 명령입니다.

`ask`는 전체 turn이 끝난 뒤 최종 answer와 metrics를 한 번에 반환합니다.

## 4. Stream Handling

`stream`은 JSONL을 stdout으로 출력합니다.

앱 서버는 child process stdout을 line-by-line으로 읽고, 각 줄을 JSON으로 parse합니다.

```bash
./claude_tmux_control.py stream --session-id "$SESSION_ID" --cwd "$PROJECT_DIR" "$USER_PROMPT"
```

대표 event:

| event | 의미 | UI 표시 |
| --- | --- | --- |
| `user` | 이번 turn의 user prompt | 보통 이미 표시했으므로 무시 가능 |
| `thinking` | Claude thinking block | 접힌 reasoning/progress 영역 |
| `tool_use` | tool call 시작 | "도구 실행 중" 카드 |
| `tool_result` | tool call 결과 | tool output 또는 error |
| `assistant_text` | assistant 답변 text block | 답변 영역에 append |
| `done` | turn 완료 | 입력창 재활성화 |
| `metrics` | turn final metrics | token/context/cost 패널 갱신 |
| `timeout` | 완료 전 timeout | 처리 중/재시도 UI |

예:

```json
{"event":"tool_use","session_id":"550e8400-e29b-41d4-a716-446655440000","turn_id":"turn_20260516_0001","event_id":"turn_20260516_0001:dev1-ino2:00010-00080:block0:tool_use","source_offset":10,"source_end_offset":80,"block_index":0,"timestamp":"2026-05-16T12:00:01.000Z","id":"toolu_...","name":"Bash","input":{"command":"ls"}}
{"event":"tool_result","session_id":"550e8400-e29b-41d4-a716-446655440000","turn_id":"turn_20260516_0001","event_id":"turn_20260516_0001:dev1-ino2:00081-00130:block0:tool_result","source_offset":81,"source_end_offset":130,"block_index":0,"timestamp":"2026-05-16T12:00:02.000Z","tool_use_id":"toolu_...","text":"README.md\n"}
{"event":"assistant_text","session_id":"550e8400-e29b-41d4-a716-446655440000","turn_id":"turn_20260516_0001","event_id":"turn_20260516_0001:dev1-ino2:00131-00220:block0:assistant_text","source_offset":131,"source_end_offset":220,"block_index":0,"timestamp":"2026-05-16T12:00:03.000Z","text":"현재 디렉터리는..."}
{"event":"done","session_id":"550e8400-e29b-41d4-a716-446655440000","turn_id":"turn_20260516_0001","event_id":"turn_20260516_0001:done:220","state":"ready","reason":"prompt visible; transcript ready","answer":"현재 디렉터리는..."}
{"event":"metrics","session_id":"550e8400-e29b-41d4-a716-446655440000","turn_id":"turn_20260516_0001","event_id":"turn_20260516_0001:metrics:220","source_offset":220,"source_end_offset":220,"block_index":-1,"scope":"turn_final","usage":{"input_tokens":12000,"cache_read_tokens":8000,"cache_write_tokens":500,"output_tokens":1400}}
```

현재 `stream` 명령은 아직 `session_id`를 자동 포함하지 않습니다.

구현 전까지는 앱 서버가 stdout event를 client로 전달하기 전에 request의 `session_id`를 붙입니다.

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
| model | transcript `model` 계열 field | `events --json` raw event에서 추출 |
| elapsed time | 앱 서버 wall-clock | 앱에서 측정 |
| input tokens | transcript `usage` 계열 field | `events --json` raw event에서 추출 |
| cache write tokens | transcript `usage.cache_creation_input_tokens` | `events --json` raw event에서 추출 |
| cache read tokens | transcript `usage` 계열 field | `events --json` raw event에서 추출 |
| output tokens | transcript `usage` 계열 field | `events --json` raw event에서 추출 |
| context size | transcript `context` 계열 field | `events --json` raw event에서 추출 |
| cost | 앱 서버 계산 | CLI가 보장하지 않음 |

### Elapsed Time

가장 안정적인 방식은 앱 서버에서 직접 측정하는 것입니다.

```text
turn_started_at = before send
turn_finished_at = when stream emits done
elapsed_ms = finished - started
```

### Token And Context

`stream`은 UI용 normalized event입니다.

usage/context 필드는 Claude Code transcript schema에 따라 위치와 이름이 달라질 수 있으므로, 통계 수집은 raw event에서 하는 편이 낫습니다.

고수준 `stats`/`metrics` 명령이 생기기 전까지는 bridge 내부에서 해당 tmux session의 raw events를 읽어 계산합니다.

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
- 현재 CLI는 cost를 계산하지 않습니다.

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
  "elapsed_ms": 18342,
  "usage": {
    "input_tokens": 12000,
    "cache_read_tokens": 8000,
    "cache_write_tokens": 500,
    "output_tokens": 1400
  },
  "context": {
    "current_size": 64000,
    "window_size": 200000
  },
  "cost": {
    "turn_usd": 0.0572,
    "session_usd": 0.2516,
    "estimated": true,
    "pricing_version": "anthropic-2026-05-16"
  }
}
```

final `metrics`는 replay될 수 있으므로 `turn_id`와 deterministic `event_id`로 dedupe합니다.

`done`과 `metrics`의 synthetic `event_id`는 `<turn_id>:done:<completed_offset>`, `<turn_id>:metrics:<completed_offset>` 형식을 사용합니다.

중간 metrics도 기술적으로는 가능합니다.

`stream` loop는 transcript에 새 event가 생길 때마다 읽으므로, 새 raw event에 `usage`/`context`/`model`이 포함되어 있으면 `metrics` event를 `scope: "turn_partial"`로 내보낼 수 있습니다.

다만 Claude Code가 usage를 언제 기록하는지는 버전과 event 종류에 따라 달라질 수 있습니다.

그래서 중간 metrics는 기본 UI 계약으로 삼지 않고, 필요하면 best-effort 옵션으로 둡니다.

### Cost

cost는 앱 서버가 계산하는 것이 안전합니다.

필요한 값:

```text
model
input_tokens
cache_creation_input_tokens
cache_read_input_tokens
output_tokens
pricing table version
```

가격표는 자주 바뀔 수 있으므로 CLI에 하드코딩하지 않습니다.

앱 서버가 가격표 버전과 계산식을 저장하고, UI에는 "estimated" 표시를 붙이는 것을 권장합니다.

## 6. Session Id Ownership

bridge `session_id`, tmux session name, Claude transcript 내부 `sessionId`는 같은 UUID를 중심으로 맞추는 것이 목표입니다.

| 이름 | 의미 |
| --- | --- |
| `session_id` | bridge가 생성하고 클라이언트가 이후 요청에 돌려주는 UUID |
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

검증용으로는 `events --json` raw event에서 Claude transcript의 `sessionId`가 같은지 확인할 수 있습니다.

이 명령도 고수준 `info --session-id <session_id>`가 생기면 직접 tmux session name을 요구하지 않아야 합니다.

향후 `info` 명령이 생기면 `session_id`, `tmux_session`, Claude `sessionId`, transcript path를 한 번에 반환하게 할 예정입니다.

멀티서버는 현재 고려하지 않습니다.

`--resume <session_id>`는 같은 서버의 로컬 Claude transcript를 기준으로 복구합니다.

## 7. Backend Pseudocode

```python
def send_turn(session_id, project_dir, prompt, account_env):
    started_at = monotonic()

    command = [
        "./claude_tmux_control.py",
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
            event["elapsed_ms"] = event.get("elapsed_ms") or int((monotonic() - started_at) * 1000)
            event["cost"] = event.get("cost") or estimate_or_mark_unavailable(event)
        push_to_client(session_id, event)
        if event["event"] == "done":
            answer = event["answer"]

    return answer
```

## 8. Session Cleanup

오래된 session은 별도 scheduler에서 `reap`을 주기적으로 실행합니다.

예:

```bash
./claude_tmux_control.py reap --idle-seconds 1800 --prefix ctc- --dry-run
./claude_tmux_control.py reap --idle-seconds 1800 --prefix ctc-
```

현재 `reap`은 daemon이 아닙니다.

한 번 scan하고 종료합니다.

cron, systemd timer, app scheduler 중 하나에서 호출합니다.

## 9. Error Handling

| 상황 | CLI signal | 앱 처리 |
| --- | --- | --- |
| `tmux` 없음 | exit `127` | 설치 안내, 서버 misconfig |
| `claude` 없음 | exit `127` | Claude Code 설치 안내 |
| transcript 없음 | exit `2` | starting 상태로 재시도 |
| stream timeout | exit `3`, `timeout` event | UI에 계속 처리 중 표시 |
| session 없음 | exit `2` | `start` 후 재시도 |

앱 서버는 stderr를 운영 로그에 남기되, token 값은 절대 로그에 남기지 않습니다.

## 10. Current Gaps

웹 채팅 제품 관점에서 아직 남은 CLI 개선점입니다.

- `ensure`: 고수준 `stream` 내부에서 쓰는 session 보장 단계로 유지
- `ask [--session-id] --cwd <path> "<prompt>"`: streaming 없이 전체 turn 완료 후 최종 answer와 metrics를 한 번에 반환
- `info <session-id>`: tmux/Claude transcript/sessionId metadata 반환
- 모든 answer JSON 응답에 `session_id` 포함
- `status --json`, `answer --json`, `turn --json`
- explicit `stream --attach --session-id <id>` reconnect mode
- state-write lock과 generation compare/update retry 강화
- completed-turn cumulative usage/session cost 저장
- transcript rotation follow 고도화
- cost 계산을 위한 model/usage extraction helper

이 gap이 구현되면 이 문서를 같이 갱신해야 합니다.
