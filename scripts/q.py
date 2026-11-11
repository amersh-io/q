#!/usr/bin/env python3
"""q — measure Claude Code turn cost in q, where 1 q is one week of a Claude Ultra account's usage limit.

1 q = 100 cq = 1000 mq = 1,000,000 µq. cq equals the weekly % shown by /usage.
A response's weight is w = r(model, speed) · [(input + cache_creation) + 5·output + 0.01·cache_read] tokens;
µq = w / K · 1e6, where K is the weight that fills one week (seeded, then calibrated with `/q:q calibrate <pct>`).
r is the model's rate against Opus 5.5 (MODEL_WEIGHTS), doubled for `speed: fast`; a model not in the table counts at 1.

r was last revised 2026-11-10. Stdlib only.

Usage:
  q.py stop                      Stop hook: read hook JSON on stdin, log the turn, print a systemMessage.
  q.py report [--session ID]     Totals for the session, today and the last 7 days.
  q.py calibrate <pct>           Anchor or update K from the weekly % shown by /usage.
  q.py last                      The last logged turn, for a status line.
  q.py label <in> <cw> <cr> <out> [<model>] [+ <in> <cw> <cr> <out> [<model>] …]
                                 One turn's label from its token counts (input, cache write, cache read, output) and
                                 the model that spent them, one group per model, for the hooks module that draws it
                                 on the turn's closing line.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import sys
import time

MICRO_PER_Q = 1_000_000.0
OUT_WEIGHT = 5.0
CACHE_READ_WEIGHT = 0.01
SEED_K = 5.4e8  # a measured coefficient for an Ultra account
MIN_DELTA_PCT = 10.0  # smaller rises carry ±50% rounding noise in K (the % is a whole number)

# A model's rate against Opus 5.5 — how fast it drains the weekly limit (its list price over Opus 5.5's).
# Revised 2026-11-10.
MODEL_WEIGHTS = {
    "claude-opus-5-5": 1.0,
    "claude-opus-5": 1.25,
    "claude-opus-4-8": 1.25,
    "claude-opus-4-7": 1.25,
    "claude-opus-4-6": 1.25,
    "claude-sonnet-5-5": 0.5,
    "claude-sonnet-5": 0.5,
    "claude-sonnet-4-6": 0.75,
    "claude-haiku-5-5": 0.025,
    "claude-haiku-4-5": 0.25,
    "claude-fable-5-2": 2.0,
    "claude-fable-5-1": 2.5,
    "claude-fable-5": 2.5,
}
FAST_WEIGHT = 2.0  # `speed: fast` is billed at twice the standard rate
WAIT_S = float(os.environ.get("Q_WAIT_S", "3"))

HOME = os.environ.get("Q_HOME") or os.path.join(os.path.expanduser("~"), ".q")


# ---------------------------------------------------------------- pure parts


def model_key(model: str) -> str:
    """The table's name for a model: the engine's tail (`[1m]`) taken off."""
    return model.split("[", 1)[0].strip()


def rate(model: str | None, speed: str | None = None) -> float | None:
    """r of one response — None for a model not in the table (or none given); the caller counts it at 1."""
    if not model:
        return None
    key = model_key(model)
    r = MODEL_WEIGHTS.get(key)
    if r is None:
        return None
    return r * FAST_WEIGHT if speed == "fast" and key.startswith("claude-") else r


def weight(usage: dict) -> float:
    """Limit-weighted tokens of one API response, before r."""
    inp = usage.get("input_tokens", 0) or 0
    cw = usage.get("cache_creation_input_tokens", 0) or 0
    cr = usage.get("cache_read_input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    return (inp + cw) + OUT_WEIGHT * out + CACHE_READ_WEIGHT * cr


def _trim(s: str) -> str:
    return s.rstrip("0").rstrip(".") if "." in s else s


def _short(v: float) -> str:
    a = abs(v)
    if a == 0:
        return "0"
    if a < 10:
        return _trim(f"{v:.2f}")
    if a < 100:
        return _trim(f"{v:.1f}")
    return f"{v:.0f}"


def _sig3(v: float) -> str:
    unit = ""
    if v >= 1e6:
        v, unit = v / 1e6, "M"
    elif v >= 1e3:
        v, unit = v / 1e3, "k"
    decimals = 0 if v >= 100 else 1 if v >= 10 else 2 if v >= 1 else 3
    return _trim(f"{v:.{decimals}f}") + unit


def fmt(micro: float) -> str:
    """µq → `82.1µq`, `3.2mq`, `0.5q`."""
    if abs(micro) >= MICRO_PER_Q:
        return _short(micro / MICRO_PER_Q) + "q"
    if abs(micro) >= 1000:
        return _short(micro / 1000) + "mq"
    return _short(micro) + "µq"


def label(micro: float) -> str:
    """`82.1µq · 12.2k/q` — the cost and how many such turns fit in one week."""
    if micro > 0:
        return f"{fmt(micro)} · {_sig3(MICRO_PER_Q / micro)}/q"
    return fmt(micro)


def micro_of(w: float, k: float) -> float:
    return w / k * MICRO_PER_Q


def is_prompt(e: dict) -> bool:
    """A real user prompt in the main chain (not a tool result, not meta, not a subagent)."""
    if e.get("type") != "user" or e.get("isSidechain") or e.get("isMeta"):
        return False
    c = (e.get("message") or {}).get("content")
    if isinstance(c, str):
        return bool(c.strip())
    if isinstance(c, list):
        kinds = {b.get("type") for b in c if isinstance(b, dict)}
        return "text" in kinds and "tool_result" not in kinds
    return False


def has_final(entries: list[dict]) -> bool:
    """Whether the entries include the turn's final response (a stop reason other than a tool call) after the
    last prompt among them — an earlier turn's final response that was flushed late does not count."""
    i = last_prompt(entries)
    entries = entries[i + 1 :] if i >= 0 else entries
    return any(
        e.get("type") == "assistant" and (e.get("message") or {}).get("stop_reason") not in (None, "tool_use")
        for e in entries
    )


