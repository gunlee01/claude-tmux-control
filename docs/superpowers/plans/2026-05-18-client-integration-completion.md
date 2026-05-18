# Client Integration Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the web-chat-facing cc-tmux-bridge CLI contract so another program can create/reuse Claude Code sessions, stream one turn, inspect session state, receive final metrics, and run scenario tests through a small client.

**Architecture:** Keep `claude_tmux_control.py` as the single process boundary and preserve low-level commands. Add high-level service commands on top of the existing session state, transcript cursoring, and JSONL stream normalization. Store completed turn records in local state so `info`, `ask`, and client logs can report cumulative usage/cost without rereading the full transcript every request.

**Tech Stack:** Python 3 standard library, tmux, Claude Code transcript JSONL, `unittest`, shell-based smoke commands.

---

## Success Criteria

- High-level `stream` remains the primary one-turn API and supports `--attach --session-id <uuid>` for reconnecting to an active turn without sending another prompt.
- `ask [--session-id <uuid>] --cwd <path> <prompt...>` returns one final JSON object with `session_id`, `turn_id`, `answer`, `metrics`, and `events_seen`.
- `info <session-id>` returns machine-readable session metadata, tmux active state, transcript path, Claude transcript `sessionId`, active/last turn, completed turn count, usage totals, and cost totals.
- `list` returns controlled high-level sessions from local state and tmux.
- Completed turn state stores final answer, offsets, model, usage, context, turn cost, elapsed time, and cumulative session totals.
- Final `metrics` event includes `elapsed_ms` and `cost.session_usd` when turn cost is available.
- A simple client script can run `ctc stream`, parse JSONL line-by-line, log every request/event/final summary to JSONL, and validate event order.
- Unit tests cover the new commands, state updates, cumulative metrics, attach behavior, and client parser.
- Docs and interactive HTML no longer describe implemented features as future work.
- This plan itself is reviewed by a critic subagent before implementation, revised when Major issues are found, and re-reviewed until our final judgment is `NO_MAJOR`.
- Implementation is committed in small stages, then reviewed by a critic subagent until no Major issue remains.

## Files

- Modify: `docs/superpowers/plans/2026-05-18-client-integration-completion.md`
  - Keep a critic review log and final no-Major plan gate before code changes.
- Modify: `claude_tmux_control.py`
  - Add high-level `ask`, `info`, and `list` command parsers.
  - Add `stream --attach`.
  - Add completed turn state helpers and cumulative usage/cost helpers.
  - Add JSON payload builders shared by `stream`, `ask`, `info`, and `list`.
- Modify: `tests/test_claude_tmux_control.py`
  - Add unit coverage for parsing, state updates, high-level metrics, `ask`, `info`, `list`, and attach.
- Create: `scripts/web_chat_client.py`
  - Client-style integration runner that spawns high-level `stream`, parses JSONL, logs request/events/summary, and validates the observed contract.
- Create: `tests/test_web_chat_client_script.py`
  - Unit tests for client command construction, JSONL parsing, event order validation, and log format.
- Modify: `docs/implementation-checklist.md`
  - Update checkboxes based on implemented commands and tested behavior.
- Modify: `docs/cli-manual.md`
  - Document `stream --attach`, `ask`, `info`, `list`, final metrics, and client script.
- Modify: `docs/web-chat-integration-guide.md`
  - Update web-chat integration flow to use CLI-provided elapsed/cumulative metrics.
- Modify: `docs/web-chat-integration-guide.html`
  - Mirror the markdown guide changes.
- Modify: `docs/web-chat-app-flow.html`
  - Remove stale future-work wording and show `session_usd`/`elapsed_ms`.
- Modify: `docs/test-scenarios.md`
  - Add client scenario commands and expected log checks.
- Create: `logs/integration/.gitkeep`
  - Keep an ignored directory for preserving local scenario logs.
- Create during verification: `logs/integration/<timestamp>-client-smoke.jsonl`
  - Real client scenario log artifact used by completion audit. The directory is ignored except `.gitkeep`.
- Modify: `AGENTS.md`
  - State that web integration docs and client scenario tests must be updated when the high-level CLI contract changes.

