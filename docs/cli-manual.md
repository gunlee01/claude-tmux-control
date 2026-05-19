# CLI Manual

`claude_tmux_control.py`는 Claude Code interactive CLI를 `tmux` 안에서 실행하고, 외부 프로그램이 입력 전달과 응답 조회를 할 수 있게 하는 bridge CLI입니다.

## 1. Quick Start

### Install

권장 방식은 `pipx`로 git repo에서 설치하는 것입니다.

```bash
pipx install git+ssh://git@oss.navercorp.com/gunh-lee/claude-tmux-control.git
ctc --help
```

`pipx`가 없다면 venv 안에 설치합니다.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install git+ssh://git@oss.navercorp.com/gunh-lee/claude-tmux-control.git
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
TERM=xterm-256color ctc start work --cwd "$PWD"
```

자주 쓰면 alias로 묶습니다.

```bash
alias ctc='TERM=xterm-256color command ctc'
```

### 세션 시작

```bash
ctc start work --cwd "$PWD"
```

`start`는 `work`라는 tmux session이 없으면 새로 만들고, 있으면 재사용합니다.

Claude Code 실행 command에는 기본적으로 `--dangerously-skip-permissions`가 자동으로 붙습니다.

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

### OAuth token으로 시작

호출 프로세스에 token이 있으면 새 tmux session의 Claude Code process에 `CLAUDE_CODE_OAUTH_TOKEN`으로 전달합니다.

```bash
CLAUDE_CODE_OAUTH_TOKEN="$TOKEN" ctc start work --cwd "$PWD"
```

계정별 token을 다른 환경변수명으로 들고 있다면 `--oauth-token-env`로 source env를 고릅니다.

```bash
ACCOUNT_A_TOKEN="$TOKEN" ctc start work-a --cwd "$PWD" --oauth-token-env ACCOUNT_A_TOKEN
ACCOUNT_B_TOKEN="$TOKEN" ctc start work-b --cwd "$PWD" --oauth-token-env ACCOUNT_B_TOKEN
```

### 입력 보내기

```bash
ctc send work "현재 디렉터리 구조를 요약해줘"
```

### 완료 대기 후 답변 보기

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

기본 흐름은 다음과 같습니다.

```text
client/web
  -> claude_tmux_control.py start/send/status/answer
    -> tmux session
      -> Claude Code interactive CLI
    <- transcript JSONL
  <- answer / turn / events / status
```

`stdout`/`stdin`으로 Claude Code를 직접 제어하는 구조가 아닙니다.

Claude Code가 terminal UI를 사용하므로 입력은 tmux pane에 붙여 넣고, 구조화된 응답은 transcript JSONL에서 읽습니다.

## 3. Session Rules

### Session name

현재 명령의 `session` 인자는 tmux session name입니다.

```bash
ctc start work
ctc send work "hello"
ctc answer work
```

웹/외부 client용 high-level `stream`은 UUID `session_id`를 기준으로 내부 tmux session name을 `ctc-csess-<session_id>`로 만듭니다.

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
ctc start SESSION [--cwd PATH] [--command COMMAND] [--attach] [--oauth-token-env ENV]
```

| 옵션 | 의미 |
| --- | --- |
| `--cwd PATH` | 새 session의 working directory |
| `--command COMMAND` | tmux 안에서 실행할 Claude Code command |
| `--attach` | 생성/재사용 후 바로 tmux attach |
| `--oauth-token-env ENV` | 이 env 값을 `CLAUDE_CODE_OAUTH_TOKEN`으로 주입 |

주의:

- `--command` 기본값은 `claude`입니다.
- command에 permission override가 없으면 `--dangerously-skip-permissions`가 자동으로 붙습니다.
- `--oauth-token-env`는 새 tmux session을 만들 때만 의미가 있습니다.
- `start`가 모르는 옵션은 Claude Code option으로 command 뒤에 전달합니다.

Claude Code option passthrough:

```bash
ctc start work --cwd "$PWD" --model opus --add-dir ../shared
```

옵션 경계를 명시하고 싶으면 `--`를 씁니다.

```bash
ctc start work --cwd "$PWD" -- --model opus --add-dir ../shared
```

실제 실행 command는 대략 다음처럼 됩니다.

```bash
claude --model opus --add-dir ../shared --dangerously-skip-permissions
```

### `launch`

