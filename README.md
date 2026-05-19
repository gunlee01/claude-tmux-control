# claude-tmux-control

`claude-tmux-control`은 Claude Code를 `tmux` 안에서 interactive CLI로 실행하고, 외부 프로그램이 한 턴 단위로 입력과 스트림 응답을 주고받게 하는 bridge CLI입니다.

`claude -p`처럼 매번 one-shot process를 새로 호출하는 대신, 같은 Claude Code session을 유지하면서 웹 채팅앱이나 다른 서버 프로그램에서 사용할 수 있게 만드는 것이 목적입니다.

## Requirements

- Python 3.10+
- `tmux`
- Claude Code CLI (`claude`)
- Claude Code 인증 설정 또는 `CLAUDE_CODE_OAUTH_TOKEN`

서버 환경에서 terminal type 문제가 나면 `TERM=xterm-256color`를 붙여 실행합니다.

```bash
TERM=xterm-256color ctc --help
```

## Installation

권장 설치 방식은 `pipx`입니다.

```bash
pipx install git+https://oss.navercorp.com/gunh-lee/claude-tmux-control.git
ctc --help
```

같은 패키지는 두 command를 설치합니다.

```bash
ctc --help
claude-tmux-control --help
```

`pipx`를 쓰기 어렵다면 venv에 설치할 수 있습니다.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install git+https://oss.navercorp.com/gunh-lee/claude-tmux-control.git
ctc --help
```

HTTPS 인증이 필요한 환경이면 먼저 git credential 설정을 마친 뒤 설치합니다.

```bash
git ls-remote https://oss.navercorp.com/gunh-lee/claude-tmux-control.git
```

## claude -p 대체 Stream 사용

한 번 질문하고 응답을 스트림으로 받으려면 `stream`을 사용합니다.

```bash
SESSION_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"

TERM=xterm-256color ctc stream \
  --cwd "$PWD" \
  --session-id "$SESSION_ID" \
  "현재 프로젝트 구조를 설명해줘"
```

출력은 JSONL입니다. 웹 서버나 클라이언트는 stdout을 line-by-line으로 읽으면 됩니다.

일반적인 이벤트 순서:

```text
user
thinking
tool_use
tool_result
assistant_text
done
metrics
```

최종 답변은 `done.answer`에 들어 있습니다.

토큰, elapsed time, context, 비용 추정치는 `done` 직후의 `metrics` 이벤트로 전달됩니다.

같은 대화창에서 다음 턴을 이어가려면 같은 `SESSION_ID`를 다시 넘깁니다.

```bash
TERM=xterm-256color ctc stream \
  --cwd "$PWD" \
  --session-id "$SESSION_ID" \
  "방금 설명한 내용을 더 짧게 요약해줘"
```

내부적으로는 `ctc-csess-<SESSION_ID>` tmux session을 만들거나 재사용합니다.

## Web Client Flow

웹 채팅앱에서 사용하는 기본 흐름은 다음과 같습니다.

```text
app server
  -> ctc stream --cwd <project> --session-id <uuid> "<prompt>"
  -> stdout JSONL read
  -> SSE/WebSocket으로 browser에 relay
  -> done 수신 후 입력창 활성화
  -> metrics 저장 및 표시
```

사용자가 같은 대화창에서 다음 메시지를 보내면, 같은 `SESSION_ID`로 `stream`을 한 번 더 실행합니다.

```bash
TERM=xterm-256color ctc stream \
  --cwd "$PROJECT_DIR" \
  --session-id "$SESSION_ID" \
  "$NEXT_PROMPT"
