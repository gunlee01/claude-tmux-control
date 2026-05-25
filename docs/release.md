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
pipx install git+https://github.com/gunlee01/claude-tmux-control.git@v0.2.0
```

## Version Policy

`pyproject.toml` is the source of truth for the package version. Release tags
must match it with a leading `v`, for example `version = "0.2.0"` and tag
`v0.2.0`.

The project is still pre-1.0. Do not bump to `1.0.0` until the command surface,
JSONL event contract, and state schema are intentionally declared stable.

While in `0.x`, use:

- Patch: compatible fixes, docs corrections, test-only changes, and packaging fixes.
- Minor: new commands, new flags, new output fields, state schema changes, or behavior changes that clients may notice.

Breaking changes also stay in the `0.x` minor line until stabilization. For
example, prefer `0.2.0 -> 0.3.0`, not `1.0.0`.

## Release Checklist

Before tagging:

```bash
python claude_tmux_control.py --version
python scripts/check_docs.py
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests
python -m py_compile claude_tmux_control.py scripts/stream_question.py scripts/web_chat_client.py scripts/check_docs.py
python -m pip wheel . -w /tmp/ctc-wheel-test --no-deps
docker build -t claude-tmux-control -f docker/Dockerfile .
docker run --rm -e CTC_SKIP_CLAUDE_PREFLIGHT=1 claude-tmux-control ctc --help
```

Then:

```bash
git tag v0.2.0
git push origin v0.2.0
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
