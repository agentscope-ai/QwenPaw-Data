#!/usr/bin/env bash
# Session → console/chat → SSE. API must already be up.
# Usage: ./scripts/tests/console_sse.sh [prompt...]
set -euo pipefail

BASE="${QWENPAW_DATA_API_BASE:-http://127.0.0.1:8766}"
AUTH="Authorization: Bearer ${QWENPAW_DATA_API_TOKEN:-test-token}"
TEXT="${*:-请用 Write 工具写一个 hello.txt，内容为 hi，写完后用一句话确认。}"
DS="${QWENPAW_DATA_DATASOURCE_ID:-ds_demo}"

SID=$(curl -sS -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"title":"smoke"}' "$BASE/api/v1/sessions" | jq -r .session.id)
CID=$(
  jq -nc --arg s "$SID" --arg t "$TEXT" --arg d "$DS" \
    '{session_id:$s,text:$t,datasource_id:$d}' |
    curl -sS -H "$AUTH" -H "Content-Type: application/json" \
      -d @- "$BASE/api/v1/console/chat" | jq -r .chat.id
)
echo "session=$SID chat=$CID"
curl -N -sS -H "$AUTH" -H "Accept: text/event-stream" \
  "$BASE/api/v1/sessions/$SID/chats/$CID/events?after_sequence_number=-1"
echo
