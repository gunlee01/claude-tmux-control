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
  -> persist session_id, optional done.answer, metrics
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
TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc-csess- --dry-run
TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc-csess-
```

`--dry-run`으로 먼저 결과를 확인합니다.

web session만 정리하려면 `--prefix ctc-csess-`를 권장합니다.

`--prefix ctc-`는 controlled 전체 정리용입니다. high-level `ctc-csess-*`뿐 아니라 사용자가 low-level로 만든 `ctc-*` tmux session도 state file이 있으면 대상이 될 수 있습니다.

## 4. reap 안전 정책

`reap`은 다음 조건을 모두 만족할 때만 session을 종료합니다.

| 조건 | 설명 |
| --- | --- |
| prefix match | 지정한 prefix 대상. web-only 권장은 `ctc-csess-` |
| idle 초과 | 마지막 입력/상태 기준 `--idle-seconds` 초과 |
| no active work | high-level `active_turn`이 없거나 `ready` 상태 |
| not working | transcript/screen 기준 working이 아님 |

high-level `ctc-csess-<SESSION_ID>` session은 `~/.cache/claude-tmux-control/sessions/<SESSION_ID>.json` state file의 mtime을 idle 기준으로 사용합니다.

오래됐어도 high-level `active_turn`이 남아 있고 `ready`가 아니면 종료하지 않습니다.

`timeout`이나 `interrupted`도 입력 가능 또는 정리 가능 신호가 아닙니다. 이런 session은 `stream --attach`, 같은 `session_id` 재시도, 또는 운영자 판단에 따른 `kill`로 별도 처리합니다.

## 5. 수동 종료

특정 tmux session을 종료할 때:

```bash
TERM=xterm-256color ctc kill "ctc-csess-$SESSION_ID"
```

tmux session을 직접 종료해도 Claude process는 같이 종료됩니다.

CLI `kill`도 high-level bridge state나 Claude transcript를 삭제하지는 않습니다.

즉, 이것은 대화 기록 삭제가 아니라 process stop입니다. 같은 `SESSION_ID`로 다음 `stream`을 호출하면 남아 있는 state/transcript를 기준으로 다시 `--resume`될 수 있습니다.

low-level `start work` 같은 직접 tmux session에서는 같은 이름의 local state file도 함께 제거됩니다.

## 6. 장애/복구 처리

| 상황 | 권장 처리 |
| --- | --- |
| `turn_in_progress` | 같은 session_id로 `stream --attach`, `replay --last 1`, 또는 잠시 후 재시도 |
| 사용자 취소 요청 | `cancel "$SESSION_ID"` 후 `last --last 1` 또는 `stream --attach`로 완료까지 수신 |
| timeout | UI에 처리 중 표시, attach/retry 허용 |
| Ctrl+C/interrupted | attach 가능. tmux가 없으면 다음 stream에서 즉시 복구 |
| tmux session missing | 같은 session_id로 stream 호출 시 resume/start 경로 사용 |
| cwd mismatch | 잘못된 session 연결로 보고 거절 |
| needs confirmation | 운영자 확인 또는 session 재시작 |

`stream --attach`는 완료된 과거 turn 조회가 아닙니다.

해당 session에 `active_turn`이 남아 있고, 내부 tmux session과 transcript를 찾을 수 있을 때만 사용합니다.

진행 중 응답을 취소하려면 `cancel`로 내부 tmux pane에 `Escape`를 보냅니다.

```bash
ctc cancel "$SESSION_ID"
ctc last "$SESSION_ID" --last 1
```

`cancel`은 완료 판정을 하지 않습니다. 취소 뒤에는 `last` 또는 `stream --attach`로 `done`/`metrics`까지 이어서 받습니다.

tool 실행 중 ESC로 취소된 경우 Claude Code transcript에는 tool rejection과 user interrupt marker가 남을 수 있습니다. 이 패턴은 CLI가 취소 완료로 처리하므로, `done.answer`가 없어도 `done`/`metrics`가 출력되면 다음 입력을 받을 수 있습니다.

이미 완료된 답변의 최종값만 필요하면 앱 서버가 저장한 `done.answer` 또는 `ctc info "$SESSION_ID" --json`의 state를 확인합니다.

완료된 최근 turn의 JSONL event를 다시 받아야 하면 `replay`를 사용합니다.

```bash
ctc replay "$SESSION_ID" --last 1
ctc replay "$SESSION_ID" --last 5
ctc last "$SESSION_ID" --last 1
```

마지막 turn이 아직 진행 중이면 `last`/`replay`는 새 prompt를 보내지 않고 현재 `active_turn`에 attach해서 `done`/`metrics`까지 이어서 출력합니다. `--last N`에서는 완료 turn replay와 active turn attach가 같은 stdout stream에 이어서 나올 수 있습니다.

## 7. 권장 cron

```cron
* * * * * cd /path/to/claude-tmux-control && TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc-csess- >> logs/reap.log 2>&1
```

운영 로그에는 OAuth token이나 사용자 secret이 남지 않게 합니다.

## 8. 점검 명령

관리 중인 세션 목록:

```bash
TERM=xterm-256color ctc list --json
```

특정 세션 확인:

```bash
TERM=xterm-256color ctc info "$SESSION_ID" --json
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
- [ ] 장애 대응에 `info`, `list`, `stream --attach`, `cancel`, `kill`, `reap` 절차를 둔다.