## Task 0: Pre-Implementation Critic Gate

**Files:**
- Modify: `docs/superpowers/plans/2026-05-18-client-integration-completion.md`

- [ ] **Step 1: Ask critic to review this plan before implementation**

Use a subagent prompt that asks for:

- Major/Minor/Nit classification.
- Major gaps against the user objective.
- Missing test or artifact evidence.
- Unsafe sequencing.

- [ ] **Step 2: Record critic result**

Append a short review log entry under `Plan Critic Review Log` in this document:

```markdown
- Round N: MAJOR_REMAINS
  - Major: ...
  - Action: ...
```

- [ ] **Step 3: Revise and repeat until no Major**

If any Major is accepted by the main agent, revise the plan and rerun the critic. Implementation starts only after the main agent records:

```markdown
- Round N: NO_MAJOR
```

- [ ] **Step 4: Commit the accepted plan**

Run:

```bash
git add docs/superpowers/plans/2026-05-18-client-integration-completion.md
git commit -m "클라이언트 통합 완성 계획 추가

🤖 Co-authored with AI agent"
```

## Task 1: Completed Turn State And Final Metrics

**Files:**
- Modify: `claude_tmux_control.py`
- Modify: `tests/test_claude_tmux_control.py`

- [ ] **Step 1: Add failing tests for completed turn storage**

Add tests that call `build_pending_turn_state()`, then complete a fake turn with a metrics payload. The expected state shape is:

```json
{
  "completed_turns": [
    {
      "turn_id": "turn_...",
      "answer": "done",
      "completed_offset": 456,
      "elapsed_ms": 2500,
      "model": "claude-sonnet-4.6",
      "usage": {
        "input_tokens": 10,
        "cache_read_tokens": 20,
        "cache_write_tokens": 30,
        "output_tokens": 40
      },
      "cost": {
        "estimated": true,
        "turn_usd": 0.001
      }
    }
  ],
  "usage_totals": {
    "input_tokens": 10,
    "cache_read_tokens": 20,
    "cache_write_tokens": 30,
    "output_tokens": 40
  },
  "cost_totals": {
    "currency": "USD",
    "session_usd": 0.001
  }
}
```

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_claude_tmux_control.HighLevelStreamSetupTest
```

Expected: fail because completed turn storage does not exist yet.

- [ ] **Step 2: Add runtime timing fields**

Add fields to `StreamRuntime`:

```python
started_at_monotonic: float = 0.0
started_at_utc: str | None = None
```

Populate them in `prepare_high_level_stream()` using `time.monotonic()` and `_utc_timestamp(now())`. Keep current constructor call sites compatible by giving defaults.

- [ ] **Step 3: Add state helpers**

Add helpers:

```python
def build_completed_turn_record(runtime, turn_events, completed_offset, elapsed_ms, metrics):
    ...

def add_completed_turn_to_state(state, completed_record):
    ...

def usage_totals_from_completed_turns(turns):
    ...

def cost_totals_from_completed_turns(turns):
    ...
```

Rules:

- De-duplicate completed turns by `turn_id`.
- Sum only numeric usage fields.
- Sum `cost.turn_usd` when it is numeric and `cost.currency == "USD"`. Preserve `estimated: true` in the current pricing implementation so callers know the value comes from `claude_pricing.json`.
- Keep at most the latest 200 completed turn records in state.

- [ ] **Step 4: Include elapsed and cumulative cost in final metrics**

Change `high_level_metrics_payload()` to accept `elapsed_ms` and optional `state`. Include:

```json
{
  "elapsed_ms": 2500,
  "cost": {
    "turn_usd": 0.001,
    "session_usd": 0.003
  }
}
```

Use the current turn cost plus existing state cumulative value for the streamed final metrics. Then `_mark_turn_done()` persists the same final record and recomputed totals.

- [ ] **Step 5: Run tests and commit**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
python3 -m py_compile claude_tmux_control.py
git add claude_tmux_control.py tests/test_claude_tmux_control.py
git commit -m "고수준 턴 메트릭 누적 저장 추가

🤖 Co-authored with AI agent"
```

## Task 2: High-Level `info` And `list`

