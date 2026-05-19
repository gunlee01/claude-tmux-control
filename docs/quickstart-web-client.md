# Web Client Quickstart

이 문서는 웹 채팅앱이나 외부 프로그램이 `claude_tmux_control.py`를 어떤 순서로 호출해야 하는지 정리합니다.

명령별 상세 옵션은 [CLI Manual](./cli-manual.md)을 기준으로 봅니다.

## 1. 기본 원칙

외부 클라이언트는 high-level `stream`을 기본 API로 사용합니다.

```bash
TERM=xterm-256color ctc stream --cwd "$PROJECT_DIR" --session-id "$SESSION_ID" "$USER_PROMPT"
```

`SESSION_ID`가 없으면 클라이언트가 UUID를 생성해서 넘깁니다.

같은 채팅방은 같은 `SESSION_ID`를 계속 사용합니다.

CLI는 내부적으로 `ctc-csess-<SESSION_ID>` tmux session을 만들거나 재사용합니다.

## 2. 새 대화 시작

1. 앱 서버가 UUID를 만듭니다.
2. `stream --cwd ... --session-id ... "$PROMPT"`를 실행합니다.
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
| `metrics` | elapsed/token/context/cost 표시 |
| `timeout` | 처리 중/재연결 상태 표시 |

`done.answer`가 최종 답변입니다.

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
| timeout/interrupted + tmux ready + transcript ready | 이전 turn finalize 후 새 prompt 전송 |
| timeout/interrupted + 아직 미확인 | `turn_in_progress` 또는 attach 필요 |

## 5. 연결 끊김과 재연결

브라우저나 앱 서버 연결이 끊겼다면 새 prompt를 보내지 말고 먼저 attach할 수 있습니다.

```bash
TERM=xterm-256color ctc stream --attach --session-id "$SESSION_ID" --timeout 300
```

`attach`는 기존 `active_turn` transcript를 다시 읽습니다.

새 입력은 보내지 않습니다.

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

## 7. 오래된 세션 정리

tmux session은 별도 정리하지 않으면 계속 살아있습니다.

운영 서버에서는 scheduler가 `reap`을 주기적으로 실행해야 합니다.

먼저 dry-run으로 확인합니다.

```bash
TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc- --dry-run
```

확인 후 실제 정리합니다.

```bash
TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc-
```

cron 예:

```cron
* * * * * cd /path/to/claude-tmux-control && TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc- >> logs/reap.log 2>&1
```

자세한 운영 정책은 [Operations Guide](./operations.md)를 봅니다.

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
