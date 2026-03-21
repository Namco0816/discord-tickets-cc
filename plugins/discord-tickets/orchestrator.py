#!/usr/bin/env python3
"""
CC Tickets Orchestrator — watches Discord forum channel, spawns claude sessions.

When a user creates a forum post, this spawns a claude session in a tmux window
that communicates through that thread. When the thread is archived/deleted, the
session is terminated.

Usage:
    python orchestrator.py                    # uses ~/.claude/channels/discord/.env
    python orchestrator.py --channel 123456   # override forum channel ID

Each session runs in tmux as "cct-<thread_id>" — attach with:
    tmux attach -t cct-<thread_id>
"""

import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cc-tickets")

# ---- Config from .env ----

def load_env():
    env_file = Path.home() / ".claude" / "channels" / "discord" / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())


class CCTicketOrchestrator:
    def __init__(self, channel_id: str, allowed_users: list = None,
                 max_sessions: int = 5, timeout_minutes: int = 60,
                 working_dir: str = None):
        self.channel_id = channel_id
        self.allowed_users = set(allowed_users or [])
        self.max_sessions = max_sessions
        self.timeout_minutes = timeout_minutes
        default_ws = Path.home() / ".cct_workspace"
        default_ws.mkdir(exist_ok=True)
        self.working_dir = working_dir or str(default_ws)

        # thread_id -> tmux session name
        self.sessions: Dict[str, str] = {}
        # thread_id -> last_activity
        self.last_activity: Dict[str, float] = {}

        self.client = None
        self.bot_user_id: Optional[str] = None

    async def start(self):
        try:
            import discord
        except ImportError:
            logger.error("discord.py not installed. Run: pip install discord.py")
            raise SystemExit(1)

        intents = discord.Intents.default()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True

        self.client = discord.Client(intents=intents)

        @self.client.event
        async def on_ready():
            self.bot_user_id = str(self.client.user.id)
            logger.info(f"Connected as {self.client.user} — watching channel {self.channel_id}")
            await self._reconcile()

        @self.client.event
        async def on_thread_create(thread):
            await self._on_thread_create(thread)

        @self.client.event
        async def on_thread_update(before, after):
            tid = str(after.id)
            if tid in self.sessions:
                if not getattr(before, 'archived', False) and getattr(after, 'archived', False):
                    logger.info(f"Thread {tid} archived — killing session")
                    self._kill_session(tid)

        @self.client.event
        async def on_thread_delete(thread):
            tid = str(thread.id)
            if tid in self.sessions:
                logger.info(f"Thread {tid} deleted — killing session")
                self._kill_session(tid)

        @self.client.event
        async def on_message(message):
            if not message.author.bot:
                tid = str(message.channel.id)
                if tid in self.sessions:
                    self.last_activity[tid] = time.time()

        # Start reaper
        asyncio.create_task(self._reaper_loop())

        token = os.environ.get("DISCORD_BOT_TOKEN", "")
        if not token:
            logger.error("DISCORD_BOT_TOKEN not set")
            raise SystemExit(1)

        await self.client.start(token)

    async def _on_thread_create(self, thread):
        parent_id = str(thread.parent_id) if thread.parent_id else ""
        if parent_id != self.channel_id:
            return

        # Skip threads created by our bot
        if thread.owner_id and str(thread.owner_id) == self.bot_user_id:
            return

        creator_id = str(thread.owner_id) if thread.owner_id else ""
        if self.allowed_users and creator_id not in self.allowed_users:
            logger.info(f"Ignoring thread {thread.id} from non-allowed user {creator_id}")
            try:
                await thread.send("Not authorized to create ticket sessions.")
            except Exception:
                pass
            return

        if len(self.sessions) >= self.max_sessions:
            logger.warning(f"Max sessions ({self.max_sessions}) reached")
            try:
                await thread.send(f"Max concurrent sessions ({self.max_sessions}) reached. Close a ticket first.")
            except Exception:
                pass
            return

        tid = str(thread.id)
        tmux_name = f"cct-{tid}"

        logger.info(f"New thread {tid} ({thread.name}) — spawning session in tmux:{tmux_name}")

        # Use a login shell so Claude Code finds its plugins and config
        shell_cmd = (
            f'export TICKET_THREAD_ID="{tid}" TICKET_CHANNEL_ID="{self.channel_id}"; '
            f'exec claude --dangerously-skip-permissions '
            f'--dangerously-load-development-channels "plugin:discord-tickets@discord-tickets-cc"'
        )
        cmd = [
            "tmux", "new-session", "-d", "-s", tmux_name, "-c", self.working_dir,
            "bash", "-lc", shell_cmd,
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True)
            self.sessions[tid] = tmux_name
            self.last_activity[tid] = time.time()
            logger.info(f"Session started: tmux:{tmux_name}")

            # Auto-accept the trust dialog and dev-channels warning
            # by sending Enter keystrokes after short delays
            async def _auto_accept():
                for _ in range(3):
                    await asyncio.sleep(2)
                    subprocess.run(
                        ["tmux", "send-keys", "-t", tmux_name, "Enter"],
                        capture_output=True
                    )
            asyncio.create_task(_auto_accept())

            logger.info(f"Attach with: tmux attach -t {tmux_name}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to spawn tmux session: {e.stderr.decode()}")
            try:
                await thread.send("Failed to start Claude Code session.")
            except Exception:
                pass

    def _kill_session(self, thread_id: str):
        tmux_name = self.sessions.pop(thread_id, None)
        self.last_activity.pop(thread_id, None)
        if tmux_name:
            try:
                subprocess.run(["tmux", "kill-session", "-t", tmux_name],
                             capture_output=True, timeout=5)
                logger.info(f"Killed tmux session {tmux_name}")
            except Exception as e:
                logger.warning(f"Error killing session {tmux_name}: {e}")

    def _session_alive(self, tmux_name: str) -> bool:
        result = subprocess.run(
            ["tmux", "has-session", "-t", tmux_name],
            capture_output=True
        )
        return result.returncode == 0

    async def _reaper_loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self._reap()
            except Exception as e:
                logger.error(f"Reaper error: {e}")

    async def _reap(self):
        now = time.time()
        for tid in list(self.sessions.keys()):
            tmux_name = self.sessions[tid]

            # Check if tmux session is still alive
            if not self._session_alive(tmux_name):
                logger.info(f"Session {tmux_name} died — cleaning up")
                self.sessions.pop(tid, None)
                self.last_activity.pop(tid, None)
                await self._post(tid, "Claude Code session ended.")
                await self._archive(tid)
                continue

            # Check timeout
            if self.timeout_minutes > 0:
                last = self.last_activity.get(tid, now)
                if now - last > self.timeout_minutes * 60:
                    logger.info(f"Session {tmux_name} timed out")
                    await self._post(tid, f"Session timed out ({self.timeout_minutes}m inactivity).")
                    self._kill_session(tid)
                    await self._archive(tid)

    async def _post(self, thread_id: str, message: str):
        if not self.client:
            return
        try:
            ch = await self.client.fetch_channel(int(thread_id))
            if hasattr(ch, 'send'):
                await ch.send(message)
        except Exception:
            pass

    async def _archive(self, thread_id: str):
        if not self.client:
            return
        try:
            ch = await self.client.fetch_channel(int(thread_id))
            if hasattr(ch, 'edit'):
                await ch.edit(archived=True)
        except Exception:
            pass

    async def _reconcile(self):
        """On startup, check for existing tmux sessions from previous runs."""
        result = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                              capture_output=True, text=True)
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if line.startswith("cct-"):
                    tid = line[4:]  # strip "cct-" prefix
                    self.sessions[tid] = line
                    self.last_activity[tid] = time.time()
                    logger.info(f"Reconcile: found existing session {line}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="CC Tickets — auto-spawn Claude sessions from Discord forum posts")
    parser.add_argument("--channel", "-c", default=None, help="Forum channel ID (default: from .env)")
    parser.add_argument("--allowed-users", "-u", nargs="*", default=None, help="Allowed Discord user IDs")
    parser.add_argument("--max-sessions", "-m", type=int, default=5, help="Max concurrent sessions")
    parser.add_argument("--timeout", "-t", type=int, default=60, help="Inactivity timeout (minutes, 0=disable)")
    parser.add_argument("--working-dir", "-d", default=None, help="Working directory for claude sessions")
    args = parser.parse_args()

    load_env()

    channel_id = args.channel or os.environ.get("TICKET_CHANNEL_ID", "")
    if not channel_id:
        logger.error("No forum channel ID. Use --channel or set TICKET_CHANNEL_ID in ~/.claude/channels/discord/.env")
        raise SystemExit(1)

    orchestrator = CCTicketOrchestrator(
        channel_id=channel_id,
        allowed_users=args.allowed_users,
        max_sessions=args.max_sessions,
        timeout_minutes=args.timeout,
        working_dir=args.working_dir,
    )

    logger.info("CC Tickets Orchestrator starting...")
    logger.info(f"  Forum channel: {channel_id}")
    logger.info(f"  Max sessions: {args.max_sessions}")
    logger.info(f"  Timeout: {args.timeout}m")
    logger.info(f"  Working dir: {args.working_dir or '~'}")
    logger.info(f"  Attach to any session: tmux attach -t cct-<thread_id>")

    asyncio.run(orchestrator.start())


if __name__ == "__main__":
    main()
