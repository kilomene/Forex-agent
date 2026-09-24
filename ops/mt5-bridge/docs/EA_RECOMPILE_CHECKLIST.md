# NovaTrader.mq5 recompile + redeploy — BLOCKING checklist

**Status: NOT DONE. This is a hard blocker for defects 1 and 2.**

## Why this is blocking

`~/workspace/mt5/bridge/NovaTrader.mq5` was fixed 2026-09-23 22:39
(FindJsonValuePos + skip-and-continue in `LoadSeenTickets()`,
whitespace-tolerant `ParseTicketEntry()`), but the deployed binary

    .../MQL5/Experts/NovaTrader.ex5   (built 2026-09-23 10:54)

**predates the fix by ~12 hours**. The live terminal is therefore still
running the OLD loader: one malformed entry in `nova_positions_seen.json`
aborts the whole load, and the next `SaveSeenTickets()` permanently drops
every later ticket from the file. The Python watchdog
(`ticket_tracker.audit_seen_file`, now wired into the 5-minute
`supervise.py` tick) can *detect* a loss, but only the recompiled EA
*prevents* it.

Source-level verification (2026-09-24, repair round 2) confirms the .mq5
is correct:
- `LoadSeenTickets()`: `while(true)` loop, `StringFind(txt,"\"ticket\"",pos)`,
  `FindJsonValuePos` for the value, **no `break` on malformed entries** --
  skip + `Print()` + `continue`.
- `FindJsonValuePos()`: skips space/tab/CR/LF around the colon on both
  sides -- `"ticket" : 123`, tabs, newlines all parse.
- `ParseTicketEntry()`: returns false only when no ticket key exists;
  missing optional fields leave out-params empty, never fail the entry.
- The `"tickets":[` wrapper header cannot false-match: the search key is
  the quoted `"ticket"` (closing quote included), so `"tickets":` never
  matches.

## Recompile + redeploy steps (founder / operator)

1. Compile (headless, from the MT5 dir):
   `wine64 MetaEditor64.exe /compile:"MQL5\Experts\NovaTrader.mq5" /log:"MQL5\Experts\NovaTrader.log"`
   Expect **0 errors, 0 warnings** in the log.
2. Copy the fresh `NovaTrader.ex5` to **both**
   `MQL5/Experts/` **and** `MQL5/Experts/Advisors/` (Navigator shows Advisors/).
3. Restart the terminal via `run-mt5-tunnel.sh` (LD_PRELOAD shim is mandatory).
   The chart profile auto re-attaches both EAs.
4. Verify in the terminal log (`logs/`, UTF-16LE):
   - `NovaTrader ... loaded successfully`
   - `NovaTrader: loaded N seen tickets from nova_positions_seen.json (skipped 0 malformed)`
5. Verify `nova_positions_seen.json` keeps its `{"tickets": [...]}` schema
   after a save cycle, and the 5-minute `supervise.py` tick logs
   `seen-audit: OK`.
6. **Only then** may defects 1 and 2 be marked VERIFIED. Until then they
   remain "fix present in source, not in the deployed binary".

## Do NOT

- Do not hand-edit `nova_positions_seen.json` while the terminal runs.
- Do not skip step 2: an ex5 in only one directory leaves Navigator
  showing the stale build.
