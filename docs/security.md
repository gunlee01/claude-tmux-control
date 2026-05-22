# Security Guide

[English](./security.md) | [한국어](./security.ko.md)

`claude-tmux-control` runs Claude Code in an interactive terminal and can launch it with broad permissions. Treat it as a privileged automation component.

## Token Handling

- Prefer environment variables such as `CLAUDE_CODE_OAUTH_TOKEN`.
- Do not pass token values directly in command arguments.
- Do not commit tokens, `.env` files, transcript files, tmux captures, or Docker image layers containing secrets.
- Do not print token values in logs, errors, examples, tests, or screenshots.
- Use separate `CLAUDE_CONFIG_DIR` values when isolating multiple accounts.

## Permission Mode

New Claude Code sessions are launched with `--dangerously-skip-permissions` by default.

This is required for non-interactive service flows where dynamic approvals cannot be handled, but it means Claude Code can run tools without per-action confirmation.

Use these controls around it:

- Run inside a restricted project directory.
- Use a dedicated user or container user.
- Mount only the repositories and state directories the service needs.
- Avoid host-level credentials in the runtime environment.
- Keep production tokens separate from local development tokens.

## Docker

Never bake auth tokens into a Docker image.

Pass auth at runtime:

```bash
docker run --rm \
  -e CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  claude-tmux-control \
  ctc --help
```

When using persistent volumes, remember that Claude Code config, bridge state, and transcripts can contain sensitive project or prompt data.

## State And Transcript Data

The bridge stores local state under `~/.cache/claude-tmux-control/` by default. Claude Code stores transcripts under its own config directory.

These files can contain:

- prompts
- assistant answers
- tool input/output summaries
- file paths
- token usage and model metadata

Protect these directories with normal secret-handling practices. Do not upload them into support tickets or public issues without review.

## Reporting

This repository does not currently define a private vulnerability intake process. For public distribution, add a `SECURITY.md` or private contact channel before inviting external security reports.
