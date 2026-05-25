# Claude Code First-run Preseed

[English](./claude-code-first-run-preseed.md) | [한국어](./claude-code-first-run-preseed.ko.md)

이 문서는 `claude-tmux-control`이 Claude Code interactive tmux session을 시작하기 전에 왜 Claude Code 설정을 미리 만들어두는지 정리합니다.

## 목적

`ctc stream`은 backend process가 tmux 안에서 Claude Code를 실행하고, client로 JSONL event를 streaming하는 service flow를 목표로 합니다.

이 흐름에서는 사람이 tmux pane 안의 first-run prompt를 보고 직접 답해줄 수 없습니다. 첫 interactive 실행이 onboarding, project trust, dangerous-mode warning, managed settings approval에서 멈추면 client는 정상적인 assistant answer를 받지 못하고 stream이 멈춘 것처럼 보입니다.

preseed 로직의 목적은 fresh container나 fresh Claude config directory에서도 Claude Code가 바로 service-ready 상태로 시작되게 만드는 것입니다.

## 막는 현상

preseed가 없으면 첫 실행 때 다음 prompt에서 멈출 수 있습니다.

- global Claude Code onboarding
- working directory project trust 확인
- project onboarding 화면
- `--dangerously-skip-permissions` warning
- managed settings security approval

이 prompt들은 사람이 직접 쓰는 terminal session에서는 정상 동작입니다.

하지만 backend flow에서는 아래처럼 문제가 됩니다.

```text
client/backend
  -> ctc stream
    -> tmux session
      -> Claude Code waits for first-run confirmation
  <- final assistant answer 없음
```

## 효과

preseed가 적용되면 `ctc`는 tmux 안에서 Claude Code를 시작하고 바로 요청 prompt를 처리할 수 있습니다.

기대 흐름은 이렇습니다.

```text
container start
  -> Docker entrypoint가 Claude config 준비
  -> 필요한 경우 claude -p preflight로 managed-settings cache 생성
ctc stream
  -> 새 Claude process 시작 전 요청된 --cwd를 preseed
  -> tmux 안에서 Claude Code 시작
  -> assistant_text, done, metrics event streaming
```

주의할 점은, 이 로직이 인증을 만들어주지는 않는다는 것입니다.

실제 실행에는 여전히 유효한 auth source가 필요합니다.

- `CLAUDE_CODE_OAUTH_TOKEN`
- `ANTHROPIC_API_KEY`

## 로직 위치

| 영역 | 위치 | 역할 |
| --- | --- | --- |
| Docker image wiring | `docker/Dockerfile` | `ctc-docker-entrypoint`를 image entrypoint로 등록 |
| Docker first-run preseed | `docker/entrypoint.sh` | `ctc` 실행 전 Claude config 작성 |
| Docker managed-settings preflight | `docker/entrypoint.sh` | auth env가 있으면 `claude -p`를 한 번 실행 |
| Runtime project trust preseed | `claude_tmux_control.py` `preseed_claude_project_trust()` | 새 high-level Claude process 시작 전 `--cwd`를 trust 처리 |
| Runtime call site | `claude_tmux_control.py` `prepare_high_level_stream()` | `controller.start_session(...)` 직전에 preseed 호출 |

## Docker EntryPoint Preseed

Docker entrypoint는 `docker run`으로 전달된 command가 실행되기 전에 먼저 실행됩니다.

trusted workdir은 다음 순서로 정합니다.

```text
CTC_TRUSTED_WORKDIR -> PWD -> /repo
```

Claude config directory는 다음 순서로 정합니다.

```text
CLAUDE_CONFIG_DIR -> ~/.claude
```

그 다음 아래 파일을 갱신합니다.

- `~/.claude.json`
- `CLAUDE_CONFIG_DIR/settings.json` 또는 `~/.claude/settings.json`

가능한 한 기존 unrelated field는 보존합니다.

## Runtime Preseed

Docker preseed는 container의 초기 working directory를 기준으로 합니다.

그런데 high-level `ctc stream --cwd PATH` 요청은 다른 project directory를 대상으로 할 수 있습니다.

그래서 `ctc` 자체도 새 high-level Claude Code process를 시작하기 직전에 `preseed_claude_project_trust()`를 실행합니다.

이미 tmux session이 살아 있으면 다시 실행하지 않습니다. 기존 session은 시작 당시 Claude process와 environment를 그대로 유지합니다.

## Managed Settings Preflight

`hasCompletedOnboarding`만으로는 managed settings approval이 사라지지 않을 수 있습니다.

Claude Code는 remote managed settings에 민감한 env, hook, shell setting이 있고 cached settings와 다르면 interactive mode에서 security dialog를 띄울 수 있습니다.

Docker entrypoint는 이를 피하기 위해 다음 명령을 먼저 실행합니다.

```bash
claude -p "Reply with exactly: ctc-docker-preflight-ok"
```

`claude -p`는 non-interactive print mode라서 interactive approval dialog에서 멈추지 않고 managed-settings cache를 만들 수 있습니다.

이후 tmux 안에서 실행되는 Claude Code는 같은 settings를 이미 cached 상태로 봅니다.

preflight를 생략하려면 다음 env를 설정합니다.

```bash
CTC_SKIP_CLAUDE_PREFLIGHT=1
```

기본적으로 preflight는 아래 auth env 중 하나가 있을 때만 실행됩니다.

- `CLAUDE_CODE_OAUTH_TOKEN`
- `ANTHROPIC_API_KEY`

auth env가 없어도 강제로 실행하려면 다음 env를 설정합니다.

```bash
CTC_FORCE_CLAUDE_PREFLIGHT=1
```

## 예시 JSON

Claude Code config schema는 Claude Code version에 따라 바뀔 수 있습니다.

아래는 현재 이 project가 작성하는 형태의 예시입니다.

preseed 후 `~/.claude.json` 예시:

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

기존 unrelated field가 있으면 보존합니다.

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

`settings.json` 예시:

```json
{
  "skipDangerousModePermissionPrompt": true
}
```

managed-settings preflight는 Claude Code의 managed-settings cache도 만들거나 갱신할 수 있습니다.

보통 effective Claude config directory 아래에 생깁니다.

```text
~/.claude/remote-settings.json
```

이 파일 내용은 Claude Code가 소유합니다. 이 project가 직접 hand-authoring하지 않습니다.

## 운영 메모

- token 값은 Docker image, docs, logs, committed env file에 남기지 않습니다.
- container를 재생성해도 first-run state를 유지해야 하면 Claude config directory를 volume으로 mount합니다.
- `--cwd`와 `CTC_TRUSTED_WORKDIR`는 backend-controlled 또는 allowlist 검증된 path로 둡니다. preseed가 해당 directory를 trusted로 표시하기 때문입니다.
- Claude Code upgrade 이후에는 이 동작을 다시 확인해야 합니다. config key와 first-run behavior는 Claude Code implementation detail이지 안정 protocol이 아닙니다.
