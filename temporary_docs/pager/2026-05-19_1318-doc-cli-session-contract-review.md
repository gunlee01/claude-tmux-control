# claude-tmux-control 문서/CLI 세션 계약 리뷰

- Last Updated At: 2026-05-19 15:59 KST
- Scope: `README.md`와 연결 문서가 현재 `claude_tmux_control.py` 구현과 맞는지 검토
- Review Type: documentation contract review
- Review Inputs: local code review + 3 subagent reviews
- Status: completed

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

Status: accepted

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

- Decision: accept. 운영 웹앱은 앱 서버가 UUID를 생성해 `--session-id`로 넘기는 방식을 권장한다. 단, `--session-id` 생략도 지원하며 이 경우 CLI가 생성한 첫 event의 `session_id`를 저장해 이후 turn에 재사용한다.

#### Progress

- 2026-05-19 13:36 KST: README, quickstart, CLI manual, web chat guide를 “앱 서버 생성 권장 + 생략 시 CLI 생성값 저장” 규칙으로 통일했다.

---

### 3. [Major] `ctc-$SESSION_ID` 예시가 실제 high-level tmux session 규칙과 충돌한다

Status: accepted

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

- Decision: accept. `ctc-$SESSION_ID` 형태는 문서에서 쓰지 않는다. high-level 내부 tmux session은 `ctc-csess-$SESSION_ID`로만 설명하고, low-level smoke/debug 예시는 `work` 같은 독립 tmux session name을 사용한다.

#### Progress

- 2026-05-19 13:48 KST: README 연결 문서와 repository docs에서 `ctc-$SESSION_ID`, `ctc start ctc...`, `stream/send/answer "ctc..."` 예시가 남아 있지 않음을 확인했다.
- 2026-05-19 13:48 KST: 남아 있는 `ctc-csess-*` 표현은 high-level 내부 tmux session 규칙, ops kill, 또는 low-level start 금지 문맥으로만 사용됨을 확인했다.

---

### 4. [Major] `answer`, `turn`, `events`, `kill` 인자 타입이 web guide에서 흐릿하다

Status: accepted

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

- Decision: accept. `answer/turn/events/kill`은 bridge UUID를 받는 web-facing 명령이 아니라 tmux session name을 받는 low-level/debug 명령으로 문서화한다. 웹 UI는 `stream.done.answer` 또는 `ask.ask_result.answer`를 사용한다.

#### Progress

- 2026-05-19 13:49 KST: web chat integration guide의 command table을 high-level/web 명령과 low-level/debug 명령으로 분리했다.
- 2026-05-19 13:49 KST: CLI manual에서 `answer`를 웹 UI 본문용으로 권장하던 문구를 제거하고, `events`/`kill` 인자 의미를 tmux session name으로 명시했다.
- 2026-05-19 13:49 KST: HTML guide에도 `answer "$SESSION_ID"`를 호출하지 말고 `stream.done.answer` 또는 `ask_result.answer`를 쓰라는 command boundary를 추가했다.

---

### 5. [Major] `kill` 문서가 high-level state 삭제처럼 읽힌다

Status: accepted

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

- Decision: accept. high-level state 삭제 기능은 보류한다. 현재 `kill`은 대화 삭제가 아니라 tmux/Claude Code process stop으로만 문서화한다.

#### Progress

- 2026-05-19 13:55 KST: README, Operations, CLI manual, Web Chat guide에서 `kill`이 high-level bridge state/transcript를 삭제하지 않는 process stop임을 명시했다.
- 2026-05-19 13:55 KST: 같은 `SESSION_ID`로 다음 high-level `stream`을 호출하면 state/transcript 기준으로 다시 `--resume`될 수 있음을 문서화했다.

---

### 6. [Minor] `attach` 성공 조건이 좁게 설명되지 않았다

Status: accepted

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

- Decision: accept. 별도 `answer --session-id`는 지금 추가하지 않는다. `attach`는 active turn 재연결 전용으로 문서화하고, 완료된 답변은 앱 서버가 저장한 `done.answer`/`ask_result.answer` 또는 `info` state를 사용한다.

#### Progress

- 2026-05-19 13:56 KST: README, quickstart, CLI manual, web chat guide, operations guide에 attach 성공 조건을 명시했다.
- 2026-05-19 13:56 KST: attach가 완료된 과거 turn 조회가 아니라 `active_turn`이 남은 `active/timeout/interrupted` turn 재연결 전용임을 문서화했다.

---

### 7. [Minor] `reap` 문서가 active_turn skip 조건을 덜 보수적으로 설명한다

Status: accepted

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

- Decision: accept. 이번에는 기능 변경 없이 문서만 보강한다. `timeout/interrupted` stale active turn 자동 정리는 별도 설계가 필요하므로 현재 `reap`은 보수적 skip 정책으로 유지한다.

#### Progress

- 2026-05-19 14:00 KST: README, Operations, CLI manual, quickstart, web chat guide에 high-level `active_turn`이 남아 있고 `ready`가 아니면 `reap`이 보수적으로 skip한다고 명시했다.
- 2026-05-19 14:00 KST: `timeout`/`interrupted`는 입력 가능 또는 정리 가능 신호가 아니며 attach/retry/kill로 별도 처리한다고 문서화했다.

---

### 8. [Minor] exit code 표가 high-level 오류를 빠뜨린다

Status: accepted

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

- Decision: accept. CLI manual을 source table로 보강하고, README/quickstart/web guide에는 클라이언트가 처리해야 하는 high-level exit code를 요약한다.

#### Progress

