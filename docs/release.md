# Release Guide

[English](./release.md) | [한국어](./release.ko.md)

This project can be distributed directly from GitHub today. PyPI and Docker registry releases can be added later when the API contract stabilizes.

## Current Install Path

GitHub install with `pipx`:

```bash
pipx install git+https://github.com/gunlee01/claude-tmux-control.git
ctc --help
```

Install from a tag:

```bash
pipx install git+https://github.com/gunlee01/claude-tmux-control.git@v0.1.0
```

## Release Checklist

Before tagging:

```bash
python scripts/check_docs.py
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests
python -m py_compile claude_tmux_control.py scripts/stream_question.py scripts/web_chat_client.py scripts/check_docs.py
python -m pip wheel . -w /tmp/ctc-wheel-test --no-deps
docker build -t claude-tmux-control -f docker/Dockerfile .
docker run --rm -e CTC_SKIP_CLAUDE_PREFLIGHT=1 claude-tmux-control ctc --help
```

Then:

```bash
git tag v0.1.0
git push origin v0.1.0
```

Create a GitHub Release with:

- compatibility notes for Claude Code versions tested
- known limitations around transcript parsing
- Docker image build notes
- migration notes for stream event contract changes

## PyPI Readiness

Publish to PyPI only after these are stable:

- command names and exit codes
- JSONL event contract
- state directory schema
- pricing table update process
- documented support policy for Claude Code changes

When ready, the target user command becomes:

```bash
pipx install claude-tmux-control
```

## Docker Registry Readiness

For registry publishing, decide:

- image name and tag policy
- supported CPU architectures
- Claude Code CLI version pinning strategy
- whether images are rebuilt on every Claude Code update
- how security updates are communicated

Do not publish images that contain tokens, user config, transcripts, or local state.
