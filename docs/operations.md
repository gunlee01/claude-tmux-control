# Operations Guide

[English](./operations.md) | [한국어](./operations.ko.md)

This guide covers session lifecycle, idle cleanup, and recovery procedures for running `ctc` on a server.

## 1. Session Lifecycle

A high-level web session creates these resources.

| Resource | Location/name |
| --- | --- |
| tmux session | `ctc-csess-<SESSION_ID>` |
| Claude Code process | inside the tmux pane |
| transcript JSONL | `~/.claude/projects/<encoded-cwd>/<session>.jsonl` |
| bridge state | `~/.cache/claude-tmux-control/sessions/<SESSION_ID>.json` |
| lock file | `~/.cache/claude-tmux-control/locks/<SESSION_ID>.lock` |

A tmux session stays alive until it is explicitly killed or reaped.

## 2. Standard Loop

```text
app server
  -> stream one turn
  -> persist session_id, optional done.answer, metrics
  -> return UI control to user

scheduler
  -> run reap periodically
  -> kill idle controlled sessions
```

`reap` scans once and exits. Run it through cron, systemd timer, or an application scheduler.

## 3. Idle Cleanup

Example 30-minute cleanup:

```bash
TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc-csess- --dry-run
TERM=xterm-256color ctc reap --idle-seconds 1800 --prefix ctc-csess-
```

Use `--dry-run` first.

Use `--prefix ctc-csess-` for web sessions. `--prefix ctc-` is broader and can include manually created controlled sessions.

## 4. Reap Safety Policy

`reap` kills a session only when all conditions hold.

| Condition | Meaning |
| --- | --- |
| prefix match | target matches the requested prefix |
| idle exceeded | last input/state age is greater than `--idle-seconds` |
| no active work | high-level `active_turn` is absent or `ready` |
| not working | transcript/screen does not indicate active work |

For high-level sessions, `reap` first checks whether a stale `active_turn` can be finalized from a ready transcript and ready tmux screen. If it can, a real reap finalizes the turn before applying the idle kill decision. `--dry-run` only simulates this check and does not write state or kill sessions.

`timeout` and `interrupted` do not automatically mean ready. Use `stream --attach`, retry with the same `session_id`, or explicitly `kill` after inspection.

## 5. Manual Stop

```bash
TERM=xterm-256color ctc kill "ctc-csess-$SESSION_ID"
```

Killing a tmux session also terminates the Claude Code process inside it. It does not delete bridge state or Claude transcript data. A later `ctc stream` with the same `SESSION_ID` can resume when state/transcript data exists.

## 6. Recovery

| Situation | Recommended handling |
| --- | --- |
| `turn_in_progress` | attach to the active turn, replay last turn, or retry later |
| user cancel | run `cancel`, then `last --last 1` or `stream --attach` |
| timeout | keep UI in processing state and allow attach/retry |
| Ctrl+C/interrupted | attach if tmux still exists; otherwise retry same session |
| tmux missing | next stream can start/resume with same session id |
| cwd mismatch | treat as incorrect session binding and reject |
| needs confirmation | operator inspection or session restart |

`stream --attach` is not a historical replay command. It attaches only to an active turn.

To replay completed turns:

```bash
ctc replay "$SESSION_ID" --last 1
ctc replay "$SESSION_ID" --last 5
ctc last "$SESSION_ID" --last 1
```

If the last turn is still active, `last`/`replay` attach to it and continue until `done`/`metrics`.

## 7. Suggested Cron

```cron
* * * * * TERM=xterm-256color /path/to/ctc reap --idle-seconds 1800 --prefix ctc-csess- >> /var/log/ctc-reap.log 2>&1
```

Do not log OAuth tokens or other secrets.

## 8. Inspection Commands

```bash
TERM=xterm-256color ctc list --json
TERM=xterm-256color ctc info "$SESSION_ID" --json
TERM=xterm-256color tmux list-sessions
```

## 9. Operations Checklist

- [ ] `tmux` and `claude` are in `PATH`.
- [ ] the app server sets `TERM=xterm-256color`.
- [ ] session ids are UUIDs.
- [ ] the same chat uses the same session id.
- [ ] a `reap` scheduler is registered.
- [ ] `reap --dry-run` is verified before production use.
- [ ] logs redact OAuth tokens and env secrets.
- [ ] runbooks include `info`, `list`, `stream --attach`, `cancel`, `kill`, and `reap`.
