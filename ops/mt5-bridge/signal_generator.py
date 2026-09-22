#!/usr/bin/env python3
"""
Nova Python Signal Generator — live version.

Fetches M15 forex candles from Yahoo Finance (free, no key), computes
EMA20/50, RSI14, ATR14 (mirroring NovaSignals.mq5), detects closed-candle
EMA crossovers with RSI confirmation, and appends signal records to
MQL5/Files/nova_signals.jsonl for the existing Telegram bridge.

Signal-only. Never places trades.

Usage:
  python3 signal_generator.py --once    # single evaluation cycle
  python3 signal_generator.py --loop    # run every 15 min (daemon)
  python3 signal_generator.py --test    # synthetic test signal
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

MQL5_FILES = os.path.expanduser(
    "~/workspace/mt5/prefix/drive_c/Program Files/MetaTrader 5/MQL5/Files")
os.makedirs(MQL5_FILES, exist_ok=True)
SIGNALS_PATH = os.path.join(MQL5_FILES, "nova_signals.jsonl")
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "run", "siggen_state.json")

SYMBOLS = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "JPY=X",      # Yahoo uses JPY=X for USD/JPY
    "AUDUSD": "AUDUSD=X",
}
TIMEFRAME = "M15"

EMA_FAST, EMA_SLOW = 20, 50
RSI_PERIOD, RSI_OB, RSI_OS = 14, 70, 30
ATR_PERIOD = 14
SL_MULT, TP_MULT = 1.5, 3.0


def fetch_candles(yahoo_symbol, n=120):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
           f"?interval=15m&range=5d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    res = data["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    ts = res["timestamp"]
    candles = []
    for i in range(len(ts)):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        candles.append({"t": ts[i], "o": o, "h": h, "l": l, "c": c})
    return candles[-n:]


def ema_series(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi_last(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    # Wilder's smoothing on last `period` values
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    # smooth further back
    for i in range(len(gains) - period - 1, -1, -1):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    return 100 - (100 / (1 + ag / al))


def atr_last(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    return sum(trs[-period:]) / period


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(s, f)


def write_signal(symbol, direction, price, sl, tp, meta):
    rec = {
        "type": "signal.detected",
        "symbol": symbol,
        "timeframe": TIMEFRAME,
        "direction": direction,
        "price": round(price, 5),
        "sl": round(sl, 5),
        "tp": round(tp, 5),
        "time": datetime.now(timezone.utc).isoformat(),
        "source": "nova-python",
        "meta": meta,
    }
    with open(SIGNALS_PATH, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"SIGNAL {symbol} {direction} @ {price:.5f} SL {sl:.5f} TP {tp:.5f} | {meta}")


def evaluate_once():
    state = load_state()
    for symbol, ysym in SYMBOLS.items():
        try:
            candles = fetch_candles(ysym)
        except Exception as e:
            print(f"{symbol}: fetch failed: {e}")
            continue
        if len(candles) < 60:
            print(f"{symbol}: not enough candles ({len(candles)})")
            continue
        # closed-candle only: drop the forming candle
        closed = candles[:-1]
        last_ts = closed[-1]["t"]
        key = f"{symbol}_last_ts"
        if state.get(key) == last_ts:
            print(f"{symbol}: no new closed candle")
            continue
        state[key] = last_ts

        closes = [c["c"] for c in closed]
        highs = [c["h"] for c in closed]
        lows = [c["l"] for c in closed]

        ef = ema_series(closes, EMA_FAST)
        es = ema_series(closes, EMA_SLOW)
        # crossover on last two closed candles
        prev_bull = ef[-2] > es[-2]
        curr_bull = ef[-1] > es[-1]
        crossed_up = (not prev_bull) and curr_bull
        crossed_dn = prev_bull and (not curr_bull)

        rsi = rsi_last(closes, RSI_PERIOD)
        atr = atr_last(highs, lows, closes, ATR_PERIOD)
        price = closes[-1]

        direction = None
        if crossed_up and rsi < RSI_OB:
            direction = "BUY"
        elif crossed_dn and rsi > RSI_OS:
            direction = "SELL"

        meta = {"ema_fast": round(ef[-1], 5), "ema_slow": round(es[-1], 5),
                "rsi": round(rsi, 2), "atr": round(atr, 5),
                "candle_time": last_ts}
        if direction:
            sl = price - SL_MULT * atr if direction == "BUY" else price + SL_MULT * atr
            tp = price + TP_MULT * atr if direction == "BUY" else price - TP_MULT * atr
            write_signal(symbol, direction, price, sl, tp, meta)
        else:
            print(f"{symbol}: no signal (RSI {rsi:.1f}, "
                  f"EMA{EMA_FAST} {ef[-1]:.5f} / EMA{EMA_SLOW} {es[-1]:.5f})")
    save_state(state)


def main():
    if "--test" in sys.argv:
        write_signal("EURUSD", "BUY", 1.14660, 1.14510, 1.15110,
                     {"test": True, "note": "synthetic bridge test"})
        return
    if "--loop" in sys.argv:
        print("signal generator loop started (15-min cycle)")
        while True:
            try:
                evaluate_once()
            except Exception as e:
                print(f"cycle error: {e}")
            time.sleep(900)
        return
    evaluate_once()


if __name__ == "__main__":
    main()
