# discord-tickets

A Claude Code plugin that maps each session to a Discord forum thread. Chat with Claude Code from your phone, approve permissions with emoji reactions, and manage sessions as forum posts.

Inspired by the official [Discord channel plugin](https://github.com/anthropics/claude-plugins-official/tree/main/external_plugins/discord) by Anthropic.

## Features

- **Forum thread per session** — each Claude Code session creates a Discord forum post
- **Two-way interaction** — send messages in Discord, get responses from Claude
- **Permission approval via reactions** — Bash commands prompt in Discord with ✅ Yes / 🔓 Yes for all / ❌ No
- **AskUserQuestion forwarding** — when Claude asks you a question with options, it appears in Discord for you to answer
- **Persistent typing indicator** — shows "typing..." while Claude is working
- **Session lifecycle** — thread archives when session ends, shows duration
- **Orchestrator** (optional) — create a forum post in Discord to auto-spawn a Claude session on your server

## Quick Start

### 1. Create a Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Create a new application → Bot → copy the **bot token**
3. Enable these intents: **Server Members**, **Message Content**
4. Invite the bot to your server with permissions: `Send Messages`, `Read Message History`, `Add Reactions`, `Manage Threads`

### 2. Create a Forum Channel

Create a forum channel in your Discord server (e.g., `#cc-tickets`). Right-click → Copy Channel ID (enable Developer Mode in Discord settings if needed).

### 3. Save Credentials

```bash
mkdir -p ~/.claude/channels/discord
cat > ~/.claude/channels/discord/.env << 'EOF'
DISCORD_BOT_TOKEN=your_bot_token_here
TICKET_CHANNEL_ID=your_forum_channel_id_here
EOF
chmod 600 ~/.claude/channels/discord/.env
```

### 4. Install the Plugin

```bash
# Add the marketplace
claude plugin marketplace add Namco0816/discord-tickets-cc

# Install the plugin
claude plugin install discord-tickets@discord-tickets-cc
```

### 5. Launch

```bash
cd ~/my-project
claude --dangerously-load-development-channels "plugin:discord-tickets@discord-tickets-cc"
```

A forum post will be created automatically. Send messages there to interact with Claude.

## Usage Modes

You can run both modes simultaneously — they don't conflict.

### Forward Mode

Start a Claude Code session manually — a forum thread is created for interaction:

```bash
cd ~/my-project
claude --dangerously-load-development-channels "plugin:discord-tickets@discord-tickets-cc"
```

Set a custom thread title:

```bash
TICKET_SESSION_NAME="Fix auth bug" claude --dangerously-load-development-channels "plugin:discord-tickets@discord-tickets-cc"
```

### Backward Mode (orchestrator)

A persistent process watches the forum channel. When someone creates a forum post in Discord, a Claude Code session spawns automatically on your server in a tmux window.

**Prerequisites:**
- The plugin must be installed (steps 1–4 above)
- `tmux` (`sudo apt install tmux` or `brew install tmux`)
- Python 3.8+ with `discord.py` (`pip install discord.py`)

**Setup:**

```bash
# Clone the repo (needed to run the orchestrator script)
git clone https://github.com/Namco0816/discord-tickets-cc.git
cd discord-tickets-cc/plugins/discord-tickets

# Install Python dependency
pip install discord.py
```

**Launch:**

```bash
# Basic — uses channel ID from ~/.claude/channels/discord/.env
python orchestrator.py

# Specify working directory for spawned sessions
python orchestrator.py --working-dir ~/my-project

# Restrict who can create sessions (Discord user IDs)
python orchestrator.py --allowed-users 123456789 987654321

# Full example
python orchestrator.py --working-dir ~/projects --max-sessions 3 --timeout 120
```

**Run 24/7 in background:**

```bash
# Start in a detached tmux session
tmux new-session -d -s cct-orchestrator "cd /path/to/discord-tickets-cc/plugins/discord-tickets && python orchestrator.py"

# View logs
tmux attach -t cct-orchestrator
# Detach: Ctrl+B then D

# Stop
tmux kill-session -t cct-orchestrator
```

To survive server reboots, add to crontab:

```bash
crontab -e
# Add:
@reboot tmux new-session -d -s cct-orchestrator "cd /path/to/discord-tickets-cc/plugins/discord-tickets && python orchestrator.py"
```

**How it works:**
1. You create a forum post in Discord (e.g., "Fix the auth bug")
2. The orchestrator detects it and spawns `claude` in a tmux window
3. Claude connects to that thread — you interact entirely from Discord
4. When you archive/close the post, the session terminates

**Managing sessions:**

```bash
# List active sessions
tmux list-sessions | grep cct-

# Attach to a session (see what Claude is doing in terminal)
tmux attach -t cct-<thread_id>

# Detach from tmux: Ctrl+B then D

# Manually kill a session
tmux kill-session -t cct-<thread_id>
```

### Orchestrator Options

| Flag | Default | Description |
|------|---------|-------------|
| `--channel, -c` | from `.env` | Forum channel ID |
| `--working-dir, -d` | `~/.cct_workspace` | Working directory for spawned sessions |
| `--max-sessions, -m` | 5 | Max concurrent sessions |
| `--timeout, -t` | 60 | Inactivity timeout in minutes (0 = disable) |
| `--allowed-users, -u` | any | Discord user IDs allowed to create sessions |

## Permission Modes

The plugin includes a permission hook that forwards tool approval to Discord. Control it with the `CC_TICKET_PERMISSION_MODE` environment variable:

| Mode | Behavior |
|------|----------|
| `prompt-bash` (default) | Only Bash commands prompt in Discord. Edits auto-approved. |
| `prompt-all` | Bash + Edit + Write all prompt in Discord |
| `allow-all` | Everything auto-approved (no Discord prompts) |

When prompted, react with:
- ✅ **Yes** — approve this one call
- 🔓 **Yes for all** — approve all future calls of this tool type (session-scoped)
- ❌ **No** — deny

## Interactive Features

- **AskUserQuestion** — when Claude asks a question with options, it appears in the Discord thread as a numbered list. Reply with a number or type your answer.
- **Plan mode** — say "plan this first" in Discord and Claude enters plan mode naturally. Say "go ahead" to exit.
- **Multi-turn conversations** — Claude maintains full context across messages in the thread.

## How It Works

```
Discord Forum Channel
        │
        ▼
┌──────────────┐     stdio      ┌──────────────┐
│  MCP Plugin  │◄──────────────►│  Claude Code  │
│  (server.ts) │                │               │
│  - discord.js│                │  Your IDE /   │
│  - 1 thread  │                │  Terminal     │
└──────────────┘                └──────────────┘
```

The plugin is an MCP server that:
1. Connects to Discord via discord.js
2. Creates/attaches to a forum thread
3. Routes messages between Discord and Claude Code via the `claude/channel` protocol
4. Handles typing indicators, reactions, attachments, and message chunking

## Requirements

- [Claude Code](https://claude.com/claude-code) CLI
- [Bun](https://bun.sh) runtime (for the MCP server)
- A Discord bot with appropriate permissions
- Python 3.8+ and `discord.py` (for orchestrator only)
- `tmux` (for orchestrator only)
- `jq` and `curl` (for permission hook)

## License

MIT
