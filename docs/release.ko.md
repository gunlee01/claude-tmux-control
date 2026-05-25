# Release Guide

[English](./release.md) | [한국어](./release.ko.md)

이 프로젝트는 현재 GitHub URL만으로도 설치할 수 있습니다. PyPI와 Docker registry 배포는 API contract가 더 안정된 뒤 추가하는 것이 좋습니다.

## Current Install Path

GitHub에서 `pipx`로 설치합니다.

```bash
pipx install git+https://github.com/gunlee01/claude-tmux-control.git
ctc --help
```

tag 기준 설치도 가능합니다.

```bash
pipx install git+https://github.com/gunlee01/claude-tmux-control.git@v0.2.0
```

## Version Policy

package version의 source of truth는 `pyproject.toml`입니다. Release tag는
앞에 `v`를 붙여서 맞춥니다. 예를 들어 `version = "0.2.0"`이면 tag는
`v0.2.0`입니다.

이 프로젝트는 아직 pre-1.0입니다. command surface, JSONL event contract,
state schema를 안정 contract로 선언하기 전까지 `1.0.0`으로 올리지 않습니다.

`0.x` 단계에서는 다음 기준을 씁니다.

- Patch: 호환되는 bug fix, docs correction, test-only change, packaging fix.
- Minor: 새 command, 새 flag, 새 output field, state schema 변경, client가 관찰할 수 있는 behavior change.

Breaking change도 안정화 전에는 `0.x` minor line 안에서 처리합니다. 예를 들어
`0.2.0 -> 0.3.0`을 사용하고, 안정화 선언 없이 `1.0.0`으로 올리지 않습니다.

## Release Checklist

tag를 만들기 전에 확인합니다.

```bash
python claude_tmux_control.py --version
python scripts/check_docs.py
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests
python -m py_compile claude_tmux_control.py scripts/stream_question.py scripts/web_chat_client.py scripts/check_docs.py
python -m pip wheel . -w /tmp/ctc-wheel-test --no-deps
docker build -t claude-tmux-control -f docker/Dockerfile .
docker run --rm -e CTC_SKIP_CLAUDE_PREFLIGHT=1 claude-tmux-control ctc --help
```

그 다음 tag를 push합니다.

```bash
git tag v0.2.0
git push origin v0.2.0
```

GitHub Release에는 다음 내용을 포함합니다.

- 테스트한 Claude Code version 호환성
- transcript parsing 관련 known limitation
- Docker image build note
- stream event contract 변경 시 migration note

## PyPI Readiness

아래가 안정화된 뒤 PyPI 배포를 권장합니다.

- command name과 exit code
- JSONL event contract
- state directory schema
- pricing table update process
- Claude Code 변경 대응 support policy

준비되면 사용자는 이렇게 설치할 수 있습니다.

```bash
pipx install claude-tmux-control
```

## Docker Registry Readiness

registry에 image를 올리려면 먼저 정해야 합니다.

- image name과 tag policy
- 지원 CPU architecture
- Claude Code CLI version pinning 전략
- Claude Code update마다 image를 rebuild할지 여부
- security update 공지 방식

token, user config, transcript, local state가 들어간 image는 publish하지 마세요.
