# EA redeploy verification (2026-09-23) — manual steps for MQL5-side logic

The Python suite (`tests/test_ea_redeploy.py`) covers the EA's file
contracts from the consumer side. The MQL5 logic itself cannot be unit
tested; verify it live with these steps after any NovaTrader.mq5 change.

## 1. Broker-position publishing
- After re-attach, `MQL5/Files/nova_positions.json` mtime must advance
  within 60 s and `server_time` must track the broker clock.
- Open/close a position manually on the demo account: the file must
  rewrite within ~2 s of the ticket-set change (MaybePublishPositions).
- Schema per entry: ticket, symbol, type (BUY/SELL), volume, open_price,
  current_price, open_time (epoch), sl, tp, profit, magic.

## 2. Breakeven lock
- Needs a live position moving +1R. On the demo account, open a tiny
  position with a wide TP, wait until floating profit >= initial SL
  distance, then check the MQL5 log for
  `NovaTrader: BREAKEVEN LOCK ticket=... SL -> entry ...`
  and confirm the position's SL equals the entry price in the terminal.
- Negative checks: SL is never widened (only moves toward/onto entry);
  positions with other magic numbers are never touched; disabling via
  InBreakevenLock=false stops all modifies.

## 3. Exit reconciliation
- Close one of our (magic 20260921) positions manually via the terminal
  (or let SL/TP hit). Within ~2 s, `nova_trades.jsonl` must gain a
  `trade.closed` with `"reason":"broker"` and a real numeric profit.
- Restart the terminal with an open our-magic position, then close it
  broker-side: exactly ONE trade.closed must appear (seen-set persistence
  + AlreadyClosedInTradesFile guard => no duplicates).
- Honest-unknown path cannot be forced quickly (24 h missing threshold);
  code-reviewed: profit/exit_price are null, profit_status "unknown",
  note states P&L is under reconciliation and not invented.
- REGRESSION (2026-09-23): AlreadyClosedInTradesFile used a fixed 600-char
  window after each "trade.closed", which bled into the NEXT line and
  matched that record's ticket (false "already closed" -> silently dropped
  real closes for GBPCAD 10628887244 and NZDUSD 10623564384). Fixed to
  match within the single JSON line only. The guard relies on
  one-JSON-per-line in nova_trades.jsonl (enforced by test below).

## 4. Regression trip-wires
- `InMaxSpreadPoints` remains vestigial: no spread filter logic may
  reference it (owner order 2026-09-22).
- Demo-only guard, mandatory SL/TP on new orders, 60 s specs publish,
  2 s command poll, EnsureSymbols recovery: all unchanged.
