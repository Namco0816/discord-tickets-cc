#!/usr/bin/env bash
# Discord Permission Hook for CC Tickets
#
# Mirrors Claude Code's native permission UX via Discord reactions.
#
# Permission modes (CC_TICKET_PERMISSION_MODE env var):
#   prompt-bash  (default) — only Bash prompts in Discord; edits auto-allowed
#   prompt-all   — Bash + Edit + Write all prompt in Discord
#   allow-all    — everything auto-allowed (no Discord prompts)
#
# Reaction options:
#   ✅ Yes (once)    🔓 Yes for all [tool]    ❌ No
#
# Falls through to terminal prompt on timeout (2 min).

set -euo pipefail

# ---- Config ----
COORD_FILE="${CC_TICKET_COORD_FILE:-$HOME/.claude/channels/discord/ticket_thread_id}"
# Derive allow-all dir from coord file path so it's stable per session
ALLOW_ALL_DIR="${COORD_FILE%.thread}.permissions"
POLL_INTERVAL=2
POLL_TIMEOUT=120
PERMISSION_MODE="${CC_TICKET_PERMISSION_MODE:-prompt-bash}"

# Reaction emoji (URL-encoded for Discord API)
YES_EMOJI_URL="%E2%9C%85"       # ✅
ALL_EMOJI_URL="%F0%9F%94%93"    # 🔓
NO_EMOJI_URL="%E2%9D%8C"        # ❌

# ---- Read stdin ----
INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // "unknown"')
TOOL_INPUT=$(echo "$INPUT" | jq -c '.tool_input // {}')

# ---- Allow-all mode: skip everything ----
if [ "$PERMISSION_MODE" = "allow-all" ]; then
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"allow-all mode"}}'
  exit 0
fi

# ---- Auto-allow: tools that never need permission ----
case "$TOOL_NAME" in
  Read|Glob|Grep|WebSearch|WebFetch|Agent|TaskCreate|TaskUpdate|TaskGet|TaskList|TaskOutput|TaskStop|AskUserQuestion|Skill|ToolSearch|ExitPlanMode|EnterPlanMode|NotebookEdit)
    echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"Safe tool auto-approved"}}'
    exit 0
    ;;
  mcp__*)
    echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"MCP tool auto-approved"}}'
    exit 0
    ;;
esac

# ---- prompt-bash mode: also auto-allow Edit/Write ----
if [ "$PERMISSION_MODE" = "prompt-bash" ]; then
  case "$TOOL_NAME" in
    Edit|Write)
      exit 0  # let Claude Code handle (auto-allows in acceptEdits-like behavior)
      ;;
  esac
fi

# ---- Check "allow all" memory for this tool type ----
mkdir -p "$ALLOW_ALL_DIR" 2>/dev/null || true
if [ -f "$ALLOW_ALL_DIR/$TOOL_NAME" ]; then
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"allow\",\"permissionDecisionReason\":\"Previously approved (allow all $TOOL_NAME)\"}}"
  exit 0
fi

# ---- Load bot token ----
BOT_TOKEN="${DISCORD_BOT_TOKEN:-}"
if [ -z "$BOT_TOKEN" ]; then
  ENV_FILE="$HOME/.claude/channels/discord/.env"
  [ -f "$ENV_FILE" ] && BOT_TOKEN=$(grep '^DISCORD_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2- || true)
fi
[ -z "$BOT_TOKEN" ] && exit 0  # no token → fall through to terminal

# ---- Read thread ID ----
[ ! -f "$COORD_FILE" ] && exit 0
THREAD_ID=$(tr -d '[:space:]' < "$COORD_FILE")
[ -z "$THREAD_ID" ] && exit 0

# ---- Format tool description ----
case "$TOOL_NAME" in
  Bash)
    CMD=$(echo "$TOOL_INPUT" | jq -r '.command // "unknown"' | head -c 800)
    DESC=$(printf '```\n%s\n```' "$CMD")
    ;;
  Edit)
    FILE=$(echo "$TOOL_INPUT" | jq -r '.file_path // "unknown"')
    OLD=$(echo "$TOOL_INPUT" | jq -r '.old_string // ""' | head -c 150)
    NEW=$(echo "$TOOL_INPUT" | jq -r '.new_string // ""' | head -c 150)
    if [ -n "$OLD" ]; then
      DESC=$(printf '**File:** `%s`\n```diff\n- %s\n+ %s\n```' "$FILE" "$OLD" "$NEW")
    else
      DESC="**File:** \`${FILE}\`"
    fi
    ;;
  Write)
    FILE=$(echo "$TOOL_INPUT" | jq -r '.file_path // "unknown"')
    PREVIEW=$(echo "$TOOL_INPUT" | jq -r '.content // ""' | head -3 | head -c 200)
    if [ -n "$PREVIEW" ]; then
      DESC=$(printf '**File:** `%s`\n```\n%s\n...\n```' "$FILE" "$PREVIEW")
    else
      DESC="**File:** \`${FILE}\`"
    fi
    ;;
  *)
    DESC="\`$(echo "$TOOL_INPUT" | jq -c '.' | head -c 300)\`"
    ;;
