# CLI Manual

[English](./cli-manual.md) | [한국어](./cli-manual.ko.md)

`claude_tmux_control.py`는 Claude Code interactive CLI를 `tmux` 안에서 실행하고, 외부 프로그램이 입력 전달과 응답 조회를 할 수 있게 하는 bridge CLI입니다.

## 1. Quick Start

### Install

권장 방식은 `pipx`로 git repo에서 설치하는 것입니다.

```bash
pipx install git+https://github.com/gunlee01/claude-tmux-control.git
ctc --help
ctc --version
```

`pipx`가 없다면 venv 안에 설치합니다.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install git+https://github.com/gunlee01/claude-tmux-control.git
ctc --help
```

개발 checkout에서는 editable install을 사용합니다.

```bash
pip install -e .
ctc --help
```

설치 후 entrypoint는 두 개입니다.

```bash
ctc --help
claude-tmux-control --help
```

### chmod

소스 파일을 직접 실행하려면 실행 권한을 줍니다.

```bash
chmod +x ./claude_tmux_control.py
```

### Linux terminal fallback

서버에서 `missing or unsuitable terminal: xterm-ghostty`가 나오면 `TERM`을 안정적인 값으로 바꿔 실행합니다.

```bash
TERM=xterm-256color ctc stream --cwd "$PWD" "hello"
```

자주 쓰면 alias로 묶습니다.

```bash
alias ctc='TERM=xterm-256color command ctc'
```

### Web/client stream 시작

웹 클라이언트와 외부 프로그램은 high-level `stream`을 기본 API로 사용합니다.

```bash
SESSION_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"

TERM=xterm-256color ctc stream \
  --cwd "$PWD" \
  --session-id "$SESSION_ID" \
  "현재 디렉터리 구조를 요약해줘"
```

운영 웹앱에서는 앱 서버가 UUID를 먼저 생성해 `--session-id`로 넘기는 방식을 권장합니다.

`--session-id`를 생략해도 됩니다.

생략하면 CLI가 UUID를 생성하고, 모든 stream event에 `session_id`를 포함합니다. 클라이언트는 첫 event의 `session_id`를 저장해서 이후 turn에 다시 넘기면 됩니다.

같은 대화창의 다음 turn은 같은 `SESSION_ID`로 다시 `stream`을 호출합니다.

```bash
TERM=xterm-256color ctc stream \
  --cwd "$PWD" \
  --session-id "$SESSION_ID" \
  "방금 답변을 더 짧게 요약해줘"
```

이 모드에서 `SESSION_ID`는 bridge session UUID입니다.

내부 tmux session name은 `ctc-csess-<SESSION_ID>`로 만들어지지만, 일반적인 웹 클라이언트는 이 값을 직접 넘기지 않습니다.

### Low-level tmux 세션 시작

아래 명령은 사람이 tmux session을 직접 만들고 확인하는 debug/smoke test용입니다.

```bash
ctc start work --cwd "$PWD"
```

`start`는 `work`라는 tmux session이 없으면 새로 만들고, 있으면 재사용합니다.

`work`는 tmux session name입니다. bridge session UUID가 아니며, 웹 클라이언트의 기본 API가 아닙니다.

Claude Code 실행에는 기본적으로 `--dangerously-skip-permissions`가 자동으로 붙습니다.

실행 전에는 `tmux`와 Claude Code executable을 확인합니다.

없으면 tmux session 생성 전에 종료합니다.

```text
tmux not found in PATH.
Install tmux first, then retry.
Example: sudo yum install -y tmux
```

```text
Claude Code executable not found in PATH: claude
Install Claude Code CLI first, then retry.
Example: curl -fsSL https://claude.ai/install.sh | bash
After install, confirm with: claude --version
```

### High-level OAuth token으로 시작

호출 프로세스에 token이 있으면 새 tmux session의 Claude Code process에 `CLAUDE_CODE_OAUTH_TOKEN`으로 전달합니다.

```bash
CLAUDE_CODE_OAUTH_TOKEN="$TOKEN" ctc stream --cwd "$PWD" "hello"
```

계정별 token을 다른 환경변수명으로 들고 있다면 `--oauth-token-env`로 source env를 고릅니다.

```bash
ACCOUNT_A_TOKEN="$TOKEN" ctc stream --cwd "$PWD" --oauth-token-env ACCOUNT_A_TOKEN "hello"
ACCOUNT_B_TOKEN="$TOKEN" ctc stream --cwd "$PWD" --oauth-token-env ACCOUNT_B_TOKEN "hello"
```

low-level `start`에서도 같은 옵션을 쓸 수 있지만, 웹 클라이언트는 보통 `stream --cwd ...`에서 token source를 고릅니다.

### Low-level 입력 보내기

```bash
ctc send work "현재 디렉터리 구조를 요약해줘"
```

### Low-level 완료 대기 후 답변 보기

```bash
ctc wait-ready work --timeout 120
ctc answer work
```

한 번에 기다리고 답변만 보려면:

```bash
ctc answer work --wait --timeout 120
```

## 2. Mental Model

이 CLI는 세 가지 원본을 씁니다.

| 대상 | 원본 | 용도 |
| --- | --- | --- |
| 입력 | `tmux load-buffer` + `paste-buffer` + `send-keys Enter` | Claude Code terminal UI에 prompt 전달 |
| 화면 | `tmux capture-pane` | 사람이 보는 화면, 디버깅, fallback 상태 확인 |
| 응답/이벤트 | Claude Code transcript JSONL | assistant text, thinking, tool_use, tool_result, usage 조회 |

웹 클라이언트의 기본 흐름은 다음과 같습니다.

```text
client/web
  -> ctc stream --cwd PATH [--session-id UUID] [--model MODEL] [--claude-args "ARGS"] PROMPT
    -> internal tmux session ctc-csess-<UUID>
      -> Claude Code interactive CLI
    <- transcript JSONL
  <- JSONL events / done / metrics
