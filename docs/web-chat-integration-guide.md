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

### 실행 원본

| 목적 | CLI 명령 | 원본 |
| --- | --- | --- |
| session 생성/재사용 | `ensure` target, 현재는 `start` 조합 | `tmux new-session` + Claude `--session-id`/`--resume` |
| prompt 입력 | `send` | `tmux load-buffer` + `paste-buffer` |
| 진행 stream | `stream` | Claude Code transcript JSONL + tmux ready check |
| raw event 조회 | `events --json` | Claude Code transcript JSONL |
| 최종 답변만 조회 | `answer` | Claude Code transcript JSONL |
| 전체 turn 조회 | `turn` | Claude Code transcript JSONL |
| session 종료 | `kill` | `tmux kill-session` |
| 오래된 session 정리 | `reap` | tmux session + local state |

## 2. Startup Flow

웹 서버는 첫 prompt에서 `session_id`가 없으면 UUID를 생성합니다.

현재 CLI에는 `ensure`가 아직 없으므로, 구현 전까지는 앱 서버가 UUID 생성과 `start` command 구성을 담당합니다.

첫 시작은 Claude Code에 같은 UUID를 `--session-id`로 넘깁니다.

```bash
SESSION_ID="$(uuidgen | tr '[:upper:]' '[:lower:]')"
TMUX_SESSION="ctc-csess-$SESSION_ID"

TERM=xterm-256color \
./claude_tmux_control.py start "$TMUX_SESSION" \
  --cwd "$PROJECT_DIR" \
  -- --session-id "$SESSION_ID"
```

Claude Code 옵션을 그대로 넘겨야 하면 `start` 뒤에 붙입니다.

```bash
TERM=xterm-256color \
./claude_tmux_control.py start "$TMUX_SESSION" \
  --cwd "$PROJECT_DIR" \
  -- --session-id "$SESSION_ID" --model opus --add-dir ../shared
```

계정별 OAuth token을 써야 하면 요청 처리 프로세스에서 source env를 선택합니다.

```bash
ACCOUNT_A_TOKEN="$TOKEN" \
TERM=xterm-256color \
./claude_tmux_control.py start "$TMUX_SESSION" \
  --cwd "$PROJECT_DIR" \
  --oauth-token-env ACCOUNT_A_TOKEN \
  -- --session-id "$SESSION_ID"
```

`start`는 같은 tmux session이 이미 있으면 재사용합니다.

따라서 웹 서버는 "없으면 만들고, 있으면 그대로 사용" 흐름에 `start`를 그대로 쓸 수 있습니다.

tmux session이 이미 종료된 상태에서 클라이언트가 `session_id`를 보내면 새 tmux session을 만들고 Claude Code를 resume합니다.

```bash
TMUX_SESSION="ctc-csess-$SESSION_ID"

TERM=xterm-256color \
./claude_tmux_control.py start "$TMUX_SESSION" \
  --cwd "$PROJECT_DIR" \
  -- --resume "$SESSION_ID"
```

향후 `ensure` 명령은 이 판단을 CLI 내부에서 처리해야 합니다.

## 3. One Turn Flow

한 사용자 입력 turn은 반드시 session별 lock 안에서 처리합니다.

같은 `tmux_session`에 동시에 두 prompt를 보내면 transcript와 완료 판정이 꼬일 수 있습니다.

권장 흐름:

```text
POST /conversations/:id/messages
  -> if request.session_id is empty, generate UUID
  -> tmux_session = ctc-csess-<session_id>
  -> acquire lock(session_id)
  -> if tmux exists, reuse it
  -> else if this is a new session, start Claude with --session-id <session_id>
  -> else start Claude with --resume <session_id>
  -> status 또는 wait-ready로 이전 turn 완료 확인
  -> send prompt
  -> stream events until done
  -> include session_id in every client event
  -> collect usage/context/cost
  -> release lock(session_id)
```

CLI 예:

```bash
./claude_tmux_control.py wait-ready "$TMUX_SESSION" --timeout 5
./claude_tmux_control.py send "$TMUX_SESSION" "$USER_PROMPT"
./claude_tmux_control.py stream "$TMUX_SESSION" --timeout 900 --idle 2
```

`wait-ready`가 실패하면 정책을 정해야 합니다.

