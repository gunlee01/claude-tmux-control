# claude-tmux-control docs

[English](./README.md) | [한국어](./README.ko.md)

이 디렉터리는 Claude Code tmux bridge CLI를 외부 프로그램/웹에서 쓰기 위한 제품 요구사항과 진행 체크리스트를 관리합니다.

## Documents

- [CLI Manual](./cli-manual.ko.md): 설치, 실행, 인증, 명령별 옵션, 외부 연동 예시
- [Web Client Quickstart](./quickstart-web-client.ko.md): 웹/외부 클라이언트가 따라 할 표준 호출 순서
- [Web Chat Integration Guide](./web-chat-integration-guide.ko.md): 웹/외부 프로그램에서 다중 Claude Code session을 채팅 UI로 붙이는 방법
- [Interactive Web Chat Guide](./web-chat-integration-guide.html): 같은 내용을 브라우저에서 탐색하는 인터랙티브 HTML 문서
- [Web Chat App Flow](./web-chat-app-flow.html): 브라우저 채팅앱이 CLI stream을 호출하고 event를 UI에 반영하는 흐름도 HTML
- [Operations Guide](./operations.ko.md): idle cleanup, reap, kill, 장애 복구 운영 절차
- [Docker Guide](./docker.ko.md): Docker 이미지 build, first-run preseed, managed settings preflight
- [Claude Code First-run Preseed](./claude-code-first-run-preseed.ko.md): first-run prompt 회피 목적, 효과, 생성 config, 코드 위치
- [Security Guide](./security.ko.md): token handling, permission mode, transcript/state data, Docker safety
- [Release Guide](./release.ko.md): GitHub, PyPI, Docker registry release checklist
- [Claude Pricing Table](../claude_pricing.json): metrics cost 계산에 쓰는 Claude model별 USD/MTok 단가
- [Local Storage Plan](./local-storage-plan.ko.md): transcript cursor, session state, lock, offset 기반 streaming 계획
- [Test Scenarios](./test-scenarios.ko.md): client-style 반복 테스트와 실제 Claude Code smoke 시나리오
- [PRD](./PRD.ko.md): 전체 제품 요구사항과 설계 방향
- [Implementation Checklist](./implementation-checklist.ko.md): 앞으로 진행할 작업 체크리스트

## Current Direction

CLI의 장기 목표는 다른 프로그램이 Claude Code interactive session을 다음 흐름으로 제어할 수 있게 하는 것입니다.

```text
client/web
  -> claude-tmux-control CLI
    -> tmux session
      -> Claude Code
    <- Claude transcript JSONL
  <- stream/replay JSONL events / done / metrics
```

핵심 원칙:

- 입력 전달은 `tmux`를 사용합니다.
- 웹/외부 client는 `ctc stream --cwd ... [--session-id ...] PROMPT`의 JSONL stdout을 주 계약으로 사용합니다.
- 진행 중 응답 취소는 `ctc cancel SESSION_ID`로 `Escape`를 보내고, 이후 `last` 또는 `stream --attach`로 완료까지 수신합니다. tool 실행 취소는 final answer 없이 `done`/`metrics`로 닫힐 수 있습니다.
- 완료된 최근 turn 복구는 `ctc last SESSION_ID --last N` 또는 `ctc replay SESSION_ID --last N`의 JSONL stdout을 사용합니다.
- 응답, tool call, completion status, metrics는 Claude Code transcript JSONL을 주 원본으로 사용합니다.
- `tmux capture-pane`은 화면 디버깅과 confirmation fallback 용도로만 둡니다.
- 외부 session id는 tmux session name에 포함해서 매핑 저장소 의존성을 줄입니다.
- `answer`, `turn`, `events`는 사람이 tmux session을 직접 확인하는 low-level debug 명령으로 둡니다.