```

`stdout`/`stdin`으로 Claude Code를 직접 제어하는 구조가 아닙니다.

Claude Code가 terminal UI를 사용하므로 입력은 tmux pane에 붙여 넣고, 구조화된 응답은 transcript JSONL에서 읽습니다.

## 3. Session Rules

### Session name

low-level 명령의 `SESSION` 인자는 tmux session name입니다.

```bash
ctc start work
ctc send work "hello"
ctc answer work
```

웹/외부 client용 high-level `stream`, `ask`, `info`는 UUID `session_id`를 기준으로 동작합니다.

high-level `stream`은 내부 tmux session name을 `ctc-csess-<session_id>`로 만듭니다.

일반적인 웹 클라이언트는 `ctc-csess-...` 값을 직접 명령 인자로 넘기지 않습니다.

### Reuse

`start`는 같은 tmux session이 이미 있으면 새 Claude Code를 띄우지 않습니다.

```bash
ctc start work
# created session: work

ctc start work
# reused session: work
```

### State file

`send`는 마지막 prompt와 cwd를 local state에 기록합니다.

이 값은 transcript를 고를 때 힌트로 사용됩니다.

권위 있는 session mapping 저장소로 쓰는 것은 아닙니다.

## 4. Command Reference

### `start`

새 tmux session을 만들고 Claude Code를 실행합니다.

```bash
ctc start SESSION [--cwd PATH] [--model MODEL] [--claude-args "ARGS"] [--attach] [--oauth-token-env ENV] [--env-file PATH] [--env NAME]
```

| 옵션 | 의미 |
| --- | --- |
| `--cwd PATH` | 새 session의 working directory |
| `--model MODEL` | 새 Claude Code process에 전달할 model |
| `--claude-args "ARGS"` | 신뢰된 추가 Claude Code CLI argument. shell 실행 없이 parsing |
| `--attach` | 생성/재사용 후 바로 tmux attach |
| `--oauth-token-env ENV` | 이 env 값을 `CLAUDE_CODE_OAUTH_TOKEN`으로 주입 |
| `--env-file PATH` | 새 tmux session에 주입할 추가 env file |
| `--env NAME` | 현재 `ctc` process env에서 특정 key만 복사 |

주의:

- 실행 파일은 항상 `claude`입니다. 임의 shell command는 받지 않습니다.
- permission override가 없으면 `--dangerously-skip-permissions`가 자동으로 붙습니다.
- `--oauth-token-env`, `--env-file`, `--env`는 새 tmux session을 만들 때만 의미가 있습니다.
- `--model`, `--claude-args`도 새 Claude Code process를 시작할 때만 적용됩니다.

Claude Code option 예:

```bash
ctc start work --cwd "$PWD" --model opus
```

추가 Claude Code option이 필요하면 `--claude-args`를 하나의 shell argument로 quote합니다.

```bash
ctc start work --cwd "$PWD" --model opus --claude-args "--add-dir ../shared"
```

실제 실행 command는 대략 다음처럼 됩니다.

```bash
claude --model opus --add-dir ../shared --dangerously-skip-permissions
```

### `launch`

이미 존재하는 tmux session 안에 Claude Code 실행 command를 붙여 넣고 실행합니다.

```bash
ctc launch SESSION [--model MODEL] [--claude-args "ARGS"]
```

주의:

- 기존 shell pane에 command를 입력하는 방식입니다.
- OAuth token env 주입은 하지 않습니다.
- 이미 Claude Code가 실행 중인 pane에는 `launch` 대신 `send`를 씁니다.
- 실행 파일은 항상 `claude`입니다.

### `send`

Claude Code tmux session에 prompt를 붙여 넣습니다.

```bash
ctc send SESSION "prompt text"
```

stdin에서 읽기:

```bash
printf '긴 프롬프트\n여러 줄\n' | ctc send work
```

입력만 붙여 넣고 Enter를 누르지 않기:

```bash
ctc send work --no-enter "draft prompt"
```

### `status`

현재 session 상태를 추정합니다.

```bash
ctc status SESSION
```

출력 예:

```text
working: latest transcript event after user is tool_use
ready: matched ...; transcript ready
needs_confirmation: matched ...
unknown: no ready, working, or confirmation marker matched
```

옵션:

| 옵션 | 의미 |
| --- | --- |
| `--height N` | 화면 판정에 사용할 pane history line 수 |
| `--transcript PATH` | 특정 transcript JSONL을 직접 지정 |
| `--root PATH` | Claude transcript root 지정 |
| `--screen-only` | transcript를 보지 않고 화면만 사용 |

주의:

- 완료 판정은 화면 glyph만 믿으면 안 됩니다.
- 기본은 transcript와 화면을 함께 봅니다.
- prompt 전송 직후 transcript race window에서는 `working`으로 봅니다.

### `wait-ready`

Claude Code가 입력 가능한 상태로 돌아올 때까지 기다립니다.

```bash
ctc wait-ready SESSION --timeout 120
```

옵션:

| 옵션 | 의미 |
| --- | --- |
| `--timeout SEC` | 최대 대기 시간 |
| `--interval SEC` | polling 간격 |
| `--idle SEC` | ready 화면이 안정적으로 유지되어야 하는 시간 |
| `--screen-only` | transcript를 보지 않고 화면만 사용 |

exit code:

| code | 의미 |
| --- | --- |
| `0` | ready |
| `3` | timeout 또는 ready가 아님 |

### `answer`

특정 session의 assistant text 답변만 출력합니다.

```bash
ctc answer SESSION
```

완료까지 기다린 뒤 출력:

```bash
ctc answer SESSION --wait --timeout 120
```

최근 N개 답변 출력:

```bash
ctc answer SESSION --tail 3
```

특징:

- assistant `text` block만 출력합니다.
- `thinking`, `tool_use`, `tool_result`는 제외합니다.
- `SESSION`에 맞는 state/cwd transcript를 찾지 못하면 실패합니다. 최신 transcript 전체로 fallback하지 않습니다.
- low-level tmux session 디버깅용입니다. 웹 UI의 최종 채팅 본문은 high-level `stream`의 `done.answer` 또는 `ask`의 `ask_result.answer`를 사용합니다. 취소된 turn처럼 final answer가 없는 경우에는 `done`/`metrics` 도착을 완료 기준으로 둡니다.

### `turn`

최신 turn 전체를 출력합니다.

```bash
ctc turn SESSION
```

계속 갱신:

```bash
ctc turn SESSION --follow
```

최근 N개 turn:

```bash
ctc turn SESSION --tail 3
```

출력 섹션:

```text
[user]
...

