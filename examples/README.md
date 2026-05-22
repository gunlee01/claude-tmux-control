# Examples

[English](./README.md) | [한국어](./README.ko.md)

These examples show how another program can call `ctc` without reading Claude Code transcript files directly.

## Shell Stream

`shell-stream.sh` sends one prompt and prints JSONL events until the turn finishes.

```bash
SESSION_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')" \
  ./examples/shell-stream.sh "$PWD" "Explain this repository"
```

## Minimal Python Client

`web-client-minimal.py` is a backend-style example. It starts `ctc stream`, reads stdout line by line, and routes events by type.

```bash
python3 examples/web-client-minimal.py \
  --cwd "$PWD" \
  --session-id "$(python3 -c 'import uuid; print(uuid.uuid4())')" \
  "Explain this repository"
```

The example intentionally treats `ctc` stdout as the only integration contract.

## Docker Compose

`docker-compose.yml` shows the volume layout for a service that needs persistent Claude config and bridge state.

```bash
CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  docker compose -f examples/docker-compose.yml run --rm ctc \
  ctc stream --cwd /repo "Summarize this repository"
```
