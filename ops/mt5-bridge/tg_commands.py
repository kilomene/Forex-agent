#!/usr/bin/env python3
"""
Nova MT5 demo-trading Telegram command handler.

LAUNCH REQUIRES TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in environment.
Do not launch without explicit user approval.

Polls Telegram getUpdates with an offset (urllib only, no third-party
dependencies) and answers commands from the whitelisted chat id ONLY --
messages from any other chat/user are ignored entirely.

Commands:
  /stop    write "0" to run/trading_enabled  -> reply "Trading stopped"
  /start   write "1" to run/trading_enabled  -> reply
           "Trading enabled (config gates still apply)"
  /status  reply with equity, open trade count, today's P/L
  /pnl     reply with today's closed profit

Token and chat id come from os.environ ONLY. Nothing secret is ever
written to a file or a log.

Env:
  TELEGRAM_BOT_TOKEN   bot token (required to launch)
  TELEGRAM_CHAT_ID     whitelisted destination chat id (required to launch)
  MT5_FILES_DIR        dir with nova_trades.jsonl / nova_symbol_specs.json
                       (default: ~/workspace/mt5/prefix/drive_c/Program Files/MetaTrader 5/MQL5/Files)
  TRADER_STATE_DIR     state dir holding trading_enabled + offset state
                       (default: <bridge dir>/run)

State: run/tg_commands.state.json (getUpdates offset)
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
FILES_DIR = os.environ.get(
    "MT5_FILES_DIR",
    os.path.expanduser(
        "~/workspace/mt5/prefix/drive_c/Program Files/MetaTrader 5/MQL5/Files"
    ),
)
STATE_DIR = os.environ.get("TRADER_STATE_DIR", os.path.join(BASE, "run"))
os.makedirs(STATE_DIR, exist_ok=True)
STATE_PATH = os.path.join(STATE_DIR, "tg_commands.state.json")
KILL_PATH = os.path.join(STATE_DIR, "trading_enabled")
TRADES_PATH = os.path.join(FILES_DIR, "nova_trades.jsonl")
SPECS_PATH = os.path.join(FILES_DIR, "nova_symbol_specs.json")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"offset": 0}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def tg_call(method, payload):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        print(f"telegram http {e.code}: {e.read()[:200]}", flush=True)
    except Exception as e:
        print(f"telegram error: {e}", flush=True)
    return {}


def tg_reply(text):
    tg_call("sendMessage", {"chat_id": CHAT_ID, "text": text})


def parse_mt5_time(s):
    if not s:
        return None
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(s).strip(), fmt)
        except ValueError:
            continue
    return None


def load_specs():
    try:
        with open(SPECS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def trade_stats():
    """(open_count, todays_closed_profit, today_str) from broker server clock."""
    specs = load_specs()
    acct = specs.get("account") or {}
    server_dt = parse_mt5_time(acct.get("time")) or parse_mt5_time(specs.get("time"))
    today = (server_dt or datetime.now()).date().isoformat()
    opened = set()
    closed = set()
    profit = 0.0
    try:
        with open(TRADES_PATH, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                t = ev.get("type")
                if t == "trade.opened" and ev.get("ticket") is not None:
                    opened.add(ev["ticket"])
                elif t == "trade.closed":
                    if ev.get("ticket") is not None:
                        closed.add(ev["ticket"])
                    dt = parse_mt5_time(ev.get("time"))
                    if dt and dt.date().isoformat() == today:
                        try:
                            profit += float(ev.get("profit") or 0)
                        except (TypeError, ValueError):
                            pass
    except OSError:
        pass
    return len(opened - closed), profit, today


def handle_command(text):
    cmd = (text or "").strip().split()[0].split("@")[0].lower()
    if cmd == "/stop":
        tmp = KILL_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write("0")
        os.replace(tmp, KILL_PATH)
        return "Trading stopped"
    if cmd == "/start":
        tmp = KILL_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write("1")
        os.replace(tmp, KILL_PATH)
        return "Trading enabled (config gates still apply)"
    if cmd == "/status":
        specs = load_specs()
        acct = specs.get("account") or {}
        equity = acct.get("equity", "?")
        server = acct.get("server", "?")
        open_count, profit, today = trade_stats()
        kill = "1"
        try:
            with open(KILL_PATH) as f:
                kill = f.read().strip() or "?"
        except OSError:
            kill = "missing(=0)"
        return (f"Status ({today})\n"
                f"Server: {server}\n"
                f"Equity: {equity}\n"
                f"Open trades: {open_count}\n"
                f"Today's closed P/L: {profit:+.2f}\n"
                f"Kill switch: {kill}")
    if cmd == "/pnl":
        _, profit, today = trade_stats()
        return f"Today's closed P/L ({today}): {profit:+.2f}"
    return None


def poll_once(state):
    res = tg_call("getUpdates", {
        "offset": state.get("offset", 0),
        "timeout": 25,
        "allowed_updates": ["message"],
    })
    if not res.get("ok"):
        return
    for upd in res.get("result", []):
        state["offset"] = upd.get("update_id", 0) + 1
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        if str(chat.get("id")) != str(CHAT_ID):
            continue  # not the whitelisted chat: ignore
        reply = handle_command(msg.get("text", ""))
        if reply:
            tg_reply(reply)
            print(f"handled command -> {reply.splitlines()[0]}", flush=True)
    save_state(state)


def main():
    if not BOT_TOKEN or not CHAT_ID:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required",
              file=sys.stderr)
        return 2
    print("tg_commands polling (whitelist chat only)", flush=True)
    state = load_state()
    while True:
        try:
            poll_once(state)
        except Exception as e:
            print(f"loop error: {e}", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
