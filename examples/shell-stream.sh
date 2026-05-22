#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <cwd> <prompt...>" >&2
  exit 2
fi

cwd="$1"
shift

session_id="${SESSION_ID:-$(python3 -c 'import uuid; print(uuid.uuid4())')}"

TERM="${TERM:-xterm-256color}" ctc stream \
  --cwd "$cwd" \
  --session-id "$session_id" \
  "$@"
