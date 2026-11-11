---
description: Show turn cost totals in q (this session, today, last 7 days), or calibrate K with `/q:q calibrate <weekly %>`
argument-hint: "[calibrate <weekly % from /usage>]"
allowed-tools: Bash(python3:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/q.py" report --session "${CLAUDE_SESSION_ID}" $ARGUMENTS`

Repeat the output above to the user verbatim in a code block. Add nothing else.