이미 존재하는 tmux session 안에 Claude Code command를 붙여 넣고 실행합니다.

```bash
ctc launch SESSION [--command COMMAND]
```

주의:

- 기존 shell pane에 command를 입력하는 방식입니다.
- OAuth token env 주입은 하지 않습니다.
- 이미 Claude Code가 실행 중인 pane에는 `launch` 대신 `send`를 씁니다.
- `launch`가 모르는 옵션도 Claude Code option으로 전달합니다.

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
- 웹 UI의 최종 채팅 본문에는 보통 이 명령이 적합합니다.

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
{"event":"metrics","scope":"turn_final","elapsed_ms":2500,"usage":{"input_tokens":10,"output_tokens":5},"cost":{"estimated":true,"turn_usd":0.0001,"session_usd":0.0003}}
```

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

attach는 local state의 `active_turn`과 transcript offset을 사용하며, 새 입력을 보내지 않습니다.

`timeout` 또는 Ctrl+C 이후에도 `active_turn`은 남습니다.

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
| `last_turn` | 마지막 완료/실패 turn state |
| `usage_totals` | completed turn 기반 token 합계 |
| `cost_totals` | completed turn 기반 session USD 합계 |

### `list`

local state와 active tmux session을 합쳐 high-level controlled session 목록을 출력합니다.

```bash
ctc list --json
```

`ctc-csess-<uuid>` tmux session은 state file이 없어도 목록에 포함됩니다.

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
- 서비스 연동에서는 가능하면 session을 지정합니다.

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

예:

```bash
ctc kill ctc-abc123
```

주의:

- `kill`은 지정한 session을 바로 종료합니다.
- 종료 후 해당 session의 local state file도 제거합니다.

### `reap`

오래 idle 상태인 controlled session을 정리합니다.

```bash
ctc reap --idle-seconds 1800 --prefix ctc-
```

먼저 dry run으로 확인:

```bash
ctc reap --idle-seconds 1800 --prefix ctc- --dry-run
```

동작 기준:

- 기본 prefix는 `ctc-`입니다.
- prefix가 맞는 tmux session만 대상으로 봅니다.
- 마지막 입력 시각은 session state file의 mtime으로 판단합니다.
- transcript가 아직 `working`이면 오래됐어도 종료하지 않습니다.
- state file이 없는 session은 안전하게 skip합니다.

cron 예:

```cron
* * * * * cd /path/to/claude-tmux-control && ctc reap --idle-seconds 1800 --prefix ctc- >> reap.log 2>&1
```

## 5. Integration Recipes

### 웹 서버에서 한 turn 실행

웹 채팅처럼 한 요청에서 session 보장, prompt 전송, stream 수신까지 처리하려면 high-level `stream`을 씁니다.

```bash
ctc stream --session-id "$SESSION_ID" --cwd "$PROJECT_DIR" "$USER_PROMPT"
```

`--session-id`를 생략하면 bridge가 UUID를 생성합니다.

새 session은 `claude --session-id <uuid> --dangerously-skip-permissions "<prompt>"`로 시작합니다.

기존 state가 있고 tmux session이 없으면 `claude --resume <uuid> --dangerously-skip-permissions "<prompt>"`로 시작합니다.

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
- `context`
- `cost.turn_usd`
- `cost.session_usd`

high-level stream은 다음 안전장치를 둡니다.

- client-provided `session_id`는 UUID만 허용합니다.
- 기존 state의 canonical `cwd`와 요청 `cwd`가 다르면 `session_cwd_mismatch`로 실패합니다.
- 짧은 `send_lock`과 durable `active_turn`으로 동시 prompt 전송을 막습니다.
- `timeout`/`interrupted` 상태라도 이전 turn이 완료된 것으로 확인되면 다음 prompt 전에 state-only finalize합니다.
- stale/malformed lock은 오래된 경우에만 복구합니다.
- transcript는 cwd project dir와 Claude `sessionId`가 맞는 파일만 사용합니다.
- partial JSONL line은 offset을 전진시키지 않습니다.

### Low-level 웹 서버 조합

디버깅이나 수동 조작에서는 low-level 명령을 조합할 수 있습니다.

```bash
CLAUDE_CODE_OAUTH_TOKEN="$TOKEN" ctc start "ctc-$SESSION_ID" --cwd "$WORKDIR"
```

### 사용자 입력 전달

```bash
ctc send "ctc-$SESSION_ID" "$USER_PROMPT"
```

동시에 같은 session에 여러 prompt를 보내면 안 됩니다.

low-level `send`는 high-level `stream`의 lock/state 보호를 쓰지 않습니다.

운영 서비스에서는 high-level `stream --session-id ... --cwd ...`를 우선 사용합니다.

### 진행 상태 polling

```bash
ctc status "ctc-$SESSION_ID"
```

또는 완료까지 blocking:

```bash
ctc wait-ready "ctc-$SESSION_ID" --timeout 300
```

### 최종 답변 조회

```bash
ctc answer "ctc-$SESSION_ID"
```

### tool call 포함 진행 표시

```bash
ctc stream "ctc-$SESSION_ID" --timeout 300
```

`stream`은 JSONL을 stdout으로 계속 출력하고, 완료 시 `done` event를 출력한 뒤 exit code `0`으로 종료합니다.

polling 방식을 유지하려면 `status` 또는 `wait-ready`를 사용합니다.

### 오래된 session 정리

30분 동안 새 입력이 없던 `ctc-` session 정리:

```bash
ctc reap --idle-seconds 1800 --prefix ctc-
```

운영 적용 전 확인:

```bash
ctc reap --idle-seconds 1800 --prefix ctc- --dry-run
```

### 질문 전송 + stream smoke test

`scripts/stream_question.py`는 테스트용 wrapper입니다.

한 번 실행하면 `send`로 질문을 보내고, 이어서 `stream` stdout을 읽어 사람이 보기 쉬운 형태로 출력합니다.

```bash
./scripts/stream_question.py "ctc-$SESSION_ID" "현재 디렉터리 구조를 요약해줘"
```

raw JSONL을 그대로 보고 싶으면:

```bash
./scripts/stream_question.py "ctc-$SESSION_ID" "현재 디렉터리 구조를 요약해줘" --raw-json
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