**Files:**
- Modify: `claude_tmux_control.py`
- Modify: `tests/test_claude_tmux_control.py`

- [ ] **Step 1: Add parser tests**

Add tests for:

```bash
./claude_tmux_control.py info 550e8400-e29b-41d4-a716-446655440000 --json
./claude_tmux_control.py list --json
```

Expected parsed fields:

- `command_name == "info"` or `"list"`
- `state_dir == DEFAULT_STATE_DIR`
- `json is True`

- [ ] **Step 2: Add payload behavior tests**

Add tests that cover:

- `info` for a known state file with active tmux session.
- `info` when state exists but tmux session is inactive.
- `info` extracts Claude transcript `sessionId` from a transcript record.
- `list` includes state-backed sessions.
- `list` includes tmux-only `ctc-csess-<uuid>` sessions when the state file is missing.
- `list` ignores non-UUID controlled-looking filenames.

- [ ] **Step 3: Implement `info` payload builder**

Add:

```python
def build_session_info_payload(session_id, state_dir, root, controller):
    ...
```

Payload fields:

- `event: "info"`
- `session_id`
- `tmux_session`
- `tmux_active`
- `cwd`
- `state_path`
- `transcript_path`
- `claude_transcript_session_id`
- `active_turn`
- `last_turn`
- `completed_turn_count`
- `usage_totals`
- `cost_totals`

Extract Claude transcript session id by scanning the known transcript for the first string field named `sessionId`, `session_id`, or nested message/session id.

- [ ] **Step 4: Implement `list` payload builder**

Add:

```python
def build_session_list_payload(state_dir, root, controller):
    ...
```

It should read `state_dir/sessions/*.json`, validate UUID filenames, and include tmux sessions with prefix `ctc-csess-` even if state is missing.

- [ ] **Step 5: Wire `_run_command()`**

`info --json` and `list --json` print JSON. Non-JSON mode can print compact table lines, but JSON is the service contract.

- [ ] **Step 6: Run tests and commit**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
git add claude_tmux_control.py tests/test_claude_tmux_control.py
git commit -m "세션 info와 list 명령 추가

🤖 Co-authored with AI agent"
```

## Task 3: High-Level `ask`

**Files:**
- Modify: `claude_tmux_control.py`
- Modify: `tests/test_claude_tmux_control.py`

- [ ] **Step 1: Add parser and behavior tests**

Add tests for:

```bash
./claude_tmux_control.py ask --cwd /tmp/project "hello"
./claude_tmux_control.py ask --session-id 550e8400-e29b-41d4-a716-446655440000 --cwd /tmp/project "hello"
```

Behavior test uses a fake writer list from the existing stream function and asserts the final printed JSON contains:

- `event: "ask_result"`
- `session_id`
- `turn_id`
- `answer`
- `metrics`
- `events_seen`

Also add timeout/failure tests:

- When the shared turn runner returns non-ready, `ask` exits non-zero.
- The non-ready JSON object contains `event: "ask_result"`, `state`, `reason`, `session_id` when available, and omits a misleading final `answer`.

- [ ] **Step 2: Implement shared high-level runner**

Refactor `_run_high_level_stream()` into a helper:

```python
def run_high_level_turn(args, controller, write):
    ...
```

It prepares the runtime, waits for transcript, streams until done, and returns a structured result:

```python
{
  "status": ScreenStatus(...),
  "events": [...],
  "done": {...},
  "metrics": {...}
}
```

Keep stdout behavior for `stream` unchanged.

- [ ] **Step 3: Implement `_run_high_level_ask()`**

Use the shared runner with an in-memory JSONL writer. Print one compact JSON object:

```json
{
  "event": "ask_result",
  "session_id": "...",
  "turn_id": "...",
  "answer": "...",
  "metrics": {...},
  "events_seen": 8
}
```

Exit code is `0` only when the stream status is `ready`.

- [ ] **Step 4: Run tests and commit**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
git add claude_tmux_control.py tests/test_claude_tmux_control.py
git commit -m "비스트리밍 ask 명령 추가

🤖 Co-authored with AI agent"
```

## Task 4: `stream --attach`