```

이 호출은 새 prompt를 Claude Code에 전송합니다.

기존 tmux session이 살아 있으면 그대로 재사용하고, `reap` 등으로 tmux session이 없어졌지만 state/transcript가 남아 있으면 새 tmux session을 만들고 Claude Code를 `--resume <SESSION_ID>`로 실행합니다.

단, 이전 turn이 아직 끝나지 않았거나 완료 여부를 확인할 수 없으면 새 메시지는 거절됩니다.

이때 `ctc stream`은 stderr에 JSON error를 쓰고 exit code `5`로 종료합니다.

```json
{"event":"error","error":"turn_in_progress"}
```

클라이언트는 이 응답을 받으면 새 메시지를 큐에 넣거나, 입력창을 잠시 비활성화하고 기존 turn에 attach해야 합니다.

```bash
TERM=xterm-256color ctc stream --attach --session-id "$SESSION_ID" --timeout 300
```

브라우저 연결이 끊긴 뒤 같은 turn을 이어서 볼 때도 동일하게 `stream --attach`를 사용합니다.

세션 상태 확인:

```bash
TERM=xterm-256color ctc info "$SESSION_ID" --json
```

## Session Cleanup

`ctc stream`은 내부적으로 `ctc-csess-<SESSION_ID>` tmux session을 만듭니다.

tmux session은 자동으로 사라지지 않습니다. 운영 서버에서는 `reap`을 주기적으로 실행해서 오래된 session을 정리해야 합니다.

먼저 dry-run으로 정리 대상을 확인합니다.

```bash
TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc- --dry-run
```

문제가 없으면 실제 정리를 실행합니다.

```bash
TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc-
```

`reap`은 daemon이 아닙니다. 한 번 scan하고 종료합니다.

cron, systemd timer, app scheduler 중 하나로 반복 실행합니다.

cron 예:

```cron
* * * * * TERM=xterm-256color /path/to/ctc reap --idle-seconds 1800 --prefix ctc- >> /var/log/ctc-reap.log 2>&1
```

정리 기준:

- 기본적으로 `ctc-` prefix session만 정리합니다.
- 마지막 입력/상태 기준으로 `--idle-seconds`를 넘은 session만 정리합니다.
- Claude가 아직 working 상태로 보이면 정리하지 않습니다.

`reap`으로 tmux session이 종료되면 그 안에서 실행 중이던 Claude Code process도 같이 종료됩니다.

하지만 Claude Code 대화 session 자체를 못 쓰게 되는 것은 아닙니다.

같은 `SESSION_ID`로 다음 `stream` 요청을 보내면, 기존 tmux session이 없더라도 state/transcript가 남아 있는 경우 새 tmux session을 만들고 Claude Code를 `--resume <SESSION_ID>`로 실행합니다.

```bash
TERM=xterm-256color ctc stream \
  --cwd "$PWD" \
  --session-id "$SESSION_ID" \
  "이전 대화 이어서 설명해줘"
```

즉, `reap`은 누적된 tmux/Claude Code process를 정리하는 운영 절차이고, 저장된 Claude Code session을 의도적으로 삭제하는 기능은 아닙니다.

## Authentication

기본 Claude Code 인증을 그대로 사용할 수 있습니다.

OAuth token을 외부에서 받은 경우에는 환경변수로 넘깁니다.

```bash
CLAUDE_CODE_OAUTH_TOKEN="$TOKEN" \
TERM=xterm-256color ctc stream \
  --cwd "$PWD" \
  --session-id "$SESSION_ID" \
  "hello"
```

계정별 token을 다른 환경변수명으로 관리한다면 `--oauth-token-env`를 사용합니다.

```bash
ACCOUNT_A_TOKEN="$TOKEN" \
TERM=xterm-256color ctc stream \
  --cwd "$PWD" \
  --session-id "$SESSION_ID" \
  --oauth-token-env ACCOUNT_A_TOKEN \
  "hello"
```

Claude Code 실행 command에는 기본적으로 `--dangerously-skip-permissions`가 붙습니다.

## Useful Commands

```bash
ctc list --json
ctc info "$SESSION_ID" --json
ctc kill "ctc-csess-$SESSION_ID"
ctc reap --idle-seconds 1800 --prefix ctc- --dry-run
ctc reap --idle-seconds 1800 --prefix ctc-
```

저수준 tmux session을 직접 다룰 수도 있습니다.

```bash
ctc start work --cwd "$PWD"
ctc send work "현재 디렉터리 구조를 요약해줘"
ctc answer work --wait --timeout 120
ctc turn work --tail 3
ctc events work --json --tail 50
```

자세한 옵션은 CLI manual을 봅니다.

```bash
ctc --help
```

## Limitations

이 프로젝트는 Claude Code의 현재 구현에 의존합니다.

특히 다음 동작은 Claude Code 내부 구현이 바뀌면 깨질 수 있습니다.

- Claude Code transcript JSONL 위치와 schema
- `tool_use`, `tool_result`, `usage`, `context` 필드 구조
- terminal 화면에서 ready/working 상태를 추정하는 방식
- `--session-id`, `--resume`, `CLAUDE_CODE_OAUTH_TOKEN` 같은 Claude Code CLI option/env 동작

즉, 이 CLI는 Claude Code의 공식 안정 프로토콜 위에 만든 SDK가 아닙니다.

Claude Code 업데이트 후에는 `stream`, `done`, `metrics`, `reap` 동작을 smoke test로 확인해야 합니다.

## Documentation

- [CLI Manual](./docs/cli-manual.md)
- [Web Client Quickstart](./docs/quickstart-web-client.md)
- [Web Chat Integration Guide](./docs/web-chat-integration-guide.md)
- [Operations Guide](./docs/operations.md)
