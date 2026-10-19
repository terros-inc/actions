#!/usr/bin/env bash
set -euo pipefail

export PI_CODING_AGENT_DIR=/tmp/pi-agent
export PI_OFFLINE=1
export XDG_CACHE_HOME=/tmp/pi-cache
mkdir -p "$PI_CODING_AGENT_DIR"
cp /review/context/models.json "$PI_CODING_AGENT_DIR/models.json"

exec pi --print \
  --model "$PI_MODEL_ID" \
  --thinking "$PI_THINKING" \
  --no-session \
  --no-approve \
  --no-extensions \
  --no-mcp \
  --no-skills \
  --no-prompt-templates \
  --no-themes \
  --no-context-files \
  --tools read,bash,grep,find,ls \
  --append-system-prompt /review/action/prompt.md \
  "$(cat /review/context/request.txt)"
