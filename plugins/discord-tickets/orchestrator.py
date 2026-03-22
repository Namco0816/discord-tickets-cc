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
import json
import logging
import os
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


THREADS_DIR = Path.home() / ".claude" / "channels" / "discord" / "threads"


class _ProcResult:
    __slots__ = ("returncode", "stdout", "stderr")
    def __init__(self, rc: int, out: bytes, err: bytes):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


async def _run(*cmd: str, timeout: float = 10) -> _ProcResult:
    """Run a subprocess without blocking the event loop."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return _ProcResult(proc.returncode, out or b"", err or b"")


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
        # thread_id -> timestamp when session ended (cooldown to prevent instant re-spawn)
        self._cooldowns: Dict[str, float] = {}
        # thread IDs currently being spawned (prevents double-spawn from rapid messages)
        self._resuming: set = set()

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

        self.client = discord.Client(intents=intents, max_messages=100)

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
                    await self._kill_session(tid)

        @self.client.event
        async def on_thread_delete(thread):
            tid = str(thread.id)
            if tid in self.sessions:
                logger.info(f"Thread {tid} deleted — killing session")
                await self._kill_session(tid)

        @self.client.event
        async def on_message(message):
            if message.author.bot:
                return
            tid = str(message.channel.id)

            # Active session — just update activity timestamp
            if tid in self.sessions:
                self.last_activity[tid] = time.time()
                return

            # Check if this thread belongs to our forum channel
            parent_id = str(getattr(message.channel, 'parent_id', '') or '')
            if parent_id != self.channel_id:
                return

            # Check allowed users
            if self.allowed_users and str(message.author.id) not in self.allowed_users:
                return

            # Cooldown: don't resume within 10s of session ending
            ended_at = self._cooldowns.get(tid)
            if ended_at and time.time() - ended_at < 10:
                return

            # Already spawning for this thread
            if tid in self._resuming:
                return

            logger.info(f"Message in orphaned thread {tid} from {message.author} — resuming session")
            await self._spawn_session(tid, message.channel)

        @self.client.event
        async def on_disconnect():
            logger.warning("Gateway disconnected — discord.py will auto-reconnect")

        @self.client.event
        async def on_resumed():
            logger.info("Gateway resumed")

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

        tid = str(thread.id)
        logger.info(f"New thread {tid} ({thread.name}) — spawning session")
        await self._spawn_session(tid, thread)

    def _read_thread_state(self, tid: str) -> Optional[dict]:
        """Read persisted session state (session_id, workspace) for a thread."""
        state_file = THREADS_DIR / f"{tid}.json"
        try:
            return json.loads(state_file.read_text())
        except Exception:
            return None

    async def _spawn_session(self, tid: str, thread):
        """Spawn a Claude Code session for a thread (new or resumed).

        If a previous session state file exists for this thread, resumes with
        --resume <session_id> in the original workspace so the user keeps
        their full conversation context.
        """
        if tid in self._resuming:
            return
        if len(self.sessions) >= self.max_sessions:
            logger.warning(f"Max sessions ({self.max_sessions}) reached")
            try:
                await thread.send(f"Max concurrent sessions ({self.max_sessions}) reached. Close a ticket first.")
            except Exception:
                pass
            return

        tmux_name = f"cct-{tid}"
        self._resuming.add(tid)

        try:
            # Kill any zombie tmux session with the same name
            if await self._session_alive(tmux_name):
                logger.info(f"Killing zombie tmux session {tmux_name}")
                await _run("tmux", "kill-session", "-t", tmux_name, timeout=5)

            # Unarchive thread if needed (e.g. user sent a message to an archived thread)
            if getattr(thread, 'archived', False):
                try:
                    await thread.edit(archived=False)
                except Exception:
                    pass

            # Check for previous session state → resume with context
            state = self._read_thread_state(tid)
            workspace = self.working_dir
            resume_id = None
            if state:
                workspace = state.get("workspace") or self.working_dir
                resume_id = state.get("session_id")

            cmd = [
                "tmux", "new-session", "-d", "-s", tmux_name, "-c", workspace,
                "env",
                f"TICKET_THREAD_ID={tid}",
                f"TICKET_CHANNEL_ID={self.channel_id}",
                "claude",
            ]
            if resume_id:
                cmd.extend(["--resume", resume_id])
                logger.info(f"Resuming session {resume_id} in {workspace}")
            cmd.extend([
                "--dangerously-skip-permissions",
                "--dangerously-load-development-channels", "plugin:discord-tickets@discord-tickets-cc",
            ])

            proc = await _run(*cmd)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.decode())
            self.sessions[tid] = tmux_name
            self.last_activity[tid] = time.time()
            self._cooldowns.pop(tid, None)
            logger.info(f"Session started: tmux:{tmux_name}")

            # Auto-accept the trust dialog and dev-channels warning
            # by sending Enter keystrokes after short delays
            async def _auto_accept(name: str = tmux_name):
                try:
                    for _ in range(3):
                        await asyncio.sleep(2)
                        await _run("tmux", "send-keys", "-t", name, "Enter")
                except Exception as e:
                    logger.warning(f"Auto-accept failed for {name}: {e}")
            asyncio.create_task(_auto_accept())

            logger.info(f"Attach with: tmux attach -t {tmux_name}")
        except Exception as e:
            logger.error(f"Failed to spawn tmux session: {e}")
            try:
                await thread.send("Failed to start Claude Code session.")
            except Exception:
                pass
        finally:
            self._resuming.discard(tid)

    async def _kill_session(self, thread_id: str):
        tmux_name = self.sessions.pop(thread_id, None)
        self.last_activity.pop(thread_id, None)
        if tmux_name:
            try:
                await _run("tmux", "kill-session", "-t", tmux_name, timeout=5)
                logger.info(f"Killed tmux session {tmux_name}")
            except Exception as e:
                logger.warning(f"Error killing session {tmux_name}: {e}")

    async def _session_alive(self, tmux_name: str) -> bool:
        try:
            proc = await _run("tmux", "has-session", "-t", tmux_name, timeout=5)
            return proc.returncode == 0
        except asyncio.TimeoutError:
            logger.warning(f"tmux has-session timed out for {tmux_name}")
            return True  # assume alive if we can't check

    async def _reaper_loop(self):
        consecutive_errors = 0
        while True:
            delay = min(30 * (2 ** consecutive_errors), 300)  # backoff: 30s → 60s → … → 5m max
            await asyncio.sleep(delay)
            try:
                await self._reap()
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Reaper error (streak {consecutive_errors}, next in {delay}s): {e}")

    async def _reap(self):
        now = time.time()

        # Clean stale cooldowns (older than 60s)
        for tid in list(self._cooldowns):
            if now - self._cooldowns[tid] > 60:
                del self._cooldowns[tid]

        for tid in list(self.sessions.keys()):
            tmux_name = self.sessions[tid]

            # Check if tmux session is still alive
            if not await self._session_alive(tmux_name):
                logger.info(f"Session {tmux_name} died — cleaning up (resumable)")
                self.sessions.pop(tid, None)
                self.last_activity.pop(tid, None)
                self._cooldowns[tid] = now
                await self._post(tid, "Session ended. Send a message to start a new one.")
                # Don't archive — allow resume when user sends a new message
                continue

            # Check timeout
            if self.timeout_minutes > 0:
                last = self.last_activity.get(tid, now)
                if now - last > self.timeout_minutes * 60:
                    logger.info(f"Session {tmux_name} timed out")
                    await self._post(tid, f"Session timed out ({self.timeout_minutes}m inactivity). Send a message to start a new one.")
                    await self._kill_session(tid)
                    self._cooldowns[tid] = now
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
        try:
            result = await _run("tmux", "list-sessions", "-F", "#{session_name}")
        except Exception:
            return
        if result.returncode == 0 and result.stdout:
            for line in result.stdout.decode().strip().split('\n'):
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