- 2026-05-19 14:01 KST: CLI manual exit code 표에 `5`, `127`의 Claude executable case, `130`을 추가했다.
- 2026-05-19 14:01 KST: README, quickstart, web chat guide에 high-level state error와 interrupt 처리 기준을 추가했다.

---

### 9. [Minor] `reap --prefix ctc-`가 high-level 전용처럼 읽힌다

Status: accepted

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

- Decision: accept. CLI 기본 prefix `ctc-`는 유지하되, web-only 운영 문서에서는 `ctc-csess-`를 권장한다. `ctc-`는 controlled 전체 정리용으로 설명한다.

#### Progress

- 2026-05-19 14:03 KST: README, Operations, CLI manual, quickstart, web chat guide에서 web-only reap 예시를 `--prefix ctc-csess-`로 변경했다.
- 2026-05-19 14:03 KST: `ctc-` prefix는 low-level `ctc-*` tmux session도 state file이 있으면 대상이 될 수 있는 controlled 전체 정리용이라고 명시했다.

---

### 10. [Nit] `UUID v4` 표현이 코드의 검증 조건보다 강하다

Status: accepted

#### Evidence

- 문서는 `UUID v4`를 권장 형식으로 씁니다.
- 코드상 `uuid.UUID(session_id)`와 lowercase canonical string만 확인합니다.
- version 4를 강제하지 않습니다.

#### Risk

작은 불일치이지만, 클라이언트가 v1/v7 UUID를 쓰면 문서상 불허처럼 보일 수 있습니다.

#### Recommended Direction

```text
canonical hyphenated UUID string. UUID v4 권장. uppercase input은 lowercase canonical form으로 정규화.
```

으로 조정합니다.

#### Decision Needed

- 코드도 v4로 강제할지, 문서를 완화할지 결정.

#### Decision

- accept. 코드는 그대로 두고 문서를 완화한다.
- CLI generated id는 UUID v4, client-provided id는 canonical hyphenated UUID string으로 정의한다.
- UUID v4는 client-provided id에도 권장하지만 version 4 여부는 강제하지 않는다.
- uppercase UUID input은 허용하고 lowercase canonical form으로 정규화한다.

#### Progress

- 2026-05-19 14:05 KST: web integration guide, PRD, implementation checklist, HTML guide의 `UUID v4` 표현을 코드 계약에 맞게 완화했다.

---

### 11. [Major] `stream` start/resume 문서가 새 client-provided `session_id` 케이스를 잘못 설명한다

Status: accepted

#### Evidence

- `web-chat-integration-guide.md`는 `session_id`가 있고 tmux session이 없으면 항상 `--resume <session_id>`로 시작한다고 설명했다.
- 실제 구현은 기존 state 또는 matching transcript가 있을 때만 `--resume`을 사용한다.
- 기존 state/transcript가 없는 client-provided UUID는 `--session-id <session_id>`로 새 Claude Code session을 시작한다.

#### Risk

웹 서버가 첫 요청부터 자체 UUID를 만들어 넘기는 정상 흐름을 `resume` 흐름으로 오해할 수 있다.

#### Decision

- accept. 문서를 구현 기준으로 수정한다.
- `session_id`가 있어도 known state/transcript가 없으면 first-run `--session-id`를 사용한다고 명시한다.
- 함께 발견된 low-level stream metrics 예시, 불완전한 README stream 예시, UUID uppercase normalization 표현도 같이 정리한다.

#### Progress

- 2026-05-19 14:29 KST: README, docs README, quickstart, CLI manual, PRD, web guide MD/HTML을 구현 계약에 맞게 보정했다.

---

### 12. [Minor] context metrics 예시가 실측보다 강하게 보인다

Status: accepted

#### Evidence

- 실측한 Claude Code 2.1.144 transcript에는 `usage`는 있으나 `context`, `context_window`, `context_usage`가 없었다.
- 로컬 project transcript 4,907개 파일, 809,753 JSONL line을 스캔했을 때 context 계열 top-level field count는 0이었다.
- 기존 guide 예시는 `metrics.context.current_size`가 항상 나오는 것처럼 보였다.

#### Decision

- accept. context는 transcript에 실제 context 계열 field가 있을 때만 `metrics.context`에 포함한다.
- 정보가 없으면 usage 기반 추정치를 `context`에 넣지 않는다.
- UI 예시에서도 `input + cache_read + cache_write`를 context size로 표시하지 않는다.

#### Progress

- 2026-05-19 15:59 KST: README, quickstart, CLI manual, PRD, web guide MD/HTML, test를 context optional 계약으로 보정했다.

## Proposed Review Order

1. Finding 1: high-level/low-level API 경계
2. Finding 2: `SESSION_ID` 생성 주체
3. Finding 3: `ctc-$SESSION_ID` 제거/정리
4. Finding 4: 명령별 인자 타입 표 분리
5. Finding 5: `kill` 의미 정리
6. Finding 6-10: attach/reap/exit/prefix/UUID 세부 정리
7. Finding 11: stream start/resume 문서 보정
8. Finding 12: context metrics optional 계약 보정

## Decision Log

- 2026-05-19 13:18 KST: pager 생성. 아직 accept/skip 결정 없음.
- 2026-05-19 14:05 KST: Finding 1-10 모두 반영 완료. Critical 없음.
- 2026-05-19 14:29 KST: 추가 subagent 재검토에서 Finding 11을 확인하고 문서 보정 완료. Critical 없음.
- 2026-05-19 15:59 KST: context metrics 실측 결과를 반영해 추정 context를 넣지 않는 계약으로 문서와 테스트를 보정했다.
