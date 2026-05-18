# Test Scenarios

이 문서는 high-level `stream [--session-id] --cwd <path> <prompt>`를 웹 클라이언트처럼 반복 검증하기 위한 시나리오 목록입니다.

## Must Have

| Scenario | Failure caught | Expected observable |
| --- | --- | --- |
| UUID validation | path/tmux target injection | invalid `session_id` fails before state path is built |
| cwd mismatch | cross-project transcript mixup | existing session with different canonical cwd returns `session_cwd_mismatch` |
| active turn blocks second prompt | prompt interleaving | second stream returns `turn_in_progress` and does not call tmux send |
| first launch command | permission prompt / missing session id | tmux starts `claude --session-id <uuid> --dangerously-skip-permissions "<prompt>"` |
| resume command | losing Claude transcript continuity | inactive existing session starts `claude --resume <uuid> --dangerously-skip-permissions "<prompt>"` |
| repeated prompt anchor | wrong turn selection | stream starts from pre-send offset, not earlier same prompt text |
| stable event ids | replay dedupe failure | every progress event has `turn_id`, `event_id`, `source_offset`, `source_end_offset`, `block_index` |
| done then metrics | missing final accounting | `done` is followed by `metrics` with same `turn_id` and deterministic synthetic ids |
| client scenario log | backend/UI contract drift | `scripts/web_chat_client.py` writes `request`, `event`, and `summary` JSONL records |
| tool_result preview limit | oversized payload | `tool_result.text` is truncated by default and marks `text_truncated` |
| tool_result not final | premature input enable | stream times out or keeps working until final assistant text appears |
| low-level compatibility | breaking existing shell workflow | `stream SESSION` remains accepted and keeps old behavior |

## Client-Style Scenario

이 스크립트는 웹 서버가 `stream` subprocess를 실행하고 stdout JSONL을 SSE/WebSocket으로 중계하는 상황을 흉내 낸다.

```bash
mkdir -p logs/integration
SESSION_ID="$(python3 - <<'PY'
import uuid
print(uuid.uuid4())
PY
)"
LOG="logs/integration/$(date -u +%Y%m%dT%H%M%SZ)-client-smoke-${SESSION_ID}.jsonl"
TERM=xterm-256color python3 scripts/web_chat_client.py \
  --ctc ./claude_tmux_control.py \
  --cwd "$PWD" \
  --session-id "$SESSION_ID" \
  --prompt "Reply with exactly: client-ok" \
  --expect-answer "client-ok" \
  --tool-result-limit 100 \
  --log "$LOG" \
  --timeout 180
tail -n 20 "$LOG"
```

Expected:

- client exit code is `0`
- log contains one `request` record
- log contains streamed `event` records
- log contains one `summary` record
- `summary.ok` is `true`
- `summary.answer` is `client-ok`
- `summary.metrics.cost.turn_usd` exists when Claude transcript usage/model are available
- `summary.metrics.cost.session_usd` exists when turn cost is available
- stream order has `done` before `metrics`

Cleanup:

```bash
TERM=xterm-256color ./claude_tmux_control.py kill "ctc-csess-${SESSION_ID}" || true
```

## Optional Real Smoke

Run only when `tmux` and `claude` are available and a disposable project directory is safe.

```bash
SESSION_ID="$(python3 - <<'PY'
import uuid
print(uuid.uuid4())
PY
)"
PROJECT_DIR="$(mktemp -d)"
TERM=xterm-256color ./claude_tmux_control.py stream \
  --session-id "$SESSION_ID" \
  --cwd "$PROJECT_DIR" \
  "Reply with exactly: ctc-smoke-ok"
```

Expected:

- JSONL contains `assistant_text`
- JSONL contains `done`
- JSONL contains `metrics`
- every event includes the same `session_id`
- final `metrics.event_id` is `<turn_id>:metrics:<completed_offset>`

Second turn:

```bash
TERM=xterm-256color ./claude_tmux_control.py stream \
  --session-id "$SESSION_ID" \
  --cwd "$PROJECT_DIR" \
  "Reply with exactly: ctc-smoke-ok-2"
```

Expected:

- same `session_id`
- existing tmux session reused if still active
- no `session_cwd_mismatch`

Resume after tmux is gone:

```bash
TERM=xterm-256color tmux kill-session -t "ctc-csess-$SESSION_ID"
TERM=xterm-256color ./claude_tmux_control.py stream \
  --session-id "$SESSION_ID" \
  --cwd "$PROJECT_DIR" \
  "Reply with exactly: ctc-smoke-ok-3"
```

Expected:

- bridge starts `claude --resume <session_id> ...`
- stream still emits `assistant_text`, `done`, and `metrics`
- transcript selection stays within the same cwd and `session_id`

Mismatched cwd check:

```bash
OTHER_DIR="$(mktemp -d)"
TERM=xterm-256color ./claude_tmux_control.py stream \
  --session-id "$SESSION_ID" \
  --cwd "$OTHER_DIR" \
  "This should fail"
```

Expected:

- non-zero exit
- stderr JSON contains `session_cwd_mismatch`
