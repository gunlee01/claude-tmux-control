# claude-tmux-control

`claude -p`처럼 one-shot으로 호출하지 않고, Claude Code를 `tmux` 안에 일반 interactive CLI로 띄운 뒤 외부 CLI에서 입력을 보내고 화면 출력을 읽는 도구입니다.

## Requirements

- Python 3.10+
- tmux
- Claude Code CLI (`claude`)가 `PATH`에 있어야 실제 Claude 세션을 실행할 수 있습니다.

실행 시 `tmux`가 없으면 먼저 설치하라는 안내와 함께 종료합니다. `start`, `launch`, `chat`처럼 Claude Code를 띄우는 명령은 `claude` 또는 `--command`에 지정한 executable도 미리 확인합니다.

## Installation

권장 설치 방식은 `pipx`입니다.

```bash
pipx install git+ssh://git@oss.navercorp.com/gunh-lee/claude-tmux-control.git
ctc --help
```

`pipx`가 없다면 Python venv 안에 설치할 수 있습니다.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install git+ssh://git@oss.navercorp.com/gunh-lee/claude-tmux-control.git
ctc --help
```

개발 중인 checkout에서는 editable install을 사용합니다.

```bash
pip install -e .
ctc --help
```

설치하면 두 entrypoint를 사용할 수 있습니다.

```bash
ctc --help
claude-tmux-control --help
```

소스 파일을 직접 실행하는 방식도 계속 지원합니다.

```bash
chmod +x ./claude_tmux_control.py
./claude_tmux_control.py --help
```

## Commands

자세한 CLI 매뉴얼은 [docs/cli-manual.md](./docs/cli-manual.md)에 있습니다.

Product direction and future work are tracked under [docs](./docs/README.md).

새 tmux 세션을 만들고 Claude Code 실행:

```bash
ctc start work --cwd "$PWD"
```

Claude Code 옵션을 그대로 넘기기:

```bash
ctc start work --cwd "$PWD" --model opus --add-dir ../shared
```

옵션 경계가 헷갈리면 `--` 뒤에 Claude Code 옵션을 둡니다.

```bash
ctc start work --cwd "$PWD" -- --model opus --add-dir ../shared
```

기본 실행 command에는 `--dangerously-skip-permissions`가 자동으로 붙습니다.

OAuth token을 받은 경우에는 호출 프로세스의 환경변수로 넘기면 새 tmux session 안의 Claude Code 프로세스에 `CLAUDE_CODE_OAUTH_TOKEN`으로 주입됩니다.

```bash
CLAUDE_CODE_OAUTH_TOKEN="$TOKEN" ctc start work --cwd "$PWD"
```

계정별 token을 다른 변수명으로 관리한다면 source env 이름만 지정할 수 있습니다.

```bash
ACCOUNT_A_TOKEN="$TOKEN" ctc start work --cwd "$PWD" --oauth-token-env ACCOUNT_A_TOKEN
```

이미 있는 tmux 세션 안에서 Claude Code 실행:

```bash
ctc launch work
```

Claude Code에 입력 보내기:

```bash
ctc send work "현재 디렉터리 구조를 요약해줘"
```

현재 화면 읽기:

```bash
ctc capture work
```

화면 변화 계속 보기:

```bash
ctc watch work
```

화면 변화분을 계속 출력하고 파일에도 append:

```bash
ctc follow work --append claude-screen.log
```

현재 화면 기준 상태 추정:

```bash
ctc status work
ctc wait-ready work --timeout 120
```

Claude Code transcript 이벤트 확인:

```bash
ctc events work
ctc events work --follow
ctc events --transcript ~/.claude/transcripts/ses_xxx.jsonl --json
ctc events --root ~/.claude/projects --tail 50
```

특정 세션의 마지막 응답 본문만 출력:

```bash
ctc answer work
ctc answer work --wait --timeout 120
ctc answer work --tail 3
```

특정 세션의 최신 turn을 thinking/tool/text 포함해서 출력:

```bash
ctc turn work
ctc turn work --follow
ctc turn work --tail 3
```

특정 세션의 최신 turn을 JSONL로 stream하고 완료 시 종료:

```bash
ctc stream work
ctc stream work --timeout 300 --idle 2
```

특정 세션 종료와 오래된 controlled session 정리:

```bash
ctc kill work
ctc reap --idle-seconds 1800 --prefix ctc- --dry-run
ctc reap --idle-seconds 1800 --prefix ctc-
```

질문 전송부터 stream 출력까지 한 번에 테스트:

```bash
./scripts/stream_question.py work "현재 디렉터리 구조를 요약해줘"
```

같은 프로그램 안에서 입력하고 답변 화면을 확인:

```bash
ctc chat work --cwd "$PWD"
```

`chat`에서는 `/quit` 또는 `/exit`로 종료합니다. Claude 답변이 멈춘 상태가 `--idle` 초 동안 유지되면 다음 입력 프롬프트로 돌아옵니다.

## Notes

- 입력은 `tmux load-buffer` + `paste-buffer`로 전달해서 긴 prompt와 공백을 안전하게 보냅니다.
- 출력은 `tmux capture-pane`으로 읽으므로 raw stdout이 아니라 터미널에 렌더링된 화면 기준입니다.
- `start`와 `chat`은 `--oauth-token-env`로 지정한 source env 값을 새 tmux session의 `CLAUDE_CODE_OAUTH_TOKEN`으로 전달합니다. 이미 떠 있는 shell에 command를 붙여 넣는 `launch`는 OAuth token 주입 대상이 아닙니다.
- `start`, `launch`, `chat`에서 우리 CLI가 모르는 옵션은 Claude Code 옵션으로 command 뒤에 그대로 전달합니다. 다른 명령에서는 모르는 옵션을 에러로 처리합니다.
- Claude Code 실행 command에는 기본적으로 `--dangerously-skip-permissions`가 붙습니다. `--command`에 `--permission-mode ...` 또는 `--dangerously-skip-permissions`가 이미 있으면 추가로 붙이지 않습니다.
- 기존 세션에 `launch`를 쓰면 현재 pane에 `claude` 명령을 그대로 입력합니다. 이미 Claude Code가 실행 중인 pane에는 `launch` 대신 `send`를 쓰세요.
- `status`와 `wait-ready`는 Claude Code의 공식 상태 API가 아니라 화면 문구 기반 휴리스틱입니다.
- `events <session>`은 해당 tmux session의 마지막 prompt와 cwd를 기준으로 transcript를 고릅니다. session을 생략하면 `~/.claude/transcripts/*.jsonl`와 `~/.claude/projects/**/*.jsonl` 중 최신 파일을 읽으므로 다른 Claude 세션이 섞일 수 있습니다. `tool_use`, `tool_result`, timestamp, usage/context 필드는 transcript에 존재할 때만 표시합니다.
- `answer <session>`은 해당 session transcript에서 최신 user turn 이후 마지막 assistant `text` block만 출력합니다. `thinking`과 `tool_use`는 제외합니다. `--tail N` 또는 `--count N`으로 최근 N개 답변을 볼 수 있습니다.
- `turn <session>`은 최신 user turn부터 현재까지의 `thinking`, `tool_use`, `tool_result`, assistant `text`를 순서대로 출력합니다. `--tail N` 또는 `--count N`으로 최근 N개 turn을 볼 수 있습니다.
- `stream <session>`은 최신 user turn의 `user`, `thinking`, `tool_use`, `tool_result`, `assistant_text`를 JSONL로 출력합니다. 최종 assistant text 이후 transcript와 tmux 화면이 ready 상태로 안정되면 `done` event를 출력하고 종료합니다.
- `reap`은 기본적으로 `ctc-` prefix session만 정리합니다. 마지막 입력 시간이 `--idle-seconds`보다 오래됐고 transcript가 `working` 상태가 아닌 session만 종료합니다.
- Claude Code 버전이나 설정에 따라 transcript schema, token/context 기록 여부, 화면 문구가 다를 수 있습니다.
