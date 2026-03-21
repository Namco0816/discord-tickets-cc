#!/usr/bin/env bash
# Discord Permission Hook for CC Tickets
#
# Mirrors Claude Code's native permission UX via Discord.
#
# Permission modes (CC_TICKET_PERMISSION_MODE env var):
#   prompt-bash  (default) — only Bash prompts in Discord; edits auto-allowed
#   prompt-all   — Bash + Edit + Write all prompt in Discord
#   allow-all    — everything auto-allowed (no Discord prompts)
#
# Also handles:
#   - AskUserQuestion → posts question + options to Discord, polls for text reply
#   - Permission prompts → reaction-based approval (✅ 🔓 ❌)
#
# Falls through to terminal prompt on timeout.

set -euo pipefail

# ---- Config ----
COORD_FILE="${CC_TICKET_COORD_FILE:-$HOME/.claude/channels/discord/ticket_thread_id}"
ALLOW_ALL_DIR="${COORD_FILE%.thread}.permissions"
POLL_INTERVAL=2
PERMISSION_POLL_TIMEOUT=120   # 2 min for permission prompts
QUESTION_POLL_TIMEOUT=300     # 5 min for user questions
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

# ---- Helper: load bot token and thread ID ----
load_discord_context() {
  BOT_TOKEN="${DISCORD_BOT_TOKEN:-}"
  if [ -z "$BOT_TOKEN" ]; then
    ENV_FILE="$HOME/.claude/channels/discord/.env"
    [ -f "$ENV_FILE" ] && BOT_TOKEN=$(grep '^DISCORD_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2- || true)
  fi
  [ -z "$BOT_TOKEN" ] && return 1

  [ ! -f "$COORD_FILE" ] && return 1
  THREAD_ID=$(tr -d '[:space:]' < "$COORD_FILE")
  [ -z "$THREAD_ID" ] && return 1
  return 0
}

# ---- Helper: edit a Discord message ----
edit_msg() {
  local MSG_ID="$1"
  local NEW_MSG="$2"
  local EP=$(jq -n --arg c "$NEW_MSG" '{"content":$c}')
  curl -s -X PATCH \
    -H "Authorization: Bot $BOT_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$EP" \
    "https://discord.com/api/v10/channels/${THREAD_ID}/messages/${MSG_ID}" > /dev/null 2>&1
}

# ===============================================================
# AskUserQuestion — forward to Discord, poll for text reply
# ===============================================================
if [ "$TOOL_NAME" = "AskUserQuestion" ]; then
  if ! load_discord_context; then
    exit 0  # no Discord context → fall through to terminal
  fi

  # Extract questions array
  QUESTIONS=$(echo "$TOOL_INPUT" | jq -r '.questions // []')
  NUM_QUESTIONS=$(echo "$QUESTIONS" | jq 'length')

  if [ "$NUM_QUESTIONS" -eq 0 ]; then
    exit 0
  fi

  # Build Discord message with all questions
  MESSAGE=""
  for i in $(seq 0 $((NUM_QUESTIONS - 1))); do
    Q=$(echo "$QUESTIONS" | jq -r ".[$i]")
    QUESTION_TEXT=$(echo "$Q" | jq -r '.question // "Question?"')
    OPTIONS=$(echo "$Q" | jq -r '.options // []')
    NUM_OPTIONS=$(echo "$OPTIONS" | jq 'length')

    MESSAGE="${MESSAGE}❓ **${QUESTION_TEXT}**"$'\n'

    if [ "$NUM_OPTIONS" -gt 0 ]; then
      for j in $(seq 0 $((NUM_OPTIONS - 1))); do
        LABEL=$(echo "$OPTIONS" | jq -r ".[$j].label // \"Option $((j+1))\"")
        DESC=$(echo "$OPTIONS" | jq -r ".[$j].description // \"\"")
        NUM=$((j + 1))
        if [ -n "$DESC" ]; then
          MESSAGE="${MESSAGE}${NUM}. **${LABEL}** — ${DESC}"$'\n'
        else
          MESSAGE="${MESSAGE}${NUM}. **${LABEL}**"$'\n'
        fi
      done
    fi
    MESSAGE="${MESSAGE}"$'\n'
  done

  MESSAGE="${MESSAGE}Reply with a number to choose, or type your answer."

  # Post to Discord
  PAYLOAD=$(jq -n --arg content "$MESSAGE" '{"content": $content}')
  RESPONSE=$(curl -s -X POST \
    -H "Authorization: Bot $BOT_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    "https://discord.com/api/v10/channels/${THREAD_ID}/messages")

  MSG_ID=$(echo "$RESPONSE" | jq -r '.id // empty')
  [ -z "$MSG_ID" ] && exit 0

  # Poll for a text reply (not reaction)
  ELAPSED=0
  USER_ANSWER=""

  while [ $ELAPSED -lt $QUESTION_POLL_TIMEOUT ]; do
    sleep $POLL_INTERVAL
    ELAPSED=$((ELAPSED + POLL_INTERVAL))

    # Fetch messages after our question
    MESSAGES=$(curl -s \
      -H "Authorization: Bot $BOT_TOKEN" \
      "https://discord.com/api/v10/channels/${THREAD_ID}/messages?after=${MSG_ID}&limit=5")

    # Find first non-bot message
    USER_ANSWER=$(echo "$MESSAGES" | jq -r '[.[] | select(.author.bot != true)] | first | .content // empty' 2>/dev/null || true)

    if [ -n "$USER_ANSWER" ]; then
      break
    fi
  done

  if [ -n "$USER_ANSWER" ]; then
    # Edit question to show it was answered
    edit_msg "$MSG_ID" "$(printf '✅ %s\n\n**Answer:** %s' "$QUESTION_TEXT" "$USER_ANSWER")"

    # Deny the tool but provide the answer in the reason — Claude reads this and continues
    REASON=$(jq -n --arg answer "$USER_ANSWER" '"User answered via Discord: " + $answer')
    echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"deny\",\"permissionDecisionReason\":${REASON}}}"
    exit 0
  else
    # Timeout
    edit_msg "$MSG_ID" "$(printf '⏰ %s\n\n*(Timed out — waiting for terminal input)*' "$QUESTION_TEXT")"
    exit 0  # fall through to terminal
  fi
fi

# ===============================================================
# Auto-allow: tools that never need permission
# ===============================================================
case "$TOOL_NAME" in
  Read|Glob|Grep|WebSearch|WebFetch|Agent|TaskCreate|TaskUpdate|TaskGet|TaskList|TaskOutput|TaskStop|Skill|ToolSearch|ExitPlanMode|EnterPlanMode|NotebookEdit)
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
      echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"Edit/Write auto-approved (prompt-bash mode)"}}'
      exit 0
      ;;
  esac
