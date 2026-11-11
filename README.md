# q

q (quota) is a unit for the cost of a Claude Code turn: **1 q is one week of a Claude Ultra account's usage limit.**
`N/q` is the inverse — how many such turns fit in one week. This plugin logs every turn and reports it as:

```
q: 82.1µq · 12.2k/q (~seed K)
```

## The unit

| unit | of 1 q | note |
|---|---|---|
| q  | 1 | one week of an Ultra account |
| cq | 1/100 | equals the weekly % shown by `/usage` |
| mq | 1/1,000 | |
| µq | 1/1,000,000 | |

A turn's weight counts what the limit counts, not dollars (cache reads dominate dollars but barely touch the limit), and
how fast the model drains it:

```
w  = r(model, speed) × [(input + cache_creation) + 5 · output + 0.01 · cache_read]   # every API response of the turn, subagents included
µq = w / K · 1,000,000
```

`r` is the model's rate against Opus 5.5 — its list price over Opus 5.5's:

| model | r |
|---|---|
| `claude-opus-5-5` | 1.0 |
| `claude-opus-5`, `claude-opus-4-8`, `claude-opus-4-7`, `claude-opus-4-6` | 1.25 |
| `claude-sonnet-5-5`, `claude-sonnet-5` | 0.5 |
| `claude-sonnet-4-6` | 0.75 |
| `claude-haiku-5-5` | 0.025 |
| `claude-haiku-4-5` | 0.25 |
| `claude-fable-5-2` | 2.0 |
| `claude-fable-5-1`, `claude-fable-5` | 2.5 |

A response with `speed: fast` counts twice (fast is billed at twice the standard rate). The model name is looked up with
the engine's tail taken off (`claude-opus-5-5[1m]` → `claude-opus-5-5`). A model not in the table counts at `r = 1`, and
`/q:q` says how many responses were counted that way.

`K` is the weight that fills one week. It starts at `5.4e8` (shown as `~seed K`) and should be calibrated per account.

The q unit, the weights and `r` are defined in `scripts/q.py` (`r` last revised 2026-11-10).

## Install

From GitHub (needs access to `amersh-io/q`):

```
/plugin marketplace add amersh-io/q
/plugin install q@q
```

Or for one session from a local checkout:

```
claude --plugin-dir ~/path/to/q
```

Requires `python3` (stdlib only).

## Use

- After every turn the line that closes it shows what the turn cost:

  ```
  ✻ Baked for 2s · done 4:36 PM · q ~79.9µq · 12.5k/q
  ```

  `~` means `K` is still the seed (see calibrate below). When subagents are still running as the turn ends, the engine
  writes `Waiting for … background agent` on that line instead, and the cost goes on the row beneath it. A follow-up turn
  the engine draws no closing line for shows nothing; its tokens are still in the ledger.
- A Stop hook logs every turn to the ledger (and also emits `q: <cost> · <N>/q`, which Claude Code shows only in the
  detailed transcript, `ctrl+o`).
- `/q:q` — totals for this session, today, and the last 7 days.
- `/q:q calibrate <pct>` — calibrate `K`. Look at the weekly % in `/usage` and run `/q:q calibrate 35` (say). Work for a while, then
  run it again with the new %. After a rise of 10 points or more (the % is a whole number, so a smaller rise moves
  `K` by up to half), `K = Δw / (Δ% / 100)`. If the weekly limit reset in between, the second call just re-anchors; so
  does an anchor set before `r` was counted.

### Where the number shows

The cost on the closing line is drawn by a hooks module (`hooks/q.ts`, a Claude Code "mod": early access, verified on
Claude Code 2.1.319 in the terminal). It needs a Claude Code build that loads hooks modules; an installed plugin's module
runs with no extra setting. Where modules do not load — an older build, an organization policy that turns plugin code off,
`claude -p`, or a surface other than the terminal (the desktop and mobile apps draw their own footer) — you see no number
on that line, but nothing else changes: the Stop hook still logs every turn, `/q:q` still reports, and the status line
below still works.

The module only draws; the Stop hook is the one that counts, so a turn is never logged twice. The module asks
`scripts/q.py label` for the text, one token group per model, so the weights, `r`, `K` and the format live in one
place. The engine's turn event carries no `speed`, so a fast turn is drawn at the standard rate on that line; the ledger
counts it at twice. Where a known plugin that draws the same number is loaded, the module stays silent.

Data lives in `~/.q/` (`ledger.jsonl` one line per turn, `state.json` for `K`, `sessions/` cursors). Set `Q_HOME` to move it.

### Status line

For the last turn's cost in the status line too (plugins cannot set the status line), point your own status line at the script
(`~/.claude/settings.json`; use the path of your checkout or of the installed plugin under `~/.claude/plugins/`):

```json
{ "statusLine": { "type": "command", "command": "python3 /path/to/q/scripts/q.py last" } }
```

## Limits

- It is an estimate. The weights (5 for output, 0.01 for cache reads) and `r` (list prices) are a model of the limit, not
  the vendor's formula.
- Only this machine's turns are counted. If the account is also used elsewhere (web, another machine), calibration
  underestimates `K`.
- `K` drifts between weekly resets; calibrate again now and then.
- Claude Code only — the claude.ai web/app usage is not seen.
- It does not read your Claude login token or call any API.

## Develop

```
python3 -m unittest discover -s tests
claude plugin validate .
```

Running a session with `claude --plugin-dir .` writes the module's types under `.claude-plugin/types/` (ignored by git);
after that `tsc -p .` type-checks `hooks/q.ts`.
