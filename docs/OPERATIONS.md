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

## Lifecycle (start / stop / restart / status / health / logs)

```bash
./scripts/forex-daemons start            # start all four daemons
./scripts/forex-daemons start health_monitor   # or one by name
./scripts/forex-daemons stop             # stop all (SIGTERM, graceful; SIGKILL after 10s)
./scripts/forex-daemons restart          # stop + start + restart safety sequence
./scripts/forex-daemons status           # running (pid ...) / stopped / stopped (stale pidfile)
./scripts/forex-daemons health [--json]  # aggregate: daemons + kill-switch latch +
                                         # broker_status + event-journal tail + disk
./scripts/forex-daemons safety [--json]  # run the restart safety sequence standalone
./scripts/forex-daemons prune            # bounded event-journal pruning (see below)
./scripts/forex-daemons logs [daemon] [--lines N] [--follow]
```

- **No duplicates, ever.** A lock serializes every mutating command, and
  each daemon re-checks its PID file at startup — a second launcher
  always loses and exits quietly. `start` on a running daemon is a
  no-op. Stale PID files (dead process) are reclaimed automatically.
- **Restart safety sequence** (runs at the end of every `restart`, or
  via `safety`): 1) load local state — kill-switch latch, persisted
  daemon states, idempotency store (read-only); 2) broker probe via
  `broker_status()` — an unavailable broker is reported honestly, never
  faked; 3) reconcile positions with `core.reconciliation` (reporting
  only — it never trades; skipped honestly when the broker is down);
  4) verify event state — journal intact, SSE `resume_from` stable
  across the restart; 5) verify monitoring/notifications/MCP config;
  6) prune the event journal; 7) report health. **The sequence never
  submits, modifies, or closes a trade** — idempotency keys are only
  read, and a pre-restart `request_id` is still recognized after the
  restart (no double execution, guaranteed by the persisted
  `execution_idempotency` table).
- **Forex lifecycle ≠ agent lifecycle.** Daemons are launched detached
  (`setsid` + `nohup`, own session), so they survive the exit of the
  agent process that started them. Restarting the agent does NOT restart
  the forex subsystem — use `forex-daemons restart` (or the systemd
  target) for that.
- **systemd vs scripts.** On hosts with systemd, enable the units in
  `daemon/systemd/` (`forex-agent.target` + the four services; the prune
  timer `forex-journal-prune.timer` for daily pruning). The unit
  `ExecStart` lines run the exact same `python3 -m daemon.<name>`
  commands as the script. Where systemd is absent (containers, macOS),
  `scripts/forex-daemons` is the supported fallback supervisor — same
  PID discipline, same commands.
- **Kill switch survives restart.** The latch lives in the SQLite
  `kill_switch` table; an engaged latch is still engaged after any
  restart, and an unreadable latch fails closed (treated as engaged).

## Event-journal pruning

The push-event delivery journal (`event_journal`, the SSE replay log) is
pruned boundedly so it cannot grow without limit:

- `events.journal_retention_days` (default **30**) — drop events older
  than N days. Env: `EVENTS_JOURNAL_RETENTION_DAYS`.
- `events.journal_max_events` (default **100000**) — keep at most N
  newest events. Env: `EVENTS_JOURNAL_MAX_EVENTS`.

Set either to 0 to disable that bound. Pruning runs on every
`forex-daemons restart` (safety sequence), on `forex-daemons prune`,
and daily via the `forex-journal-prune.timer` systemd timer. Newest
events are always kept, so SSE `resume_from` semantics are unaffected.
The kill-switch latch, idempotency keys, risk state, and audit log are
never pruned.

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

### Worker-down behavior (local safety survives)

A dead, unreachable, or contract-mismatched Worker changes nothing
locally: the kill-switch latch, risk state, execution gateway, and event
journal are all local SQLite; daemons keep running; `request_trade`
keeps enforcing risk + dry-run rules. The only degradation is that
cloud sync stops and events accumulate in the local journal (bounded by
the pruning policy) until sync resumes. Never treat `WORKER_UNAVAILABLE`
as a trading-safety event.

## Broker-down behavior

The broker is the least reliable component by design, so the subsystem
treats "broker down" as a normal operating state, not an incident:

- `scripts/forex broker-status --json` shows exactly which capability
  is missing (`connected: false` ≠ `trading_available: false` —
  logged-in is not the same as allowed-to-trade).
- `health_monitor` emits `broker.disconnected` (**CRITICAL**) into the
  event journal on any transition.
- Any broker-dependent tool call returns `BROKER_UNAVAILABLE` (or a
  structured `{ok: false, error_code, ...}`) — never synthetic data.
- Signal detection, journaling, events, notifications, config, and the
  kill switch keep working. Positions already managed locally are
  unaffected; the gateway refuses new submissions while the broker is
  unreachable (they are `rejected`, auditable in the journal).
- Recovery: fix the terminal/gateway, then `scripts/forex broker-status
  --json` — no daemon restart is required. The restart safety sequence
  also probes the broker and reports honestly when it is down.

## Local API server (SSE + HTTP)

`scripts/local_api.py` is the only process serving `GET /events` (SSE),
`/events/latest`, `/trade/request`, and the other HTTP routes. It is
**not** supervised by `scripts/forex-daemons` or systemd — run it in the
foreground (or under your own supervisor) on the same box:

```bash
python3 scripts/local_api.py                 # http://127.0.0.1:8765
FOREX_API_PORT=9000 python3 scripts/local_api.py
```

Binds 127.0.0.1 only — never expose the port beyond the host. If it is
not running, the CLI and MCP interfaces still work fully (same
capability surface); only the push stream and HTTP routes are absent.

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
