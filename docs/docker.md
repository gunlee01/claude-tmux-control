# Docker Guide

[English](./docker.md) | [한국어](./docker.ko.md)

This guide explains how to build and run `claude-tmux-control` as a Docker image and how the image bypasses Claude Code first-run prompts.

## Purpose

The image includes:

- Python 3
- `tmux`
- Claude Code CLI (`claude`)
- `ctc` and `claude-tmux-control` console commands
- an entrypoint that pre-seeds Claude Code first-run state

A web server or another backend process can use this image to call `ctc stream` while keeping an interactive Claude Code session alive.

## Build

Build from the repository root.

```bash
docker build -t claude-tmux-control -f docker/Dockerfile .
```

The image runs as the non-root `ctc` user. Claude Code may reject `--dangerously-skip-permissions` when run as root or through sudo.

For module-extraction refactors, run the no-auth Docker contract smoke before merging:

```bash
scripts/docker_refactor_contract_check.sh
```

This verifies image build, console scripts, installed imports, pricing data, entrypoint preseed files, and the local refactor contract gate without requiring Claude auth. It does not prove live Claude stream behavior unless `CTC_DOCKER_LIVE_SMOKE=1` is set with auth env.

## Quick Run

Pass auth at runtime. Do not bake tokens into the image.

```bash
docker run --rm -it \
  -e CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  -v "$PWD":/repo \
  -w /repo \
  claude-tmux-control \
  ctc stream --cwd /repo "Summarize this project"
```

Use a fixed `--session-id` to continue the same conversation.

```bash
SESSION_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"

docker run --rm -it \
  -e CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  -v "$PWD":/repo \
  -w /repo \
  claude-tmux-control \
  ctc stream --cwd /repo --session-id "$SESSION_ID" "First question"
```

Run the next turn with the same `SESSION_ID`.

## First-run Preseed

Fresh Claude Code containers can show first-run prompts. `docker/entrypoint.sh` prepares these files before running `ctc`.

For a fuller explanation of why this exists, what happens without it, and the generated config shape, see [Claude Code First-run Preseed](./claude-code-first-run-preseed.md).

| Target | File | Action |
| --- | --- | --- |
| global onboarding | `~/.claude.json` | set `hasCompletedOnboarding: true` |
| workspace trust | `~/.claude.json` | register the current workdir as trusted |
| bypass permissions warning | `~/.claude/settings.json` or `CLAUDE_CONFIG_DIR/settings.json` | set `skipDangerousModePermissionPrompt: true` |
| managed settings approval | `~/.claude/remote-settings.json` | create cache through `claude -p` preflight |

The trusted workdir defaults to the container working directory. Override it with `CTC_TRUSTED_WORKDIR`.

```bash
docker run --rm -it \
  -e CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  -e CTC_TRUSTED_WORKDIR=/workspace \
  -v "$PWD":/workspace \
  -w /workspace \
  claude-tmux-control \
  ctc stream --cwd /workspace "Summarize"
```

## Managed Settings Preflight

`hasCompletedOnboarding` alone does not skip managed settings approval.

Claude Code shows the managed settings security dialog when remote managed settings contain potentially dangerous env, hook, or shell settings, the cached settings differ, and the process is interactive.

The Docker entrypoint runs this non-interactive preflight before interactive `ctc stream`:

```bash
claude -p "Reply with exactly: ctc-docker-preflight-ok"
```

`claude -p` runs in print mode, so Claude Code skips the managed settings approval dialog and writes `remote-settings.json`. Later, the tmux interactive Claude Code process sees the same settings as already cached and starts without stopping at the prompt.

## Environment Variables

| Variable | Meaning |
| --- | --- |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code OAuth token. Recommended runtime auth path |
| `ANTHROPIC_API_KEY` | API key auth path |
| `CLAUDE_CONFIG_DIR` | override Claude Code config directory |
| `CTC_TRUSTED_WORKDIR` | trusted project path to preseed. Defaults to current working directory |
| `CTC_SKIP_CLAUDE_PREFLIGHT=1` | skip `claude -p` preflight |
| `CTC_FORCE_CLAUDE_PREFLIGHT=1` | force preflight even without auth env |

Never write token values into the image, Dockerfile, README, docs, or logs.

## Persistent State

If containers are recreated for every request, Claude Code config and `ctc` state disappear. Mount volumes when sessions must survive container restarts.

```bash
docker volume create ctc-claude-home
docker volume create ctc-state

docker run --rm -it \
  -e CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  -e CLAUDE_CONFIG_DIR=/home/ctc/.claude \
  -v ctc-claude-home:/home/ctc/.claude \
  -v ctc-state:/home/ctc/.cache/claude-tmux-control \
  -v "$PWD":/repo \
  -w /repo \
  claude-tmux-control \
  ctc stream --cwd /repo --session-id "$SESSION_ID" "Continue"
```

## Cleanup

Even inside Docker, tmux sessions are not automatically removed. Run `reap` periodically.

```bash
docker run --rm \
  -e CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  -v ctc-claude-home:/home/ctc/.claude \
  -v ctc-state:/home/ctc/.cache/claude-tmux-control \
  -v "$PWD":/repo \
  -w /repo \
  claude-tmux-control \
  ctc reap --idle-seconds 1800 --prefix ctc-csess-
```

`reap` kills tmux sessions. If state/transcript data remains, the next `ctc stream` with the same `SESSION_ID` can start a new tmux session and resume Claude Code.

## Limitations

This setup depends on current Claude Code first-run, settings, and managed-settings behavior. Claude Code changes can require updates to `docker/entrypoint.sh` and this guide.

Implementation-dependent parts include:

- onboarding and project trust keys in `~/.claude.json`
- `skipDangerousModePermissionPrompt` in `settings.json`
- `claude -p` skipping managed settings approval and refreshing the remote settings cache
