# Examples

[English](./README.md) | [한국어](./README.ko.md)

이 예제들은 다른 프로그램이 Claude Code transcript file을 직접 읽지 않고 `ctc`를 호출하는 방식을 보여줍니다.

## Shell Stream

`shell-stream.sh`는 prompt 하나를 보내고 turn이 끝날 때까지 JSONL event를 출력합니다.

```bash
SESSION_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')" \
  ./examples/shell-stream.sh "$PWD" "Explain this repository"
```

## Minimal Python Client

`web-client-minimal.py`는 backend-style 예제입니다. `ctc stream`을 실행하고 stdout을 line-by-line으로 읽은 뒤 event type별로 처리합니다.

```bash
python3 examples/web-client-minimal.py \
  --cwd "$PWD" \
  --session-id "$(python3 -c 'import uuid; print(uuid.uuid4())')" \
  "Explain this repository"
```

이 예제는 의도적으로 `ctc` stdout만 integration contract로 취급합니다.

## Docker Compose

`docker-compose.yml`은 persistent Claude config와 bridge state가 필요한 service의 volume 구성을 보여줍니다.

```bash
CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  docker compose -f examples/docker-compose.yml run --rm ctc \
  ctc stream --cwd /repo "Summarize this repository"
```