**Files:**
- Modify: `claude_tmux_control.py`
- Modify: `tests/test_claude_tmux_control.py`

- [ ] **Step 1: Add attach tests**

Create state with an `active_turn` and assert:

- `stream --attach --session-id <uuid>` does not call `send_prompt`.
- It resumes reading from `active_turn.replay_start_offset`.
- It emits events with the existing `turn_id`.
- It rejects attach when no active turn exists.
- It rejects attach when tmux session is missing.
- It returns a structured error when an active turn exists but transcript path cannot be resolved.
- It refreshes owner/heartbeat metadata only after the caller successfully takes attach ownership.

- [ ] **Step 2: Implement attach runtime**

Add:

```python
def prepare_high_level_attach(controller, session_id, state_dir, root):
    ...
```

Rules:

- Require a valid existing UUID.
- Require `active_turn.stream_state in {"active", "timeout"}`.
- Require the controlled tmux session to exist.
- Use stored `cwd`, `prompt_preview`, `turn_id`, `replay_start_offset`, and transcript path.
- Do not write a new `active_turn` except heartbeat/owner takeover metadata.

- [ ] **Step 3: Wire stream parser**

Add `stream --attach`. In attach mode:

- `--session-id` is required.
- `--cwd` and prompt are optional.
- No prompt is sent.
- JSONL output remains the same event contract.

- [ ] **Step 4: Run tests and commit**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
git add claude_tmux_control.py tests/test_claude_tmux_control.py
git commit -m "진행 중 턴 attach 스트림 추가

🤖 Co-authored with AI agent"
```

## Task 5: Client Integration Script And Scenario Logs

**Files:**
- Create: `logs/integration/.gitkeep`
- Create: `scripts/web_chat_client.py`
- Create: `tests/test_web_chat_client_script.py`
- Modify: `docs/test-scenarios.md`

- [ ] **Step 1: Add client script tests**

Test:

- Command construction includes `stream`, optional `--session-id`, `--cwd`, `--oauth-token-env`, and prompt.
- JSONL parser accepts `assistant_text`, `tool_use`, `tool_result`, `done`, and `metrics`.
- Validator requires `done` before `metrics`.
- Log writer emits `request`, `event`, and `summary` records.

- [ ] **Step 2: Implement `scripts/web_chat_client.py`**

CLI shape:

```bash
python3 scripts/web_chat_client.py \
  --ctc ./claude_tmux_control.py \
  --cwd "$PWD" \
  --prompt "Reply with exactly: client-ok" \
  --log "logs/integration/client-smoke.jsonl" \
  --timeout 120
