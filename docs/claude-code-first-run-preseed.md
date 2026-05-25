# Claude Code First-run Preseed

[English](./claude-code-first-run-preseed.md) | [한국어](./claude-code-first-run-preseed.ko.md)

This document explains why `claude-tmux-control` prepares Claude Code configuration before starting an interactive tmux session.

## Purpose

`ctc stream` is designed for service flows where a backend process starts Claude Code inside tmux and streams JSONL events back to a client.

That flow cannot rely on a human seeing and answering Claude Code first-run prompts inside the tmux pane. If the first interactive run stops on onboarding, project trust, dangerous-mode warnings, or managed-settings approval, the client sees no useful assistant answer and the stream can appear stuck.

The preseed logic makes a fresh container or a fresh Claude config directory behave like a service-ready Claude Code environment before the interactive session starts.

## What It Prevents

Without preseed, a first run can stop on prompts such as:

- global Claude Code onboarding
- project trust confirmation for the working directory
- project onboarding screens
- `--dangerously-skip-permissions` warning
- managed settings security approval

These prompts are valid for a human terminal session, but they break the expected backend flow:

```text
client/backend
  -> ctc stream
    -> tmux session
      -> Claude Code waits for first-run confirmation
  <- no final assistant answer
```

## Effect

With preseed enabled, `ctc` can start Claude Code in tmux and proceed directly to the requested prompt.

Expected service flow:

```text
container start
  -> Docker entrypoint prepares Claude config
  -> optional claude -p preflight creates managed-settings cache
ctc stream
  -> preseed requested --cwd before new Claude process
  -> start Claude Code in tmux
  -> stream assistant_text, done, and metrics events
```

This does not create or mint authentication. The runtime still needs a valid Claude auth source such as `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`.

## Where The Logic Lives

| Area | Location | Role |
| --- | --- | --- |
| Docker image wiring | `docker/Dockerfile` | installs `ctc-docker-entrypoint` as the image entrypoint |
| Docker first-run preseed | `docker/entrypoint.sh` | writes initial Claude config before running `ctc` |
| Docker managed-settings preflight | `docker/entrypoint.sh` | runs `claude -p` once when auth env is available |
| Runtime project trust preseed | `claude_tmux_control.py` `preseed_claude_project_trust()` | prepares `--cwd` before a new high-level Claude process |
| Runtime call site | `claude_tmux_control.py` `prepare_high_level_stream()` | calls preseed immediately before `controller.start_session(...)` |

## Docker EntryPoint Preseed

The Docker entrypoint runs before the command passed to `docker run`.

It chooses the trusted workdir from `CTC_TRUSTED_WORKDIR`, or falls back to the container current working directory:

```text
CTC_TRUSTED_WORKDIR -> PWD -> /repo
```

It chooses the Claude config directory from `CLAUDE_CONFIG_DIR`, or falls back to:

```text
~/.claude
```

Then it updates:

- `~/.claude.json`
- `CLAUDE_CONFIG_DIR/settings.json` or `~/.claude/settings.json`

The update preserves unrelated existing fields where possible.

## Runtime Preseed

Docker preseed covers the container's initial working directory. A high-level `ctc stream --cwd PATH` request may target a different project directory.

For that reason, `ctc` also runs `preseed_claude_project_trust()` when it is about to start a new Claude Code process for a high-level session.

It does not run this again when an existing tmux session is already active. Existing sessions keep their original Claude process and environment.

## Managed Settings Preflight

`hasCompletedOnboarding` is not enough for managed settings.

Claude Code can still show a managed-settings security dialog when remote managed settings contain sensitive env, hook, or shell settings and the cached settings differ.

The Docker entrypoint handles this by running:

```bash
claude -p "Reply with exactly: ctc-docker-preflight-ok"
```

`claude -p` runs in non-interactive print mode, so it can create the managed-settings cache without stopping on the interactive approval dialog. Later, the tmux Claude Code process sees the same settings as already cached.

The preflight is skipped when:

```bash
CTC_SKIP_CLAUDE_PREFLIGHT=1
```

By default, it runs only when at least one auth env is present:

- `CLAUDE_CODE_OAUTH_TOKEN`
- `ANTHROPIC_API_KEY`

It can be forced without auth env by setting:

```bash
CTC_FORCE_CLAUDE_PREFLIGHT=1
```

## Example Files

The exact Claude Code config schema can change across Claude Code versions. The examples below show the shape this project currently writes.

Example `~/.claude.json` after preseed:

```json
{
  "hasCompletedOnboarding": true,
  "projects": {
    "/repo": {
      "allowedTools": [],
      "hasTrustDialogAccepted": true,
      "hasCompletedProjectOnboarding": true,
      "projectOnboardingSeenCount": 4
    }
  }
}
```

If the file already has unrelated fields, they are preserved:

```json
{
  "theme": "dark",
  "hasCompletedOnboarding": true,
  "projects": {
    "/repo": {
      "allowedTools": [
        "Bash"
      ],
      "custom": "keep",
      "hasTrustDialogAccepted": true,
      "hasCompletedProjectOnboarding": true,
      "projectOnboardingSeenCount": 4
    }
  }
}
```

Example `settings.json`:

```json
{
  "skipDangerousModePermissionPrompt": true
}
```

Managed-settings preflight may also create or refresh Claude Code's managed-settings cache, typically under the effective Claude config directory, for example:

```text
~/.claude/remote-settings.json
```

The contents of that file are owned by Claude Code and should not be hand-authored by this project.

## Operational Notes

- Do not write token values into Docker images, docs, logs, or committed env files.
- Mount the Claude config directory as a volume if containers are recreated and first-run state should persist.
- Treat `--cwd` and `CTC_TRUSTED_WORKDIR` as backend-controlled or allowlisted paths, because preseed marks those directories as trusted.
- Recheck this behavior after Claude Code upgrades. The config keys and first-run behavior are Claude Code implementation details, not a stable protocol.
