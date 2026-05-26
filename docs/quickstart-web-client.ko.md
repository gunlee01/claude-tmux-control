# Web Client Quickstart

[English](./quickstart-web-client.md) | [한국어](./quickstart-web-client.ko.md)

이 문서는 웹 채팅앱이나 외부 프로그램이 `claude_tmux_control.py`를 어떤 순서로 호출해야 하는지 정리합니다.

명령별 상세 옵션은 [CLI Manual](./cli-manual.ko.md)을 기준으로 봅니다.

## 1. 기본 원칙

외부 클라이언트는 high-level `stream`을 기본 API로 사용합니다.

```bash
TERM=xterm-256color ctc stream --cwd "$PROJECT_DIR" --session-id "$SESSION_ID" "$USER_PROMPT"
```

운영 웹앱에서는 앱 서버가 UUID를 먼저 생성해 `--session-id`로 넘기는 방식을 권장합니다.

`--session-id`를 생략해도 됩니다.

생략하면 CLI가 UUID를 생성하고, 모든 stream event에 `session_id`를 포함합니다. 클라이언트는 첫 event의 `session_id`를 저장해서 이후 turn에 다시 넘기면 됩니다.

같은 채팅방은 같은 `SESSION_ID`를 계속 사용합니다.

CLI는 내부적으로 `ctc-csess-<SESSION_ID>` tmux session을 만들거나 재사용합니다.

## 2. 새 대화 시작

1. 앱 서버가 UUID를 만듭니다. 또는 `--session-id`를 생략하고 첫 stream event의 `session_id`를 저장합니다.
2. `stream --cwd ... [--session-id ...] "$PROMPT"`를 실행합니다.
3. stdout JSONL을 한 줄씩 읽어 UI에 반영합니다.
4. `done`을 받으면 최종 답변을 확정합니다.
5. 바로 이어지는 `metrics`를 저장하고 UI에 표시합니다.

```bash
SESSION_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"

TERM=xterm-256color ctc stream \
  --cwd "$PROJECT_DIR" \
  --session-id "$SESSION_ID" \
  --timeout 300 \
  "$USER_PROMPT"
```

## 3. 이벤트 처리 순서

일반적인 한 turn의 이벤트 순서는 다음과 같습니다.

```text
user
thinking
tool_use
tool_result
assistant_text
done
metrics
```

모든 이벤트는 JSONL입니다.

클라이언트는 `event_id`로 중복 replay를 제거할 수 있습니다.

| event | UI 처리 |
| --- | --- |
| `user` | 사용자 입력 echo 또는 로그 |
| `thinking` | 진행 중 표시. 숨겨진 reasoning text는 제공되지 않을 수 있음 |
| `tool_use` | 도구 실행 카드 표시 |
| `tool_result` | 도구 결과 preview 표시 |
| `assistant_text` | 답변 본문 append |
| `done` | 최종 answer 확정, 입력창 활성화 |
| `metrics` | elapsed/token/cost 표시. transcript에 context 정보가 있으면 context도 표시 |
| `timeout` | 처리 중/재연결 상태 표시 |

일반 완료 turn에서는 `done.answer`가 최종 답변입니다.

취소되었거나 tool 실행 중 interrupt된 turn은 final assistant text 없이 끝날 수 있습니다. 이 경우 `done.answer`가 없을 수 있지만, `done`과 `metrics`가 오면 입력창을 다시 열 수 있습니다.

`metrics`는 `done` 직후 별도로 옵니다.

## 4. 다음 턴 보내기

같은 채팅방의 다음 입력은 같은 `SESSION_ID`로 보냅니다.

```bash
TERM=xterm-256color ctc stream \
  --cwd "$PROJECT_DIR" \
  --session-id "$SESSION_ID" \
  "$NEXT_PROMPT"
```

CLI는 새 prompt를 보내기 전에 이전 `active_turn`을 확인합니다.

| 이전 상태 | CLI 동작 |
| --- | --- |
| 완료 확인됨 | 이전 turn을 state-only finalize 후 새 prompt 전송 |
| 아직 working | `turn_in_progress` |
| 사용자 취소 | `cancel` 호출 후 `last` 또는 `attach`로 완료 대기 |
| timeout/interrupted + tmux ready + transcript ready | 이전 turn finalize 후 새 prompt 전송 |
| timeout/interrupted + 아직 미확인 | `turn_in_progress` 또는 attach 필요 |

주요 exit code:

| code | 처리 |
| --- | --- |
| `2` | 요청 오류 또는 session/transcript 없음. stderr 확인 |
| `3` | timeout. UI는 처리 중/재연결 상태 유지 |
| `5` | `turn_in_progress` 같은 high-level state error. 새 입력을 큐에 넣거나 attach |
| `127` | `tmux` 또는 Claude Code 설치/경로 문제 |
| `130` | client interrupt. 같은 session id로 attach 또는 다음 요청 시 완료 검사 |

## 5. 연결 끊김과 재연결

브라우저나 앱 서버 연결이 끊겼다면 새 prompt를 보내지 말고 먼저 attach할 수 있습니다.

