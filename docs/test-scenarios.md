# Test Scenarios

[English](./test-scenarios.md) | [한국어](./test-scenarios.ko.md)

## Must Have

- Run the local refactor contract gate before and after each module-extraction phase.
- Run the no-auth Docker refactor contract gate before merging module-extraction changes.
- Run the authenticated Docker live smoke when auth env is available.
- Start a new high-level stream session.
- Send the second turn to the same session.
- Replay the last completed turn.
- Reconnect to an active turn after client disconnect.
- Cancel a long-running tool turn and continue the same session.
- Kill or reap an idle tmux session and resume later.
- Reject a new prompt while a turn is active.
- Preserve `done` then `metrics` ordering.
- Redact tokens from logs and docs.

## Refactor Contract Gate

Run this before moving code and after each extraction phase.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/refactor_contract_check.py --phase all
```

For a narrower phase gate:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/refactor_contract_check.py --phase 0
PYTHONDONTWRITEBYTECODE=1 python3 scripts/refactor_contract_check.py --phase 7
```

Expected:

- `import claude_tmux_control as ctc` facade contract remains stable.
- console script targets remain `claude_tmux_control:main`.
- transcript dataclass identity remains canonical through `transcript_events.py`.
- import boundaries prevent lower modules from importing `ctc_cli`, `ctc_bridge_sessions`, or the facade.
- unit tests, py-compile, docs check, `--version`, and `--help` pass.

## Client-style Scenario

```bash
SESSION_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"

ctc stream --cwd "$PWD" --session-id "$SESSION_ID" "Reply with exactly: one"
ctc stream --cwd "$PWD" --session-id "$SESSION_ID" "Reply with exactly: two"
ctc last "$SESSION_ID" --last 2
```

Expected:

- first stream returns `done.answer = one`.
- second stream returns `done.answer = two`.
- replay returns both completed turns.

## Disconnect Scenario

Start a long-running prompt, kill the client process, then attach.

```bash
ctc stream --cwd "$PWD" --session-id "$SESSION_ID" "Use Bash to sleep 8; echo done"
ctc stream --attach --session-id "$SESSION_ID" --timeout 120
```

Expected:

- a new prompt during the active turn returns `turn_in_progress`.
- attach streams through `done`/`metrics`.

## Cancel Scenario

```bash
ctc stream --cwd "$PWD" --session-id "$SESSION_ID" "Use Bash to sleep 30"
ctc cancel "$SESSION_ID"
ctc last "$SESSION_ID" --last 1
ctc stream --cwd "$PWD" --session-id "$SESSION_ID" "Reply with exactly: after-cancel"
```

Expected:

- cancelled turn reaches `done`/`metrics`.
- command output from the interrupted tool may be absent.
- next turn succeeds.

## Docker Refactor Contract Smoke

This smoke does not require Claude auth. It verifies build/install/import/entrypoint basics inside the Docker image.

```bash
scripts/docker_refactor_contract_check.sh
```

Expected:

- Docker image builds.
- container runs as non-root `ctc`.
- `tmux`, `claude`, `ctc`, and `claude-tmux-control` are available.
- `ctc --version`, `claude-tmux-control --version`, and `ctc --help` work.
- installed package imports pass from outside the repository cwd.
- pricing data file is installed and readable.
- entrypoint preseed files are created.
- local refactor contract gate passes inside the image.

## Authenticated Docker Live Smoke

```bash
docker build -t claude-tmux-control -f docker/Dockerfile .

docker run --rm \
  -e CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  -v "$PWD":/repo \
  -w /repo \
  claude-tmux-control \
  ctc stream --cwd /repo "Reply with exactly: docker-ok"
```

Expected:

- entrypoint preflight succeeds.
- no onboarding/trust/bypass/managed-settings prompt blocks the stream.
- output includes `done.answer = docker-ok` and final `metrics`.

The helper script can run this authenticated live smoke when auth env is available:

```bash
CTC_DOCKER_LIVE_SMOKE=1 scripts/docker_refactor_contract_check.sh
```