```

Optional:

- `--session-id <uuid>`
- `--oauth-token-env ACCOUNT_A_TOKEN`
- `--state-dir "$HOME/.cache/claude-tmux-control"`
- `--expect-answer "client-ok"`

Exit codes:

- `0`: done and metrics observed, expectation matched.
- `3`: stream process failed.
- `4`: contract validation failed.

- [ ] **Step 3: Add scenario docs**

Document how to run:

```bash
mkdir -p logs/integration
LOG="logs/integration/$(date -u +%Y%m%dT%H%M%SZ)-client-smoke.jsonl"
python3 scripts/web_chat_client.py --cwd "$PWD" --prompt "Reply with exactly: client-ok" --expect-answer client-ok --log "$LOG"
tail -n 20 "$LOG"
```

`logs/integration/*.jsonl` is ignored by git but preserved locally as the completion evidence artifact.

- [ ] **Step 4: Run tests and commit**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
python3 -m py_compile claude_tmux_control.py scripts/web_chat_client.py scripts/stream_question.py
git add logs/integration/.gitkeep scripts/web_chat_client.py tests/test_web_chat_client_script.py docs/test-scenarios.md
git commit -m "웹채팅 클라이언트 통합 테스트 스크립트 추가

🤖 Co-authored with AI agent"
```

## Task 6: Docs And HTML Contract Updates

**Files:**
- Modify: `docs/implementation-checklist.md`
- Modify: `docs/cli-manual.md`
- Modify: `docs/web-chat-integration-guide.md`
- Modify: `docs/web-chat-integration-guide.html`
- Modify: `docs/web-chat-app-flow.html`
- Modify: `AGENTS.md`

- [ ] **Step 1: Update docs**

Remove stale future-work wording for implemented items:

- `ask`
- `info`
- `list`
- `stream --attach`
- `elapsed_ms`
- `cost.session_usd`
- completed turn cumulative store
- client scenario script

- [ ] **Step 2: Validate docs**

Run:

```bash
python3 - <<'PY'
from html.parser import HTMLParser
from pathlib import Path
class P(HTMLParser): pass
for path in [Path("docs/web-chat-integration-guide.html"), Path("docs/web-chat-app-flow.html")]:
    P().feed(path.read_text(encoding="utf-8"))
print("html ok")
PY
rg -n "future work|app server가 enrich|앱 서버가 enrich|elapsed/session cumulative" docs AGENTS.md
```

Expected: no stale future-work wording for features implemented by this plan. Any remaining mention must explicitly describe an unimplemented future feature not covered here.

- [ ] **Step 3: Run tests and commit**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
git add docs AGENTS.md
git commit -m "웹채팅 연동 문서 최신 계약 반영

🤖 Co-authored with AI agent"
```

## Task 7: Scenario Integration Test And Critic Review

**Files:**
- Modify only if failures require fixes.

- [ ] **Step 1: Run full local verification**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
python3 -m py_compile claude_tmux_control.py scripts/web_chat_client.py scripts/stream_question.py
```

- [ ] **Step 2: Run real client smoke and preserve the log artifact**

Run:

```bash
SESSION_ID="$(python3 - <<'PY'
import uuid
print(uuid.uuid4())
PY
)"
mkdir -p logs/integration
LOG="logs/integration/$(date -u +%Y%m%dT%H%M%SZ)-client-smoke-${SESSION_ID}.jsonl"
TERM=xterm-256color python3 scripts/web_chat_client.py \
  --ctc ./claude_tmux_control.py \
  --cwd "$PWD" \
  --session-id "$SESSION_ID" \
  --prompt "Reply with exactly: client-ok" \
  --expect-answer "client-ok" \
  --log "$LOG" \
  --timeout 180
tail -n 20 "$LOG"
TERM=xterm-256color ./claude_tmux_control.py kill "ctc-csess-${SESSION_ID}" || true
```

Expected:

- Client exits `0`.
- Log has one `request`, multiple `event` records, and one `summary`.
- Observed stream order has `done` before `metrics`.
- Summary includes `session_id`, `turn_id`, `answer`, `metrics.cost.turn_usd`, and `metrics.cost.session_usd`.
- The exact `$LOG` path is recorded in the completion audit.

If `tmux` or `claude` is unavailable, this is not silently skipped. Record the missing executable/error as a blocker, run the unit-level fake client tests as partial evidence, and do not mark the objective complete until either the real smoke runs or the user explicitly accepts the environment limitation.

- [ ] **Step 3: Ask critic subagent for implementation review**

Prompt the critic to check:

- Any Major gap against this plan and the user objective.
- Any command contract mismatch for a web chat backend.
- Whether tests and real client logs cover the critical behavior.
- Whether docs match actual code.

- [ ] **Step 4: Fix Major issues and repeat review**

Repeat implementation review until our final judgment is `NO_MAJOR`.

- [ ] **Step 5: Completion audit**

Build a prompt-to-artifact checklist with evidence:

- plan critic loop
- staged commits
- implemented commands
- unit test output
- client script
- real scenario log
- implementation critic loop
- docs updated

Only then mark the active goal complete.

## Plan Critic Review Log

- Round 1: MAJOR_REMAINS
  - Major: Plan lacked an explicit pre-implementation critic gate.
  - Major: Scenario logs were written only to `/tmp` and were not preserved as audit artifacts.
  - Major: Real client smoke was conditional without blocker handling.
  - Action: Added Task 0, durable `logs/integration/*.jsonl` evidence, and a no-silent-skip rule for real smoke.
- Round 2: NO_MAJOR
  - Major: None.
  - Minor cleanup: aligned the client CLI example with `logs/integration` and included `.gitkeep` in the client artifact commit command.
