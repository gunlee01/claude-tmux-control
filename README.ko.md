# claude-tmux-control

[English](./README.md) | [한국어](./README.ko.md)

[![Latest release](https://img.shields.io/github/v/release/gunlee01/claude-tmux-control?sort=semver)](https://github.com/gunlee01/claude-tmux-control/releases/latest)

`claude-tmux-control`은 Claude Code를 `tmux` 안에서 interactive CLI로 실행하고, 외부 프로그램이 한 턴 단위로 입력과 스트림 응답을 주고받게 하는 bridge CLI입니다.

`claude -p`처럼 매번 one-shot process를 새로 호출하는 대신, 같은 Claude Code session을 유지하면서 웹 채팅앱이나 다른 서버 프로그램에서 사용할 수 있게 만드는 것이 목적입니다.

## Command Model

웹 클라이언트와 외부 프로그램은 high-level API를 기본으로 사용합니다.

| 용도 | 명령 | 인자 의미 |
| --- | --- | --- |
| 한 turn 실행/stream | `ctc stream --cwd PATH [--session-id UUID] PROMPT` | bridge session UUID |
| 진행 중 turn 재연결 | `ctc stream --attach --session-id UUID` | bridge session UUID |
| 진행 중 turn 취소 | `ctc cancel UUID` | bridge session UUID |
| 완료된 turn replay | `ctc last UUID --last N` 또는 `ctc replay UUID --last N` | bridge session UUID |
| 세션 상태 조회 | `ctc info UUID --json` | bridge session UUID |
| 세션 목록 조회 | `ctc list --json` | high-level bridge sessions |
| 오래된 web process 정리 | `ctc reap --idle-seconds N --prefix ctc-csess-` | high-level web tmux prefix |

Low-level tmux 명령도 있지만, 웹 클라이언트 계약이 아니라 디버깅/수동 smoke test용입니다.

| 용도 | 명령 | 인자 의미 |
| --- | --- | --- |
| 직접 tmux 세션 시작 | `ctc start TMUX_SESSION` | 임의의 tmux session name. 예: `work` |
| 직접 prompt 전송 | `ctc send TMUX_SESSION PROMPT` | tmux session name |
| 직접 답변/turn 조회 | `ctc answer/turn/events TMUX_SESSION` | tmux session name |

`ctc start work`는 유효한 명령입니다.

다만 `work`는 bridge `session_id`가 아니라 사용자가 직접 정한 tmux session name입니다.

웹 채팅앱에서는 보통 `ctc start work`를 호출하지 않고, `ctc stream --cwd ... [--session-id ...] ...`만 호출합니다.

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
pipx install git+https://github.com/gunlee01/claude-tmux-control.git
ctc --help
ctc --version
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
pip install git+https://github.com/gunlee01/claude-tmux-control.git
ctc --help
ctc --version
```

HTTPS 인증이 필요한 환경이면 먼저 git credential 설정을 마친 뒤 설치합니다.

```bash
git ls-remote https://github.com/gunlee01/claude-tmux-control.git
```

## Docker

Docker 이미지로도 실행할 수 있습니다.

이미지는 Python 3, `tmux`, Claude Code CLI, `ctc`를 포함하고, container 시작 시 Claude Code first-run prompt를 미리 처리합니다.

```bash
docker build -t claude-tmux-control -f docker/Dockerfile .
```

```bash
docker run --rm -it \
  -e CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  -v "$PWD":/repo \
  -w /repo \
  claude-tmux-control \
  ctc stream --cwd /repo "현재 프로젝트를 요약해줘"
```

Docker entrypoint는 `~/.claude.json`, `~/.claude/settings.json`을 preseed하고, `claude -p` preflight로 managed settings cache를 먼저 만듭니다.

자세한 동작 원리와 운영용 volume/reap 예시는 [Docker Guide](./docs/docker.ko.md)를 봅니다.

## License

This project is licensed under the [Apache License 2.0](./LICENSE).

Claude Code itself is distributed separately by Anthropic and is subject to its own license and terms.

## claude -p 대체 Stream 사용

한 번 질문하고 응답을 스트림으로 받으려면 `stream`을 사용합니다.

```bash
SESSION_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"

TERM=xterm-256color ctc stream \
  --cwd "$PWD" \
  --session-id "$SESSION_ID" \
  "현재 프로젝트 구조를 설명해줘"
```

운영 웹앱에서는 앱 서버가 UUID를 먼저 생성해 `--session-id`로 넘기는 방식을 권장합니다.

`--session-id`를 생략해도 됩니다. 이 경우 CLI가 UUID를 생성하고, 모든 stream event에 `session_id`를 포함합니다. 클라이언트는 첫 event의 `session_id`를 저장해서 이후 turn에 다시 넘기면 됩니다.

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

일반 완료 turn의 최종 답변은 `done.answer`에 들어 있습니다.

취소되었거나 tool 실행 중 interrupt된 turn은 final assistant text 없이 닫힐 수 있습니다. 이 경우 `done.answer`가 없을 수 있지만, `done`과 `metrics`가 오면 해당 turn은 완료된 것으로 봅니다.

토큰, elapsed time, 비용 추정치는 `done` 직후의 `metrics` 이벤트로 전달됩니다.

`context`는 Claude Code transcript가 context 계열 필드를 제공할 때만 `metrics.context`로 전달됩니다. 정보가 없으면 추정값을 넣지 않고 필드 자체를 생략합니다.

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

이 상황의 exit code는 `5`입니다.

자주 보는 exit code:

| code | 의미 |
| --- | --- |
| `2` | 요청 오류 또는 session/transcript 없음 |
| `3` | timeout 또는 ready 대기 실패 |
| `5` | high-level runtime state error. 예: `turn_in_progress` |
| `127` | `tmux` 또는 Claude Code executable 없음 |
| `130` | client interrupt |

클라이언트는 이 응답을 받으면 새 메시지를 큐에 넣거나, 입력창을 잠시 비활성화하고 기존 turn에 attach해야 합니다.

```bash
TERM=xterm-256color ctc stream --attach --session-id "$SESSION_ID" --timeout 300
```

사용자가 진행 중인 응답을 취소하면 `cancel`로 Claude Code pane에 `Escape`를 보냅니다.

```bash
TERM=xterm-256color ctc cancel "$SESSION_ID"
TERM=xterm-256color ctc last "$SESSION_ID" --last 1
```

`cancel`은 취소 key만 전송합니다. 취소 후 transcript/화면이 완료 상태가 되면 `last` 또는 `stream --attach`가 이어서 `done`/`metrics`까지 받습니다.

오래된 `active_turn`을 명시적으로 포기해야 하면 `--reset`을 붙여 `active_turn`을 `last_turn`으로 옮기고 비울 수 있습니다.

high-level `stream`이 `--timeout`에 도달하면 timeout을 취소 경계로 처리합니다. `timeout` event를 출력하고 `Escape`를 보낸 뒤 tmux session을 종료합니다. cleanup이 성공하면 해당 turn을 `last_turn`에 timeout으로 남기고 `active_turn`을 비워 다음 prompt를 resume 경로로 받을 수 있게 합니다.

```bash
TERM=xterm-256color ctc cancel "$SESSION_ID" --reset
```

Claude Code는 tool 실행 중 ESC를 받으면 transcript에 `User rejected tool use`와 `[Request interrupted by user for tool use]`를 남길 수 있습니다. CLI는 이 패턴을 취소 완료로 보고 해당 turn을 `done`/`metrics`로 닫습니다.

브라우저 연결이 끊긴 뒤 같은 turn을 이어서 볼 때도 동일하게 `stream --attach`를 사용합니다.

`attach`는 완료된 과거 turn을 다시 조회하는 명령이 아닙니다.

`active_turn`이 남아 있는 진행 중, timeout, interrupted turn에 다시 붙을 때만 사용합니다. 일반 stream timeout은 Escape/session cleanup 성공 후 `active_turn`을 비우므로 attach 대상이 아닙니다. 이미 완료되어 `active_turn`이 정리된 turn의 최종 답변은 `info`의 `last_turn`/state나 앱 서버가 저장해 둔 `done.answer`를 봅니다. 취소된 turn처럼 final answer가 없는 경우도 있으므로, completion 판단은 `done`/`metrics` 도착 여부를 기준으로 둡니다.

이미 완료된 turn의 JSONL event를 다시 받아야 하면 `replay`를 사용합니다.

```bash
TERM=xterm-256color ctc replay "$SESSION_ID" --last 1
TERM=xterm-256color ctc replay "$SESSION_ID" --last 5
TERM=xterm-256color ctc last "$SESSION_ID" --last 1
```

마지막 turn이 아직 진행 중이면 `last`/`replay`는 새 prompt를 보내지 않고 현재 `active_turn`에 attach해서 `done`/`metrics`까지 이어서 출력합니다. `--last N`에서 마지막 turn이 진행 중이면, 이전 완료 turn들을 먼저 replay한 뒤 진행 중 turn을 attach합니다.

세션 상태 확인:

```bash
TERM=xterm-256color ctc info "$SESSION_ID" --json
```

## Session Cleanup

`ctc stream`은 내부적으로 `ctc-csess-<SESSION_ID>` tmux session을 만듭니다.

tmux session은 자동으로 사라지지 않습니다. 운영 서버에서는 `reap`을 주기적으로 실행해서 오래된 session을 정리해야 합니다.

먼저 dry-run으로 정리 대상을 확인합니다.

```bash
TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc-csess- --dry-run
```

문제가 없으면 실제 정리를 실행합니다.

```bash
TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc-csess-
```

`reap`은 daemon이 아닙니다. 한 번 scan하고 종료합니다.

cron, systemd timer, app scheduler 중 하나로 반복 실행합니다.

cron 예:

```cron
* * * * * TERM=xterm-256color /path/to/ctc reap --idle-seconds 1800 --prefix ctc-csess- >> /var/log/ctc-reap.log 2>&1
```

정리 기준:

- web session만 정리하려면 `ctc-csess-` prefix를 권장합니다.
- `ctc-` prefix는 controlled 전체 정리용이며, 사용자가 low-level로 만든 `ctc-*` tmux session도 포함할 수 있습니다.
- 마지막 입력/상태 기준으로 `--idle-seconds`를 넘은 session만 정리합니다.
- high-level `active_turn`이 남아 있으면 먼저 같은 session transcript로 완료 처리할 수 있는지 확인합니다.
- 완료 처리할 수 없어도 tmux 화면이 `ready`이면 idle 기준에 따라 정리할 수 있습니다.
- tmux 화면이 `ready`가 아니거나 Claude가 아직 working 상태로 보이면 정리하지 않습니다.

`interrupted`는 입력 가능 또는 정리 가능 신호가 아닙니다. 남아 있는 `timeout` active turn은 Escape 전송/정리가 끝나지 않은 상태이므로 inspect 또는 `cancel --reset`으로 별도 처리합니다.

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

Claude process에 추가 env가 필요하면 project env file 또는 명시적 whitelist를 씁니다.

기본적으로 high-level 명령은 `--cwd` 아래의 `.ctc.env`가 있으면 읽습니다. 다른 파일은 `--env-file PATH`로 지정하고, 현재 `ctc` process env에서 특정 key만 복사하려면 `--env NAME`을 씁니다.

```bash
SERVICE_API_KEY="$TOKEN" \
TERM=xterm-256color ctc stream \
  --cwd "$PWD" \
  --env SERVICE_API_KEY \
  "hello"
```

env 주입은 새 tmux session을 만들 때만 적용됩니다. 기존 session은 시작 당시 env를 유지합니다. `CLAUDE_CODE_OAUTH_TOKEN`은 `--oauth-token-env` 전용이라 `.ctc.env`나 `--env`에서는 거절됩니다.

## Claude Launch Options

bridge는 항상 고정된 `claude` 실행 파일을 사용합니다.

model 선택은 `--model MODEL`을 씁니다.

신뢰된 추가 Claude Code option은 `--claude-args "ARGS"`로 전달합니다.

```bash
TERM=xterm-256color ctc stream \
  --cwd "$PWD" \
  --model opus \
  --claude-args "--add-dir ../shared" \
  "hello"
```

이 옵션들은 새 Claude Code process를 시작할 때만 적용됩니다. 기존 tmux session은 시작 당시 model과 argument를 유지합니다.

Claude Code 실행에는 기본적으로 `--dangerously-skip-permissions`가 붙습니다. 이 값은 non-interactive service flow에는 편하지만, Claude Code가 action별 승인 없이 tool을 실행할 수 있다는 뜻입니다. 제한된 project directory, container, dedicated service user 같은 통제된 환경에서만 쓰세요.

기본 permission 동작을 바꾸려면 신뢰된 `--claude-args`로 Claude Code permission option을 전달합니다.

```bash
TERM=xterm-256color ctc stream \
  --cwd "$PWD" \
  --claude-args "--permission-mode plan" \
  "hello"
```

`--claude-args`에 `--permission-mode ...` 또는 `--dangerously-skip-permissions`가 이미 있으면 bridge는 `--dangerously-skip-permissions`를 추가로 붙이지 않습니다.

token, transcript, Docker, `--dangerously-skip-permissions` 관련 주의사항은 [Security Guide](./docs/security.ko.md)를 봅니다.

## Examples

실행 가능한 예제는 [examples](./examples/README.ko.md)에 있습니다.

- `shell-stream.sh`: `ctc stream`을 감싼 one-turn shell wrapper
- `web-client-minimal.py`: stdout JSONL을 읽는 최소 backend-style consumer
- `docker-compose.yml`: Claude config와 bridge state를 volume으로 유지하는 예시

## Useful Commands

웹/외부 프로그램에서 주로 쓰는 high-level 명령입니다.

```bash
ctc list --json
ctc info "$SESSION_ID" --json
ctc reap --idle-seconds 1800 --prefix ctc-csess- --dry-run
ctc reap --idle-seconds 1800 --prefix ctc-csess-
```

## Operational Process Stop

특정 tmux process를 강제로 멈출 때만 `kill`을 씁니다.

`kill`의 인자는 bridge `session_id`가 아니라 tmux session name입니다.

```bash
ctc kill "ctc-csess-$SESSION_ID"
```

이 명령은 tmux session과 그 안의 Claude Code process를 종료합니다.

high-level bridge state와 Claude transcript를 삭제하지는 않습니다. 같은 `SESSION_ID`로 다음 `stream`을 호출하면 남아 있는 state/transcript를 기준으로 다시 `--resume`될 수 있습니다.

웹 클라이언트의 일반 흐름에서는 `kill`보다 주기적인 `reap`으로 오래 idle 상태인 session을 정리하는 편이 낫습니다.

## Low-level Debug Commands

아래 명령은 웹 클라이언트의 기본 계약이 아니라, 사람이 tmux session을 직접 만들고 확인하는 디버깅용입니다.

`work`는 예시 tmux session name입니다. bridge `session_id` UUID가 아닙니다.

여기에 `ctc-csess-$SESSION_ID`를 넣지 마세요.

high-level session을 만들거나 이어가려면 `ctc stream --cwd "$PWD" --session-id "$SESSION_ID" "$PROMPT"`를 사용하세요.

진행 중인 turn에 다시 붙을 때만 `ctc stream --attach --session-id "$SESSION_ID"`를 사용합니다.

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

- [CLI Manual](./docs/cli-manual.ko.md)
- [Web Client Quickstart](./docs/quickstart-web-client.ko.md)
- [Web Chat Integration Guide](./docs/web-chat-integration-guide.ko.md)
- [Operations Guide](./docs/operations.ko.md)
- [Docker Guide](./docs/docker.ko.md)
- [Security Guide](./docs/security.ko.md)
- [Release Guide](./docs/release.ko.md)
