# Security Guide

[English](./security.md) | [한국어](./security.ko.md)

`claude-tmux-control`은 Claude Code를 interactive terminal 안에서 실행하고, 넓은 권한으로 Claude Code를 띄울 수 있습니다. 그래서 권한 있는 자동화 컴포넌트로 취급해야 합니다.

## Token Handling

- `CLAUDE_CODE_OAUTH_TOKEN` 같은 environment variable 사용을 권장합니다.
- token 값을 command argument로 직접 넘기지 마세요.
- token, `.env`, transcript, tmux capture, secret이 들어간 Docker image layer를 commit하지 마세요.
- log, error, example, test, screenshot에 token 값을 출력하지 마세요.
- 여러 계정을 분리해야 하면 `CLAUDE_CONFIG_DIR`를 계정별로 분리합니다.

## Claude Environment Files

새 tmux session을 만들 때 `ctc`는 `<cwd>/.ctc.env`, 명시적 `--env-file PATH`, 현재 process env의 `--env NAME` whitelist를 Claude Code process에 주입할 수 있습니다.

이 값들은 secret으로 취급하세요.

- `.ctc.env`를 commit하지 마세요.
- 값이 shell history에 남지 않도록 `KEY=VALUE` command argument보다 `--env NAME`을 선호하세요.
- env 주입은 새 session 생성 시점에만 적용됩니다. 이미 실행 중인 Claude Code session은 시작 당시 env를 유지합니다.
- `CLAUDE_CODE_OAUTH_TOKEN`은 `--oauth-token-env` 전용입니다. `.ctc.env`와 `--env`에서는 거절됩니다.

## Permission Mode

새 Claude Code session은 기본적으로 `--dangerously-skip-permissions`로 실행됩니다.

동적 승인 UI를 처리할 수 없는 service flow에는 필요하지만, Claude Code가 action별 확인 없이 tool을 실행할 수 있다는 뜻입니다.

bridge는 항상 고정된 `claude` 실행 파일을 사용합니다.

`--claude-args`는 permission mode를 포함해 Claude Code 동작을 바꿀 수 있으므로 권한 있는 운영자 설정으로 취급하세요. 신뢰할 수 없는 client 입력을 그대로 넣지 마세요.

기본 dangerous mode를 쓰지 않으려면 신뢰된 launch argument로 Claude Code permission option을 전달합니다.

```bash
ctc stream --cwd "$PWD" --claude-args "--permission-mode plan" "hello"
```

`--claude-args`에 `--permission-mode ...` 또는 `--dangerously-skip-permissions`가 이미 있으면 bridge는 `--dangerously-skip-permissions`를 추가로 붙이지 않습니다.

새 high-level Claude Code process를 실행하기 전에 bridge는 요청된 `--cwd`를 `~/.claude.json`의 trusted project로 preseed하고, 적용될 Claude config directory의 `settings.json`에 `skipDangerousModePermissionPrompt`를 설정합니다. service flow의 interactive trust prompt를 피하기 위한 동작이지만, `--cwd`를 제어하는 caller가 해당 directory를 trusted로 표시할 수 있다는 뜻입니다. `--cwd`는 backend-controlled path로 두거나 allowlist로 검증하세요.

다음 기준으로 범위를 제한하세요.

- 제한된 project directory 안에서 실행합니다.
- dedicated user 또는 container user로 실행합니다.
- service에 필요한 repository와 state directory만 mount합니다.
- host-level credential을 runtime environment에 넣지 않습니다.
- production token과 local development token을 분리합니다.

## Docker

auth token을 Docker image에 bake하지 마세요.

runtime environment로 전달합니다.

```bash
docker run --rm \
  -e CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  claude-tmux-control \
  ctc --help
```

persistent volume을 사용할 때는 Claude Code config, bridge state, transcript에 민감한 prompt나 project 정보가 남을 수 있음을 고려해야 합니다.

## State And Transcript Data

bridge는 기본적으로 local state를 `~/.cache/claude-tmux-control/` 아래에 저장합니다. Claude Code transcript는 Claude Code config directory 아래에 저장됩니다.

이 파일에는 다음 정보가 포함될 수 있습니다.

- prompt
- assistant answer
- tool input/output summary
- file path
- token usage와 model metadata

이 directory들은 secret에 준해서 보호하세요. 검토 없이 public issue나 support ticket에 올리지 마세요.

## Reporting

현재 이 repository에는 private vulnerability intake 절차가 정의되어 있지 않습니다. public distribution을 본격화하기 전에는 `SECURITY.md` 또는 private contact channel을 추가하는 것이 좋습니다.
