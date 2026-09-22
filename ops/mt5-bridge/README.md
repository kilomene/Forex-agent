# ops/mt5-bridge

Live MT5 deployment layer for the 30-day autonomous demo-trading experiment
(see `EXPERIMENT_CHARTER.md`).

## What lives here

- `*.py` — bridge daemons and ops scripts:
  - `trade_executor.py` — signal → risk-gated execution, journaling
  - `signal_bridge.py` — Telegram bot signal notifications
  - `tg_subscribers.py` / `tg_commands.py` — Telegram subscriber/command handling
  - `watch_mt5.py` — MT5 terminal + Wine self-healing watchdog
  - `supervise.py` — daemon supervisor
  - `backup_trading.py` — 15-min trade-data snapshots + close reconciliation
  - `daily_summary.py`, `weekend_reflection.py`, `final_report.py` — experiment reporting
  - `market_hours.py`, `shadow_tracker.py`, `signal_generator.py`
  - `chat_notify.py`, `trade_notify.py`, `review_trades.py`, `push_evidence.py`, `mt5_tunnel.py`
- `*.mq5` — MQL5 expert advisors (`NovaSignals`, `NovaTrader`)
- `mt5_redir.c` — LD_PRELOAD shim source (broker traffic via egress proxy)
- `tests/` — bridge test suite

## What is NEVER committed here

- `run/` — live state (journal, positions, signals, cursors, kill-switch flag)
- `*.log`, `__pycache__/`, compiled artifacts (`*.so`)
- credentials — all secrets come from environment variables at runtime

Sync rule: material changes to these scripts are pushed to this repo;
`run/` state stays local-only (backed up on a 15-min schedule instead).
