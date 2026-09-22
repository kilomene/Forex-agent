# MT5 Stack Self-Recovery

How the trading stack rebuilds itself after a platform container restart
("wipe"), and how to trigger a rebuild manually. Written 2026-09-22 as
parts 1-2 of the 4-part resilience build.

## The wipe problem

Container restarts wipe the system layer: the apt-installed `wine64`
disappears, `/usr/local/bin` symlinks vanish, and every process dies
(Xvfb, the MT5 terminal, all Python daemons). `~/workspace` survives
intact, including the local apt archive cache at
`/var/cache/apt/archives`. Positions live broker-side and ride through
outages; the stack just needs to be rebuilt underneath them.

## Components

| File | Role |
|---|---|
| `~/workspace/mt5/supervise_stack.py` | **Part 1 — the unified supervisor.** One periodic healer that ensures the whole stack in dependency order. Only ever STARTS missing components; never kills or restarts anything running (the live `trade_executor.py` is never touched while alive). Exit 0 = healthy or healed, 2 = hard failure. Logs to `bridge/run/supervise_stack.log`. |
| `~/workspace/mt5/scripts/restore-stack.sh` | **Part 2 — one-command restore.** Human- or cron-runnable. Idempotent: safe to run when everything is already healthy. |
| `bridge/watch_mt5.py` | Reused by the supervisor (imported, not duplicated): `ensure_wine()` (dpkg reinstall from the local apt cache + symlink recreate), `terminal_alive()`, `feed_fresh()`, `relaunch()`, `gui_reattach()`. |
| `bridge/supervise.py` | Superseded for scheduling, kept as a reference/manual fallback. |

## Healing order (supervise_stack.py)

1. **wine** — `ensure_wine()`: if `/usr/lib/wine/wine64` is gone, reinstalls
   `wine64`/`wine`/`libwine`/`fonts-wine` + dependency debs from
   `/var/cache/apt/archives` via `dpkg` (no network, no apt index), then
   recreates the `/usr/local/bin/wine64` symlink. Hard failure (exit 2) if
   this fails.
2. **Xvfb** — starts `:99` if missing.
3. **MT5 terminal** — if no `terminal64.exe` process, relaunches via
   `scripts/run-mt5-tunnel.sh`, which is **mandatory**: it exports the
   `LD_PRELOAD` CONNECT shim so the terminal can reach the broker through
   the egress proxy. A terminal started without the shim looks alive but
   never authorizes. Waits 60 s, then verifies. Hard failure if still down.
4. **feed** — `nova_feed.json` must be fresh (< 180 s) and `connected:true`
   (proves the NovaSignals EA is attached and publishing). On staleness:
   wait 45 s, then best-effort GUI EA re-attach, then re-check. Hard
   failure if still stale.
5. **daemons** — `trade_executor.py`, `signal_bridge.py`,
   `tg_subscribers.py`: starts any that are missing via nohup with logs in
   `bridge/run/`. Liveness is checked by scanning `/proc` for a python
   interpreter whose argv names the script file — robust to absolute vs
   relative launch paths and immune to admin shells that merely mention a
   script name (the old `pgrep -f` trap).

A non-blocking `flock` on `/tmp/supervise_stack.lock` makes overlapping
runs a no-op, so two healers can never launch duplicate terminals/daemons.

## Cron schedule

- **`mt5-stack-supervisor`** — every 5 minutes. The single periodic healer.
- `mt5-watchdog` and `daemon-supervisor` — **disabled** 2026-09-22,
  superseded by the unified supervisor (they are kept, disabled, as
  manual-fallback records). Exactly one cron owns the stack now, so no two
  healers can fight over terminal launches.

## Manual restore

```bash
bash ~/workspace/mt5/scripts/restore-stack.sh
```

Steps: (1) best-effort repo sync — `git fetch origin` +
`git reset --hard origin/main` on `~/workspace/forex-migration/repo`,
preferring the `github-forex` deploy-key SSH alias, falling back to plain
https, warning and continuing if neither works; (2) runs
`supervise_stack.py`; (3) prints a concise health summary (wine version,
Xvfb/terminal PIDs, EA attachment evidence from the newest terminal log,
feed age, each daemon PID).

Health-only check without healing:

```bash
python3 ~/workspace/mt5/supervise_stack.py --health
```

## Recovery time

Worst case after a wipe: the next 5-minute cron pass rebuilds everything
automatically (wine reinstall ~1-2 min, terminal start + authorization
~1 min, feed/EA attach check, daemon starts). No manual work required.
`restore-stack.sh` does the same on demand.

## Known limits

- The old `bridge/supervise.py` alive-check (`pgrep -f`) is still fooled by
  name-mentioning shells; the unified supervisor does not use it.
- SSH to github.com through the egress proxy currently fails (proxy resets
  the connection), so the restore script's deploy-key step falls back to
  https fetch until the deploy-key wiring (sibling task) is complete.
- `bridge/run/` holds logs and state and is never committed to git; the
  repo sync step only touches the repo clone.
