# discord-tickets

A Claude Code plugin that maps each session to a Discord forum thread. Chat with Claude Code from your phone, approve permissions with emoji reactions, and manage sessions as forum posts.

## Features

- **Forum thread per session** — each Claude Code session creates a Discord forum post
- **Two-way interaction** — send messages in Discord, get responses from Claude
- **Permission approval via reactions** — Bash commands prompt in Discord with ✅ Yes / 🔓 Yes for all / ❌ No
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

**Option A: From GitHub (recommended)**

```bash
# Add the marketplace
claude plugin marketplace add github:Namco0816/discord-tickets-cc

# Install the plugin
claude plugin install discord-tickets@discord-tickets
```

**Option B: Local install**

```bash
# Clone the repo
git clone https://github.com/Namco0816/discord-tickets-cc.git

# Add as local marketplace
claude plugin marketplace add /path/to/discord-tickets

# Install
claude plugin install discord-tickets@discord-tickets
```

### 5. Launch

```bash
# Forward mode: start Claude, interact via Discord
claude --dangerously-load-development-channels "plugin:discord-tickets@discord-tickets"
```

A forum post will be created automatically. Send messages there to interact with Claude.

## Usage Modes

### Forward Mode (recommended)

Start a Claude Code session manually — a forum thread is created for interaction:

```bash
cd ~/my-project
claude --dangerously-load-development-channels "plugin:discord-tickets@discord-tickets"
```

You can also set a thread title via environment variable:

```bash
TICKET_SESSION_NAME="Fix auth bug" claude --dangerously-load-development-channels "plugin:discord-tickets@discord-tickets"
```

### Backward Mode (orchestrator)

A persistent process watches the forum channel. Create a post in Discord → a Claude session spawns automatically on your server.

```bash
# Install dependencies
pip install discord.py

# Start the orchestrator
python orchestrator.py

# With options
python orchestrator.py --working-dir ~/projects --max-sessions 3 --timeout 120
```

Each session runs in a tmux window. Attach to any session:

```bash
tmux attach -t cct-<thread_id>
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

## How It Works

```
Discord Forum Channel
        │
        ▼
┌──────────────┐     stdio      ┌──────────────┐
│  MCP Plugin  │◄──────────────►│  Claude Code  │
│  (server.ts) │                │              │
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

## Plugin Structure

```
discord-tickets/
├── .claude-plugin/
│   └── plugin.json          # Plugin manifest
├── .mcp.json                # MCP server definition
├── server.ts                # MCP channel server (discord.js)
├── package.json             # Bun dependencies
├── hooks/
│   ├── hooks.json           # PreToolUse hook config
│   └── permission-hook.sh   # Reaction-based approval
├── skills/
│   └── configure/
│       └── SKILL.md         # /discord-tickets:configure skill
├── orchestrator.py          # Backward mode (optional)
└── README.md
```

## License

MIT
