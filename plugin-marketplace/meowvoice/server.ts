#!/usr/bin/env bun
/**
 * MeowVoice MCP Bridge v2.0 — minimal stdio relay + HTTP inject endpoint.
 * Zero business logic. Process-level error handlers prevent crashes.
 */
import { timingSafeEqual } from 'node:crypto'
import { readFileSync, chmodSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js'

// Plugin-spawned MCP servers don't receive .mcp.json env blocks — load from file.
const ENV_FILE = join(homedir(), '.claude', 'channels', 'meowvoice', '.env')
try {
  chmodSync(ENV_FILE, 0o600)
  for (const line of readFileSync(ENV_FILE, 'utf8').split('\n')) {
    const m = line.match(/^(\w+)=(.*)$/)
    if (m && process.env[m[1]] === undefined) process.env[m[1]] = m[2]
  }
} catch {}

const PORT = parseInt(process.env.MEOWVOICE_PLUGIN_PORT ?? '8401', 10)
const PIN = process.env.MEOWVOICE_PIN ?? ''
const AUDIO_SERVER = process.env.MEOWVOICE_AUDIO_SERVER ?? 'http://127.0.0.1:8400'

if (!PIN) { process.stderr.write('meowvoice: MEOWVOICE_PIN required\n'); process.exit(1) }

const mcp = new Server(
  { name: 'meowvoice', version: '2.0.0' },
  {
    capabilities: { tools: {}, experimental: { 'claude/channel': {} } },
    instructions:
      'Voice commands from Kevin arrive as <channel source="plugin:meowvoice:meowvoice">. ' +
      'Reply with voice_reply (concise, 2-4 sentences, conversational, no markdown). ' +
      'Acknowledge long tasks first, then proceed.',
  },
)

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [{
    name: 'voice_reply',
    description: 'Send spoken reply to Kevin via TTS. Keep concise and conversational — this is spoken output.',
    inputSchema: {
      type: 'object' as const,
      properties: {
        text: { type: 'string', description: 'Reply text to speak' },
        message_id: { type: 'string', description: 'Correlation message_id from inbound voice command' },
      },
      required: ['text'],
    },
  }],
}))

mcp.setRequestHandler(CallToolRequestSchema, async (req) => {
  if (req.params.name !== 'voice_reply') {
    return { content: [{ type: 'text' as const, text: 'unknown tool' }], isError: true }
  }
  const args = (req.params.arguments ?? {}) as Record<string, unknown>
  try {
    const resp = await fetch(`${AUDIO_SERVER}/voice/reply-callback`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Voice-Pin': PIN },
      body: JSON.stringify({ text: args.text, message_id: args.message_id ?? '' }),
    })
    if (!resp.ok) return { content: [{ type: 'text' as const, text: `TTS failed: ${resp.status}` }], isError: true }
    return { content: [{ type: 'text' as const, text: 'Voice reply sent' }] }
  } catch (e: unknown) {
    return { content: [{ type: 'text' as const, text: `TTS error: ${e instanceof Error ? e.message : e}` }], isError: true }
  }
})

let msgCounter = 0

Bun.serve({
  port: PORT,
  async fetch(request: Request): Promise<Response> {
    try {
      const url = new URL(request.url)
      if (url.pathname === '/health') return Response.json({ status: 'ok', plugin: 'meowvoice', port: PORT })
      if (request.method !== 'POST' || url.pathname !== '/inject') return Response.json({ error: 'Not found' }, { status: 404 })
      const pinHeader = request.headers.get('x-voice-pin') ?? ''
      if (pinHeader.length !== PIN.length || !timingSafeEqual(Buffer.from(pinHeader), Buffer.from(PIN)))
        return Response.json({ error: 'Invalid PIN' }, { status: 401 })

      const body = (await request.json()) as { text?: string; user?: string }
      const text = body.text?.trim()
      if (!text) return Response.json({ error: 'Empty text' }, { status: 400 })

      const messageId = `voice-${Date.now()}-${++msgCounter}`
      await mcp.notification({
        method: 'notifications/claude/channel',
        params: {
          content: text,
          meta: { chat_id: 'voice', message_id: messageId, user: body.user ?? 'Kevin', user_id: '524111626438180865', ts: new Date().toISOString() },
        },
      })
      process.stderr.write(`meowvoice: injected: ${text.slice(0, 60)}\n`)
      return Response.json({ ok: true, message_id: messageId })
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      process.stderr.write(`meowvoice: error: ${msg}\n`)
      return Response.json({ error: msg }, { status: 500 })
    }
  },
})

process.on('uncaughtException', (err) => process.stderr.write(`meowvoice: uncaught: ${err.message}\n`))
process.on('unhandledRejection', (reason) => process.stderr.write(`meowvoice: unhandled: ${reason}\n`))

// Parent 死亡 → stdin EOF → 自動退出，防止孤兒進程
process.stdin.on('end', () => { process.stderr.write('meowvoice: stdin EOF, parent gone — exiting\n'); process.exit(0) })
process.stdin.on('error', () => { process.stderr.write('meowvoice: stdin error — exiting\n'); process.exit(1) })

process.stderr.write(`meowvoice: bridge v2.0 ready on :${PORT}\n`)
await mcp.connect(new StdioServerTransport())
