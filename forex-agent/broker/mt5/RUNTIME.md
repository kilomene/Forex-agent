# MT5 Runtime Requirements

This document states, without ambiguity, what it takes to run MetaTrader 5
with this agent on a given machine. Read it before assuming anything about
"installing MT5".

## The one fact that matters

**`pip install MetaTrader5` on Linux does NOT give you a working MT5
environment.** The `MetaTrader5` Python package ships Windows-only wheels;
on Linux the import fails outright, and even where the import succeeds
there is no terminal behind it unless you provide one (see below). Any
broker operation in that state raises `BrokerError(BROKER_UNAVAILABLE)`
with an explanatory message — never fake data, never a silent simulation.

## What real MT5 requires

Exactly one of the following must be true:

### A. Local runtime — Windows host (supported)
- A Windows machine with the official MetaTrader 5 terminal installed
  (`terminal64.exe`), logged in to a broker account (demo or live).
- The `MetaTrader5` package installed in the same Python environment.
- Credentials: login / password / server (`MT5_LOGIN`, `MT5_PASSWORD`,
  `MT5_SERVER`; optional `MT5_TERMINAL_PATH` to pin a specific
  `terminal64.exe`).

This is the only configuration where `MT5Adapter` talks to the terminal
directly (the "local gateway" path in `broker/mt5/adapter.py`).

### B. Local runtime — Wine-compatible runtime (best effort, not supported)
- MT5 terminal running under Wine (or equivalent), with the Python
  environment able to `import MetaTrader5` and reach that terminal.
- Works in practice for some setups; not tested or supported here.
- If you run this way, set `MT5_ALLOW_NONWINDOWS_RUNTIME=1` to silence
  the platform warning logged at connect time.

### C. Remote gateway host (recommended for Linux agent hosts)
- A separate machine (Windows host or VM, or a Wine runtime) runs the
  MT5 terminal **plus** a small HTTP gateway service that implements the
  operation contract in `broker/mt5/gateway_contract.md`:
  fixed endpoints under `/api/v1/`, bearer-token auth, JSON envelopes.
- The Linux agent host sets:
  - `MT5_GATEWAY_URL` — e.g. `https://mt5-gateway.example.com`
  - `MT5_GATEWAY_TOKEN` — bearer token (required; the client refuses to
    run a remote gateway unauthenticated)
- `MT5Adapter` then routes every operation through
  `RemoteMT5GatewayTransport`. The Forex core does not change — it keeps
  calling the same `BrokerAdapter` methods.

## What happens without any of the above

The adapter reports honestly:

- `health()` → `connected=False` with a message naming the missing piece.
- Every broker operation raises `BrokerError(BROKER_UNAVAILABLE)` —
  no fake account, no fake positions, no fake fills.
- `broker_status()` returns structured flags so an agent can tell
  "configured but unreachable" apart from "connected".

## broker_status() field reference

`broker_status()` returns `{"broker": {...}}`. All flags default to
`False`; `detail` carries short human-readable reasons. Never raises.

| Field | Meaning |
|---|---|
| `provider` | Adapter name: `"mt5"` or `"disconnected"`. |
| `mode` | MT5 only: `"local"` (direct terminal) or `"remote"` (HTTP gateway). |
| `configured` | A broker is selected (`True` for `mt5`, even with no runtime). `False` only for the `disconnected` provider. |
| `reachable` | The runtime answers: local → `MetaTrader5` package importable; remote → gateway `ping()` succeeds (auth included). |
| `connected` | Logged in: `connect()` succeeded and the session still answers. |
| `account_available` | `account_info()` succeeds right now. |
| `market_data_available` | Symbol/quotes data retrievable right now. |
| `trading_available` | Terminal reports trading allowed (`trade_allowed`) and we are connected. `False` does NOT mean "safe to ignore" — check `detail`. |
| `detail` | Dict of short reasons, e.g. `{"reason": "gateway unreachable: ..."}`. |

Example:

```json
{"broker": {"provider": "mt5", "mode": "remote", "configured": true,
  "reachable": true, "connected": true, "account_available": true,
  "market_data_available": true, "trading_available": false,
  "detail": {"trading": "terminal reports trade_allowed=false"}}}
```

Exposed via:
- `BrokerAdapter.broker_status()` (any adapter; agent tools call it through `backend.broker_adapter()`),
- CLI: `scripts/forex broker-status [--json]`,
- local API: `GET /status` → `broker_status` key (and `GET /health` broker component).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `BROKER_UNAVAILABLE` "MetaTrader5 package is not installed" | Linux host, no terminal/gateway. Pick runtime option A, B, or C above. |
| `GATEWAY_UNREACHABLE` | `MT5_GATEWAY_URL` wrong / host down / firewall. The detail names the URL. |
| `GATEWAY_AUTH_FAILED` | Missing or wrong `MT5_GATEWAY_TOKEN` (HTTP 401/403 from the gateway). |
| `GATEWAY_REJECTED` "Unknown gateway operation" | Client/server contract mismatch — never a user typo; report it. |
| Status shows `reachable=true, connected=false` | Runtime answers but `connect()` was never called or the session dropped. |
| `trading_available=false` while connected | Terminal has trading disabled (e.g. investor password, algo-trading unchecked). Fix on the terminal/gateway host. |
