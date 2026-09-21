"""Outcome labeling for backtest signals — ported from forex-bot-bridge/backtest.py.

Pure function, zero broker/strategy deps: walks forward from the entry
candle checking each candle's high/low against SL/TP. Returns "win",
"loss", or "timeout".

Tie-break rule (deliberate conservative bias, kept from the original): when
a single candle's range contains BOTH the SL and the TP (common on volatile
candles with a wide wick), the SL is assumed hit first. Without knowing the
actual tick path inside the candle, the optimistic assumption would
systematically overstate win rate.

Lookahead rule: the entry candle itself is never checked — you cannot know
the outcome of the candle you entered on.
"""


def label_outcome(candles, entry_index, direction, stop_loss, take_profit,
                  max_lookahead=200) -> str:
    is_buy = direction == "BUY"
    end_index = min(entry_index + 1 + max_lookahead, len(candles))

    for i in range(entry_index + 1, end_index):
        c = candles[i]
        hit_tp = (c.high >= take_profit) if is_buy else (c.low <= take_profit)
        hit_sl = (c.low <= stop_loss) if is_buy else (c.high >= stop_loss)

        if hit_tp and hit_sl:
            return "loss"  # tie-break: assume the worse outcome
        if hit_sl:
            return "loss"
        if hit_tp:
            return "win"

    return "timeout"
