# Backend State Contract

The files under `~/workspace/mt5/bridge/run/` and the EA-published files in
`MQL5/Files/` are the authoritative state of this trading system. There is
**no mobile app** in this system (§30, §31); any UI (chat alerts, dashboards,
reports, future apps) is a read-only view over these files.

## Authoritative sources

| Fact | Authoritative source | Never infer from |
|---|---|---|
| Open positions | `MQL5/Files/nova_positions.json` (EA feed, broker-side) | journal, UI state |
| Fills / closes / deals | `MQL5/Files/nova_trades.jsonl` (EA writes what the broker confirmed) | local command log |
| Signals | `MQL5/Files/nova_signals.jsonl` | chat messages |
| Full event history | `bridge/run/nova_journal.jsonl` (append-only) | memory, summaries |
| Risk posture | `bridge/run/risk_config.json` + `run/trading_enabled` (kill switch) | UI toggles |
| Trading mode | mode record (`trading_mode.py` / `run/trading_mode.json`) | env defaults, assumptions |
| Reconciliation | `{"type":"reconcile.finding"}` lines in the journal | "no alert seen" |

## Rules for any UI or consumer

1. **Read, don't infer.** Display LIVE / CONNECTED / EXECUTED / CLOSED /
   PROFIT only when the authoritative file above says so. A command written
   to `nova_commands.jsonl` is a *request*; execution is confirmed only by a
   broker `trade.opened` / `trade.closed` event.
2. **A position is never CLOSED until the broker confirms it.** No UI, worker,
   or cloud function may mark a position closed on timeout, on missing feed,
   or on assumption. See `reconcile.py`: `missing_position` is a warning,
   not a closure.
3. **Outages imply nothing about broker state.** If the terminal, a daemon,
   the chat bridge, or a cloud worker is down, positions may still be open
   and moving at the broker. On recovery, reconcile from the broker files
   first, then resume.
4. **Stale files are not truth.** Every consumer must check file freshness
   (positions ≤ 60 s, specs ≤ 300 s per `trade_executor.py` constants) and
   label stale data as STALE rather than displaying it as current.
5. **Journal is append-only.** Consumers may tail it with their own cursors;
   nothing rewrites history. Corrections are new events, never edits.
6. **Kill switch is a file.** `run/trading_enabled` containing `0` means
   trading is halted, regardless of what any UI shows. `1` means armed.
7. **Mode comes from the mode record.** DEMO vs LIVE is decided by the mode
   record alone, verified against the broker account block. There is no
   silent fallback from LIVE to DEMO (`NO_LIVE_TO_DEMO_FALLBACK`).
