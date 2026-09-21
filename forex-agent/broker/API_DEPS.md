# broker/ API_DEPS — cross-area imports owned by CORE-BUILDER

The broker package is the ONLY place that talks to MetaTrader 5, and
even there the import is optional/degraded. Everything outside
`broker/mt5/` sees only the `BrokerAdapter` interface.

## The MetaTrader5 rule

`import MetaTrader5` may appear **only** under `broker/mt5/` —
currently exactly one site: `broker/mt5/adapter.py` (inside
`try/except ImportError`, binding `mt5 = None` when the terminal isn't
installed, e.g. Linux). No test may import MetaTrader5. Verified by:
`grep -rn "import MetaTrader5" forex-agent/ --include="*.py"` must show
hits only under `broker/mt5/`.

When `mt5 is None`, `MT5Adapter` still imports cleanly; any operation
raises `BrokerError("BROKER_UNAVAILABLE", ...)` instead of ImportError.

## Imports INTO broker/

| Imported by | From | Names | Contract |
|---|---|---|---|
| `broker/mt5/adapter.py` | `broker` (base) | `BrokerAdapter`, `AccountInfo`, `BrokerError`, `BrokerHealth`, `Candle`-via-`core.market` (see below), `Deal`, `Order`, `OrderRequest`, `OrderResult`, `Position`, `Quote`, `SymbolSpec`, `MAGIC_NUMBER`, `extract_signal_id_from_comment`, error codes | Base dataclasses + interface defined in `broker/__init__.py` |
| `broker/mt5/adapter.py` | `core.market` | `Candle` | `Candle(time, open, high, low, close, volume)` — candles returned oldest→newest, closed candles |
| `broker/mt5/shared.py` | `broker` (base) | `MAGIC_NUMBER`, `extract_signal_id_from_comment`, `OrderRequest` | `MAGIC_NUMBER = 20260817`; `extract_signal_id_from_comment(comment) -> str \| None` parses `approved:<signal_id>` |
| `broker/mt5/shared.py` | `core.market` | `Candle` | as above |
| `broker/disconnected.py` | `broker` (base) | `BrokerAdapter`, `BrokerError`, `BROKER_UNAVAILABLE` | Every operation raises `BrokerError(BROKER_UNAVAILABLE, ...)` |

`core.market` is imported by `broker/mt5/*` only so both dialects
(`MT5Client` legacy + direct calls) return the same `Candle` type the
rest of core consumes. Direction of dependency: broker → core.market
(types only, no logic).

## What broker provides to others

`broker/__init__.py` exports the full surface; consumers import from
`broker`, never from `broker.mt5` directly (except the daemon, which
chooses the adapter class).

**Interface** — `BrokerAdapter` (abc):

| Method | Returns | Notes |
|---|---|---|
| `connect(creds: dict) -> None` | | `creds`: login/password/server (+ path/timeout); raises `BrokerError(CREDENTIALS_INVALID/BROKER_UNAVAILABLE)` |
| `disconnect() -> None` | | |
| `account_info()` | `AccountInfo(balance, equity, currency)` | equity includes floating |
| `symbols(names=None)` | `list[SymbolSpec]` | `SymbolSpec`: name, volume_min/max/step, tick_value, tick_size, contract_size, digits, point |
| `candles(symbol, timeframe, count=200)` | `list[Candle]` | closed candles, oldest→newest |
| `positions()` | `list[Position]` | `Position`: ticket, position_id, symbol, direction, volume, price_open, price_current, sl, tp, profit, swap, magic, comment, time_open; `.is_own`, `.signal_id` |
| `orders()` | `list[Order]` | pending orders |
| `submit_order(req: OrderRequest)` | `OrderResult` | **broker-confirmed only** — returns after the broker confirms; raises `BrokerError` on reject/transport error. Never a provisional fill. |
| `modify_order(ticket, sl, tp)` | `None` | SL/TP modify; raises `BrokerError` |
| `close_position(ticket)` | `float` | close price; raises `BrokerError` |
| `deal_history(from_, to, position_id=None)` | `list[Deal]` | `Deal`: ticket, position_id, symbol, direction, entry "IN"/"OUT", volume, price, profit, commission, swap, magic, comment, time |
| `health()` | `BrokerHealth(connected, adapter, server_time, message)` | |
| `quote(symbol)` | `Quote \| None` | `Quote`: symbol, bid, ask, time; `.spread`; None when unavailable |

**Errors** — `BrokerError(code, message, detail=None)` with codes:
`BROKER_UNAVAILABLE`, `INVALID_SYMBOL`, `MARKET_CLOSED`,
`RISK_LIMIT_EXCEEDED`, `DAILY_LOSS_LIMIT`, `MAX_EXPOSURE`,
`INVALID_ORDER`, `CONFIG_INVALID`, `CREDENTIALS_INVALID`.

**Conventions** — `MAGIC_NUMBER = 20260817` tags every bot order;
`OrderRequest.order_comment()` yields `approved:<signal_id>`;
`is_own_position(magic)` / `Position.is_own` filter bot positions.

**Adapters** — `broker/mt5/adapter.MT5Adapter` (both original dialects:
the `MT5Client` class and the direct module-level MT5 calls from
exit/reconciliation/performance/cost code — all absorbed here),
`broker/disconnected.DisconnectedAdapter` (raises `BROKER_UNAVAILABLE`
for every operation; used when no broker is configured).
