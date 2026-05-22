# claude-tmux-control docs

[English](./README.md) | [한국어](./README.ko.md)

This directory contains product notes, integration guides, and operational references for using `claude-tmux-control` from external programs.

## Documents

- [CLI Manual](./cli-manual.md): command reference, options, authentication, and examples
- [Web Client Quickstart](./quickstart-web-client.md): standard call sequence for web and backend clients
- [Web Chat Integration Guide](./web-chat-integration-guide.md): multi-session chat integration contract
- [Interactive Web Chat Guide](./web-chat-integration-guide.html): browser-readable interactive guide
- [Web Chat App Flow](./web-chat-app-flow.html): HTML flow diagram for browser chat integration
- [Operations Guide](./operations.md): idle cleanup, `reap`, `kill`, and recovery procedures
- [Docker Guide](./docker.md): Docker image build, first-run preseed, and managed settings preflight
- [Claude Pricing Table](../claude_pricing.json): USD/MTok pricing table for metrics estimation
- [Local Storage Plan](./local-storage-plan.md): state, cursoring, locks, and efficient streaming plan
- [Test Scenarios](./test-scenarios.md): client-style tests and Claude Code smoke scenarios
- [PRD](./PRD.md): product requirements and design decisions
- [Implementation Checklist](./implementation-checklist.md): completed and future work

## Current Direction

The long-term goal is to let another program control Claude Code interactive sessions through this flow:

```text
client/web
  -> claude-tmux-control CLI
    -> tmux session
      -> Claude Code
    <- Claude transcript JSONL
  <- stream/replay JSONL events / done / metrics
```

Core principles:

- Send input through `tmux` because Claude Code is a terminal UI.
- Use `ctc stream --cwd ... [--session-id ...] PROMPT` stdout JSONL as the service-facing API.
- Use `ctc cancel SESSION_ID` to send Escape, then `last` or `stream --attach` to finish receiving `done`/`metrics`.
- Use `ctc last SESSION_ID --last N` or `ctc replay SESSION_ID --last N` to recover completed turn events.
- Treat Claude Code transcript JSONL as the primary source for answer text, tool calls, completion state, and metrics.
- Use `tmux capture-pane` only for screen readiness, debugging, and fallback checks.
- Keep low-level `answer`, `turn`, and `events` commands as manual tmux debugging tools.
