# 구현 체크리스트

[English](./implementation-checklist.md) | [한국어](./implementation-checklist.ko.md)

## 1. Source Model

- [x] Claude Code transcript JSONL을 primary structured output으로 사용한다.
- [x] tmux screen capture는 readiness/debugging fallback으로만 사용한다.
- [x] assistant text, thinking, tool use, tool result, done, metrics event를 normalize한다.

## 2. Session Identity

- [x] high-level session id는 UUID를 사용한다.
- [x] high-level session을 `ctc-csess-<session_id>` tmux name으로 매핑한다.
- [x] `session_id`를 path나 tmux name에 쓰기 전에 검증한다.
- [x] 기존 session state와 다른 cwd가 들어오면 거절한다.

## 3. CLI Contract

- [x] high-level `stream`을 추가한다.
- [x] `ask`를 추가한다.
- [x] `cancel`을 추가한다.
- [x] `last`/`replay`를 추가한다.
- [x] `info`/`list`를 추가한다.
- [x] low-level tmux debug command를 유지한다.

## 4. Machine-readable Output

- [x] JSONL stream event를 출력한다.
- [x] `session_id`, `turn_id`, `event_id`, source offset, block index를 포함한다.
- [x] `done`을 `metrics`보다 먼저 출력한다.
- [x] transcript usage가 있을 때 final token/cost metrics를 출력한다.

## 5. Local Storage And Cursoring

- [x] bridge state를 `~/.cache/claude-tmux-control/` 아래에 저장한다.
- [x] send/start/resume 전에 transcript baseline을 캡처한다.
- [x] start/resume/send 구간에는 짧은 send lock을 사용한다.
- [x] state write에는 state-write lock과 generation compare/update를 사용한다.
- [x] active turn state와 heartbeat를 추적한다.
- [x] replay와 deduplication을 위해 transcript offset을 추적한다.
- [x] stale active turn을 보수적으로 복구한다.

## 6. Lifecycle And Cleanup

- [x] 기존 tmux session이 ready이면 재사용한다.
- [x] tmux는 없지만 state/transcript가 있으면 Claude Code를 resume한다.
- [x] `reap`에 `--dry-run`을 추가한다.
- [x] active/working session은 reap에서 제외한다.
- [x] 명시적 process stop을 위한 `kill`을 추가한다.

## 7. Authentication And Permissions

- [x] caller-provided OAuth token을 `CLAUDE_CODE_OAUTH_TOKEN`으로 전달한다.
- [x] `--oauth-token-env`를 지원한다.
- [x] `.ctc.env`, `--env-file`, `--env`로 project env injection을 지원한다.
- [x] Claude Code launch 기본값은 `--dangerously-skip-permissions`로 둔다.
- [x] permission override flag를 중복 추가하지 않는다.
- [x] Claude Code executable은 `claude`로 고정하고, trusted launch option은 `--model` / `--claude-args`로 전달한다.
- [x] 새 Claude Code process 실행 전에 high-level `--cwd`를 Claude Code trusted project로 preseed한다.

## 8. Docker

- [x] Python, tmux, Claude Code, `ctc`를 포함한 Dockerfile을 추가한다.
- [x] non-root user로 실행한다.
- [x] onboarding, trust, bypass warning preseed를 entrypoint에 추가한다.
- [x] managed settings cache를 위한 `claude -p` preflight를 추가한다.
- [x] Docker 사용법과 제한사항을 문서화한다.

## 9. Testing

- [x] core CLI behavior unit test를 추가한다.
- [x] CI에서 Docker image build와 CLI smoke를 검증한다.
- [x] stream/replay/cancel/kill/reap behavior scenario harness를 추가한다.
- [x] stale state write가 완료된 active turn을 되살리지 않는 regression test를 추가한다.
- [x] 격리된 임시 Claude home/config를 사용해 high-level trusted project preseed integration test를 추가한다.
- [x] normalized stream event contract를 위한 transcript compatibility fixture를 추가한다.

## 10. Future Work

- [ ] exactly-once delivery를 위한 explicit client acknowledgement protocol을 추가한다.
- [x] machine-readable stats command를 추가한다.
- [x] `active_turn_recovery` guidance로 failed/timeout recovery UX를 개선한다.
- [x] Claude Code transcript schema change 추적을 위한 transcript compatibility fixture를 추가한다.
- [ ] managed-settings preflight와 `ctc stream`을 실제로 실행하는 optional authenticated Docker stream smoke를 추가한다.
