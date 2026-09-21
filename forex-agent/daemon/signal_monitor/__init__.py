"""signal_monitor — deterministic signal detection.

Every cycle:
  1. Poll the local event journal for ``market.candle_closed`` events
     since the last run (written by market_monitor).
  2. For each new closed candle: fetch a full candle window and run
     EmaRsiStrategy.evaluate (pure TA, no broker side effects).
  3. On a fresh trigger: emit ``signal.detected`` with a deterministic
     signal_id ``sig:<SYMBOL>:<TF>:<candle_time>`` (deduped — the same
     candle never emits twice, even across restarts).

This daemon NEVER trades. It only detects and announces. The agent
(external brain) decides: forex.get_signal -> review -> forex.request_trade
through the execution gateway with risk checks and kill-switch gating.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Callable, Dict, List, Optional

from daemon.common import Daemon, get_adapter, get_daemon_state, get_store, load_config, main, save_daemon_state
from agent.events import bus as event_bus

logger = logging.getLogger("forex_agent.daemon.signal_monitor")

CANDLE_WINDOW = 200


def _signal_id(symbol: str, timeframe: str, candle_time: str) -> str:
    return "sig:%s:%s:%s" % (symbol, timeframe, candle_time)


class SignalMonitor(Daemon):
    name = "signal_monitor"
    interval = 60.0

    def __init__(self, interval: Optional[float] = None,
                 evaluate: Optional[Callable] = None):
        super().__init__(interval)
        self._evaluate_override = evaluate

    def _evaluate(self, config, adapter, symbol: str, timeframe: str):
        if self._evaluate_override is not None:
            return self._evaluate_override(symbol, timeframe)
        from core.strategies import EmaRsiStrategy  # noqa: PLC0415
        trading = getattr(config, "trading", None)
        strategy_cfg = getattr(trading, "ema_rsi", None)
        strategy = EmaRsiStrategy(strategy_cfg, timeframe=timeframe)
        candles = adapter.candles(symbol, timeframe, count=CANDLE_WINDOW)
        return strategy.evaluate(symbol, candles)

    def run_once(self) -> None:
        config = load_config()
        store = get_store()
        event_bus.configure(store=store)
        adapter = get_adapter(config)
        state = get_daemon_state(store, self.name)
        since = state.get("last_poll_ts")
        new_events = [e for e in event_bus.poll(since=since)
                      if e.get("event") == "market.candle_closed"]
        emitted: List[str] = state.get("emitted", [])
        emitted_set = set(emitted)

        for event in new_events:
            symbol = event.get("symbol")
            timeframe = event.get("timeframe")
            candle_time = event.get("candle_time")
            if not (symbol and timeframe and candle_time):
                continue
            sid = _signal_id(symbol, timeframe, candle_time)
            if sid in emitted_set:
                continue
            try:
                signal = self._evaluate(config, adapter, symbol, timeframe)
            except Exception:
                logger.exception("signal evaluation failed for %s %s", symbol, timeframe)
                continue
            if signal is None:
                continue
            payload = {
                "event": "signal.detected",
                "signal_id": sid,
                "symbol": symbol,
                "timeframe": timeframe,
                "direction": signal.direction,
                "entry_price": signal.entry_price,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "candle_time": candle_time,
                "strategy": getattr(signal, "strategy", "ema_rsi"),
                "trigger": getattr(signal, "trigger", "") or "",
            }
            event_bus.publish(payload)
            emitted.append(sid)
            emitted_set.add(sid)
            logger.warning("signal detected: %s %s %s", sid, signal.direction,
                           getattr(signal, "trigger", ""))

        seen_ts = [e.get("ts", "") for e in new_events if e.get("ts")]
        if seen_ts:
            latest = max(seen_ts)
            if not since or latest > since:
                state["last_poll_ts"] = latest
        state["emitted"] = emitted[-500:]  # bounded dedupe history
        save_daemon_state(store, self.name, state)


def main_entry(argv=None):
    return main(lambda: SignalMonitor(), argv)


if __name__ == "__main__":
    sys.exit(main_entry(sys.argv[1:]))
