#!/usr/bin/env bash
set -euo pipefail

ctc_preseed_claude_code() {
  local trusted_dir="${CTC_TRUSTED_WORKDIR:-${PWD:-/repo}}"
  local config_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

  mkdir -p "$config_dir"

  CTC_TRUSTED_WORKDIR="$trusted_dir" CLAUDE_CONFIG_DIR="$config_dir" python3 - <<'PY'
import json
import os
from pathlib import Path

home = Path.home()
trusted_dir = str(Path(os.environ["CTC_TRUSTED_WORKDIR"]).resolve())
config_dir = Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser()

global_config_path = home / ".claude.json"
try:
    global_config = json.loads(global_config_path.read_text())
    if not isinstance(global_config, dict):
        global_config = {}
except (FileNotFoundError, json.JSONDecodeError):
    global_config = {}

projects = global_config.get("projects")
if not isinstance(projects, dict):
    projects = {}

project = projects.get(trusted_dir)
if not isinstance(project, dict):
    project = {}

project.update(
    {
        "allowedTools": project.get("allowedTools") or [],
        "hasTrustDialogAccepted": True,
        "hasCompletedProjectOnboarding": True,
        "projectOnboardingSeenCount": max(int(project.get("projectOnboardingSeenCount") or 0), 4),
    }
)
projects[trusted_dir] = project

global_config.update(
    {
        "hasCompletedOnboarding": True,
        "projects": projects,
    }
)

global_config_path.write_text(json.dumps(global_config, ensure_ascii=False, indent=2))

settings_path = config_dir / "settings.json"
try:
    settings = json.loads(settings_path.read_text())
    if not isinstance(settings, dict):
        settings = {}
except (FileNotFoundError, json.JSONDecodeError):
    settings = {}

settings["skipDangerousModePermissionPrompt"] = True
settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2))
PY
}

ctc_preflight_claude_code() {
  if [[ "${CTC_SKIP_CLAUDE_PREFLIGHT:-}" == "1" ]]; then
    return 0
  fi

  if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" && -z "${ANTHROPIC_API_KEY:-}" && "${CTC_FORCE_CLAUDE_PREFLIGHT:-}" != "1" ]]; then
    return 0
  fi

  local preflight_output
  if ! preflight_output="$(claude -p "Reply with exactly: ctc-docker-preflight-ok" 2>&1)"; then
    echo "ctc-docker-entrypoint: Claude Code preflight failed" >&2
    echo "$preflight_output" >&2
    return 1
  fi

  if [[ "$preflight_output" != *"ctc-docker-preflight-ok"* ]]; then
    echo "ctc-docker-entrypoint: Claude Code preflight returned an unexpected response" >&2
    echo "$preflight_output" >&2
    return 1
  fi
}

ctc_preseed_claude_code
ctc_preflight_claude_code

exec "$@"