[thinking]
...

[tool_use] Bash
...

[tool_result]
...

[assistant]
...
```

tool call 진행 상황까지 UI에 보여주려면 `turn`이 `answer`보다 적합합니다.

`turn`도 `SESSION`에 맞는 state/cwd transcript를 찾지 못하면 실패합니다. 최신 transcript 전체로 fallback하지 않습니다.

### `stream`

최신 user turn을 JSONL로 출력하고, 답변이 완료되면 종료합니다.

```bash
ctc stream SESSION
```

타임아웃과 ready 안정화 시간 조정:

```bash
ctc stream SESSION --timeout 300 --idle 2 --interval 0.5
```

`tool_result.text`는 기본 100자로 축약됩니다. 한도를 바꾸려면:

```bash
ctc stream SESSION --tool-result-limit 240
```

음수는 축약을 끕니다.

```bash
ctc stream SESSION --tool-result-limit -1
```

출력 예:

```jsonl
{"event":"user","timestamp":"t0","text":"질문"}
{"event":"thinking","timestamp":"t1","text":"","text_available":false,"has_signature":true,"note":"thinking text unavailable; signature present"}
{"event":"tool_use","timestamp":"t2","id":"toolu_1","caller":"assistant","name":"Task","input":{"prompt":"subagent work"}}
{"event":"tool_result","timestamp":"t3","tool_use_id":"toolu_1","is_error":false,"text":"long tool output...","text_truncated":true,"text_full_length":2048}
{"event":"assistant_text","timestamp":"t4","text":"최종 답변"}
{"event":"done","state":"ready","reason":"...; transcript ready","answer":"최종 답변"}
```

low-level `ctc stream SESSION`은 usage/cost `metrics`를 보장하지 않습니다.

turn final `metrics`가 필요하면 high-level `ctc stream --cwd ... [--session-id ...] PROMPT` 또는 `ctc ask`를 사용합니다.

완료 조건:

- 대상 user turn을 transcript에서 찾습니다.
- 마지막 meaningful transcript event가 final assistant text여야 합니다.
- `tool_use`, `tool_result`, `thinking` 상태면 계속 대기합니다.
- `Task` 같은 subagent tool 결과가 돌아온 뒤에도 final assistant text가 없으면 완료로 보지 않습니다.
- transcript ready 이후 tmux 화면도 ready 상태로 `--idle` 초 동안 안정되어야 `done`을 출력합니다.
- Claude Code가 thinking text 대신 signature만 기록하는 경우 `thinking.text`는 빈 문자열이고, `text_available:false`, `has_signature:true`가 같이 나옵니다.
- `tool_result`는 payload 크기를 줄이기 위해 text/result preview를 축약하고, 축약 시 `...`, `text_truncated`, `text_full_length`를 포함합니다.

서비스에서 중간 진행을 실시간으로 보여줄 때는 `turn --follow`보다 `stream`을 우선 사용합니다.

high-level 웹 세션은 `--session-id`와 `--cwd`를 사용합니다.

```bash
ctc stream --session-id "$SESSION_ID" --cwd "$PROJECT_DIR" "$USER_PROMPT"
```

브라우저가 끊긴 뒤 진행 중인 turn에 다시 붙을 때는 새 prompt 없이 attach합니다.

```bash
ctc stream --attach --session-id "$SESSION_ID"
```

진행 중인 응답을 취소하려면:

```bash
ctc cancel "$SESSION_ID"
ctc last "$SESSION_ID" --last 1
```

`cancel`은 내부 tmux session에 `Escape` key를 보내고 JSON 결과를 출력합니다.

완료 판정은 하지 않습니다. 취소 후에도 `active_turn`이 남아 있으면 `last` 또는 `stream --attach`로 이어서 `done`/`metrics`까지 받습니다.

Claude Code가 tool 실행 중 ESC를 받으면 transcript에 `User rejected tool use`와 `[Request interrupted by user for tool use]`를 남길 수 있습니다. CLI는 이 조합을 취소 완료로 해석하고, 후속 `last`/`attach`에서 해당 turn을 `done`/`metrics`로 닫습니다.

attach는 local state의 `active_turn`과 transcript offset을 사용하며, 새 입력을 보내지 않습니다.

성공하려면 해당 session에 `active_turn`이 남아 있어야 하고, `active_turn.stream_state`가 `active`, `timeout`, `interrupted` 중 하나여야 합니다.

또한 내부 tmux session과 transcript를 찾을 수 있어야 합니다.

`timeout` 또는 Ctrl+C 이후에도 `active_turn`은 남을 수 있으므로 이 경우 attach할 수 있습니다.

이미 완료되어 `active_turn`이 정리된 turn에는 attach할 수 없습니다. 완료된 마지막 답변은 앱 서버가 저장한 `done.answer`, `ask_result.answer`, 또는 `info`의 `last_turn`/state를 사용합니다. final answer가 없는 취소 turn도 있으므로 완료 여부는 `done`/`metrics` 도착으로 판단합니다.

다음 `stream --cwd ... --session-id ... "$USER_PROMPT"` 요청은 새 prompt를 보내기 전에 이전 turn을 검사합니다.

이전 turn이 tmux 화면과 transcript 모두에서 완료로 확인되면 CLI는 이전 turn을 state에만 `done/metrics`로 finalize하고 새 prompt를 계속 보냅니다.

확인되지 않으면 `turn_in_progress`로 실패합니다.

### `ask`

한 turn을 실행하되 중간 JSONL을 출력하지 않고 최종 결과 한 줄만 JSON으로 출력합니다.

```bash
ctc ask --session-id "$SESSION_ID" --cwd "$PROJECT_DIR" "$USER_PROMPT"
```

출력 예:

```json
{"event":"ask_result","session_id":"...","turn_id":"...","state":"ready","answer":"최종 답변","metrics":{"event":"metrics","elapsed_ms":2500},"events_seen":4}
```

`ask`도 내부적으로는 high-level turn 실행 경로를 사용하므로 session 생성, 재사용, resume, lock, metrics 저장 규칙은 `stream`과 같습니다.

### `info`

high-level session metadata를 조회합니다.

```bash
ctc info "$SESSION_ID" --json
```

주요 필드:

| 필드 | 의미 |
| --- | --- |
| `session_id` | bridge UUID |
| `tmux_session` | `ctc-csess-<session_id>` |
| `tmux_active` | 현재 tmux session 존재 여부 |
| `transcript_path` | 연결된 Claude transcript JSONL |
| `claude_transcript_session_id` | transcript 내부 `sessionId` |
| `active_turn` | 진행 중 turn state |
| `active_turn_recovery` | active/timeout/interrupted/failed 상태별 attach/cancel/new prompt 가능 여부와 권장 조치 |
| `last_turn` | 마지막 완료/실패 turn state |
| `usage_totals` | completed turn 기반 token 합계 |
| `cost_totals` | completed turn 기반 session USD 합계 |

completed turn state는 최신 완료 turn 최대 200개만 보관합니다. `usage_totals`와 `cost_totals`는 전체 transcript JSONL을 다시 읽지 않고, 이 보관 window의 completed turn records에서 재계산합니다.

### `list`

local state와 active tmux session을 합쳐 high-level controlled session 목록을 출력합니다.

```bash
ctc list --json
```

`ctc-csess-<uuid>` tmux session은 state file이 없어도 목록에 포함됩니다.

### `stats`

Claude transcript의 model, normalized usage, context, event count, read offset, estimated cost를 machine-readable JSON으로 출력합니다.

```bash
ctc stats "$SESSION_ID" --json
ctc stats --transcript PATH --json
```

### `events`

Claude Code transcript JSONL event를 읽습니다.

```bash
ctc events SESSION
```

raw JSON으로 보기:

```bash
ctc events SESSION --json --tail 50
```

계속 보기:

```bash
ctc events SESSION --follow
```

특정 transcript 파일 직접 보기:

```bash
ctc events --transcript ~/.claude/transcripts/session.jsonl --json
```

주의:

- `SESSION`을 생략하면 가장 최신 transcript를 봅니다.
- 이 경우 다른 Claude Code session이 섞일 수 있습니다.
- 서비스 연동에서는 high-level `stream --session-id ... --cwd ...`의 stdout JSONL을 사용합니다.
- low-level 디버깅에서만 tmux session name을 지정합니다.

### `capture`

tmux pane의 현재 렌더링된 화면을 출력합니다.

```bash
ctc capture SESSION --height 200
```

디버깅용입니다.

응답 본문 파싱에는 `answer`나 `turn`을 우선 사용합니다.

### `watch`

tmux 화면 전체를 주기적으로 다시 그립니다.

```bash
ctc watch SESSION
```

사람이 터미널에서 session 진행 상황을 볼 때 씁니다.

### `follow`

tmux 화면 변화분을 stdout으로 흘리고, 선택적으로 파일에 append합니다.

```bash
ctc follow SESSION --append claude-screen.log
```

주의:

- 이 출력은 terminal rendering 기준입니다.
- 구조화된 응답 저장에는 transcript 기반 `events`, `turn`, `answer`를 우선 사용합니다.

### `chat`

간단한 interactive wrapper입니다.

```bash
ctc chat SESSION --cwd "$PWD"
```

`/quit` 또는 `/exit`로 종료합니다.

### `kill`

특정 tmux session을 종료합니다.

```bash
ctc kill SESSION
```

`SESSION`은 bridge UUID가 아니라 tmux session name입니다.

예:

```bash
ctc kill ctc-abc123
```

주의:

- `kill`은 지정한 session을 바로 종료합니다.
- high-level `ctc-csess-<uuid>` tmux session을 종료해도 `sessions/<uuid>.json` bridge state와 Claude transcript는 삭제하지 않습니다.
- 같은 `session_id`로 다음 high-level `stream`을 호출하면 남아 있는 state/transcript를 기준으로 다시 `--resume`될 수 있습니다.
- low-level `start work` 같은 직접 tmux session에서는 같은 이름의 local state file도 함께 제거합니다.

### `reap`

오래 idle 상태인 controlled session을 정리합니다.

```bash
ctc reap --idle-seconds 1800 --prefix ctc-csess-
```

먼저 dry run으로 확인:

```bash
ctc reap --idle-seconds 1800 --prefix ctc-csess- --dry-run
```

동작 기준:

- 기본 prefix는 `ctc-`입니다.
- web session만 정리하려면 더 좁은 `ctc-csess-` prefix를 권장합니다.
- `ctc-`는 controlled 전체 정리용이며, low-level `ctc-*` tmux session도 state file이 있으면 대상이 될 수 있습니다.
- prefix가 맞는 tmux session만 대상으로 봅니다.
- 마지막 입력 시각은 session state file의 mtime으로 판단합니다.
- high-level `ctc-csess-<uuid>` session은 `sessions/<uuid>.json` state file을 기준으로 판단합니다.
- 오래된 high-level `active_turn`이 남아 있으면 ready transcript와 ready tmux 화면으로 완료 처리할 수 있는지 먼저 확인합니다.
- 실제 reap에서는 완료 처리 가능할 때 state-only finalize 후 종료 판단을 계속합니다.
- `--dry-run`은 이 확인을 시뮬레이션만 하고 state를 쓰거나 session을 종료하지 않습니다.
- 완료 처리할 수 없는 high-level `active_turn`이 남아 있고 `ready`가 아니면 오래됐어도 종료하지 않습니다.
- transcript/screen 기준으로 아직 `working`이면 오래됐어도 종료하지 않습니다.
- `timeout`이나 `interrupted`는 입력 가능 또는 정리 가능 신호가 아닙니다. `attach`, 같은 `session_id` 재시도, 또는 운영자 판단에 따른 `kill`로 별도 처리합니다.
- state file이 없는 session은 안전하게 skip합니다.

cron 예:

```cron
* * * * * cd /path/to/claude-tmux-control && ctc reap --idle-seconds 1800 --prefix ctc-csess- >> reap.log 2>&1
```

## 5. Integration Recipes

### 웹 서버에서 한 turn 실행

웹 채팅처럼 한 요청에서 session 보장, prompt 전송, stream 수신까지 처리하려면 high-level `stream`을 씁니다.

```bash
ctc stream --session-id "$SESSION_ID" --cwd "$PROJECT_DIR" "$USER_PROMPT"
```

운영 웹앱에서는 앱 서버가 UUID를 먼저 생성해 `--session-id`로 넘기는 방식을 권장합니다.

`--session-id`를 생략하면 CLI가 UUID를 생성합니다. 이 경우 클라이언트는 첫 stream event의 `session_id`를 저장해서 이후 turn에 다시 넘깁니다.

새 session은 `claude --session-id <uuid> --dangerously-skip-permissions`로 Claude Code를 먼저 시작한 뒤, tmux `load-buffer`/`paste-buffer`/`send-keys Enter`로 prompt를 전송합니다.

기존 state 또는 matching transcript가 있고 tmux session이 없으면 `claude --resume <uuid> --dangerously-skip-permissions`로 복구한 뒤, 같은 tmux 입력 경로로 prompt를 전송합니다.

tmux session이 이미 active이면 ready 화면인지 확인한 뒤 prompt를 전송합니다.

출력은 JSONL입니다.

```json
{"event":"assistant_text","session_id":"...","turn_id":"...","event_id":"...","source_offset":10,"source_end_offset":80,"block_index":0,"text":"..."}
{"event":"done","session_id":"...","turn_id":"...","event_id":"<turn_id>:done:<offset>","source_offset":80,"source_end_offset":80,"block_index":-1,"answer":"..."}
{"event":"metrics","session_id":"...","turn_id":"...","event_id":"<turn_id>:metrics:<offset>","source_offset":80,"source_end_offset":80,"block_index":-1,"scope":"turn_final"}
```

final `metrics`에는 transcript에서 확인 가능한 경우 다음 정보가 포함됩니다.

- `elapsed_ms`
- `model`
- `usage.input_tokens`
- `usage.cache_read_tokens`
- `usage.cache_write_tokens`
- `usage.output_tokens`
- `usage.api_call_count`: usage가 기록된 internal API call 수. 같은 request/message identity의 중복 usage event는 한 번만 셈
- `context`: transcript에 `context`, `context_window`, `context_usage`가 있을 때만 포함. 없으면 추정하지 않고 생략
- `cost.turn_usd`
- `cost.session_usd`

final `metrics`는 사용자가 보낸 prompt 하나에 대한 user-visible turn 기준입니다. Claude Code가 한 prompt를 처리하면서 여러 internal API call을 기록하면 `metrics.usage`는 각 call의 input, cache read, cache write, output token을 합산하고, `api_call_count`는 합산에 포함된 call 개수를 제공합니다. transcript에 `result.total_cost_usd`가 있으면 `metrics.cost.turn_usd`는 그 Claude CLI total을 우선 사용하고, 없을 때만 합산 usage와 `claude_pricing.json`으로 추정합니다.

high-level stream은 다음 안전장치를 둡니다.

- client-provided `session_id`는 UUID만 허용합니다.
- 기존 state의 canonical `cwd`와 요청 `cwd`가 다르면 `session_cwd_mismatch`로 실패합니다.
- 짧은 `send_lock`과 durable `active_turn`으로 동시 prompt 전송을 막습니다.
- `timeout`/`interrupted` 상태라도 이전 turn이 완료된 것으로 확인되면 다음 prompt 전에 state-only finalize합니다.
- stale/malformed lock은 오래된 경우에만 복구합니다.
- transcript는 cwd project dir와 Claude `sessionId`가 맞는 파일만 사용합니다.
- partial JSONL line은 offset을 전진시키지 않습니다.

### Web 서버 조합

운영 서비스에서는 high-level `stream --session-id ... --cwd ...`를 사용합니다.

```bash
CLAUDE_CODE_OAUTH_TOKEN="$TOKEN" ctc stream \
  --session-id "$SESSION_ID" \
  --cwd "$WORKDIR" \
  "$USER_PROMPT"