```bash
TERM=xterm-256color ctc stream --attach --session-id "$SESSION_ID" --timeout 300
```

`attach`는 기존 `active_turn` transcript를 다시 읽습니다.

새 입력은 보내지 않습니다.

성공 조건:

- `--session-id`가 필요합니다.
- 해당 session에 `active_turn`이 남아 있어야 합니다.
- `active_turn.stream_state`가 `active`, `timeout`, `interrupted` 중 하나여야 합니다.
- 내부 tmux session과 transcript를 찾을 수 있어야 합니다.

`attach`는 완료된 과거 turn 조회가 아닙니다.

이미 완료된 마지막 답변은 앱 서버가 저장한 `done.answer` 또는 `info`의 `last_turn`/state를 사용합니다. final answer가 없는 취소 turn도 있으므로, turn 완료 여부는 `done`/`metrics`를 기준으로 판단합니다.

사용자가 진행 중 응답을 취소하면 `cancel`을 호출합니다.

```bash
TERM=xterm-256color ctc cancel "$SESSION_ID"
TERM=xterm-256color ctc last "$SESSION_ID" --last 1
```

`cancel`은 `Escape` key만 보내고 JSON 결과를 반환합니다. 취소 후에는 `last` 또는 `stream --attach`로 `done`/`metrics`까지 받습니다.

tool 실행 중 취소된 turn은 final answer 없이 닫힐 수 있습니다. 이 경우에도 `done`과 `metrics`가 오면 해당 turn은 완료된 것으로 보고 다음 입력을 허용합니다.

## 6. 세션 상태 확인

앱 서버가 세션 상태를 확인할 때는 `info`를 사용합니다.

```bash
TERM=xterm-256color ctc info "$SESSION_ID" --json
```

주로 확인할 필드:

| field | 의미 |
| --- | --- |
| `tmux_active` | tmux session 생존 여부 |
| `active_turn` | 진행 중이거나 복구 대상인 turn |
| `last_turn` | 마지막 완료/실패/복구 turn |
| `completed_turn_count` | 저장된 완료 turn 수 |
| `usage_totals` | 세션 누적 token |
| `cost_totals` | 세션 누적 비용 |

완료된 최근 turn event를 다시 받아야 하면 `replay`를 사용합니다.

```bash
TERM=xterm-256color ctc replay "$SESSION_ID" --last 1
TERM=xterm-256color ctc replay "$SESSION_ID" --last 5
TERM=xterm-256color ctc last "$SESSION_ID" --last 1
```

마지막 turn이 진행 중이면 `last`/`replay`는 현재 `active_turn`에 attach해서 완료될 때까지 JSONL을 출력합니다. `--last N`은 완료 turn replay와 active turn attach를 한 stdout stream 안에서 이어서 보낼 수 있습니다.

## 7. 오래된 세션 정리

tmux session은 별도 정리하지 않으면 계속 살아있습니다.

운영 서버에서는 scheduler가 `reap`을 주기적으로 실행해야 합니다.

먼저 dry-run으로 확인합니다.

```bash
TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc-csess- --dry-run
```

확인 후 실제 정리합니다.

```bash
TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc-csess-
```

`reap`은 high-level `active_turn`이 남아 있으면 먼저 같은 session transcript로 완료 처리할 수 있는지 확인합니다. 완료 처리할 수 없어도 tmux 화면이 `ready`이면 idle 기준에 따라 정리할 수 있고, `ready`가 아니면 보수적으로 skip합니다.

`timeout`이나 `interrupted` session도 자동 정리 대상으로 가정하지 말고, 먼저 `attach`/retry로 상태를 확인하거나 운영자 판단에 따라 `kill`합니다.

web session만 정리하려면 `ctc-csess-` prefix를 권장합니다. `ctc-` prefix는 controlled 전체 정리용이라 low-level `ctc-*` session도 포함될 수 있습니다.

cron 예:

```cron
* * * * * cd /path/to/claude-tmux-control && TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc-csess- >> logs/reap.log 2>&1
```

자세한 운영 정책은 [Operations Guide](./operations.ko.md)를 봅니다.

## 8. 권장 서버 흐름

```text
POST /chat
  -> validate project/cwd/session_id
  -> spawn CLI stream process
  -> read stdout line by line
  -> forward JSONL/SSE/WebSocket events to browser
  -> on done: enable input
  -> on metrics: persist usage/cost
  -> on timeout: show reconnect state

background scheduler
  -> reap idle controlled tmux sessions
```

## 9. 최소 구현 체크리스트

- [ ] 같은 채팅방에 같은 `SESSION_ID`를 저장한다.
- [ ] stdout JSONL을 line-buffered로 읽는다.
- [ ] `event_id`로 중복 event를 제거한다.
- [ ] `done` 전에는 입력창을 비활성화한다.
- [ ] `metrics`를 별도 event로 저장한다.
- [ ] timeout/interrupted 후 `attach` 또는 같은 session_id 재시도를 지원한다.
- [ ] `reap`을 scheduler에 등록한다.
- [ ] token/env 값은 로그에 남기지 않는다.
