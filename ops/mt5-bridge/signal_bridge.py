#!/usr/bin/env python3
"""
Nova MT5 -> Telegram signal bridge (public broadcast).

Tails the JSONL file written by the NovaSignals.mq5 expert advisor
(<mt5 prefix>/drive_c/Program Files/MetaTrader 5/MQL5/Files/nova_signals.jsonl)
and forwards each new signal.detected event to ALL subscribed Telegram
chats (run/subscribers.json, managed by tg_subscribers.py).

Signal-only routing: this bridge sends NOTHING except signal notifications.
No account updates, no heartbeats, no warnings.

Env:
  MT5_FILES_DIR        dir containing nova_signals.jsonl
                       (default: ~/workspace/mt5/prefix/drive_c/Program Files/MetaTrader 5/MQL5/Files)
  SIGNALS_FILE         filename override (default: nova_signals.jsonl)

Auth: sending goes through ~/workspace/skills/telegram-bot/bin/tg.py,
which authenticates with the stored custom.telegram-bot credential via
the authd surrogate flow. No bot token lives in this process's env,
files, or logs — notifications survive reboots via the vault credential.

State: <bridge dir>/run/signal_bridge.state.json  (seen signal ids, file offset)
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(BASE, "run")
os.makedirs(RUN_DIR, exist_ok=True)
STATE_PATH = os.path.join(RUN_DIR, "signal_bridge.state.json")

# Vault-backed Telegram sender (authd surrogate flow; no token here).
TG_CLI = os.path.expanduser("~/workspace/skills/telegram-bot/bin/tg.py")
SUBS_PATH = os.path.join(RUN_DIR, "subscribers.json")
FILES_DIR = os.environ.get(
    "MT5_FILES_DIR",
    os.path.expanduser(
        "~/workspace/mt5/prefix/drive_c/Program Files/MetaTrader 5/MQL5/Files"
    ),
)
SIGNALS_FILE = os.environ.get("SIGNALS_FILE", "nova_signals.jsonl")
SIGNALS_PATH = os.path.join(FILES_DIR, SIGNALS_FILE)
FEED_PATH = os.path.join(FILES_DIR, "nova_feed.json")


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


def load_subscribers():
    try:
        with open(SUBS_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_subscribers(subs):
    tmp = SUBS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(subs, f)
    os.replace(tmp, SUBS_PATH)


def tg_send_all(text):
    """Broadcast a signal to every subscriber. Best-effort per chat.

    Permanently dead chats (bot blocked / chat gone: HTTP 401/403/400)
    are removed from the subscriber list. Transient failures are logged
    and the queue keeps moving. Returns True when the signal line may be
    advanced (sent to >=1 chat, or nobody to send to); False when every
    send failed and the line should be retried later.
    """
    subs = load_subscribers()
    if not subs:
        print("no subscribers; signal not sent", flush=True)
        return True
    dead = []
    sent = 0
    for cid in list(subs.keys()):
        try:
            r = subprocess.run(
                [sys.executable, TG_CLI, "send", str(cid), text],
                capture_output=True, text=True, timeout=30)
            ok = r.returncode == 0
            err = (r.stderr or r.stdout or "")[:150]
        except Exception as e:
            ok, err = False, str(e)[:150]
        if ok:
            sent += 1
        else:
            if any(code in err for code in (" 403", " 401", " 400",
                                            "error_code\":403",
                                            "error_code\":401",
                                            "error_code\":400",
                                            "bot was blocked",
                                            "chat not found")):
                dead.append(cid)
                print(f"removing dead subscriber {cid}", flush=True)
            else:
                print(f"send failed chat {cid}: {err}", flush=True)
    for cid in dead:
        subs.pop(cid, None)
    if dead:
        save_subscribers(subs)
    print(f"broadcast: {sent}/{len(subs) + len(dead)} delivered", flush=True)
    return sent > 0


def fmt_signal(sig):
    d = sig.get("direction", "?")
    arrow = "🟢" if d == "BUY" else "🔴"
    return (
        f"{arrow} <b>SIGNAL DETECTED</b>\n"
        f"Symbol: {sig.get('symbol', '?')} {sig.get('timeframe', '')}\n"
        f"Direction: <b>{d}</b>\n"
        f"Entry: {sig.get('entry_price')}\n"
        f"SL: {sig.get('stop_loss')}   TP: {sig.get('take_profit')}\n"
        f"Strategy: {sig.get('strategy', 'ema_rsi')}\n"
        f"{sig.get('trigger', '')}"
    )


def process_new_lines(state):
    try:
        size = os.path.getsize(SIGNALS_PATH)
    except OSError:
        return 0
    offset = state.get("offset", 0)
    if size < offset:  # file rotated/truncated
        offset = 0
    sent = 0
    seen = set(state.get("seen", []))
    with open(SIGNALS_PATH, "r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        while True:
            line_start = f.tell()
            line = f.readline()
            if not line:
                offset = f.tell()
                break
            line = line.strip()
            if not line:
                offset = f.tell()
                continue
            try:
                sig = json.loads(line)
            except ValueError:
                offset = f.tell()
                continue
            if sig.get("type") != "signal.detected":
                offset = f.tell()
                continue  # signal-only routing
            sid = sig.get("id") or f"{sig.get('symbol')}-{sig.get('candle_time')}-{sig.get('direction')}"
            if sid in seen:
                offset = f.tell()
                continue
            if tg_send_all(fmt_signal(sig)):
                seen.add(sid)
                sent += 1
                print(f"broadcast signal {sid}", flush=True)
                offset = f.tell()
            else:
                # every send failed: hold the offset at the START of the
                # failed line so it is retried on the next pass
                offset = line_start
                print(f"FAILED to broadcast {sid}; retrying at offset {offset}",
                      flush=True)
                break
    state["offset"] = offset
    state["seen"] = sorted(seen)[-500:]
    save_state(state)
    return sent


def feed_status():
    """One-line liveness check of the MT5 feed (for logs only, never sent)."""
    try:
        with open(FEED_PATH) as f:
            feed = json.load(f)
        syms = feed.get("symbols", {})
        ages = []
        now_note = feed.get("time", "?")
        return (
            f"feed time={now_note} connected={feed.get('connected')} "
            f"login={feed.get('login')} server={feed.get('server')} "
            f"symbols={len(syms)}"
        )
    except (OSError, ValueError) as e:
        return f"no feed yet ({e})"


def main():
    print("bridge auth: vault-backed (custom.telegram-bot surrogate)", flush=True)
    print("bridge mode: public broadcast to run/subscribers.json", flush=True)
    state = load_state()
    print(f"bridge watching {SIGNALS_PATH}", flush=True)
    print(f"feed: {feed_status()}", flush=True)
    total = 0
    while True:
        try:
            total += process_new_lines(state)
        except Exception as e:
            print(f"loop error: {e}", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