```

동시에 같은 `SESSION_ID`에 여러 prompt를 보내면 안 됩니다.

high-level `stream`은 `send_lock`과 `active_turn`으로 중복 전송을 막고, 받을 수 없는 상태에서는 JSON error와 non-zero exit code를 반환합니다.

진행 중인 turn에 재연결하려면:

```bash
ctc stream --attach --session-id "$SESSION_ID" --timeout 300
```

attach는 완료된 과거 turn 조회가 아니라, `active_turn`이 남은 진행 중/timeout/interrupted turn에 다시 붙는 명령입니다.

`stream`은 JSONL을 stdout으로 계속 출력하고, 완료 시 `done` event를 출력한 뒤 exit code `0`으로 종료합니다.

### `cancel`

진행 중인 Claude Code 응답을 중단하기 위해 내부 tmux pane에 `Escape`를 보냅니다.

```bash
ctc cancel "$SESSION_ID"
```

성공 시 stdout:

```json
{"event":"cancel","exit_code":0,"session_id":"...","sent_key":"Escape"}
```

`cancel`은 prompt를 새로 보내지 않고, transcript도 읽지 않습니다. 취소 후 응답 마무리를 클라이언트에 보여주려면 `ctc last "$SESSION_ID" --last 1` 또는 `ctc stream --attach --session-id "$SESSION_ID"`를 호출합니다.

tool 실행 취소처럼 final assistant text 없이 끝난 turn도 있을 수 있습니다. 이 경우 `done.answer`는 없을 수 있지만 `done`과 `metrics`는 출력되어 turn이 닫힙니다.

### `replay`

완료된 high-level turn을 기존 stream과 같은 JSONL event 형식으로 다시 출력합니다.

`last`는 같은 기능을 더 사람이 읽기 쉬운 이름으로 제공하는 alias입니다.

```bash
ctc last "$SESSION_ID" --last 1
ctc replay "$SESSION_ID" --last 1
ctc replay "$SESSION_ID" --last 5
```

`--last N`은 최근 N개 turn을 대상으로 합니다.

마지막 turn이 아직 진행 중이면 `last`/`replay`는 새 prompt를 보내지 않고 현재 `active_turn`에 attach해서 `done`/`metrics`까지 이어서 출력합니다.

`--last N`에서 마지막 turn이 진행 중이면, 최근 완료 turn `N-1`개를 먼저 replay하고 마지막 active turn에 attach합니다.

완료된 turn replay는 state의 `completed_turns`와 transcript offset을 사용합니다. 최신 완료 turn record에 transcript path가 있으면 그 파일을 우선 사용하고, 없으면 session `cwd`와 Claude `sessionId`로 transcript를 다시 찾습니다.

transcript를 찾지 못해도 completed turn record에 저장된 `answer`, `usage`, `cost`가 있으면 최소 `done`/`metrics`는 replay합니다.

### Low-level 수동 조합

디버깅이나 수동 조작에서는 low-level 명령을 조합할 수 있습니다.

이때 `work`는 독립 tmux session name입니다.

```bash
ctc start work --cwd "$WORKDIR"
ctc send work "$USER_PROMPT"
ctc status work
ctc answer work
ctc stream work --timeout 300
```

low-level `send`는 high-level `stream`의 lock/state 보호를 쓰지 않습니다.

high-level web session이 관리하는 `ctc-csess-$SESSION_ID` 이름을 low-level `start` 예시로 쓰지 마세요.

### 오래된 session 정리

30분 동안 새 입력이 없던 high-level web session 정리:

```bash
ctc reap --idle-seconds 1800 --prefix ctc-csess-
```

운영 적용 전 확인:

```bash
ctc reap --idle-seconds 1800 --prefix ctc-csess- --dry-run
```

### 질문 전송 + stream smoke test

`scripts/stream_question.py`는 테스트용 wrapper입니다.

한 번 실행하면 `send`로 질문을 보내고, 이어서 `stream` stdout을 읽어 사람이 보기 쉬운 형태로 출력합니다.

```bash
./scripts/stream_question.py work "현재 디렉터리 구조를 요약해줘"
```

raw JSONL을 그대로 보고 싶으면:

```bash
./scripts/stream_question.py work "현재 디렉터리 구조를 요약해줘" --raw-json
```

주의:

- 대상 tmux session은 먼저 `start`로 떠 있어야 합니다.
- wrapper는 테스트/디버깅용입니다. 서비스 연동에서는 high-level `stream --session-id ... --cwd ...`의 stdout JSONL을 처리하는 편이 낫습니다.

### 웹채팅 client smoke test

`scripts/web_chat_client.py`는 웹 서버가 high-level `stream` subprocess를 실행하고 stdout JSONL을 읽는 상황을 흉내 냅니다.

```bash
mkdir -p logs/integration
SESSION_ID="$(python3 - <<'PY'
import uuid
print(uuid.uuid4())
PY
)"
LOG="logs/integration/$(date -u +%Y%m%dT%H%M%SZ)-client-smoke-${SESSION_ID}.jsonl"
TERM=xterm-256color python3 scripts/web_chat_client.py \
  --ctc ctc \
  --cwd "$PWD" \
  --session-id "$SESSION_ID" \
  --prompt "Reply with exactly: client-ok" \
  --expect-answer "client-ok" \
  --tool-result-limit 100 \
  --log "$LOG" \
  --timeout 180
