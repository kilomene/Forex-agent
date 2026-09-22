# Notifications (Phase 6)

Event → dispatcher → channels. The agent is notified through the **agent
event channel** (localhost SSE, `GET /events`), which streams the durable
delivery journal. Everything else is an optional sink.

## Architecture

```
subsystems (daemons, gateway, risk, broker, ...)
        │  bus.publish(event)          ← agent/events/bus.py (owned by the
        ▼                               events builder; never edited here)
┌─────────────────────┐
│  delivery journal   │  storage.event_journal (SQLite, UNIQUE event_id)
│  (durable, replay)  │◄──── GET /events (SSE, resume_from=<event_id>)
└─────────┬───────────┘
          │  bus.replay_after / latest_events   (public READ api only)
          ▼
┌─────────────────────┐
│    Dispatcher       │  agent/notifications/dispatcher.py
│  severity → channel │  cursor persisted in $FOREX_AGENT_HOME/run/notify.cursor
│  failure isolation  │  bounded retries (notification delivery ONLY)
└─────────┬───────────┘
          │  send(event) — one channel failing never blocks the others
          ▼
 ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐  ┌──────────┐
 │ agent  │  │ worker │  │  fcm   │  │webhook │  │ telegram │
 │PRIMARY │  │optional│  │optional│  │optional│  │ optional │
 │always  │  │        │  │legacy  │  │        │  │          │
 │on      │  │        │  │        │  │        │  │          │
 └────────┘  └────────┘  └────────┘  └────────┘  └──────────┘
```

The dispatcher **only reads** the journal and **only writes** to
notification sinks. It never touches the execution gateway, the broker
adapter, or risk state — so its retries can never re-fire a financial
action. (Retries: `max_attempts`, default 2 = one retry, small backoff.)

## Channels

