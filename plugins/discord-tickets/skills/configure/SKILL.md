---
name: configure
description: Set up the Discord ticket channel — save the bot token and forum channel ID. Use when the user asks to configure Discord tickets, set a channel, or says "how do I set this up."
user-invocable: true
allowed-tools:
  - Read
  - Write
  - Bash(mkdir:*)
  - Bash(chmod:*)
---

# /discord-tickets:configure — Discord Ticket Channel Setup

Guides the user through configuring the Discord ticket plugin.

## Required Configuration

Two values are needed, stored in `~/.claude/channels/discord/.env`:

1. **DISCORD_BOT_TOKEN** — the bot token from Discord Developer Portal
2. **TICKET_CHANNEL_ID** — the ID of a Discord forum channel where tickets will be created

## Steps

1. Read `~/.claude/channels/discord/.env` to check current state (handle missing file).

2. If `DISCORD_BOT_TOKEN` is missing, ask the user to:
   - Go to https://discord.com/developers/applications
   - Create or select a bot
   - Copy the bot token
   - Paste it here

3. If `TICKET_CHANNEL_ID` is missing or the user wants to change it, ask for the forum channel ID:
   - Enable Developer Mode in Discord (Settings → Advanced → Developer Mode)
   - Right-click the forum channel → Copy Channel ID
   - Paste it here

4. Write both values to `~/.claude/channels/discord/.env`:
   ```
   DISCORD_BOT_TOKEN=<token>
   TICKET_CHANNEL_ID=<channel_id>
   ```
   Set file permissions to `0600` (owner-only).

5. Confirm setup is complete and tell the user they can now run:
   ```
   claude --dangerously-load-development-channels server:discord-tickets
   ```

## Notes

- The `.env` file is shared with the stock Discord plugin — both read `DISCORD_BOT_TOKEN` from the same file.
- `TICKET_CHANNEL_ID` is specific to this plugin.
- The bot needs `Send Messages`, `Read Message History`, and ideally `Manage Threads` permissions in the forum channel.
