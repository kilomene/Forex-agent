"""market_monitor — candle-feed watchdog.

Every cycle, for each configured symbol/timeframe:
  * fetch the latest candles from the broker adapter;
  * if a NEW closed candle appeared since the last cycle, emit
    ``market.candle_closed`` (this is what signal_monitor reacts to);
  * if no new candle has appeared for longer than STALE_AFTER (3x the
    poll interval), emit ``market.data_stale`` — once per transition,
    not every cycle.

Read-only: never touches positions, never trades. A dead broker feed
just means stale/no events — local safety is unaffected.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from daemon.common import Daemon, get_adapter, get_daemon_state, get_store, load_config, main, save_daemon_state
from agent.events import bus as event_bus

logger = logging.getLogger("forex_agent.daemon.market_monitor")

# How many missed intervals before a feed counts as stale.
STALE_MULTIPLIER = 3


class MarketMonitor(Daemon):
    name = "market_monitor"
    interval = 60.0

    def __init__(self, interval: Optional[float] = None,
                 symbols: Optional[List[str]] = None,
                 timeframes: Optional[List[str]] = None):
        super().__init__(interval)
        self._symbols = symbols
        self._timeframes = timeframes

    def _targets(self, config) -> List[tuple]:
        trading = getattr(config, "trading", None)
        symbols = self._symbols or getattr(trading, "symbols", None) or ["EURUSD"]
        timeframes = self._timeframes or getattr(trading, "timeframes", None) or ["M15"]
        if isinstance(symbols, str):
            symbols = [symbols]
        if isinstance(timeframes, str):
            timeframes = [timeframes]
        return [(s, t) for s in symbols for t in timeframes]

    def run_once(self) -> None:
        config = load_config()
        store = get_store()
        event_bus.configure(store=store)
        adapter = get_adapter(config)
        state = get_daemon_state(store, self.name)  # "SYMBOL|TF" -> candle_time
        stale_after = self.interval * STALE_MULTIPLIER

        for symbol, timeframe in self._targets(config):
            key = "%s|%s" % (symbol, timeframe)
            try:
                candles = adapter.candles(symbol, timeframe, count=2)
            except Exception as exc:
                logger.warning("feed %s %s: fetch failed: %s", symbol, timeframe, exc)
                continue
            if not candles:
                continue
            closed = candles[-1]
            last_seen = state.get(key)
            closed_iso = closed.time.isoformat() if hasattr(closed.time, "isoformat") else str(closed.time)
            if last_seen != closed_iso:
                state[key] = closed_iso
                if last_seen is not None:
                    event_bus.publish({"event": "market.candle_closed", "symbol": symbol,
                          "timeframe": timeframe, "candle_time": closed_iso})
                    logger.info("new closed candle %s %s @ %s", symbol, timeframe, closed_iso)
            # staleness: last_seen is the newest closed candle we know
            try:
                age = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(str(closed.time).replace("Z", "+00:00"))).total_seconds()
            except Exception:
                age = 0.0
            stale_key = key + "|stale"
            if age > stale_after and not state.get(stale_key):
                state[stale_key] = True
                event_bus.publish({"event": "market.data_stale", "symbol": symbol,
                      "timeframe": timeframe, "last_tick_age_s": age,
                      "note": "no new closed candle for %.0fs" % age})
                logger.warning("feed %s %s stale (%.0fs since last candle)",
                               symbol, timeframe, age)
            elif age <= stale_after and state.get(stale_key):
                state.pop(stale_key, None)
                logger.info("feed %s %s recovered", symbol, timeframe)
        save_daemon_state(store, self.name, state)


def main_entry(argv=None):
    return main(lambda: MarketMonitor(), argv)


if __name__ == "__main__":
    sys.exit(main_entry(sys.argv[1:]))
