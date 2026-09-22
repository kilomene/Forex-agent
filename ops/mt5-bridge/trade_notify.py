#!/usr/bin/env python3
"""
trade_notify.py -- emit new trade.opened / trade.closed journal events.

Tails run/nova_journal.jsonl with its own cursor (run/trade_notify.state.json)
so trade fills and exits can wake the in-chat notifier within seconds.
Signal-only: never trades. Never touches Telegram.
State: run/trade_notify.state.json.
"""
import json
import os
import sys

RUN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run")
JOURNAL = os.path.join(RUN_DIR, "nova_journal.jsonl")
STATE_PATH = os.path.join(RUN_DIR, "trade_notify.state.json")

WANTED = {"trade.opened", "trade.closed"}


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
        size = os.path.getsize(JOURNAL)
    except OSError:
        print("NO_NEW_TRADES")
        return
    offset = state.get("offset", 0)
    if size < offset:  # rotated/truncated
        offset = 0
    seen = set(state.get("seen", []))
    out = []
    with open(JOURNAL, "r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        while True:
            line = f.readline()
            if not line:
                end = f.tell()
                break
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") not in WANTED:
                continue
            # Historical backfills (authoritative QueryClose reconciliation)
            # are journaled for the record; they are not live exits, so
            # they must not fire real-time close alerts.
            if e.get("reconciled"):
                continue
            key = f"{e.get('type')}:{e.get('ticket')}:{e.get('signal_id')}"
            if key in seen:
                continue
            seen.add(key)
            item = {
                "event": e.get("type"),
                "symbol": e.get("symbol"),
                "direction": e.get("direction"),
                "volume": e.get("volume"),
                "price": e.get("entry_price") or e.get("exit_price"),
                "profit": e.get("profit"),
                "ticket": e.get("ticket"),
            }
            out.append(item)
    state["offset"] = end
    state["seen"] = sorted(seen)[-500:]
    save_state(state)
    if init_only or not out:
        print("NO_NEW_TRADES")
        return
    for item in out:
        print(json.dumps(item, separators=(",", ":")))


if __name__ == "__main__":
    main()
