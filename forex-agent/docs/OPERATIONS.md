# OPERATIONS — running the forex-agent subsystem

## Daily operation

The subsystem is designed to run unattended. The normal state:

```bash
./scripts/forex-daemons status
# market_monitor: running (pid ...) / signal_monitor: running ...
```

- `market_monitor` — feed watchdog (60s).
- `signal_monitor` — deterministic signal detection (60s). **Never trades.**
- `position_monitor` — exit management on own positions (60s).
- `health_monitor` — fail-closed kill-switch poll (10s), Worker sync,
  performance snapshot every 6h.

Logs: `$FOREX_AGENT_HOME/log/<daemon>.log`. Events: `scripts/forex events`.

## Health checks

```bash
scripts/forex health --json        # broker, kill switch, daemons, worker
scripts/forex status --json       # account + positions + risk snapshot
```

`health_monitor` emits `health.check` on any state change plus a 5-minute
heartbeat. If the kill-switch latch becomes unreadable, it **engages
fail-closed** and logs at CRITICAL — treat that as a page-worthy event and
check disk space and DB permissions before disengaging.

## Kill switch

```bash
scripts/forex kill-switch --json            # show state
scripts/forex kill-switch --engage --reason "manual" --json
scripts/forex kill-switch --clear --json     # disengage
```

While engaged: the execution gateway rejects every new trade, the Worker's
autonomous auto-approval is skipped, and `health_monitor` keeps the cloud
mirror in sync (best-effort). The local latch is always authoritative over
the cloud.

## Trading workflow (agent-driven)

1. `scripts/forex signals --json` — pending detected signals.
2. `scripts/forex analyze EURUSD --timeframe H1 --json` — full context.
3. `scripts/forex risk --json` — headroom check.
4. Trade requests go through MCP (`forex.request_trade`) or
   `POST /trade/request` on the local API — dry-run by default; returns a
   `GatewayDecision` (`approved`/`rejected`/`blocked` + reasons).
   (The CLI is read-only plus kill-switch; it has no trade entry point
   by design.)
5. `scripts/forex positions --json` / `scripts/forex performance --json`.

A trade counts only after broker confirmation. `request-trade` in dry-run
mode simulates against the risk engine — it never touches the market.

## Worker cloud sync

With `WORKER_ENABLED=true`, `health_monitor` drains the local event queue
to the Worker every 10s:

- `signal.detected` → `POST /signals`
- performance snapshots → `POST /performance/snapshot`
- kill-switch changes → `POST /kill-switch`

Failures log `WORKER_UNAVAILABLE` and retry with backoff; a persistently
failing batch is preserved in the local journal as `worker_dead_letter`
(replayable, never silently dropped). Event types with no cloud endpoint
are counted as skipped locally.

If the cloud kill-switch mirror disagrees with the local latch, a warning
is logged — **local always wins**.

## Backups

The entire local state is one SQLite file:

```bash
cp "$FOREX_AGENT_HOME/storage/local.db" /backup/forex-$(date +%F).db
```

It holds the event queue, journal, audit log, risk state, and kill-switch
latch. Restore by copying back while daemons are stopped.

`secrets.env` (0600) is backed up separately, encrypted — never alongside
the DB in plaintext.

## Upgrades

```bash
cd forex-agent && git pull            # or unpack the new tree
python3 scripts/gen_manifest.py --check
./installer/install.sh --non-interactive --prefix "$FOREX_AGENT_HOME"
./scripts/forex-daemons restart
```

The installer re-verifies smoke tests and leaves secrets, state, and the
DB untouched.

## Troubleshooting

| Symptom | Action |
|---|---|
| Daemon keeps dying | `tail $FOREX_AGENT_HOME/log/<name>.log`; check `python3 --version` (>= 3.12); run `python3 -m daemon.<name> --once` to see the error directly. |
| Stale pidfile | `forex-daemons start` reclaims it automatically; `forex-daemons stop <name>` clears it. |
| DB locked / latch unreadable | Stop daemons, check disk space and file ownership, restart. The kill switch engages fail-closed in the meantime — that is the safe state. |
| `WORKER_UNAVAILABLE` spam | Cloud down or bad `WORKER_API_KEY`. Local operation is unaffected; fix the Worker or set `WORKER_ENABLED=false`. |
| Manifest stale warning | `python3 scripts/gen_manifest.py` and commit the result. |
| systemd unit won't start | `journalctl -u forex-health_monitor`; check `WorkingDirectory` exists and is readable by the `forex` user; `systemctl edit` to override. |

## Security notes

- The local API binds **127.0.0.1 only** — loopback is the access control.
  Never expose it beyond the box.
- `secrets.env` is 0600; the CLI redacts secret fields in `config` output.
- Dry-run default; live mode is a deliberate two-step act
  (`MODE=live` + broker credentials).
- See `docs/SECURITY.md` for the full threat model.
