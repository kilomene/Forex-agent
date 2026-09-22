#!/usr/bin/env python3
"""
Nova public Telegram bot: subscriber management.

Polls getUpdates (vault-backed via the tg.py CLI) and maintains the public
subscriber list that signal_bridge.py broadcasts to:

  /start  subscribe this chat, reply with a welcome
  /stop   unsubscribe, reply with confirmation
  /help   reply with the command list

Any other text gets the /help reply so the bot never goes silent on a user.

This bot is PUBLIC: anyone may subscribe. It NEVER touches trading
controls -- the trading kill switch (/start//stop trading, /status, /pnl)
lives in tg_commands.py, which is admin-only and must never be exposed
to the public.

State:
  run/subscribers.json        {"<chat_id>": {"name": ..., "subscribed_at": ...}}
  run/subscribers.state.json  {"offset": ...}  (getUpdates cursor)

Auth: via ~/workspace/skills/telegram-bot/bin/tg.py (authd surrogate
flow). No secrets in env, files, or logs.
"""

import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(BASE, "run")
os.makedirs(RUN_DIR, exist_ok=True)
SUBS_PATH = os.path.join(RUN_DIR, "subscribers.json")
STATE_PATH = os.path.join(RUN_DIR, "subscribers.state.json")
LOCK_PATH = os.path.join(RUN_DIR, "tg_subscribers.lock")
TG_CLI = os.path.expanduser("~/workspace/skills/telegram-bot/bin/tg.py")


def acquire_lock():
    """Singleton guard: only one subscriber poller may run. The second
    instance exits quietly so the supervisor's pgrep relaunch can never
    create a 409-conflicting duplicate."""
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another tg_subscribers instance holds the lock; exiting",
              flush=True)
        sys.exit(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh

WELCOME = (
    "Welcome to Nova Forex Signals.\n"
    "You'll now get live signal alerts from our demo MT5 system.\n\n"
    "Demo signals for education only \u2014 not financial advice.\n"
    "/stop to unsubscribe."
)
ALREADY = "You're already subscribed \u2014 signals will keep coming. /stop to unsubscribe."
BYE = "Unsubscribed. You won't get signal alerts anymore. /start to resubscribe."
HELP = ("Nova Forex Signals \u2014 live demo MT5 alerts.\n"
        "/start subscribe \u00b7 /stop unsubscribe \u00b7 /help this message")


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg):
    print(f"{ts()} {msg}", flush=True)


def load_subs():
    try:
        with open(SUBS_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_subs(subs):
    tmp = SUBS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(subs, f)
    os.replace(tmp, SUBS_PATH)


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


def tg_send(chat_id, text):
    try:
        r = subprocess.run(
            [sys.executable, TG_CLI, "send", str(chat_id), text],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            log(f"reply failed chat {chat_id}: "
                f"{(r.stderr or r.stdout)[:150]}")
        return r.returncode == 0
    except Exception as e:
        log(f"reply error chat {chat_id}: {e}")
        return False


def handle_message(subs, chat_id, name, text):
    cmd = (text or "").strip().split()[0].split("@")[0].lower()
    cid = str(chat_id)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if cmd == "/start":
        if cid not in subs:
            subs[cid] = {"name": name, "subscribed_at": now}
            save_subs(subs)
            tg_send(cid, WELCOME)
            log(f"subscribed {cid} ({name})")
        else:
            tg_send(cid, ALREADY)
    elif cmd == "/stop":
        if cid in subs:
            del subs[cid]
            save_subs(subs)
        tg_send(cid, BYE)
        log(f"unsubscribed {cid} ({name})")
    elif cmd == "/help":
        tg_send(cid, HELP)
    elif (text or "").strip():
        # Any other text gets the help reply so the bot never goes silent.
        tg_send(cid, HELP)
        log(f"help sent to {cid} ({name}) for non-command text")


def poll_once(state, subs):
    # NOTE: the Telegram-side long-poll (10s) must stay well under tg.py's
    # urlopen timeout (15s). A longer hold (e.g. 25s) makes EVERY quiet
    # cycle die at the client timeout, and the abandoned server-side hold
    # then 409-conflicts the next poll -- a self-inflicted outage loop.
    try:
        r = subprocess.run(
            [sys.executable, TG_CLI, "updates",
             str(state.get("offset", 0)), "10"],
            capture_output=True, text=True, timeout=60)
    except Exception as e:
        log(f"updates call error: {e}")
        return
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "")[:500]
        # Log the TAIL of the traceback (the actual exception line), not
        # just the header -- the first 150 chars never show the error type.
        tail_lines = [l for l in err.strip().split("\n") if l.strip()]
        err_tail = tail_lines[-1][:200] if tail_lines else "unknown"
        # 409 = another getUpdates consumer holds the bot token (or a stale
        # long-poll is still open server-side). Back off quietly; the offset
        # is untouched so no updates are lost.
        if "409" in err or "Conflict" in err:
            log(f"getUpdates 409 conflict -- another poller active? "
                f"backing off 10s (no updates lost) [{err_tail}]")
            time.sleep(10)
        else:
            log(f"updates failed [{err_tail}]")
        return
    try:
        updates = json.loads(r.stdout or "[]")
    except ValueError:
        log("updates: bad JSON")
        return
    for upd in updates:
        if not isinstance(upd, dict):
            continue
        state["offset"] = upd.get("update_id", 0) + 1
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None:
            continue
        name = chat.get("title") or chat.get("first_name") or "?"
        handle_message(subs, cid, name, msg.get("text", ""))
    save_state(state)


def main():
    _lock = acquire_lock()  # noqa: F841 -- held for process lifetime
    log("tg_subscribers polling (public bot, subscriptions only)")
    state = load_state()
    subs = load_subs()
    log(f"{len(subs)} subscriber(s) loaded")
    while True:
        try:
            poll_once(state, subs)
        except Exception as e:
            log(f"loop error: {e}")
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
