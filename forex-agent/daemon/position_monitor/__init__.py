"""position_monitor — exit management for the bot's own positions.

Every cycle runs ``core.positions.manage_open_positions`` over the
broker's open positions:
  * breakeven / trailing stop tightening (risk can only go down);
  * time-based closes past max_hold_hours;
  * on every close, core.positions emits ``position.closed`` and calls
    our on_close hook so the signal ledger is updated.

Only positions with ``is_own`` are touched — anything the bot did not
open is left alone. Close/modify power for operator-initiated closes
stays behind the execution gateway (forex.close_position); this daemon
runs the *standing* exit policy, which is the one surface the original
system already automated and which the risk audit classifies as local
safety (defense in depth), not discretionary trading.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

from daemon.common import Daemon, get_adapter, get_store, load_config, main

logger = logging.getLogger("forex_agent.daemon.position_monitor")


class PositionMonitor(Daemon):
    name = "position_monitor"
    interval = 60.0

    def __init__(self, interval: Optional[float] = None):
        super().__init__(interval)

    def run_once(self) -> None:
        from core.positions import manage_open_positions  # noqa: PLC0415

        config = load_config()
        adapter = get_adapter(config)
        exits = getattr(config, "exit_manager", None)
        if exits is not None and not getattr(exits, "enabled", True):
            logger.info("exit manager disabled by config; skipping pass")
            return
        trading = getattr(config, "trading", None)
        timeframe = getattr(trading, "timeframe", "H1") or "H1"

        def on_close(signal_id: str, reason: str, close_price: float, costs):
            # The position.closed event is already emitted by core.positions;
            # the signal ledger link is updated there via ticket->signal map.
            logger.warning("exit-manager closed signal %s: %s @ %s",
                           signal_id, reason, close_price)

        stats = manage_open_positions(adapter, exits, timeframe, on_close=on_close)
        if stats.get("closed") or stats.get("sl_tightened"):
            logger.warning("exit pass: %s", stats)
        else:
            logger.debug("exit pass: %s", stats)


def main_entry(argv=None):
    return main(lambda: PositionMonitor(), argv)


if __name__ == "__main__":
    sys.exit(main_entry(sys.argv[1:]))
