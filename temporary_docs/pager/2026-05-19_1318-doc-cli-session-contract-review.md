# claude-tmux-control 문서/CLI 세션 계약 리뷰

- Last Updated At: 2026-05-19 13:31 KST
- Scope: `README.md`와 연결 문서가 현재 `claude_tmux_control.py` 구현과 맞는지 검토
- Review Type: documentation contract review
- Review Inputs: local code review + 3 subagent reviews
- Status: open

## 결론

Critical은 없습니다.

하지만 문서가 다음 네 용어를 초반에 명확히 분리하지 않아, 웹 클라이언트 구현자가 잘못된 인자를 넘길 가능성이 큽니다.

| 용어 | 실제 의미 | 대표 명령 |
| --- | --- | --- |
| bridge `session_id` | 웹 대화창을 식별하는 UUID. high-level API의 기본 키 | `ctc stream --session-id UUID --cwd PATH PROMPT`, `ctc info UUID` |
| high-level tmux session | bridge `session_id`로 만든 내부 tmux session name | `ctc-csess-<UUID>` |
| low-level `SESSION` | 사용자가 직접 붙인 tmux session name. 예: `work` | `ctc start work`, `ctc send work`, `ctc answer work` |
| Claude transcript session id | Claude Code transcript에서 관측되는 session metadata | `info.claude_transcript_session_id` |

`ctc start work`는 현재 코드 기준 유효합니다.

다만 이것은 **웹 세션을 만드는 명령이 아니라 low-level tmux session `work`를 만드는 debug/smoke 명령**입니다.

웹 클라이언트의 주 계약은 아래로 고정하는 편이 맞습니다.

```text
Web client API:
  ctc stream --cwd PATH [--session-id UUID] PROMPT
  ctc stream --attach --session-id UUID
  ctc info UUID --json
  ctc list --json
  ctc reap --idle-seconds N --prefix ctc-csess-

Low-level debug API:
  ctc start TMUX_SESSION
  ctc send TMUX_SESSION PROMPT
  ctc answer/turn/events TMUX_SESSION
```

## Subagent 종합

| Reviewer | Focus | Summary |
| --- | --- | --- |
| Avicenna | 문서 vs 구현 source of truth | Critical 없음. `reap`, `stream`, `attach`, exit code 문서가 구현보다 느슨함 |
| Carson | 사용자/웹 클라이언트 관점 | ID vocabulary 부재, `session_id` 생성 주체 충돌, `kill`/low-level 명령 혼동 |
| Ohm | CLI 인자 일관성 | `ctc-$SESSION_ID` 예시와 `ctc-csess-<UUID>` 실제 규칙 충돌, `answer/turn/events/kill` 인자 타입 불명확 |

## Findings

### 1. [Major] README와 CLI manual이 high-level/low-level API 경계를 먼저 고정하지 않는다

Status: accepted

#### Evidence

- `README.md`는 high-level `stream --session-id`를 주 사용법으로 설명한 뒤, `Useful Commands`에서 `ctc start work`, `ctc answer work`, `ctc events work`를 같은 블록에 노출합니다.
- `docs/cli-manual.md` Quick Start는 `ctc start work --cwd "$PWD"`를 세션 시작 기본 예시로 둡니다.
- 코드상 `start/send/answer/turn/status/capture/watch/follow/kill`의 positional `SESSION`은 tmux session name입니다.
- 코드상 high-level 웹 흐름은 `stream --cwd ... [--session-id UUID] PROMPT`, `ask`, `info`, `list` 중심입니다.

#### Risk

웹 클라이언트 개발자가 `SESSION_ID` UUID와 tmux session name을 섞어서 다음처럼 잘못 호출할 수 있습니다.

```bash
ctc answer "$SESSION_ID"
ctc stream "ctc-csess-$SESSION_ID"
ctc start "$SESSION_ID"
```

#### Recommended Direction

- README 초반에 `Use this for web clients`와 `Low-level debug only`를 분리합니다.
- `ctc start work`는 유지하되 “low-level smoke/debug”로 내려보냅니다.
- low-level 명령 예시는 `TMUX_SESSION=work`처럼 UUID가 아닌 이름임을 드러냅니다.

