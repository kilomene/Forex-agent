#!/usr/bin/env python3
"""Shadow signal-outcome resolution (port from the reference repo's
learning-loop concept).

For every genuine signal (traded or skipped), record what the market did
afterward: which level was touched FIRST -- take-profit or stop-loss --
from per-minute feed snapshots. Resolutions are journaled as
signal.outcome events so the 30-day experiment can study which signals
work, not just which trades were taken.

State: run/shadow_state.json  (snapshots capped per signal)
Reads: nova_signals.jsonl, nova_feed.json
Writes: signal.outcome lines into run/nova_journal.jsonl
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

BRIDGE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BRIDGE)
import trade_executor as te  # noqa: E402

LOOKBACK_HOURS = 72
SNAP_CAP = 3000  # ~50h of minute snapshots
EXPIRE_HOURS = 72


def _parse_sig_time(s):
    dt = te.parse_mt5_time(s)
    return dt


def load_signals(files_dir, since):
    sigs = []
    path = os.path.join(files_dir, te.DEFAULT_SIGNALS_FILE)
    try:
        f = open(path)
    except OSError:
        return sigs
    with f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") != "signal.detected":
                continue
            dt = _parse_sig_time(e.get("server_time") or e.get("time"))
            if dt and dt >= since:
                sigs.append(e)
    return sigs


def load_state(run_dir):
    p = os.path.join(run_dir, "shadow_state.json")
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(run_dir, state):
    p = os.path.join(run_dir, "shadow_state.json")
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, p)


def journal(run_dir, entry):
    entry = dict(entry)
    # Repair round 2 (2026-09-24): UTC by construction (see
    # trade_executor.now_str).
    entry.setdefault("time", datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"))
    with open(os.path.join(run_dir, "nova_journal.jsonl"), "a") as f:
        f.write(json.dumps(entry) + "\n")


def resolve(direction, sl, tp, snapshots):
    """First touch wins. BUY: bid hits SL or ask hits TP. SELL mirrored."""
    for ts, bid, ask in snapshots:
        if direction == "BUY":
            if sl and bid <= sl:
                return "SL", ts
            if tp and ask >= tp:
                return "TP", ts
        else:
            if sl and ask >= sl:
                return "SL", ts
            if tp and bid <= tp:
                return "TP", ts
    return None, None


def main():
    paths = te.resolve_paths()
    files_dir, run_dir = paths["files_dir"], paths["state_dir"]
    now = datetime.now()
    since = now - timedelta(hours=LOOKBACK_HOURS)

    state = load_state(run_dir)
    signals = load_signals(files_dir, since)
    feed = te.load_feed(files_dir)
    syms = (feed.get("symbols") if feed else {}) or {}

    # register new signals
    for s in signals:
        sid = s.get("id")
        if sid and sid not in state:
            state[sid] = {
                "symbol": s.get("symbol"),
                "direction": s.get("direction"),
                "entry": s.get("entry_price"),
                "sl": s.get("stop_loss"),
                "tp": s.get("take_profit"),
                "signal_time": s.get("server_time") or s.get("time"),
                "snapshots": [],
                "resolved": None,
            }

    # snapshot + resolve
    for sid, st in state.items():
        if st.get("resolved"):
            continue
        sym = st.get("symbol")
        tick = syms.get(sym) or {}
        bid, ask = tick.get("bid"), tick.get("ask")
        if bid and ask:
            st["snapshots"].append(
                [now.strftime("%Y-%m-%d %H:%M:%S"), bid, ask])
            st["snapshots"] = st["snapshots"][-SNAP_CAP:]
        result, hit_ts = resolve(st.get("direction"), st.get("sl"),
                                 st.get("tp"), st["snapshots"])
        sig_dt = _parse_sig_time(st.get("signal_time"))
        expired = sig_dt and (now - sig_dt).total_seconds() > EXPIRE_HOURS * 3600
        if result or expired:
            outcome = result or "expired"
            st["resolved"] = {"result": outcome, "at": hit_ts,
                              "resolved_at": now.strftime("%Y-%m-%d %H:%M:%S")}
            journal(run_dir, {
                "type": "signal.outcome",
                "signal_id": sid,
                "symbol": sym,
                "direction": st.get("direction"),
                "entry": st.get("entry"),
                "sl": st.get("sl"),
                "tp": st.get("tp"),
                "outcome": outcome,
                "hit_at": hit_ts,
                "snapshots": len(st["snapshots"]),
            })

    save_state(run_dir, state)
    n_open = sum(1 for s in state.values() if not s.get("resolved"))
    n_done = sum(1 for s in state.values() if s.get("resolved"))
    print(f"shadow: tracked={len(state)} open={n_open} resolved={n_done}")


if __name__ == "__main__":
    main()
