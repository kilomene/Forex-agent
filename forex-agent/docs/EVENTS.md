# Agent event delivery (Phase 4)

The subsystem pushes structured events to the agent over a localhost
Server-Sent Events stream, backed by a persistent delivery journal. The
agent never has to poll "do we have a signal?" — but a poll fallback
exists for clients that can't hold an SSE connection.

## Endpoint

```
GET http://127.0.0.1:8765/events
```

`127.0.0.1` **only** — the loopback bind is the access control; there is
no auth. Never expose this port beyond the host. Default port 8765,
overridable with `FOREX_API_PORT`. Stdlib `ThreadingHTTPServer`, no new
dependencies.

Content negotiation on `GET /events`:

| `Accept` header | Response |
|---|---|
| contains `text/event-stream` | **SSE push stream** (`Content-Type: text/event-stream`) |
| anything else (or absent) | JSON poll of the event log — unchanged legacy behavior |

(`EventSource` in browsers sends `Accept: text/event-stream`
automatically.)

## SSE frame format

One frame per event, fields in this order:

```
id: evt_9f3ac21e4b7d4c2a8e6f0a1b2c3d4e5f
event: signal.detected
data: {"event_id":"evt_9f3a...","event":"signal.detected","severity":"NOTICE","timestamp":"2026-09-21T14:00:00+00:00","payload":{...}}

```

- `id:` — the unique `event_id`. Native SSE `Last-Event-ID` support and
  the client-side dedup key.
- `event:` — the dotted event type (`signal.detected`, `trade.executed`,
  …).
- `data:` — one JSON line: `event_id`, `event`, `severity`, `timestamp`
  (the event's `ts`), and `payload` (the full normalized event).

Keep-alive: the server sends `: ping` comment lines every 15 s while
idle, plus a `: connected to forex-agent event stream` comment on
connect, so intermediaries don't close a quiet stream. New events are
tailed from the journal every 1 s.

## resume_from semantics

```
GET /events  (Accept: text/event-stream)
  ?resume_from=evt_<id>
```

On connect the server replays **every event journaled after
`resume_from`, oldest first**, then streams live events. Use this after a
reconnect: keep the last `id:` you processed and resume from it — no
missed events, no gaps.

- `resume_from` naming an **unknown** `event_id` → HTTP 400
  (`INVALID_ARGUMENT`). A client holding an id the journal never saw is
  almost certainly talking to a fresh journal; failing loudly beats
  silently missing events.
- No `resume_from` → only **new** events from connect time are streamed.
  (To read history first, call `GET /events/latest` below.)
- At most 1000 events are replayed per connect; reconnect again with the
  newest id if you need more.

## ack semantics

```
GET /events  (Accept: text/event-stream)
  ?ack=evt_<id>
```

Acknowledges receipt. **Cumulative**: the named event and every event
journaled at/before it are marked delivered (`acked=1` in the journal).
Unknown `ack` id → HTTP 400.

Ack is a delivery marker only — it does **not** filter replays. A replay
may still return acked events (e.g. after a reconnect); the agent dedups
on `event_id`, which is unique per event (`evt_` + uuid4 hex). A
retried publish carrying an already-seen `event_id` is ignored by the
journal (`INSERT OR IGNORE`), so duplicates are identifiable and
harmless.

## Poll fallback

For clients that can't hold SSE:

```
GET /events/latest?since=evt_<id>&limit=50
```

- `since=<event_id>` → events journaled **after** it, chronological.
  Unknown id → HTTP 400.
- no `since` → the newest `limit` events (default 50), chronological.
- Response: `{"ok": true, "count": N, "events": [...],
  "last_event_id": "evt_..."}` — store `last_event_id` as your resume
  cursor.

CLI equivalents (same journal, same ordering):

```
scripts/forex events --since-id evt_<id> --json   # one-shot replay
scripts/forex events --follow --json               # tail -f, JSON lines
```

## Severity table

Every event carries `severity` in `INFO / NOTICE / WARNING / CRITICAL`.
`bus.publish(event, severity=...)` accepts an explicit severity;
otherwise it is mapped from the event type:

| Event | Severity |
|---|---|
| `signal.detected`, `signal.approved`, `signal.rejected` | NOTICE |
| `trade.requested` | INFO |
| `trade.executed` | NOTICE |
| `trade.rejected` | WARNING |
| `position.closed`, `position.modified` | NOTICE |
| `position.external_close` | WARNING |
| `risk.blocked` | WARNING — **CRITICAL when the reason is the daily-loss limit** |
| `kill_switch.activated` | CRITICAL |
| `kill_switch.cleared` | NOTICE |
| `broker.connected` | INFO |
| `broker.disconnected` | CRITICAL |
| `worker.unavailable` | WARNING |
| `worker.reconnected` | NOTICE |
| `daemon.started` | INFO |
| `daemon.stopped` | WARNING |
| `health.check` | INFO |
| `health.degraded` | WARNING |
| `health.recovered` | NOTICE |
| `performance.snapshot` | INFO |
| `market.data_stale` | WARNING |
| `market.candle_closed` | INFO |
| anything else | INFO |

## Reconnect behavior

1. Client disconnects (network, restart). It kept the last processed
   `id:` — say `evt_A`.
2. Reconnect: `GET /events` with `Accept: text/event-stream` and
   `?resume_from=evt_A[&ack=evt_A]`.
3. Server replays everything after `evt_A` in journal order, then
   continues streaming live events. The client dedups on `event_id`
   (defensive; the journal never emits the same id twice).

## Where events come from

Daemons (`signal_monitor`, `health_monitor`, `market_monitor`, …) and the
execution path publish via `agent.events.bus.publish`, which writes —
atomically per publish — to the durable FIFO queue, the pollable log,
**and** the delivery journal (`storage.event_journal` table). The SSE
stream tails that journal, so a subscriber receives daemon-published
events even though daemons run in separate processes. All readers must
use the same storage file (`FOREX_AGENT_STORAGE`, default
`forex-agent/storage/local.db`).

## Security notes

- Localhost bind only; no secrets are ever placed in events or logs
  (event payloads carry market/trade/risk facts, never credentials).
- The SSE stream holds one server thread per connection (stdlib
  `ThreadingHTTPServer`) — sized for a handful of local agent
  subscribers, not for many concurrent streams.
- `GET /events` without the SSE `Accept` header keeps the legacy JSON
  poll behavior byte-for-byte.