#### Decision Needed

- Decision: accept. `ctc start/send/answer/turn`은 제거하지 않고 debug-only 섹션으로 내린다.

#### Progress

- 2026-05-19 13:27 KST: README에 `Command Model`을 추가해 high-level web API와 low-level debug API를 분리했다.
- 2026-05-19 13:27 KST: CLI manual Quick Start를 high-level `stream` 우선으로 바꾸고, `ctc start work`를 low-level debug/smoke test로 명시했다.
- 2026-05-19 13:31 KST: CLI manual의 terminal fallback/OAuth 예시도 high-level `stream` 중심으로 정리했다.
- 2026-05-19 13:31 KST: README의 `kill` 예시는 운영용 process stop 섹션으로 분리하고, low-level debug 명령에 `ctc-csess-$SESSION_ID`를 넣지 말라고 명시했다.
- 2026-05-19 13:31 KST: `ctc --help`에 `WEB`, `LOW`, `OPS` 명령 구분을 추가하고 wrapping에 덜 민감한 테스트로 검증했다.

---

### 2. [Major] `SESSION_ID` 생성 주체 설명이 문서끼리 다르다

Status: open

#### Evidence

- `README.md`와 `quickstart-web-client.md`는 앱 서버/클라이언트가 UUID를 생성해서 넘기는 흐름을 기본 예시로 둡니다.
- `web-chat-integration-guide.md`는 `session_id`를 bridge가 생성한다고 설명합니다.
- 코드상 `validate_or_create_session_id(None)`은 `uuid.uuid4()`를 생성합니다.
- `stream --cwd PATH PROMPT`처럼 `--session-id`를 생략하면 bridge가 session id를 생성하고 JSONL event에 포함합니다.

#### Risk

첫 turn에서 클라이언트가 반드시 UUID를 만들어야 하는지, bridge가 만들어주는 값을 저장해야 하는지 불명확합니다.

#### Recommended Direction

문서 공통 문구를 다음으로 통일합니다.

```text
첫 turn에서는 클라이언트가 UUID를 직접 생성해 `--session-id`로 넘겨도 되고, 생략해도 된다.
생략하면 bridge가 UUID를 생성하며, 클라이언트는 첫 JSONL event의 `session_id`를 저장해서 이후 turn에 재사용한다.
```

#### Decision Needed

- 권장 방식은 “클라이언트 생성”으로 둘지, “생략 후 bridge 생성값 저장”으로 둘지 결정.

---

### 3. [Major] `ctc-$SESSION_ID` 예시가 실제 high-level tmux session 규칙과 충돌한다

Status: open

#### Evidence

- 코드상 high-level tmux session name은 `ctc-csess-<session_id>`입니다.
- `web_tmux_session_name()`이 `DEFAULT_WEB_SESSION_PREFIX`인 `ctc-csess-`를 붙입니다.
- `docs/cli-manual.md` 일부 low-level 웹 조합 예시는 `ctc-$SESSION_ID`를 사용합니다.

#### Risk

문서대로 `ctc-$SESSION_ID`를 쓰면 high-level `stream --session-id UUID`가 만든 session과 다른 tmux session을 만들게 됩니다.

`ctc info/list`의 high-level state와도 분리됩니다.

#### Recommended Direction

- high-level session 디버깅용이면 `TMUX_SESSION="ctc-csess-$SESSION_ID"`로 통일합니다.
- 별도 low-level smoke라면 `TMUX_SESSION="work"` 또는 `TMUX_SESSION="debug-work"`처럼 UUID와 무관한 이름을 씁니다.
- `ctc-$SESSION_ID` 예시는 제거하는 편이 안전합니다.

#### Decision Needed

- `ctc-$SESSION_ID` 형태를 문서에서 금지할지 결정.

---

### 4. [Major] `answer`, `turn`, `events`, `kill` 인자 타입이 web guide에서 흐릿하다

Status: open

#### Evidence