| Channel | Name | When it sends | Notes |
|---|---|---|---|
| Agent | `agent` | always | **Primary.** Delivery = presence in the delivery journal; the SSE stream carries it to the agent. `send()` verifies the event is journaled and re-journals it if missing (idempotent: `INSERT OR IGNORE` on `event_id`). |
| Worker | `worker` | `worker.enabled` + base URL | Forwards events to the Cloudflare Worker cloud/sync layer (`POST {base_url}/agent/events`, Bearer auth). **Best-effort:** `worker/CONTRACT.md` defines no agent-event sink yet — non-2xx/transport failures are logged and isolated. Coordinate with the Worker owner to add `POST /agent/events`. |
| FCM | `fcm` | `fcm.enabled` + `FCM_PROJECT_ID` + Worker configured + `fcm_push_path` | **Optional, legacy, never required** for agent notification. Worker-mediated (the Worker owns the FCM token registry and `src/fcm.js`). Today the Worker contract only documents `POST /devices/test-push` (a manual test hook), so `fcm_push_path` defaults to empty and the channel honestly reports **unconfigured** until the Worker owner adds a per-event push endpoint. Direct FCM HTTP v1 from the agent is deliberately not implemented (would need google-auth JWT signing; not installed). |
| Webhook | `webhook` | `webhook.enabled` + URL | POSTs the event JSON. URL from `NOTIFY_WEBHOOK_URL` (preferred — the URL may embed a token) or `notifications.webhook_url`. Timeout-bounded; failures isolated. Logs carry the host only, never the full URL. |
| Telegram | `telegram` | `telegram` enabled + `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Sends a human-readable message to a Telegram chat via a bot. Token from the `TELEGRAM_BOT_TOKEN` env var **only** (never in yaml — it is a secret); chat from `TELEGRAM_CHAT_ID` (env, preferred) or `notifications.telegram_chat_id`. `signal.detected` events arrive as a trade-signal card (symbol, direction, entry, SL, TP); other events as a one-line `[SEVERITY] event`. Setup: [TELEGRAM_SETUP.md](TELEGRAM_SETUP.md). |

## Severity routing

Default (`notifications.routing` in `config/defaults.yaml`, overridable):

| Severity | Channels |
|---|---|
| CRITICAL | agent, worker, fcm, webhook, telegram |
| WARNING | agent, worker, telegram |
| NOTICE | agent, worker, telegram |
| INFO | agent |

Unconfigured channels are **skipped** (logged at debug), never called.
Unknown severities fall back to `agent`.

## Failure semantics

* **One channel failing never blocks the others** and never breaks the
  event journal or trading safety. Every `send()` is wrapped; failures
  are logged with `event_id` + severity (never the payload).
* **At-least-once for optional sinks** across restarts: the cursor is
  persisted after each batch. The agent channel is exactly-once in
  effect (journal presence check + `INSERT OR IGNORE`).
* **Event dedup:** the dispatcher advances on `event_id` and keeps a
  bounded seen-set, so replays never double-deliver within a run.
* **Cursor unknown to the journal** (e.g. rotated DB): the dispatcher
  logs a warning and resumes at the journal head rather than silently
  missing events.
* **No secrets in code, logs, or health output.** `forex notify --status`
  and `config.redacted()` mask `webhook_url`; channel logs use the
  webhook host only.

## Configuration

`config/defaults.yaml` → `notifications:` (env overrides in brackets):

| Key | Env | Default |
|---|---|---|
| `notifications.enabled` | `NOTIFICATIONS_ENABLED` | `false` |
| `notifications.poll_interval_seconds` | `NOTIFY_POLL_INTERVAL_SECONDS` | `5.0` |
| `notifications.timeout_seconds` | `NOTIFY_TIMEOUT_SECONDS` | `5.0` |
| `notifications.max_attempts` | `NOTIFY_MAX_ATTEMPTS` | `2` |
| `notifications.backoff_seconds` | `NOTIFY_BACKOFF_SECONDS` | `1.0` |
| `notifications.channels.agent` | `NOTIFY_CHANNEL_AGENT` | `true` |
| `notifications.channels.worker` | `NOTIFY_CHANNEL_WORKER` | `true` |
| `notifications.channels.fcm` | `NOTIFY_CHANNEL_FCM` | `false` |
| `notifications.channels.webhook` | `NOTIFY_CHANNEL_WEBHOOK` | `false` |
| `notifications.channels.telegram` | `NOTIFY_CHANNEL_TELEGRAM` | `false` |
| `notifications.webhook_url` | `NOTIFY_WEBHOOK_URL` | `""` |
| (bot token — env only) | `TELEGRAM_BOT_TOKEN` | `""` |
| `notifications.telegram_chat_id` | `TELEGRAM_CHAT_ID` | `""` |
| `notifications.fcm_project_id` | `FCM_PROJECT_ID` | `""` |
| `notifications.fcm_push_path` | `NOTIFY_FCM_PUSH_PATH` | `""` |
| `notifications.routing` | (yaml only) | see table above |

## Running the dispatcher

The dispatcher is a small foreground runner the supervisor (or a human)
launches; it exits cleanly on SIGTERM/SIGINT.

```bash
# Channel health (secret-free):
scripts/forex notify --status

# One pass over events journaled since the last run:
NOTIFICATIONS_ENABLED=1 scripts/forex notify --once

# Backfill explicitly after an event_id:
NOTIFICATIONS_ENABLED=1 scripts/forex notify --once --since-id evt_abc123

# Foreground loop (what a supervisor execs):
NOTIFICATIONS_ENABLED=1 scripts/forex notify --run

# Equivalent module entry point:
NOTIFICATIONS_ENABLED=1 python3 -m agent.notifications.dispatcher
```

A fresh dispatcher starts at the journal head (no history replay — the
SSE stream already delivered it); the cursor file
`$FOREX_AGENT_HOME/run/notify.cursor` makes restarts resume where the
last run stopped. Example systemd unit for a supervisor:

```ini
[Unit]
Description=forex-agent notification dispatcher
After=network.target

[Service]
Type=simple
Environment=FOREX_AGENT_HOME=/var/lib/forex-agent
Environment=NOTIFICATIONS_ENABLED=1
WorkingDirectory=/opt/forex-agent
ExecStart=/usr/bin/python3 -m agent.notifications.dispatcher
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## What the dispatcher is NOT

* Not a second event bus — the bus (`agent/events/bus.py`) owns
  publishing, validation, and the journal. The dispatcher is a
  read-only consumer.
* Not on the trading path — it cannot request, modify, or close trades.
* Not a replacement for SSE — the agent channel *is* the SSE journal;
  the dispatcher just guarantees journaled events also reach the
  optional sinks.
