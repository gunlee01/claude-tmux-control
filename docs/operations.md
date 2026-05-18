# Operations Guide

이 문서는 `claude_tmux_control.py`를 서버에서 운영할 때 필요한 session lifecycle, idle cleanup, 장애 복구 절차를 정리합니다.

## 1. 세션 생명주기

high-level web session은 다음 리소스를 만듭니다.

| 리소스 | 위치/이름 |
| --- | --- |
| tmux session | `ctc-csess-<SESSION_ID>` |
| Claude Code process | tmux pane 내부 |
| transcript JSONL | `~/.claude/projects/<encoded-cwd>/<session>.jsonl` |
| bridge state | `~/.cache/claude-tmux-control/sessions/<SESSION_ID>.json` |
| lock file | `~/.cache/claude-tmux-control/locks/<SESSION_ID>.lock` |

tmux session은 명시적으로 정리하지 않으면 계속 살아있습니다.

## 2. 표준 운영 루프

```text
app server
  -> stream one turn
  -> persist session_id, done.answer, metrics
  -> return UI control to user

scheduler
  -> run reap periodically
  -> kill idle controlled sessions
```

`reap`은 daemon이 아닙니다.

한 번 scan하고 종료하므로 cron, systemd timer, app scheduler 중 하나에서 반복 실행합니다.

## 3. Idle Cleanup

30분 idle 정리 예:

```bash
TERM=xterm-256color ./claude_tmux_control.py reap --idle-seconds 1800 --prefix ctc- --dry-run
TERM=xterm-256color ./claude_tmux_control.py reap --idle-seconds 1800 --prefix ctc-
```

`--dry-run`으로 먼저 결과를 확인합니다.

`--prefix ctc-`는 controlled session만 대상으로 제한합니다.

현재 high-level web session 이름은 `ctc-csess-<SESSION_ID>`이므로 `ctc-` prefix에 포함됩니다.

## 4. reap 안전 정책

`reap`은 다음 조건을 모두 만족할 때만 session을 종료합니다.

| 조건 | 설명 |
| --- | --- |
| prefix match | 기본 `ctc-` prefix 대상 |
| idle 초과 | 마지막 입력/상태 기준 `--idle-seconds` 초과 |
| not working | transcript/screen 기준 working이 아님 |

오래됐어도 Claude가 아직 working이면 종료하지 않습니다.

## 5. 수동 종료

특정 tmux session을 종료할 때:

```bash
TERM=xterm-256color ./claude_tmux_control.py kill "ctc-csess-$SESSION_ID"
```

tmux session을 직접 종료해도 Claude process는 같이 종료됩니다.

다만 bridge state 정합성까지 맞추려면 CLI `kill`을 우선 사용합니다.

## 6. 장애/복구 처리

| 상황 | 권장 처리 |
| --- | --- |
| `turn_in_progress` | 같은 session_id로 `stream --attach` 또는 잠시 후 재시도 |
| timeout | UI에 처리 중 표시, attach/retry 허용 |
| Ctrl+C/interrupted | attach 가능. tmux가 없으면 다음 stream에서 즉시 복구 |
| tmux session missing | 같은 session_id로 stream 호출 시 resume/start 경로 사용 |
| cwd mismatch | 잘못된 session 연결로 보고 거절 |
| needs confirmation | 운영자 확인 또는 session 재시작 |

## 7. 권장 cron

```cron
* * * * * cd /path/to/claude-tmux-control && TERM=xterm-256color ./claude_tmux_control.py reap --idle-seconds 1800 --prefix ctc- >> logs/reap.log 2>&1
```

운영 로그에는 OAuth token이나 사용자 secret이 남지 않게 합니다.

## 8. 점검 명령

관리 중인 세션 목록:

```bash
TERM=xterm-256color ./claude_tmux_control.py list --json
```

특정 세션 확인:

```bash
TERM=xterm-256color ./claude_tmux_control.py info "$SESSION_ID" --json
```

tmux 전체 확인:

```bash
TERM=xterm-256color tmux list-sessions
```

## 9. 운영 체크리스트

- [ ] `tmux`와 `claude` CLI가 PATH에 있다.
- [ ] app server가 `TERM=xterm-256color`를 설정한다.
- [ ] session id는 UUID로 관리한다.
- [ ] 같은 채팅방은 같은 session id를 사용한다.
- [ ] `reap` scheduler를 등록한다.
- [ ] `reap --dry-run`을 먼저 검증한다.
- [ ] 로그에서 OAuth token/env 값을 redaction한다.
- [ ] 장애 대응에 `info`, `list`, `stream --attach`, `kill`, `reap` 절차를 둔다.