- parser 기준:
  - `answer SESSION`: tmux session name
  - `turn SESSION`: tmux session name
  - `events [SESSION]`: tmux session name 또는 생략 시 최신 transcript
  - `kill SESSION`: tmux session name
  - `info SESSION_ID`: bridge UUID
- `web-chat-integration-guide.md`의 command table은 `answer`, `turn`, `kill`을 web-facing command처럼 함께 노출합니다.
- 현재 `answer`는 high-level UUID 명령도 JSON 응답 명령도 아닙니다.

#### Risk

웹 서버가 `ctc answer "$SESSION_ID"`를 호출하거나, `ctc kill "$SESSION_ID"`를 호출할 수 있습니다.

이는 의도한 high-level 세션을 찾지 못하거나 종료하지 못합니다.

#### Recommended Direction

command reference를 두 표로 분리합니다.

```text
High-level/web:
  stream --cwd PATH [--session-id UUID] PROMPT
  stream --attach --session-id UUID
  ask --cwd PATH [--session-id UUID] PROMPT
  info UUID
  list
  reap

Low-level/debug:
  start TMUX_SESSION
  send TMUX_SESSION PROMPT
  answer TMUX_SESSION
  turn TMUX_SESSION
  events TMUX_SESSION
  kill TMUX_SESSION
```

#### Decision Needed

- `answer/turn/events/kill`을 web guide에서 debug-only로 내릴지 결정.

---

### 5. [Major] `kill` 문서가 high-level state 삭제처럼 읽힌다

Status: open

#### Evidence

- `README.md`와 `operations.md`는 `ctc kill "ctc-csess-$SESSION_ID"`를 운영 명령으로 보여줍니다.
- `docs/cli-manual.md`는 `kill` 종료 후 local state file도 제거한다고 설명합니다.
- 코드상 `kill`은 tmux session name을 받고 `_remove_session_state(args.session)`만 수행합니다.
- high-level state file은 `sessions/<UUID>.json`입니다.
- 따라서 `ctc kill "ctc-csess-$SESSION_ID"`는 high-level bridge state를 삭제하지 않습니다.

#### Risk

운영자가 `kill`을 “대화 세션 삭제”로 오해할 수 있습니다.

실제로는 tmux/Claude Code process만 종료되고, state/transcript가 남아 다음 `stream --session-id`에서 resume될 수 있습니다.

#### Recommended Direction

문서를 두 개념으로 나눕니다.

| 동작 | 현재 지원 |
| --- | --- |
| Stop process | `ctc kill ctc-csess-<UUID>` |
| Forget conversation/state | 현재 명시 CLI 없음. 보류 |

#### Decision Needed

- high-level state 삭제 기능은 계속 보류할지, 향후 `prune/forget`으로 문서화할지 결정.

---

### 6. [Minor] `attach` 성공 조건이 좁게 설명되지 않았다

Status: open

#### Evidence

- `stream --attach`는 `--session-id`가 필수입니다.
- 코드상 `prepare_high_level_attach()`는 active turn이 있어야 합니다.
- active turn이 이미 `done`으로 clear되면 attach 대상이 없습니다.
- tmux session과 transcript도 해석 가능해야 합니다.

#### Risk

사용자가 완료된 마지막 응답을 다시 받으려고 `attach`를 사용할 수 있습니다.

실제 의도는 “진행 중/timeout/interrupted active turn 재연결”입니다.

#### Recommended Direction

문서에 다음 문구를 추가합니다.

```text
attach는 완료된 과거 turn 조회가 아니다.
active_turn이 남아 있는 진행 중/timeout/interrupted turn에 다시 붙는 명령이다.
완료된 마지막 답변 조회는 `info`의 last_turn 또는 low-level `answer TMUX_SESSION`을 사용한다.
```

#### Decision Needed

- high-level 완료 답변 조회 명령을 `info`로 충분하다고 볼지, 별도 `answer --session-id`가 필요한지 결정.

---

### 7. [Minor] `reap` 문서가 active_turn skip 조건을 덜 보수적으로 설명한다

Status: open

#### Evidence

