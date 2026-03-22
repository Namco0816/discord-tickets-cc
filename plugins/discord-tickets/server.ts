#!/usr/bin/env bun
/**
 * Discord Ticket Channel for Claude Code.
 *
 * Thread-scoped MCP server: each Claude Code session maps to exactly one
 * Discord forum thread. On startup it either creates a new forum post or
 * attaches to an existing thread (when spawned by the orchestrator).
 *
 * Env vars:
 *   DISCORD_BOT_TOKEN     — required, or loaded from ~/.claude/channels/discord/.env
 *   TICKET_CHANNEL_ID     — required, the forum channel where tickets live
 *   TICKET_THREAD_ID      — optional, attach to existing thread (reverse direction)
 *   TICKET_SESSION_NAME   — optional, display name for the forum post title
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from '@modelcontextprotocol/sdk/types.js'
import {
  Client,
  GatewayIntentBits,
  Partials,
  ChannelType,
  type Message,
  type Attachment,
  type ThreadChannel,
  type ForumChannel,
} from 'discord.js'
import { readFileSync, writeFileSync, mkdirSync, statSync, realpathSync, chmodSync, rmSync } from 'fs'
import { homedir } from 'os'
import { join, sep } from 'path'

// ---- Config & env ----

const STATE_DIR = process.env.DISCORD_STATE_DIR ?? join(homedir(), '.claude', 'channels', 'discord')
const ENV_FILE = join(STATE_DIR, '.env')
const INBOX_DIR = join(STATE_DIR, 'inbox')

// Load token from .env if not in environment
try {
  chmodSync(ENV_FILE, 0o600)
  for (const line of readFileSync(ENV_FILE, 'utf8').split('\n')) {
    const m = line.match(/^(\w+)=(.*)$/)
    if (m && process.env[m[1]] === undefined) process.env[m[1]] = m[2]
  }
} catch {}

const TOKEN = process.env.DISCORD_BOT_TOKEN
const TICKET_CHANNEL_ID = process.env.TICKET_CHANNEL_ID
const TICKET_THREAD_ID = process.env.TICKET_THREAD_ID
const TICKET_SESSION_NAME = process.env.TICKET_SESSION_NAME

if (!TOKEN) {
  process.stderr.write(
    `cc-ticket: DISCORD_BOT_TOKEN required\n` +
    `  set in ${ENV_FILE} or as env var\n`,
  )
  process.exit(1)
}

if (!TICKET_CHANNEL_ID) {
  process.stderr.write(
    `cc-ticket: TICKET_CHANNEL_ID required\n` +
    `  set to the Discord forum channel ID where tickets should be created\n`,
  )
  process.exit(1)
}

// Safety net — keep serving on unhandled errors
process.on('unhandledRejection', err => {
  process.stderr.write(`cc-ticket: unhandled rejection: ${err}\n`)
})
process.on('uncaughtException', err => {
  process.stderr.write(`cc-ticket: uncaught exception: ${err}\n`)
})

// ---- Discord client ----

const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent,
  ],
  partials: [Partials.Channel],
})

// The single thread this session communicates through
let ticketThread: ThreadChannel | null = null
const sessionStartTime = Date.now()

// Track our sent message IDs for reply-to detection
const recentSentIds = new Set<string>()
const RECENT_SENT_CAP = 200

function noteSent(id: string): void {
  recentSentIds.add(id)
  if (recentSentIds.size > RECENT_SENT_CAP) {
    const first = recentSentIds.values().next().value
    if (first) recentSentIds.delete(first)
  }
}

// ---- Message chunking (from stock plugin) ----

const MAX_CHUNK_LIMIT = 2000
const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

function chunk(text: string, limit: number, mode: 'length' | 'newline'): string[] {
  if (text.length <= limit) return [text]
  const out: string[] = []
  let rest = text
  while (rest.length > limit) {
    let cut = limit
    if (mode === 'newline') {
      const para = rest.lastIndexOf('\n\n', limit)
      const line = rest.lastIndexOf('\n', limit)
      const space = rest.lastIndexOf(' ', limit)
      cut = para > limit / 2 ? para : line > limit / 2 ? line : space > 0 ? space : limit
    }
    out.push(rest.slice(0, cut))
    rest = rest.slice(cut).replace(/^\n+/, '')
  }
  if (rest) out.push(rest)
  return out
}

// ---- File safety ----

function assertSendable(f: string): void {
  let real, stateReal: string
  try {
    real = realpathSync(f)
    stateReal = realpathSync(STATE_DIR)
  } catch { return }
  const inbox = join(stateReal, 'inbox')
  if (real.startsWith(stateReal + sep) && !real.startsWith(inbox + sep)) {
    throw new Error(`refusing to send channel state: ${f}`)
  }
}

// ---- Attachment handling ----

async function downloadAttachment(att: Attachment): Promise<string> {
  if (att.size > MAX_ATTACHMENT_BYTES) {
    throw new Error(`attachment too large: ${(att.size / 1024 / 1024).toFixed(1)}MB, max ${MAX_ATTACHMENT_BYTES / 1024 / 1024}MB`)
  }
  const res = await fetch(att.url)
  const buf = Buffer.from(await res.arrayBuffer())
  const name = att.name ?? `${att.id}`
  const rawExt = name.includes('.') ? name.slice(name.lastIndexOf('.') + 1) : 'bin'
  const ext = rawExt.replace(/[^a-zA-Z0-9]/g, '') || 'bin'
  const path = join(INBOX_DIR, `${Date.now()}-${att.id}.${ext}`)
  mkdirSync(INBOX_DIR, { recursive: true })
  writeFileSync(path, buf)
  return path
}

function safeAttName(att: Attachment): string {
  return (att.name ?? att.id).replace(/[\[\]\r\n;]/g, '_')
}

// ---- Resolve the user's working directory ----
// The MCP server runs with --cwd pointing to the plugin dir, so process.cwd()
// is wrong. Read the parent process's cwd (claude) from /proc.
function getUserCwd(): string {
  if (process.env.CC_TICKET_CWD) return process.env.CC_TICKET_CWD
  try {
    const ppid = process.ppid
    // Walk up to find the claude process (might be grandparent if bun forks)
    for (const pid of [ppid, readFileSync(`/proc/${ppid}/stat`, 'utf8').split(' ')[3]]) {
      try {
        const cwd = readFileSync(`/proc/${pid}/cwd`, 'utf8').replace(/\0/g, '')
        if (cwd && !cwd.includes('plugins/cache')) return cwd
      } catch {
        try {
          // readlink approach
          const { execSync } = require('child_process')
          const cwd = execSync(`readlink /proc/${pid}/cwd`, { encoding: 'utf8' }).trim()
          if (cwd && !cwd.includes('plugins/cache')) return cwd
        } catch {}
      }
    }
  } catch {}
  return process.cwd()
}

const USER_CWD = getUserCwd()

// ---- Coordination file ----
// Write thread ID so external scripts (permission hooks) can find it.
const COORD_FILE = process.env.CC_TICKET_COORD_FILE ?? join(STATE_DIR, 'ticket_thread_id')

function writeCoordFile(threadId: string): void {
  try {
    mkdirSync(STATE_DIR, { recursive: true })
    writeFileSync(COORD_FILE, threadId + '\n')
    process.stderr.write(`cc-ticket: wrote thread ID to ${COORD_FILE}\n`)
  } catch (err) {
    process.stderr.write(`cc-ticket: failed to write coord file: ${err}\n`)
  }
}

// ---- Session state (for resume) ----
// Persist session ID + workspace per thread so the orchestrator can resume
// with full context instead of spawning a blank session.
const THREADS_DIR = join(STATE_DIR, 'threads')

function writeSessionState(threadId: string): void {
  const sessionId = process.env.CLAUDE_CODE_SESSION_ID
  if (!sessionId) {
    process.stderr.write('cc-ticket: CLAUDE_CODE_SESSION_ID not set — resume will start fresh\n')
    return
  }
  try {
    mkdirSync(THREADS_DIR, { recursive: true })
    const state = {
      session_id: sessionId,
      workspace: USER_CWD,
      updated_at: Date.now(),
    }
    writeFileSync(join(THREADS_DIR, `${threadId}.json`), JSON.stringify(state, null, 2) + '\n')
    process.stderr.write(`cc-ticket: saved session state for thread ${threadId} (session ${sessionId})\n`)
  } catch (err) {
    process.stderr.write(`cc-ticket: failed to write session state: ${err}\n`)
  }
}

// ---- Thread lifecycle ----

async function createOrAttachThread(): Promise<ThreadChannel> {
  if (TICKET_THREAD_ID) {
    // Reverse direction: attach to existing thread
    const ch = await client.channels.fetch(TICKET_THREAD_ID)
    if (!ch || !ch.isThread()) {
      throw new Error(`TICKET_THREAD_ID ${TICKET_THREAD_ID} is not a valid thread`)
    }
    const thread = ch as ThreadChannel
    // Unarchive if needed
    if (thread.archived) {
      await thread.setArchived(false)
    }
    const cwd = USER_CWD
    await thread.send(
      `Claude Code session connected.\n` +
      `\uD83D\uDCC1 **${cwd}**\n` +
      `Send messages here to interact. Permission requests appear with reaction buttons.`
    )
    writeCoordFile(thread.id)
    process.stderr.write(`cc-ticket: attached to existing thread ${thread.id} (${thread.name})\n`)
    return thread
  }

  // Forward direction: create new forum post
  const forumChannel = await client.channels.fetch(TICKET_CHANNEL_ID!)
  if (!forumChannel) {
    throw new Error(`TICKET_CHANNEL_ID ${TICKET_CHANNEL_ID} not found`)
  }

  const now = new Date()
  const title = TICKET_SESSION_NAME || `Claude Session — ${now.toISOString().slice(0, 16).replace('T', ' ')}`

  if (forumChannel.type === ChannelType.GuildForum) {
    const forum = forumChannel as ForumChannel

    // Find "Active" tag if it exists
    const activeTag = forum.availableTags.find(t => t.name.toLowerCase() === 'active')

    const cwd = USER_CWD
    const thread = await forum.threads.create({
      name: title,
      message: {
        content:
          `Claude Code session started.\n` +
          `\uD83D\uDCC1 **${cwd}**\n` +
          `Send messages here to interact. Permission requests appear with reaction buttons.`
      },
      ...(activeTag ? { appliedTags: [activeTag.id] } : {}),
    })

    writeCoordFile(thread.id)
    process.stderr.write(`cc-ticket: created forum post ${thread.id} (${title})\n`)
    return thread
  }

  // Fallback: regular text channel — create a thread on a starter message
  if (forumChannel.type === ChannelType.GuildText && 'send' in forumChannel) {
    const starter = await (forumChannel as any).send(`**${title}**`)
    const thread = await starter.startThread({
      name: title,
    })
    const cwd = USER_CWD
    await thread.send(
      `Claude Code session started.\n` +
      `\uD83D\uDCC1 **${cwd}**\n` +
      `Send messages here to interact. Permission requests appear with reaction buttons.`
    )
    writeCoordFile(thread.id)
    process.stderr.write(`cc-ticket: created thread ${thread.id} (${title})\n`)
    return thread
  }

  throw new Error(`TICKET_CHANNEL_ID ${TICKET_CHANNEL_ID} is not a forum or text channel (type: ${forumChannel.type})`)
}

async function closeThread(): Promise<void> {
  if (!ticketThread) return

  // Calculate session duration
  const elapsed = Math.round((Date.now() - sessionStartTime) / 1000)
  const mins = Math.floor(elapsed / 60)
  const secs = elapsed % 60
  const duration = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`

  try {
    await ticketThread.send(`Session ended. (ran for ${duration})`)
  } catch (err) {
    process.stderr.write(`cc-ticket: failed to send close message: ${err}\n`)
  }

  // Apply "Completed" tag if this is a forum post
  try {
    if (ticketThread.parent?.type === ChannelType.GuildForum) {
      const forum = ticketThread.parent as ForumChannel
      const completedTag = forum.availableTags.find(t => t.name.toLowerCase() === 'completed')
      if (completedTag) {
        const currentTags = ticketThread.appliedTags.filter(
          t => !forum.availableTags.find(at => at.id === t && at.name.toLowerCase() === 'active')
        )
        await ticketThread.setAppliedTags([...currentTags, completedTag.id])
      }
    }
  } catch {}

  // Archive thread — gracefully handle missing permissions
  try {
    await ticketThread.setArchived(true)
    process.stderr.write(`cc-ticket: archived thread ${ticketThread.id}\n`)
  } catch (err) {
    // 403 = Missing Access / Manage Threads permission — skip silently
    process.stderr.write(`cc-ticket: couldn't archive thread (bot may lack Manage Threads permission)\n`)
  }
}

// ---- MCP Server ----

const mcp = new Server(
  { name: 'cc-ticket-discord', version: '0.0.1' },
  {
    capabilities: { tools: {}, experimental: { 'claude/channel': {} } },
    instructions: [
      'The sender reads Discord, not this session. Anything you want them to see must go through the reply tool — your transcript output never reaches their chat.',
      '',
      'This session is connected to a Discord forum thread (ticket). All messages in the thread are delivered to you, and your replies go back to the same thread.',
      '',
      'Messages from Discord arrive as <channel source="discord" chat_id="..." message_id="..." user="..." ts="...">. If the tag has attachment_count, the attachments attribute lists name/type/size — call download_attachment(chat_id, message_id) to fetch them.',
      '',
      'reply accepts file paths (files: ["/abs/path.png"]) for attachments. Use react to add emoji reactions, and edit_message for interim progress updates. Edits don\'t trigger push notifications — when a long task completes, send a new reply so the user\'s device pings.',
      '',
      'fetch_messages pulls recent thread history. Discord\'s search API isn\'t available to bots — if the user asks you to find an old message, fetch more history or ask them roughly when it was.',
    ].join('\n'),
  },
)

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'reply',
      description:
        'Reply in the ticket thread. Optionally pass reply_to (message_id) for threading, and files (absolute paths) to attach.',
      inputSchema: {
        type: 'object',
        properties: {
          chat_id: { type: 'string', description: 'Ignored — always sends to the ticket thread. Kept for protocol compatibility.' },
          text: { type: 'string' },
          reply_to: {
            type: 'string',
            description: 'Message ID to thread under.',
          },
          files: {
            type: 'array',
            items: { type: 'string' },
            description: 'Absolute file paths to attach (images, logs, etc). Max 10 files, 25MB each.',
          },
        },
        required: ['text'],
      },
    },
    {
      name: 'react',
      description: 'Add an emoji reaction to a message in the ticket thread.',
      inputSchema: {
        type: 'object',
        properties: {
          chat_id: { type: 'string', description: 'Ignored — uses ticket thread.' },
          message_id: { type: 'string' },
          emoji: { type: 'string' },
        },
        required: ['message_id', 'emoji'],
      },
    },
    {
      name: 'edit_message',
      description: 'Edit a bot message in the ticket thread. Edits don\'t trigger push notifications.',
      inputSchema: {
        type: 'object',
        properties: {
          chat_id: { type: 'string', description: 'Ignored — uses ticket thread.' },
          message_id: { type: 'string' },
          text: { type: 'string' },
        },
        required: ['message_id', 'text'],
      },
    },
    {
      name: 'download_attachment',
      description: 'Download attachments from a message in the ticket thread.',
      inputSchema: {
        type: 'object',
        properties: {
          chat_id: { type: 'string', description: 'Ignored — uses ticket thread.' },
          message_id: { type: 'string' },
        },
        required: ['message_id'],
      },
    },
    {
      name: 'fetch_messages',
      description: 'Fetch recent messages from the ticket thread.',
      inputSchema: {
        type: 'object',
        properties: {
          channel: { type: 'string', description: 'Ignored — uses ticket thread.' },
          limit: {
            type: 'number',
            description: 'Max messages (default 20, Discord caps at 100).',
          },
        },
      },
    },
  ],
}))

mcp.setRequestHandler(CallToolRequestSchema, async req => {
  const args = (req.params.arguments ?? {}) as Record<string, unknown>
  if (!ticketThread) {
    return {
      content: [{ type: 'text', text: 'ticket thread not ready yet — still connecting' }],
      isError: true,
    }
  }

  try {
    switch (req.params.name) {
      case 'reply': {
        // Stop typing indicator — we're about to send the actual reply
        stopTyping()
        const text = args.text as string
        const reply_to = args.reply_to as string | undefined
        const files = (args.files as string[] | undefined) ?? []

        for (const f of files) {
          assertSendable(f)
          const st = statSync(f)
          if (st.size > MAX_ATTACHMENT_BYTES) {
            throw new Error(`file too large: ${f} (${(st.size / 1024 / 1024).toFixed(1)}MB, max 25MB)`)
          }
        }
        if (files.length > 10) throw new Error('Discord allows max 10 attachments per message')

        const chunks = chunk(text, MAX_CHUNK_LIMIT, 'newline')
        const sentIds: string[] = []

        try {
          for (let i = 0; i < chunks.length; i++) {
            const shouldReplyTo = reply_to != null && i === 0
            const sent = await ticketThread.send({
              content: chunks[i],
              ...(i === 0 && files.length > 0 ? { files } : {}),
              ...(shouldReplyTo
                ? { reply: { messageReference: reply_to, failIfNotExists: false } }
                : {}),
            })
            noteSent(sent.id)
            sentIds.push(sent.id)
          }
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err)
          throw new Error(`reply failed after ${sentIds.length} of ${chunks.length} chunk(s) sent: ${msg}`)
        }

        const result =
          sentIds.length === 1
            ? `sent (id: ${sentIds[0]})`
            : `sent ${sentIds.length} parts (ids: ${sentIds.join(', ')})`
        return { content: [{ type: 'text', text: result }] }
      }

      case 'fetch_messages': {
        const limit = Math.min((args.limit as number) ?? 20, 100)
        const msgs = await ticketThread.messages.fetch({ limit })
        const me = client.user?.id
        const arr = [...msgs.values()].reverse()
        const out =
          arr.length === 0
            ? '(no messages)'
            : arr
                .map(m => {
                  const who = m.author.id === me ? 'me' : m.author.username
                  const atts = m.attachments.size > 0 ? ` +${m.attachments.size}att` : ''
                  const text = m.content.replace(/[\r\n]+/g, ' \u23ce ')
                  return `[${m.createdAt.toISOString()}] ${who}: ${text}  (id: ${m.id}${atts})`
                })
                .join('\n')
        return { content: [{ type: 'text', text: out }] }
      }

      case 'react': {
        const msg = await ticketThread.messages.fetch(args.message_id as string)
        await msg.react(args.emoji as string)
        return { content: [{ type: 'text', text: 'reacted' }] }
      }

      case 'edit_message': {
        const msg = await ticketThread.messages.fetch(args.message_id as string)
        const edited = await msg.edit(args.text as string)
        return { content: [{ type: 'text', text: `edited (id: ${edited.id})` }] }
      }

      case 'download_attachment': {
        const msg = await ticketThread.messages.fetch(args.message_id as string)
        if (msg.attachments.size === 0) {
          return { content: [{ type: 'text', text: 'message has no attachments' }] }
        }
        const lines: string[] = []
        for (const att of msg.attachments.values()) {
          const path = await downloadAttachment(att)
          const kb = (att.size / 1024).toFixed(0)
          lines.push(`  ${path}  (${safeAttName(att)}, ${att.contentType ?? 'unknown'}, ${kb}KB)`)
        }
        return {
          content: [{ type: 'text', text: `downloaded ${lines.length} attachment(s):\n${lines.join('\n')}` }],
        }
      }

      default:
        return {
          content: [{ type: 'text', text: `unknown tool: ${req.params.name}` }],
          isError: true,
        }
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err)
    return {
      content: [{ type: 'text', text: `${req.params.name} failed: ${msg}` }],
      isError: true,
    }
  }
})

// ---- Typing indicator loop ----
// Discord's typing indicator lasts ~10s. Keep refreshing it while Claude is
// processing so the user knows the session is alive.
let typingInterval: ReturnType<typeof setInterval> | null = null

function startTyping(): void {
  if (!ticketThread) return
  // Send immediately
  void ticketThread.sendTyping().catch(() => {})
  // Refresh every 8s (before the 10s expiry)
  stopTyping()
  typingInterval = setInterval(() => {
    if (ticketThread) {
      void ticketThread.sendTyping().catch(() => {})
    }
  }, 8000)
}

function stopTyping(): void {
  if (typingInterval) {
    clearInterval(typingInterval)
    typingInterval = null
  }
}

// ---- Inbound message handling ----

client.on('messageCreate', msg => {
  if (msg.author.bot) return
  if (!ticketThread || msg.channelId !== ticketThread.id) return
  handleInbound(msg).catch(e => process.stderr.write(`cc-ticket: handleInbound failed: ${e}\n`))
})

async function handleInbound(msg: Message): Promise<void> {
  // Start persistent typing indicator (cleared when we reply)
  startTyping()

  // Ack reaction
  void msg.react('\uD83D\uDC40').catch(() => {})

  // Attachment metadata
  const atts: string[] = []
  for (const att of msg.attachments.values()) {
    const kb = (att.size / 1024).toFixed(0)
    atts.push(`${safeAttName(att)} (${att.contentType ?? 'unknown'}, ${kb}KB)`)
  }

  const content = msg.content || (atts.length > 0 ? '(attachment)' : '')

  mcp.notification({
    method: 'notifications/claude/channel',
    params: {
      content,
      meta: {
        chat_id: msg.channelId,
        message_id: msg.id,
        user: msg.author.username,
        user_id: msg.author.id,
        ts: msg.createdAt.toISOString(),
        ...(atts.length > 0 ? { attachment_count: String(atts.length), attachments: atts.join('; ') } : {}),
      },
    },
  }).catch(err => {
    process.stderr.write(`cc-ticket: failed to deliver inbound to Claude: ${err}\n`)
  })
}

// ---- Startup & shutdown ----

await mcp.connect(new StdioServerTransport())

let shuttingDown = false
async function shutdown(): Promise<void> {
  if (shuttingDown) return
  shuttingDown = true
  process.stderr.write('cc-ticket: shutting down\n')
  stopTyping()
  // Remove coord file so stale thread IDs don't leak to future sessions
  try { rmSync(COORD_FILE) } catch {}
  await closeThread()
  setTimeout(() => process.exit(0), 2000)
  void Promise.resolve(client.destroy()).finally(() => process.exit(0))
}
process.stdin.on('end', shutdown)
process.stdin.on('close', shutdown)
process.on('SIGTERM', shutdown)
process.on('SIGINT', shutdown)

client.on('error', err => {
  process.stderr.write(`cc-ticket: client error: ${err}\n`)
})

client.once('ready', async c => {
  process.stderr.write(`cc-ticket: gateway connected as ${c.user.tag}\n`)
  try {
    ticketThread = await createOrAttachThread()
    writeSessionState(ticketThread.id)
    process.stderr.write(`cc-ticket: ready — thread ${ticketThread.id}\n`)
  } catch (err) {
    process.stderr.write(`cc-ticket: failed to create/attach thread: ${err}\n`)
    process.exit(1)
  }
})

client.login(TOKEN).catch(err => {
  process.stderr.write(`cc-ticket: login failed: ${err}\n`)
  process.exit(1)
})
