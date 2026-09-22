# MT5 Gateway Transport Contract

Version: 1. Contract for `broker/mt5/gateway.py`
(`RemoteMT5GatewayTransport`) and any server that claims to be an MT5
gateway for this agent.

## Design rules (non-negotiable)

1. **Fixed operation set.** The client may invoke exactly the operations
   in the table below, bound to the listed HTTP endpoints. There is no
   generic `execute`/`command`/`rpc` operation — on either side. A server
   MUST NOT implement one for this client, and the client rejects any
   attempt to call an operation outside the set (`GATEWAY_REJECTED`)
   before any network I/O.
2. **Authenticated.** Every request carries
   `Authorization: Bearer <token>`. A server MUST return HTTP 401/403 for
   missing/invalid tokens; the client maps those to `GATEWAY_AUTH_FAILED`
   and never retries with different credentials on its own.
3. **Validated.** Responses use the envelope below; the client validates
   shape and types and raises `GATEWAY_REJECTED` on anything malformed.
   The client never trusts the server to have filtered anything.
4. **Semantic errors pass through.** A rejected order, unknown symbol, or
   closed market is a *broker* outcome, not a transport failure: the
   server returns HTTP 200 with `ok: false` and the broker error code
   (`INVALID_SYMBOL`, `MARKET_CLOSED`, `INVALID_ORDER`, ...), which the
   client re-raises unchanged.
5. **Broker-confirmed only.** `submit`/`modify`/`close` return success
   only after the terminal confirms the trade. No provisional fills.

## Envelope

Request (POST): JSON object, per-operation fields below.
Response: `{"ok": true, "data": {...}}` or
`{"ok": false, "error_code": "<BROKER_CODE>", "message": "...",
  "detail": {...}}`.

## Operation table

| Op | Method | Path | Request fields | `data` fields |
|---|---|---|---|---|
| `ping` | GET | `/api/v1/ping` | — | `{}` (reachability + auth check) |
| `terminal` | GET | `/api/v1/terminal` | — | `trade_allowed: bool, connected: bool, server: str` |
| `account` | GET | `/api/v1/account` | — | `balance, equity: float` (both required), `currency, margin, free_margin: float, leverage, login: int, server: str` |
| `symbols` | POST | `/api/v1/symbols` | `names: [str]` (empty = all) | `symbols: [{name, volume_min, volume_max, volume_step, tick_value, tick_size, contract_size, digits, point}]` |
| `market_data` | POST | `/api/v1/market_data` | `symbol, timeframe, count` | `candles: [{time: ISO-8601, open, high, low, close, volume}]` — **closed candles only**, oldest → newest |
| `quote` | POST | `/api/v1/quote` | `symbol` | `available: bool, bid, ask: float, time: ISO-8601` |
| `positions` | GET | `/api/v1/positions` | — | `positions: [{ticket, position_id, symbol, direction: "BUY"\|"SELL", volume, price_open, price_current, sl, tp, profit, swap, magic, comment, time_open: ISO-8601}]` |
| `orders` | GET | `/api/v1/orders` | — | `orders: [{ticket, symbol, direction, volume, price, sl, tp, magic, comment, time_setup: ISO-8601}]` |
| `submit` | POST | `/api/v1/submit` | `symbol, direction, volume, stop_loss, take_profit, comment, idempotency_key` | `ticket: int, volume, price: float, retcode: int, message: str, raw: object` — only on terminal confirmation |
| `modify` | POST | `/api/v1/modify` | `ticket: int, sl, tp: float` | `{}` on confirmation |
| `close` | POST | `/api/v1/close` | `ticket: int` | `price: float` — broker-confirmed close price |
| `deal_history` | POST | `/api/v1/deal_history` | `from, to: ISO-8601, position_id: int\|null` | `deals: [{ticket, position_id, symbol, direction, entry: "IN"\|"OUT"\|"INOUT", volume, price, profit, commission, swap, magic, comment, time: ISO-8601}]` |

## Client binding

`RemoteMT5GatewayTransport` (in `gateway.py`) implements exactly this
table as explicit methods: `ping/check`, `terminal_status`,
`account_info`, `symbols`, `candles`, `quote`, `positions`, `orders`,
`submit_order`, `modify_position`, `close_position`, `deal_history`.
`_request(op, ...)` looks `op` up in `OPERATIONS` and raises
`GATEWAY_REJECTED` for anything else — this is the client-side guarantee
that arbitrary commands can never be issued through the gateway, even if
a caller passes a crafted string.

## Local implementation

The local path (`MT5Adapter` direct `MetaTrader5` calls in `adapter.py`)
implements the same operation semantics in-process. It is the "local
gateway"; the remote HTTP gateway is its network twin. Both sit behind
the unchanged `BrokerAdapter` interface, so the Forex core never knows
which transport is in use.

## Versioning

Breaking changes require a new contract version and a new `/api/v2/`
prefix. The client pins v1 paths and fails closed (`GATEWAY_REJECTED`)
on unexpected envelopes rather than guessing.