- 코드상 high-level `active_turn.claude_state`가 `None` 또는 `ready`가 아니면 `reap`은 screen/transcript 판정 전에 skip합니다.
- 따라서 `working`뿐 아니라 `timeout/interrupted` 등 active turn 상태도 정리 대상에서 제외될 수 있습니다.
- 문서는 “Claude가 아직 working이면 정리하지 않는다” 정도로만 설명합니다.

#### Risk

운영자가 timeout/interrupted session이 `reap`으로 자동 정리될 것이라 기대할 수 있습니다.

#### Recommended Direction

`reap` 정책을 다음처럼 바꿔 씁니다.

```text
high-level active_turn이 남아 있고 ready가 아니면 reap은 보수적으로 skip한다.
timeout/interrupted는 입력 가능 신호가 아니며, attach/retry/kill로 별도 처리한다.
```

#### Decision Needed

- 문서만 보강할지, timeout/interrupted stale active_turn을 reap 대상으로 만드는 기능 변경을 검토할지 결정.

---

### 8. [Minor] exit code 표가 high-level 오류를 빠뜨린다

Status: open

#### Evidence

- README는 `turn_in_progress`에서 exit code `5`를 설명합니다.
- `docs/cli-manual.md` exit code 표에는 `5`와 `130`이 없습니다.
- 코드상:
  - `127`: tmux 또는 Claude executable missing
  - `5`: high-level runtime state error, 예: `turn_in_progress`
  - `130`: interrupted

#### Risk

클라이언트가 exit code 기반 retry/attach 정책을 만들 때 문서만으로 구현하기 어렵습니다.

#### Recommended Direction

CLI manual exit code 표를 source truth에 맞춥니다.

| code | 의미 |
| --- | --- |
| 5 | high-level runtime state error, stderr JSON 확인 |
| 127 | tmux 또는 Claude executable missing |
| 130 | interrupted |

#### Decision Needed

- README에도 최소 exit code 표를 둘지 결정.

---

### 9. [Minor] `reap --prefix ctc-`가 high-level 전용처럼 읽힌다

Status: open

#### Evidence

- high-level session prefix는 `ctc-csess-`입니다.
- `--prefix ctc-`는 high-level `ctc-csess-*`뿐 아니라 사용자가 low-level로 만든 `ctc-*` tmux session도 포함합니다.
- state file이 있으면 low-level session도 reap 대상입니다.

#### Risk

운영자가 web session만 정리한다고 생각했는데 low-level `ctc-*` session까지 정리할 수 있습니다.

#### Recommended Direction

web-only 운영 문서에서는 더 좁은 prefix를 권장합니다.

```bash
ctc reap --idle-seconds 1800 --prefix ctc-csess-
```

`ctc-`는 “controlled 전체” 정리용으로 설명합니다.

#### Decision Needed

- README/Operations의 기본 예시를 `ctc-csess-`로 바꿀지 결정.

---

### 10. [Nit] `UUID v4` 표현이 코드의 검증 조건보다 강하다

Status: open

#### Evidence

- 문서는 `UUID v4`를 권장 형식으로 씁니다.
- 코드상 `uuid.UUID(session_id)`와 lowercase canonical string만 확인합니다.
- version 4를 강제하지 않습니다.

#### Risk

작은 불일치이지만, 클라이언트가 v1/v7 UUID를 쓰면 문서상 불허처럼 보일 수 있습니다.

#### Recommended Direction

```text
canonical lowercase UUID. UUID v4 권장.
```

으로 조정합니다.

#### Decision Needed

- 코드도 v4로 강제할지, 문서를 완화할지 결정.

## Proposed Review Order

1. Finding 1: high-level/low-level API 경계
2. Finding 2: `SESSION_ID` 생성 주체
3. Finding 3: `ctc-$SESSION_ID` 제거/정리
4. Finding 4: 명령별 인자 타입 표 분리
5. Finding 5: `kill` 의미 정리
6. Finding 6-10: attach/reap/exit/prefix/UUID 세부 정리

## Decision Log

- 2026-05-19 13:18 KST: pager 생성. 아직 accept/skip 결정 없음.
