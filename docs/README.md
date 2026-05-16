# claude-tmux-control docs

이 디렉터리는 Claude Code tmux bridge CLI를 외부 프로그램/웹에서 쓰기 위한 제품 요구사항과 진행 체크리스트를 관리합니다.

## Documents

- [CLI Manual](./cli-manual.md): 설치, 실행, 인증, 명령별 옵션, 외부 연동 예시
- [Web Chat Integration Guide](./web-chat-integration-guide.md): 웹/외부 프로그램에서 다중 Claude Code session을 채팅 UI로 붙이는 방법
- [Interactive Web Chat Guide](./web-chat-integration-guide.html): 같은 내용을 브라우저에서 탐색하는 인터랙티브 HTML 문서
- [Local Storage Plan](./local-storage-plan.md): transcript cursor, session state, lock, offset 기반 streaming 계획
- [Test Scenarios](./test-scenarios.md): client-style 반복 테스트와 실제 Claude Code smoke 시나리오
- [PRD](./PRD.md): 전체 제품 요구사항과 설계 방향
- [Implementation Checklist](./implementation-checklist.md): 앞으로 진행할 작업 체크리스트

## Current Direction

CLI의 장기 목표는 다른 프로그램이 Claude Code interactive session을 다음 흐름으로 제어할 수 있게 하는 것입니다.

```text
client/web
  -> claude-tmux-control CLI
    -> tmux session
      -> Claude Code
    <- Claude transcript JSONL
  <- status / answer / turn / events
```

핵심 원칙:

- 입력 전달은 `tmux`를 사용합니다.
- 응답, tool call, completion status는 Claude Code transcript JSONL을 주 원본으로 사용합니다.
- `tmux capture-pane`은 화면 디버깅과 confirmation fallback 용도로만 둡니다.
- 외부 session id는 tmux session name에 포함해서 매핑 저장소 의존성을 줄입니다.