def last_prompt(entries: list[dict]) -> int:
    """Index of the last real user prompt, or -1."""
    for i in range(len(entries) - 1, -1, -1):
        if is_prompt(entries[i]):
            return i
    return -1


def sum_usage(entries: list[dict], since_ts: str | None = None) -> dict:
    """Sum usage over assistant entries, once per API response (streamed blocks repeat the same usage)."""
    seen: set[str] = set()
    tot = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "w": 0.0, "responses": 0, "unrated": 0}
    for e in entries:
        if e.get("type") != "assistant":
            continue
        if since_ts and (e.get("timestamp") or "") < since_ts:
            continue
        m = e.get("message") or {}
        u = m.get("usage")
        if not u:
            continue
        key = m.get("id") or e.get("requestId") or e.get("uuid")
        if key in seen:
            continue
        seen.add(key)
        tot["input"] += u.get("input_tokens", 0) or 0
        tot["cache_write"] += u.get("cache_creation_input_tokens", 0) or 0
        tot["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
        tot["output"] += u.get("output_tokens", 0) or 0
        w = weight(u)
        r = rate(m.get("model"), u.get("speed"))
        if r is None and w > 0:
            tot["unrated"] += 1
        tot["w"] += w * (1.0 if r is None else r)
        tot["responses"] += 1
    return tot


def add(a: dict, b: dict) -> dict:
    return {k: a.get(k, 0) + b.get(k, 0) for k in set(a) | set(b)}


def calibrate(state: dict, pct: float, w_total: float, now: str) -> tuple[dict, str]:
    """Anchor at (pct, cumulative w); a later, higher pct sets K = Δw / (Δpct/100).
    An anchor from before r (no `rated`) measured another w: it is replaced, not used."""
    st = dict(state)
    anchor = st.get("anchor") if (st.get("anchor") or {}).get("rated") else None
    new_anchor = {"pct": pct, "w": w_total, "ts": now, "rated": True}
    if not anchor or pct < anchor["pct"]:
        st["anchor"] = new_anchor
        why = "weekly limit reset since the last anchor; " if anchor else ""
        return st, f"{why}anchored at {pct:g}%. Run `/q:q calibrate <pct>` again after the weekly % has risen by {MIN_DELTA_PCT:g} or more."
    dp = pct - anchor["pct"]
    if dp < MIN_DELTA_PCT:
        return st, f"only {dp:g} points since the anchor ({anchor['pct']:g}%); need {MIN_DELTA_PCT:g} or more."
    dw = w_total - anchor["w"]
    if dw <= 0:
        return st, "no turns were logged since the anchor; nothing to calibrate from."
    k = dw / (dp / 100.0)
    st.update({"k": k, "basis": "calibrated", "updated": now, "anchor": new_anchor})
    return st, f"K = {k:.3g} weighted tokens per q (from {dp:g} points, w {dw:.3g}); re-anchored at {pct:g}%."


# ---------------------------------------------------------------- files


def _read_jsonl(path: str, start: int = 0) -> tuple[list[dict], int]:
    """Complete lines from byte offset `start`; returns entries and the offset after the last complete line."""
    try:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read()
    except OSError:
        return [], start
    end = data.rfind(b"\n")
    if end < 0:
        return [], start
    out = []
    for line in data[: end + 1].splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out, start + end + 1


def _load(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _state() -> dict:
    return _load(os.path.join(HOME, "state.json"), {})


def _k(state: dict) -> tuple[float, str]:
    k = state.get("k")
    return (k, "calibrated") if k else (SEED_K, "seed")


def _ledger() -> list[dict]:
    rows, _ = _read_jsonl(os.path.join(HOME, "ledger.jsonl"))
    return rows


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- commands


def turn(hook: dict) -> dict:
    """Usage since the previous Stop of this session (first time: since the last prompt), main + subagents."""
    sid = hook.get("session_id") or "unknown"
    tp = hook.get("transcript_path") or ""
    cursor_path = os.path.join(HOME, "sessions", f"{sid}.json")
    cursor = _load(cursor_path, None)
    first = cursor is None
    cursor = cursor or {}

    entries, end = _read_jsonl(tp, cursor.get(tp, 0))
    # The transcript is written asynchronously and may not hold the final response yet when Stop fires.
    deadline = time.monotonic() + WAIT_S
    while not has_final(entries) and time.monotonic() < deadline:
        time.sleep(0.1)
        entries, end = _read_jsonl(tp, cursor.get(tp, 0))
    since_ts = None
    if first:
        i = last_prompt(entries)
        since_ts = entries[i].get("timestamp") if i >= 0 else None
        entries = entries[i + 1 :] if i >= 0 else entries
    tot = sum_usage(entries)
    cursor[tp] = end

    subdir = os.path.join(os.path.splitext(tp)[0], "subagents")
    for p in sorted(glob.glob(os.path.join(subdir, "*.jsonl"))):
        sub, send = _read_jsonl(p, cursor.get(p, 0))
        tot = add(tot, sum_usage(sub, since_ts if first else None))
        cursor[p] = send

    _save(cursor_path, cursor)
    return tot


def cmd_stop() -> int:
    try:
        hook = json.load(sys.stdin)
    except ValueError:
        return 0
    tot = turn(hook)
    if tot.get("w", 0) <= 0:
        return 0
    row = {"ts": _now(), "session_id": hook.get("session_id"), "cwd": hook.get("cwd"), **tot}
    os.makedirs(HOME, exist_ok=True)
    with open(os.path.join(HOME, "ledger.jsonl"), "a") as f:
        f.write(json.dumps(row) + "\n")
    k, basis = _k(_state())
    tag = " (~seed K)" if basis == "seed" else ""
    print(json.dumps({"systemMessage": f"q: {label(micro_of(tot['w'], k))}{tag}"}))
    return 0


def cmd_report(session: str | None) -> int:
    k, basis = _k(_state())
    rows = _ledger()
    now = dt.datetime.now(dt.timezone.utc)
    today = dt.datetime.now().astimezone().date()
    week_ago = now - dt.timedelta(days=7)

    def total(pred) -> tuple[float, int, int]:
        sel = [r for r in rows if pred(r)]
        return sum(r.get("w", 0) for r in sel), len(sel), sum(r.get("unrated", 0) for r in sel)

    def ts(r):
        return dt.datetime.fromisoformat(r["ts"])

    lines = []
    if session:
        lines.append(("this session", *total(lambda r: r.get("session_id") == session)))
    elif rows:
        last = rows[-1].get("session_id")
        lines.append(("last session", *total(lambda r: r.get("session_id") == last)))
    lines.append(("today", *total(lambda r: ts(r).astimezone().date() == today)))
    lines.append(("last 7 days", *total(lambda r: ts(r) >= week_ago)))

    k_note = f"K {k:.3g} ({'~seed' if basis == 'seed' else 'calibrated'})"
    print(f"q totals — {k_note}")
    for name, w, n, unrated in lines:
        note = f", {unrated} response{'' if unrated == 1 else 's'} of a model with no r, counted at 1" if unrated else ""
        print(f"  {name:<13} {label(micro_of(w, k)) if w else '0'}  ({n} turn{'' if n == 1 else 's'}{note})")
    return 0


def cmd_calibrate(pct: str) -> int:
    try:
        p = float(pct.rstrip("%"))
    except ValueError:
        print("usage: /q:q calibrate <weekly % from /usage>, e.g. /q:q calibrate 35")
        return 1
    w_total = sum(r.get("w", 0) for r in _ledger())
    st, msg = calibrate(_state(), p, w_total, _now())
    _save(os.path.join(HOME, "state.json"), st)
    print(msg)
    return 0


def cmd_last() -> int:
    rows = _ledger()
    if rows:
        k, basis = _k(_state())
        print(f"q {label(micro_of(rows[-1].get('w', 0), k))}{' (~seed K)' if basis == 'seed' else ''}")
    return 0


def label_weight(args: list[str]) -> float:
    """w of `label`'s groups (`<in> <cw> <cr> <out> [<model>]`, joined by `+`); ValueError when one is malformed."""
    w = 0.0
    group: list[str] = []
    for a in [*args, "+"]:
        if a != "+":
            group.append(a)
            continue
        if len(group) not in (4, 5):
            raise ValueError(group)
        inp, cw, cr, out = (int(x) for x in group[:4])
        r = rate(group[4] if len(group) == 5 else None)
        u = {"input_tokens": inp, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr, "output_tokens": out}
        w += weight(u) * (1.0 if r is None else r)
        group = []
    return w


def cmd_label(args: list[str]) -> int:
    try:
        w = label_weight(args)
    except ValueError:
        print("usage: q.py label <input> <cache_write> <cache_read> <output> [<model>] [+ …]", file=sys.stderr)
        return 1
    if w <= 0:
        return 0
    k, basis = _k(_state())
    print(f"q {'~' if basis == 'seed' else ''}{label(micro_of(w, k))}")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, rest = argv[0], argv[1:]
    if cmd == "stop":
        return cmd_stop()
    if cmd == "calibrate":
        return cmd_calibrate(rest[0] if rest else "")
    if cmd == "last":
        return cmd_last()
    if cmd == "label":
        return cmd_label(rest)
    if cmd == "report":
        session = None
        if len(rest) >= 2 and rest[0] == "--session":
            session, rest = rest[1] or None, rest[2:]
        if rest and rest[0] == "calibrate":
            return cmd_calibrate(rest[1] if len(rest) > 1 else "")
        return cmd_report(session)
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