esac

# ---- Send permission request ----
MESSAGE=$(printf '🔐 **%s**\n%s\n\n✅ Yes  ·  🔓 Yes for all %s  ·  ❌ No' "$TOOL_NAME" "$DESC" "$TOOL_NAME")
PAYLOAD=$(jq -n --arg content "$MESSAGE" '{"content": $content}')

RESPONSE=$(curl -s -X POST \
  -H "Authorization: Bot $BOT_TOKEN" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" \
  "https://discord.com/api/v10/channels/${THREAD_ID}/messages")

MSG_ID=$(echo "$RESPONSE" | jq -r '.id // empty')
[ -z "$MSG_ID" ] && exit 0  # send failed → fall through

# ---- Add reaction buttons (with small delays to avoid rate limits) ----
for EMOJI in "$YES_EMOJI_URL" "$ALL_EMOJI_URL" "$NO_EMOJI_URL"; do
  curl -s -X PUT \
    -H "Authorization: Bot $BOT_TOKEN" \
    "https://discord.com/api/v10/channels/${THREAD_ID}/messages/${MSG_ID}/reactions/${EMOJI}/@me" > /dev/null 2>&1
  sleep 0.3
done

# ---- Poll for reaction ----
ELAPSED=0
DECISION=""

while [ $ELAPSED -lt $POLL_TIMEOUT ]; do
  sleep $POLL_INTERVAL
  ELAPSED=$((ELAPSED + POLL_INTERVAL))

  for CHECK in "yes:$YES_EMOJI_URL" "all:$ALL_EMOJI_URL" "no:$NO_EMOJI_URL"; do
    KIND="${CHECK%%:*}"
    EMOJI="${CHECK#*:}"

    REACTIONS=$(curl -s \
      -H "Authorization: Bot $BOT_TOKEN" \
      "https://discord.com/api/v10/channels/${THREAD_ID}/messages/${MSG_ID}/reactions/${EMOJI}")

    COUNT=$(echo "$REACTIONS" | jq '[.[] | select(.bot != true)] | length' 2>/dev/null || echo "0")

    if [ "$COUNT" -gt 0 ]; then
      DECISION="$KIND"
      break 2
    fi
  done
done

# ---- Apply decision ----
edit_msg() {
  local NEW_MSG="$1"
  local EP=$(jq -n --arg c "$NEW_MSG" '{"content":$c}')
  curl -s -X PATCH \
    -H "Authorization: Bot $BOT_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$EP" \
    "https://discord.com/api/v10/channels/${THREAD_ID}/messages/${MSG_ID}" > /dev/null 2>&1
}

case "$DECISION" in
  yes)
    edit_msg "$(printf '✅ **%s** — Approved\n%s' "$TOOL_NAME" "$DESC")"
    echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"allow\",\"permissionDecisionReason\":\"Approved via Discord\"}}"
    ;;
  all)
    touch "$ALLOW_ALL_DIR/$TOOL_NAME"
    edit_msg "$(printf '🔓 **%s** — Approved (all future)\n%s' "$TOOL_NAME" "$DESC")"
    echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"allow\",\"permissionDecisionReason\":\"Approved all $TOOL_NAME via Discord\"}}"
    ;;
  no)
    edit_msg "$(printf '❌ **%s** — Denied\n%s' "$TOOL_NAME" "$DESC")"
    echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"deny\",\"permissionDecisionReason\":\"Denied via Discord\"}}"
    ;;
  *)
    edit_msg "$(printf '⏰ **%s** — Timed out\n%s' "$TOOL_NAME" "$DESC")"
    exit 0  # timeout → fall through to terminal prompt
    ;;
esac

exit 0