fi

# ===============================================================
# Permission prompt — reaction-based approval (Bash, Edit, Write)
# ===============================================================

# Check "allow all" memory for this tool type
mkdir -p "$ALLOW_ALL_DIR" 2>/dev/null || true
if [ -f "$ALLOW_ALL_DIR/$TOOL_NAME" ]; then
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"allow\",\"permissionDecisionReason\":\"Previously approved (allow all $TOOL_NAME)\"}}"
  exit 0
fi

if ! load_discord_context; then
  exit 0  # no Discord context → fall through to terminal
fi

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
[ -z "$MSG_ID" ] && exit 0

# ---- Add reaction buttons ----
for EMOJI in "$YES_EMOJI_URL" "$ALL_EMOJI_URL" "$NO_EMOJI_URL"; do
  curl -s -X PUT \
    -H "Authorization: Bot $BOT_TOKEN" \
    "https://discord.com/api/v10/channels/${THREAD_ID}/messages/${MSG_ID}/reactions/${EMOJI}/@me" > /dev/null 2>&1
  sleep 0.3
done

# ---- Poll for reaction ----
ELAPSED=0
DECISION=""

while [ $ELAPSED -lt $PERMISSION_POLL_TIMEOUT ]; do
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
case "$DECISION" in
  yes)
    edit_msg "$MSG_ID" "$(printf '✅ **%s** — Approved\n%s' "$TOOL_NAME" "$DESC")"
    echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"allow\",\"permissionDecisionReason\":\"Approved via Discord\"}}"
    ;;
  all)
    touch "$ALLOW_ALL_DIR/$TOOL_NAME"
    edit_msg "$MSG_ID" "$(printf '🔓 **%s** — Approved (all future)\n%s' "$TOOL_NAME" "$DESC")"
    echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"allow\",\"permissionDecisionReason\":\"Approved all $TOOL_NAME via Discord\"}}"
    ;;
  no)
    edit_msg "$MSG_ID" "$(printf '❌ **%s** — Denied\n%s' "$TOOL_NAME" "$DESC")"
    echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"deny\",\"permissionDecisionReason\":\"Denied via Discord\"}}"
    ;;
  *)
    edit_msg "$MSG_ID" "$(printf '⏰ **%s** — Timed out\n%s' "$TOOL_NAME" "$DESC")"
    exit 0  # timeout → fall through to terminal prompt
    ;;
esac

exit 0
