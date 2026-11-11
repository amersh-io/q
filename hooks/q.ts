// Draws each turn's cost in q at the end of the line that closes the turn (`Baked for 14s · done 3:18 PM · q ~82µq · 12.2k/q`).
//
// The number comes from `scripts/q.py label` — the one copy of the weights, the model rates, K and the label — fed the
// token counts and model `turn.complete` reports, one group per model. `turn.complete` carries no `speed`, so a fast turn
// is drawn at the standard rate here; the ledger (from the transcript) counts it at twice. This module only draws: the ledger stays the Stop hook's (hooks.json), so nothing is
// counted twice. Drawn on the screen only: not in the transcript, not in the model's context.
//
// Skip when the amx plugin already draws it, so the line is not drawn twice.

import type { EngineInterface, Register } from 'claude-code'

type Tokens = Readonly<{
  input_tokens: number
  output_tokens: number
  cache_read_input_tokens: number
  cache_creation_input_tokens: number
}>
const ZERO: Tokens = { input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }

// Labels by the turn's duration: the closing line's props are only its word and duration, so that is how a line
// finds its turn — the same number `turn.complete` reports.
// `below`: subagents were still running when the turn ended, so the engine draws its waiting text on the line
// (`Waiting for 1 background agent · …`) instead of the duration — the label goes on a row of its own beneath it.
const LABELS = new Map<number, Readonly<{ label: string; at: number; below: boolean }>>()
const KEPT = 200

// Subagent runs end with their own `turn.complete` (agentId set) and their tokens are not in the main turn's usage;
// they are added to the next main turn, as the Stop hook adds subagent transcripts. Kept per model (a Haiku subagent
// weighs 1/40 of an Opus turn), keyed by the model the API reported.
let pending = new Map<string, Tokens>()
const plus = (a: Tokens, b: Tokens): Tokens => ({
  input_tokens: a.input_tokens + (b.input_tokens ?? 0),
  output_tokens: a.output_tokens + (b.output_tokens ?? 0),
  cache_read_input_tokens: a.cache_read_input_tokens + (b.cache_read_input_tokens ?? 0),
  cache_creation_input_tokens: a.cache_creation_input_tokens + (b.cache_creation_input_tokens ?? 0),
})

// True when the amx plugin is loaded: it draws this number already.
let amxDraws = false

// Subagent runs under way (their turn.start seen, their turn.complete not yet).
const running = new Set<string>()

const merge = (m: Map<string, Tokens>, model: string, u: Tokens): void => {
  m.set(model, plus(m.get(model) ?? ZERO, u))
}

async function compute($: EngineInterface, durationMs: number, groups: Map<string, Tokens>, below: boolean): Promise<void> {
  const args = [...groups].flatMap(([model, u], i) => [
    ...(i > 0 ? ['+'] : []),
    ...[u.input_tokens, u.cache_creation_input_tokens, u.cache_read_input_tokens, u.output_tokens].map(String),
    ...(model ? [model] : []),
  ])
  const argv = ['python3', `${$.plugin.root}/scripts/q.py`, 'label', ...args]
  let label = ''
  try {
    const r = await $.process.run(argv, { timeoutMs: 5000 })
    label = r.exitCode === 0 ? r.stdout.trim() : ''
  } catch {
    return
  }
  if (!label) return
  LABELS.set(durationMs, { label, at: Date.now(), below })
  if (LABELS.size > KEPT) LABELS.delete(LABELS.keys().next().value as number)
  // The line may already be drawn without it (the answer is cached per props): draw it again.
  $.ui.invalidate('ui.render')
}

// The engine draws the closing line as one element as wide as the terminal, so the label is laid over it at the
// column its words end. The props carry the word and duration only; the rest is the engine's own text — a 2-column
// glyph, ` for <duration>` and ` · done <time>` (in the engine's locale, which this runtime may not share: the
// widest form is taken). Erring wide leaves a space or two; erring narrow would cut the engine's words.
const WIDE: ReadonlyArray<readonly [number, number]> = [
  [0x1100, 0x115f], [0x2e80, 0xa4cf], [0xac00, 0xd7a3], [0xf900, 0xfaff], [0xfe30, 0xfe6f],
  [0xff00, 0xff60], [0xffe0, 0xffe6], [0x1f300, 0x1f64f], [0x1f900, 0x1f9ff], [0x20000, 0x3fffd],
]
const cells = (c: string): number => {
  const u = c.codePointAt(0) ?? 0
  return WIDE.some(([lo, hi]) => u >= lo && u <= hi) ? 2 : 1
}
const cols = (s: string): number => [...s].reduce((n, c) => n + cells(c), 0)
function lineCols(word: string, durationMs: number, at: number): number {
  const s = Math.ceil(durationMs / 1000)
  const dur = s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m ${s % 60}s`
  const d = new Date(at)
  const h12 = d.getHours() % 12 || 12
  const mm = String(d.getMinutes()).padStart(2, '0')
  const time = Math.max(cols(`오후 ${h12}:${mm}`), cols(`${h12}:${mm} PM`), cols(`${String(d.getHours()).padStart(2, '0')}:${mm}`))
  return 2 + cols(word) + cols(` for ${dur}`) + cols(' · done ') + time + 1
}

export const register: Register = (on) => {
  on('session.start', async ($, e, next) => {
    const r = await next(e)
    try {
      amxDraws = (await $.command.list()).some((c) => c.plugin === 'amx')
    } catch {
      amxDraws = false
    }
    return r
  })

  on('turn.start', async ($, e, next) => {
    if (e.agentId) running.add(e.agentId)
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    if (e.agentId) running.delete(e.agentId)
    if (amxDraws || !e.usage) return r
    merge(pending, e.usage.model ?? '', e.usage)
    if (e.agentId) return r
    const groups = pending
    pending = new Map()
    void compute($, e.durationMs, groups, running.size > 0)
    return r
  })

  // A line with more than the word and duration (the agents it waits for) is longer than the guess: left alone.
  on('ui.render', { component: 'TurnDuration' }, ($, e, next) => {
    const props = e.props as { word?: string; durationMs?: number; waitingFor?: unknown }
    const found = LABELS.get(props.durationMs ?? -1)
    if (found === undefined || props.waitingFor !== undefined || props.word === undefined) return next(e)
    const left = lineCols(props.word, props.durationMs ?? 0, found.at)
    const wrap = (drawn: unknown): never => {
      const { Box, Text } = $.ui.resolve(e)
      if (found.below) {
        return h(Box, { flexDirection: 'column' }, drawn, h(Text, { key: 'q', dimColor: true, wrap: 'truncate' }, `  · ${found.label}`)) as never
      }
      return h(
        Box,
        { flexDirection: 'column' },
        drawn,
        h(Box, { key: 'q', position: 'absolute', left, bottom: 0 }, h(Text, { dimColor: true, wrap: 'truncate' }, `· ${found.label}`)),
      ) as never
    }
    const drawn = next(e) as unknown
    return drawn instanceof Promise ? (drawn.then(wrap) as never) : wrap(drawn)
  })
}