tail -n 20 "$LOG"
```

로그에는 `request`, streamed `event`, `summary` record가 JSONL로 남습니다.

## 6. Authentication And Accounts

OAuth token을 이미 받은 경우 이 CLI는 token을 직접 발급하지 않습니다.

호출자가 token을 환경변수로 들고 있다가, `stream`, `start`, `chat` 실행 시점에 넘깁니다.

```bash
ACCOUNT_A_TOKEN="..." ctc stream --cwd "$PWD" --oauth-token-env ACCOUNT_A_TOKEN "hello"
ACCOUNT_B_TOKEN="..." ctc stream --cwd "$PWD" --oauth-token-env ACCOUNT_B_TOKEN "hello"
```

실제로 Claude Code process에는 항상 `CLAUDE_CODE_OAUTH_TOKEN` 이름으로 들어갑니다.

### 추가 Claude Environment

`stream`, `ask`, `start`, `chat`은 새 tmux session을 만들 때 추가 env를 주입할 수 있습니다.

`--env-file`을 명시하지 않고 `<cwd>/.ctc.env`가 있으면 기본으로 읽습니다.

```env
SERVICE_BASE_URL=https://api.example.test
```

다른 파일을 쓰려면 `--env-file PATH`를 넘깁니다. 현재 `ctc` process env에서 특정 key만 복사하려면 `--env NAME`을 사용합니다.

```bash
SERVICE_API_KEY="..." \
ctc stream --cwd "$PWD" --env SERVICE_API_KEY "hello"
```

env 주입은 새 tmux session 생성 시점에만 적용됩니다. 기존 session은 시작 당시 env를 유지합니다.

`CLAUDE_CODE_OAUTH_TOKEN`은 `--oauth-token-env` 전용입니다. `.ctc.env`와 `--env`에서는 이 key를 설정할 수 없습니다.

보안 주의:

- token을 command argument로 넘기지 않습니다.
- shell history에 token 값이 남지 않게 주의합니다.
- `.ctc.env`를 commit하지 않습니다.
- 운영에서는 process env 접근 권한과 log redaction을 확인해야 합니다.

## 7. Claude Launch Arguments

bridge는 항상 고정된 `claude` 실행 파일을 사용합니다. 임의 shell command는 받지 않습니다.

일반적인 model 선택은 `--model MODEL`을 씁니다.

신뢰된 추가 Claude Code option은 `--claude-args "ARGS"`로 전달합니다.

```bash
ctc start work --cwd "$PWD" --model opus
ctc stream --cwd "$PWD" --claude-args "--add-dir ../shared" "hello"
ctc stream --cwd "$PWD" --claude-args "--permission-mode plan" "hello"
```

`--claude-args`는 shell-like quoting으로 parsing하지만 shell로 실행하지 않습니다.

운영자가 통제하는 값에만 쓰고, 신뢰할 수 없는 client에 raw text box로 노출하지 마세요.

## 8. Permission Mode

이 bridge는 dynamic approval prompt 없이 동작하는 것을 목표로 합니다.

그래서 Claude Code 실행 argument에 기본적으로 다음 옵션을 붙입니다.

```bash
--dangerously-skip-permissions
```

이 기본값은 위험합니다. service flow에서 interactive approval prompt를 피할 수 있지만, Claude Code가 action별 확인 없이 tool을 실행할 수 있습니다.

Docker, isolated server, 제한된 project directory, dedicated service user 같은 통제된 환경에서만 기본값을 사용하세요.

이미 다음 중 하나가 Claude argument에 있으면 중복으로 붙이지 않습니다.

```bash
--dangerously-skip-permissions
--permission-mode ...
```

예:

```bash
ctc start work --model opus
# 실행: claude --model opus --dangerously-skip-permissions

