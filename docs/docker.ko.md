# Docker Guide

[English](./docker.md) | [한국어](./docker.ko.md)

이 문서는 `claude-tmux-control`을 Docker 이미지로 실행하는 방법과 Claude Code first-run prompt를 자동으로 처리하는 방식을 설명합니다.

## 목적

Docker 이미지는 다음 구성요소를 포함합니다.

- Python 3
- `tmux`
- Claude Code CLI (`claude`)
- `ctc`, `claude-tmux-control` entrypoint
- Claude Code first-run preseed entrypoint

웹 서버나 다른 프로그램은 이 이미지를 기반으로 `ctc stream`을 호출해 Claude Code interactive session을 유지할 수 있습니다.

## Build

repository root에서 build합니다.

```bash
docker build -t claude-tmux-control -f docker/Dockerfile .
```

이미지 안의 기본 사용자는 `ctc`입니다.

Claude Code의 `--dangerously-skip-permissions`는 root/sudo 환경에서 거부될 수 있으므로, root가 아닌 사용자로 실행해야 합니다.

module extraction refactor를 merge하기 전에는 no-auth Docker contract smoke를 실행합니다.

```bash
scripts/docker_refactor_contract_check.sh
```

이 검증은 Claude auth 없이 image build, console script, installed import, pricing data, entrypoint preseed file, local refactor contract gate를 확인합니다. 실제 Claude stream 동작은 auth env와 `CTC_DOCKER_LIVE_SMOKE=1`이 있을 때만 검증합니다.

## Quick Run

OAuth token은 이미지에 굽지 말고 runtime environment로 전달합니다.

```bash
docker run --rm -it \
  -e CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  -v "$PWD":/repo \
  -w /repo \
  claude-tmux-control \
  ctc stream --cwd /repo "현재 프로젝트를 요약해줘"
```

같은 대화를 이어가려면 `--session-id`를 고정합니다.

```bash
SESSION_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"

docker run --rm -it \
  -e CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  -v "$PWD":/repo \
  -w /repo \
  claude-tmux-control \
  ctc stream --cwd /repo --session-id "$SESSION_ID" "첫 질문"
```

다음 turn도 같은 `SESSION_ID`로 호출합니다.

```bash
docker run --rm -it \
  -e CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  -v "$PWD":/repo \
  -w /repo \
  claude-tmux-control \
  ctc stream --cwd /repo --session-id "$SESSION_ID" "이어서 설명해줘"
```

## First-run Preseed

Claude Code를 fresh container에서 처음 interactive 실행하면 여러 prompt가 뜰 수 있습니다.

`docker/entrypoint.sh`는 `ctc` 실행 전에 아래 값을 미리 설정합니다.

이 동작의 목적, 적용 효과, 없을 때 현상, 생성되는 config 예시는 [Claude Code First-run Preseed](./claude-code-first-run-preseed.ko.md)에 따로 정리되어 있습니다.

| 대상 | 파일 | 처리 |
| --- | --- | --- |
| global onboarding | `~/.claude.json` | `hasCompletedOnboarding: true` |
| workspace trust | `~/.claude.json` | 현재 workdir을 trusted project로 등록 |
| bypass permissions warning | `~/.claude/settings.json` 또는 `CLAUDE_CONFIG_DIR/settings.json` | `skipDangerousModePermissionPrompt: true` |
| managed settings approval | `~/.claude/remote-settings.json` | `claude -p` preflight로 cache 생성 |

기본 trusted workdir은 container의 현재 working directory입니다.

필요하면 `CTC_TRUSTED_WORKDIR`로 바꿀 수 있습니다.

```bash
docker run --rm -it \
  -e CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  -e CTC_TRUSTED_WORKDIR=/workspace \
  -v "$PWD":/workspace \
  -w /workspace \
  claude-tmux-control \
  ctc stream --cwd /workspace "요약해줘"
```

## Managed Settings Preflight

managed settings approval은 단순히 `hasCompletedOnboarding`만으로는 없어지지 않습니다.

Claude Code는 remote managed settings에 위험할 수 있는 env/hook/shell setting이 있고, 기존 cache와 달라졌고, interactive mode일 때 보안 승인 dialog를 띄웁니다.

Docker entrypoint는 interactive `ctc stream` 전에 다음 non-interactive 명령을 한 번 실행합니다.

```bash
claude -p "Reply with exactly: ctc-docker-preflight-ok"
```

`claude -p`는 non-interactive print mode라서 managed settings approval dialog를 띄우지 않습니다.

이 preflight가 성공하면 Claude Code가 `remote-settings.json` cache를 만들고, 이후 tmux 안에서 실행되는 interactive Claude Code는 같은 settings를 이미 cached 상태로 봅니다.

그래서 `ctc stream`이 managed settings prompt에서 멈추지 않고 바로 transcript streaming으로 진행됩니다.

## Environment Variables

| 변수 | 의미 |
| --- | --- |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code OAuth token. 권장 runtime auth 방식 |
| `ANTHROPIC_API_KEY` | API key 방식 auth를 쓰는 경우 |
| `CLAUDE_CONFIG_DIR` | Claude Code config directory override |
| `CTC_TRUSTED_WORKDIR` | preseed할 trusted project path. 기본값은 current working directory |
| `CTC_SKIP_CLAUDE_PREFLIGHT=1` | `claude -p` preflight 생략 |
| `CTC_FORCE_CLAUDE_PREFLIGHT=1` | token env가 없어도 preflight 강제 실행 |

token 값은 image, Dockerfile, README, log에 남기지 않습니다.

## Persistent State

container를 매번 새로 만들면 Claude Code config와 `ctc` state도 같이 사라집니다.

운영에서 같은 session을 container 재시작 후에도 이어가려면 config/state volume을 분리해서 mount합니다.

예:

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
  ctc stream --cwd /repo --session-id "$SESSION_ID" "계속 진행해줘"
```

## Cleanup

Docker container 안에서도 tmux session은 자동으로 정리되지 않습니다.

장시간 운영 서버에서는 `reap`을 주기적으로 실행합니다.

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

`reap`은 tmux session을 종료합니다.

같은 `SESSION_ID`로 다음 `ctc stream`을 호출하면 state/transcript가 남아 있는 경우 새 tmux session을 만들고 Claude Code를 `--resume <SESSION_ID>`로 실행합니다.

## Limitations

이 Docker setup은 Claude Code의 현재 first-run, settings, managed-settings 동작에 의존합니다.

Claude Code 구현이 바뀌면 preseed key나 `claude -p` preflight 방식이 조정될 수 있습니다.

특히 아래 동작은 Claude Code 구현 의존성이 있습니다.

- `~/.claude.json`의 onboarding/trust project key
- `settings.json`의 `skipDangerousModePermissionPrompt`
- `claude -p`가 managed settings approval dialog를 skip하고 cache를 갱신하는 동작

Claude Code를 update한 뒤 Docker stream이 first-run prompt에서 멈추면 이 문서와 `docker/entrypoint.sh`를 함께 확인해야 합니다.
