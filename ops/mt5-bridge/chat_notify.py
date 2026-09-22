#!/usr/bin/env python3
"""
In-chat signal watcher (for the every-minute cron).

Prints new signal.detected events from nova_signals.jsonl since the last run,
one compact JSON object per line. Prints exactly NO_NEW_SIGNALS when there is
nothing new. Never touches Telegram. State: run/chat_notify.state.json.

Usage:
  chat_notify.py               normal run (prints new signals or NO_NEW_SIGNALS)
  chat_notify.py --init-cursor  move cursor to EOF silently (no output of signals)
"""

import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(BASE, "run")
os.makedirs(RUN_DIR, exist_ok=True)
STATE_PATH = os.path.join(RUN_DIR, "chat_notify.state.json")
SIGNALS_PATH = os.environ.get(
    "SIGNALS_FILE_PATH",
    os.path.expanduser(
        "~/workspace/mt5/prefix/drive_c/Program Files/MetaTrader 5/MQL5/Files"
        "/nova_signals.jsonl"
    ),
)

FIELDS = (
    "id", "symbol", "timeframe", "direction", "entry_price", "stop_loss",
    "take_profit", "rsi_value", "atr_value", "candle_time", "server_time",
    "trigger",
)


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"offset": 0, "seen": []}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def main():
    init_only = "--init-cursor" in sys.argv
    state = load_state()
    try:
        size = os.path.getsize(SIGNALS_PATH)
    except OSError:
        print("NO_NEW_SIGNALS")
        return 0
    offset = state.get("offset", 0)
    if size < offset:  # rotated/truncated
        offset = 0
    seen = set(state.get("seen", []))
    new_signals = []
    try:
        with open(SIGNALS_PATH, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            data = f.read()
            end = f.tell()
    except OSError:
        print("NO_NEW_SIGNALS")
        return 0
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            sig = json.loads(line)
        except ValueError:
            continue
        if sig.get("type") != "signal.detected":
            continue
        sid = sig.get("id") or "-".join(
            str(sig.get(k, "?")) for k in ("symbol", "candle_time", "direction")
        )
        if sid in seen:
            continue
        seen.add(sid)
        if not init_only:
            new_signals.append({k: sig.get(k) for k in FIELDS})
    state["offset"] = end
    state["seen"] = sorted(seen)[-500:]
    save_state(state)
    if init_only or not new_signals:
        print("NO_NEW_SIGNALS")
    else:
        for s in new_signals:
            print(json.dumps(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
