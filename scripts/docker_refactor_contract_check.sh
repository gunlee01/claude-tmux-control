#!/usr/bin/env bash
set -euo pipefail

IMAGE="${CTC_DOCKER_IMAGE:-claude-tmux-control-refactor-contract}"

docker build -t "$IMAGE" -f docker/Dockerfile .

docker run --rm \
  -e CTC_SKIP_CLAUDE_PREFLIGHT=1 \
  "$IMAGE" \
  bash -lc '
    set -euo pipefail
    cd /opt/claude-tmux-control
    test "$(id -un)" = "ctc"
    command -v tmux
    command -v claude
    command -v ctc
    command -v claude-tmux-control
    ctc --version
    claude-tmux-control --version
    ctc --help >/tmp/ctc-help.txt
    grep -q "High-level web/client commands" /tmp/ctc-help.txt
    test -f "$HOME/.claude.json"
    test -f "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
    /opt/ctc-venv/bin/python scripts/check_package_install.py
    PYTHONPYCACHEPREFIX=/tmp/ctc-pycache python3 scripts/refactor_contract_check.py --phase all
  '

if [[ "${CTC_DOCKER_LIVE_SMOKE:-}" == "1" ]]; then
  docker_env=()
  if [[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
    docker_env+=(-e CLAUDE_CODE_OAUTH_TOKEN)
  fi
  if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
    docker_env+=(-e ANTHROPIC_API_KEY)
  fi
  if [[ "${#docker_env[@]}" -eq 0 ]]; then
    echo "CTC_DOCKER_LIVE_SMOKE=1 requires CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY" >&2
    exit 2
  fi

  live_output="$(
    docker run --rm \
      "${docker_env[@]}" \
      -v "$PWD":/repo \
      -w /repo \
      "$IMAGE" \
      ctc stream --cwd /repo "Reply with exactly: docker-ok"
  )"
  grep -q '"event": "done"' <<<"$live_output"
  grep -q '"event": "metrics"' <<<"$live_output"
  grep -q 'docker-ok' <<<"$live_output"
fi

echo "docker refactor contract check passed"