호출자가 token을 환경변수로 들고 있다가, `start`나 `chat` 실행 시점에 넘깁니다.

```bash
ACCOUNT_A_TOKEN="..." ctc start ctc-a --oauth-token-env ACCOUNT_A_TOKEN
ACCOUNT_B_TOKEN="..." ctc start ctc-b --oauth-token-env ACCOUNT_B_TOKEN
```

실제로 Claude Code process에는 항상 `CLAUDE_CODE_OAUTH_TOKEN` 이름으로 들어갑니다.

보안 주의:

- token을 command argument로 넘기지 않습니다.
- shell history에 token 값이 남지 않게 주의합니다.
- 운영에서는 process env 접근 권한과 log redaction을 확인해야 합니다.

## 7. Permission Mode

이 bridge는 dynamic approval prompt 없이 동작하는 것을 목표로 합니다.

그래서 Claude Code command에 기본적으로 다음 옵션을 붙입니다.

```bash
--dangerously-skip-permissions
```

이미 다음 중 하나가 command에 있으면 중복으로 붙이지 않습니다.

```bash
--dangerously-skip-permissions
--permission-mode ...
```

예:

```bash
ctc start work --command "claude --model opus"
# 실제 실행: claude --model opus --dangerously-skip-permissions
```

## 8. Transcript Resolution

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

## 9. Exit Codes

현재 주요 exit code는 다음과 같습니다.

| code | 의미 |
| --- | --- |
| `0` | 성공 |
| `1` | tmux command 실패 등 일반 실패 |
| `2` | session 또는 transcript를 찾지 못함 |
| `3` | ready 대기 실패 |
| `4` | 완료된 answer/turn을 찾지 못함 |
| `127` | `tmux`가 PATH에 없음 |

## 10. Troubleshooting

### `missing or unsuitable terminal: xterm-ghostty`

서버에 Ghostty terminfo가 없어서 tmux가 시작하지 못한 상황입니다.

```bash
TERM=xterm-256color ctc start work --cwd "$PWD"
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

## 11. Current Gaps

아직 구현되지 않은 service-facing 기능:

- `ensure <session-id>`
- `status --json`
- `answer --json`
- `turn --json`
- state-write lock과 generation compare/update retry 강화
- pricing table 갱신 자동화와 provider/region multiplier 지원
- transcript rotation follow 고도화

이 항목들은 `implementation-checklist.md`에서 추적합니다.