| 상태 | 권장 처리 |
| --- | --- |
| 아직 working | 새 입력 거절 또는 현재 stream에 attach |
| timeout | UI에 "아직 처리 중" 표시 |
| needs confirmation | session 재시작 또는 운영자 확인 |
| inactive | `start`로 session 재생성 후 입력 |

현재 CLI에는 `ensure`/`ask` 단일 명령이 아직 없습니다.

그래서 앱 서버가 `start -> send -> stream`을 조합합니다.

## 4. Stream Handling

`stream`은 JSONL을 stdout으로 출력합니다.

앱 서버는 child process stdout을 line-by-line으로 읽고, 각 줄을 JSON으로 parse합니다.

```bash
./claude_tmux_control.py stream "$TMUX_SESSION" --timeout 900 --idle 2
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
| `timeout` | 완료 전 timeout | 처리 중/재시도 UI |

예:

```json
{"event":"tool_use","session_id":"550e8400-e29b-41d4-a716-446655440000","timestamp":"2026-05-16T12:00:01.000Z","id":"toolu_...","name":"Bash","input":{"command":"ls"}}
{"event":"tool_result","session_id":"550e8400-e29b-41d4-a716-446655440000","timestamp":"2026-05-16T12:00:02.000Z","tool_use_id":"toolu_...","text":"README.md\n"}
{"event":"assistant_text","session_id":"550e8400-e29b-41d4-a716-446655440000","timestamp":"2026-05-16T12:00:03.000Z","text":"현재 디렉터리는..."}
{"event":"done","session_id":"550e8400-e29b-41d4-a716-446655440000","state":"ready","reason":"prompt visible; transcript ready","answer":"현재 디렉터리는..."}
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

`done`을 받기 전에는 같은 session에 새 `send`를 하지 않습니다.

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

```bash
./claude_tmux_control.py events "$TMUX_SESSION" --tail 200 --json
```

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

```bash
./claude_tmux_control.py events "$TMUX_SESSION" --tail 50 --json
```

향후 `info` 명령이 생기면 `session_id`, `tmux_session`, Claude `sessionId`, transcript path를 한 번에 반환하게 할 예정입니다.

멀티서버는 현재 고려하지 않습니다.

`--resume <session_id>`는 같은 서버의 로컬 Claude transcript를 기준으로 복구합니다.

## 7. Backend Pseudocode

```python
def send_turn(session_id, project_dir, prompt, account_env):
    session_id = session_id or new_uuid()
    tmux_session = f"ctc-csess-{session_id}"
    started_at = monotonic()

    with session_lock(session_id):
        if tmux_exists(tmux_session):
            start_args = None
        elif is_new_session(session_id):
            start_args = ["--session-id", session_id]
        else:
            start_args = ["--resume", session_id]

        if start_args:
            run([
                "./claude_tmux_control.py",
                "start",
                tmux_session,
                "--cwd",
                project_dir,
                "--oauth-token-env",
                account_env,
                "--",
                *start_args,
            ])

        run([
            "./claude_tmux_control.py",
            "wait-ready",
            tmux_session,
            "--timeout",
            "5",
        ])

        run(["./claude_tmux_control.py", "send", tmux_session, prompt])

        for event in stream_jsonl([
            "./claude_tmux_control.py",
            "stream",
            tmux_session,
            "--timeout",
            "900",
            "--idle",
            "2",
        ]):
            event["session_id"] = session_id
            push_to_client(session_id, event)
            if event["event"] == "done":
                finished_at = monotonic()
                metrics = collect_metrics(tmux_session)
                metrics["elapsed_ms"] = int((finished_at - started_at) * 1000)
                push_to_client(session_id, {"event": "metrics", "session_id": session_id, **metrics})
                return event["answer"]
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

- `ensure <session-id>`: session 생성/재사용을 machine-readable metadata로 반환
  - `session-id`가 없으면 UUID 생성
  - 첫 시작이면 `claude --session-id <uuid>`
  - tmux가 없고 기존 `session-id`가 있으면 `claude --resume <uuid>`
- `ask <session-id>`: `ensure + send + stream`을 하나로 묶은 명령
- `info <session-id>`: tmux/Claude transcript/sessionId metadata 반환
- 모든 stream/done/answer JSON 응답에 `session_id` 포함
- `status --json`, `answer --json`, `turn --json`
- `stream`의 `done` event에 usage/context summary 포함
- session별 send lock 구현
- transcript rotation follow
- cost 계산을 위한 model/usage extraction helper

이 gap이 구현되면 이 문서를 같이 갱신해야 합니다.