ctc start work --claude-args "--permission-mode plan"
# 실행: claude --permission-mode plan

ctc stream --cwd "$PWD" --claude-args "--permission-mode plan" "hello"
# --dangerously-skip-permissions를 추가로 붙이지 않음
```

client가 permission behavior를 바꿔야 하면 신뢰된 `--claude-args`로 Claude Code permission option을 전달합니다.

웹앱에서는 이것을 raw text box로 열지 말고, backend-controlled setting 또는 안전한 enum으로 받아서 `--claude-args "--permission-mode ..."` 형태로 매핑하세요.

## 9. Transcript Resolution

Claude Code transcript는 보통 아래 위치에 기록됩니다.

```text
~/.claude/projects/<encoded-cwd>/*.jsonl
~/.claude/transcripts/*.jsonl
```

session-scoped 명령은 다음 단서로 transcript를 찾습니다.

1. session state에 저장된 cwd
2. cwd-specific project transcript directory
3. 마지막으로 보낸 user prompt
4. 최신 mtime

내부 session summary prompt나 `tool_result`에 포함된 prompt 문자열은 user turn으로 보지 않습니다.

## 10. Exit Codes

현재 주요 exit code는 다음과 같습니다.

| code | 의미 |
| --- | --- |
| `0` | 성공 |
| `1` | tmux command 실패 등 일반 실패 |
| `2` | request/session/transcript를 찾지 못함. 예: `cancel`의 `tmux_session_missing` |
| `3` | ready 대기 실패 |
| `4` | 완료된 answer/turn을 찾지 못함 |
| `5` | high-level runtime state error. 예: `turn_in_progress`, `stream --attach`의 `tmux_session_missing` |
| `127` | `tmux` 또는 Claude Code executable이 PATH에 없음 |
| `130` | client interrupt. 예: Ctrl+C |

high-level `stream`/`ask` 오류는 stderr에 JSON을 출력할 수 있습니다.

```json
{"event":"error","error":"turn_in_progress"}
```

클라이언트는 exit code와 stderr JSON의 `error` 값을 함께 봐야 합니다.

같은 `error` 문자열이라도 명령에 따라 exit code가 다를 수 있습니다. 예를 들어 `cancel`은 대상 tmux session이 없으면 요청 대상이 없다는 의미로 exit `2`를 반환하고, `stream --attach`는 진행 중 turn에 붙을 수 없다는 runtime 상태로 exit `5`를 반환합니다.

`turn_in_progress`나 timeout 계열 상태에서는 `ctc info "$SESSION_ID" --json`의 `active_turn_recovery`를 확인하세요.

| 상태 | 권장 조치 |
| --- | --- |
| `active` | 기다리거나 attach, queue, cancel |
| `timeout` / `interrupted` | 새 prompt 전송 전에 같은 session으로 attach 또는 retry |
| `failed` | 새 prompt 전송 전에 inspect 또는 kill |

## 11. Troubleshooting

### `missing or unsuitable terminal: xterm-ghostty`

서버에 Ghostty terminfo가 없어서 tmux가 시작하지 못한 상황입니다.

```bash
TERM=xterm-256color ctc stream --cwd "$PWD" "hello"
```

### `events`가 다른 session을 보는 것 같음

session 인자를 지정합니다.

```bash
ctc events work --tail 20 --json
```

session을 생략하면 최신 transcript 전체에서 고르므로 다른 session이 나올 수 있습니다.

### `status`가 ready인데 실제로는 응답 중임

화면 prompt glyph만으로는 불안정할 수 있습니다.

가능하면 `status` 기본 모드나 `answer --wait`처럼 transcript를 함께 보는 명령을 씁니다.

### 답변 본문만 어디서 보나?

```bash
ctc answer work
```

tool call과 중간 진행까지 보려면:

```bash
ctc turn work
```

### 화면 그대로 보고 싶음

```bash
ctc capture work
ctc watch work
```

## 12. Current Gaps

아직 구현되지 않은 service-facing 기능:

- `ensure <session-id>`
- `status --json`
- `answer --json`
- `turn --json`
- state-write lock과 generation compare/update retry 강화
- pricing table 갱신 자동화와 provider/region multiplier 지원
- transcript rotation follow 고도화

이 항목들은 `implementation-checklist.md`에서 추적합니다.
