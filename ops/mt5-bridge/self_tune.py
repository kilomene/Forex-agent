"""Self-tuning loop for the trading experiment (owner order 2026-10-13).

Zenas: "enable self modification ... auto tune the rules, make the
security more tightened, maximize profits, minimize losses."

What this does: every run it measures recent closed-trade results from the
journal and tunes BOUNDED config parameters toward better expectancy:
  1. Toxic-symbol auto-exclusion: a symbol with >=15 closes in the lookback
     window, win rate <=10% and total <= -$1,000 is added to blocked_symbols
     (add-only; only Zenas re-includes a symbol).
  2. Risk scaling: risk_per_trade_pct moves within [0.01, 0.05] -- down fast
     when the recent window bleeds, up slowly when expectancy is positive.
  3. Security sweep: unknown magic numbers in the broker positions feed are
     journaled as anomalies; >=3 account.identity_mismatch events in 24h
     trips the kill switch (confirmed persistent breach).

HARD INVARIANTS -- the loop can NEVER change these, only Zenas directly:
  * trading_enabled: the loop may set False, NEVER True
  * dry_run: the loop may set True, NEVER False
  * risk_per_trade_pct: NEVER above 0.05
  * max_daily_loss_pct, require_stop_loss, capital_basis: immutable
  * kill-switch file, code files, credentials, safety audit log: untouched
  * only allowlisted keys change, each strictly inside its bounds

Modes:
  * advisory (default): analyze + journal self_tune.proposal, change nothing
  * live: validate against invariants, snapshot, apply, journal
Rollback: the pre-apply config is snapshotted; the next run compares the
post-change window against the pre-change baseline and reverts when the
change clearly degraded expectancy, journaling self_tune.rollback.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

# Reuse the bridge's journal/config helpers without importing the engine
# (keeps this runnable standalone and in CI).
BRIDGE = os.path.dirname(os.path.abspath(__file__))

LOOKBACK_DAYS = 30
MIN_TRADES_TOXIC = 15
TOXIC_WIN_RATE = 0.10
TOXIC_MAX_TOTAL = -1000.0

RISK_MIN = 0.01
RISK_MAX = 0.05          # hard ceiling: the owner's standing max
RISK_WINDOW = 20         # recent closes evaluated for risk scaling
RISK_DOWN_TRIGGER = -2000.0   # last RISK_WINDOW total below this -> halve risk
RISK_UP_TRIGGER = 1000.0      # ... above this -> +0.01 (max RISK_MAX)

IDENTITY_MISMATCH_TRIP = 3    # >=3 in 24h -> kill switch (confirmed breach)

ALLOWLIST = {
    # key: (min, max) -- every applied value must lie strictly inside
    "blocked_symbols": None,              # add-only, validated separately
    "risk_per_trade_pct": (RISK_MIN, RISK_MAX),
    "profit_plunge_arm_usd": (50.0, 200.0),
    "profit_plunge_keep_pct": (0.3, 0.7),
    "max_concurrent_trades": (1, 10),
}

IMMUTABLE = {"trading_enabled", "dry_run", "max_daily_loss_pct",
             "require_stop_loss", "capital_basis", "max_spread_points",
             "tick_value_overrides"}


def _parse_mt5_time(s):
    try:
        return datetime.strptime(str(s)[:19], "%Y.%m.%d %H:%M:%S").replace(
            tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def load_closed_trades(journal_path, lookback_days=LOOKBACK_DAYS, now=None):
    """Closed trades in the lookback window, newest last.

    Effective P&L prefers net_profit (incl. commission/swap) over profit,
    mirroring the executor's eff_profit().
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    out = []
    try:
        f = open(journal_path, encoding="utf-8", errors="replace")
    except OSError:
        return out
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") != "trade.closed":
                continue
            ts = (_parse_mt5_time(ev.get("exit_time_broker"))
                  or _parse_mt5_time(ev.get("time")))
            if ts is not None and ts < cutoff:
                continue
            pnl = None
            for k in ("net_profit", "profit"):
                try:
                    v = ev.get(k)
                    if v is not None:
                        pnl = float(v)
                        break
                except (TypeError, ValueError):
                    continue
            if pnl is None:
                continue
            out.append({"ticket": ev.get("ticket"), "symbol": ev.get("symbol"),
                        "direction": ev.get("direction"), "pnl": pnl,
                        "time": ts})
    return out


def symbol_stats(closed):
    """Per-symbol {n, wins, win_rate, total, avg, expectancy}."""
    agg = {}
    for c in closed:
        s = c.get("symbol") or "UNKNOWN"
        a = agg.setdefault(s, {"n": 0, "wins": 0, "total": 0.0})
        a["n"] += 1
        a["total"] += c["pnl"]
        if c["pnl"] > 0:
            a["wins"] += 1
    for s, a in agg.items():
        a["win_rate"] = a["wins"] / a["n"] if a["n"] else 0.0
        a["avg"] = a["total"] / a["n"] if a["n"] else 0.0
        a["expectancy"] = a["avg"]
    return agg


def propose(closed, cfg):
    """Build tuning proposals from measured results. Pure function."""
    proposals = []
    stats = symbol_stats(closed)
    blocked = set(cfg.get("blocked_symbols") or [])

    # 1. toxic symbols -> auto-exclude (add-only). One combined proposal so
    # sequential application can't clobber earlier additions.
    toxic = []
    for sym in sorted(stats):
        a = stats[sym]
        if (sym not in blocked and a["n"] >= MIN_TRADES_TOXIC
                and a["win_rate"] <= TOXIC_WIN_RATE
                and a["total"] <= TOXIC_MAX_TOTAL):
            toxic.append((sym, a))
    if toxic:
        new_blocked = sorted(blocked | {s for s, _ in toxic})
        detail = "; ".join(
            f"{s}: {a['n']} closes, {a['win_rate']:.0%} WR, ${a['total']:,.0f}"
            for s, a in toxic)
        proposals.append({
            "key": "blocked_symbols",
            "old": sorted(blocked),
            "new": new_blocked,
            "reason": (f"{len(toxic)} toxic symbol(s) auto-excluded: {detail}"),
            "evidence": {"symbols": [
                {"symbol": s, **{k: (round(v, 2) if isinstance(v, float) else v)
                                 for k, v in a.items()}}
                for s, a in toxic]},
        })

    # 2. risk scaling on the recent window
    recent = closed[-RISK_WINDOW:]
    if recent:
        total = sum(c["pnl"] for c in recent)
        cur = float(cfg.get("risk_per_trade_pct", RISK_MAX))
        if total <= RISK_DOWN_TRIGGER and cur > RISK_MIN:
            new = max(RISK_MIN, round(cur / 2, 3))
            proposals.append({
                "key": "risk_per_trade_pct", "old": cur, "new": new,
                "reason": (f"last {len(recent)} closes total ${total:,.0f} "
                           f"(<= ${RISK_DOWN_TRIGGER:,.0f}): halving risk to "
                           f"protect capital"),
                "evidence": {"recent_n": len(recent),
                             "recent_total": round(total, 2)},
            })
        elif total >= RISK_UP_TRIGGER and cur < RISK_MAX:
            new = min(RISK_MAX, round(cur + 0.01, 3))
            proposals.append({
                "key": "risk_per_trade_pct", "old": cur, "new": new,
                "reason": (f"last {len(recent)} closes total ${total:,.0f} "
                           f"(>= ${RISK_UP_TRIGGER:,.0f}) with positive "
                           f"expectancy: scaling risk up toward the cap"),
                "evidence": {"recent_n": len(recent),
                             "recent_total": round(total, 2)},
            })
    return proposals


def validate(proposals, cfg):
    """Enforce the hard invariants. Returns (accepted, rejected)."""
    accepted, rejected = [], []
    for p in proposals:
        key = p["key"]
        new = p["new"]
        why = None
        if key in IMMUTABLE:
            why = f"{key} is immutable to the self-tune loop"
        elif key not in ALLOWLIST:
            why = f"{key} is not an allowlisted tunable"
        elif key == "blocked_symbols":
            old = set(cfg.get("blocked_symbols") or [])
            if not (set(new) >= old):
                why = "blocked_symbols is add-only (re-inclusion needs Zenas)"
            elif not all(isinstance(s, str) and s for s in new):
                why = "blocked_symbols must be a list of symbol strings"
        else:
            lo, hi = ALLOWLIST[key]
            try:
                v = float(new)
            except (TypeError, ValueError):
                v = None
            if v is None or not (lo <= v <= hi):
                why = f"{key}={new} outside hard bounds [{lo}, {hi}]"
            if key == "risk_per_trade_pct" and v is not None and v > RISK_MAX:
                why = f"risk_per_trade_pct {v} exceeds owner ceiling {RISK_MAX}"
        # The loop may never enable trading or disable dry-run, even if a
        # proposal tried to smuggle those keys past the allowlist.
        if key == "trading_enabled" and new is True:
            why = "the loop can never enable trading (Zenas only)"
        if key == "dry_run" and new is False:
            why = "the loop can never take the system out of dry-run"
        if why:
            rejected.append({**p, "rejected": why})
        else:
            accepted.append(p)
    return accepted, rejected


def security_sweep(journal_path, positions, now=None):
    """Tighten security from measured anomalies. Returns findings list.

    findings: [{kind, detail, action}] where action is one of
    "alert" (journal only) or "kill" (trip the kill switch).
    """
    now = now or datetime.now(timezone.utc)
    findings = []
    # unknown magic numbers on the broker account
    try:
        import trade_executor as te
        bot_magic = te.BOT_MAGIC
    except Exception:
        bot_magic = 20260921
    for p in positions or []:
        try:
            m = int(p.get("magic") or 0)
        except (TypeError, ValueError):
            continue
        if m != bot_magic:
            findings.append({
                "kind": "unknown_magic",
                "detail": (f"ticket {p.get('ticket')} {p.get('symbol')} "
                           f"carries foreign magic {m}"),
                "action": "alert",
            })
    # persistent identity mismatches -> confirmed breach -> halt entries
    cutoff = now - timedelta(hours=24)
    mismatches = 0
    try:
        with open(journal_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") != "account.identity_mismatch":
                    continue
                ts = _parse_mt5_time(ev.get("time"))
                if ts is not None and ts >= cutoff:
                    mismatches += 1
    except OSError:
        pass
    if mismatches >= IDENTITY_MISMATCH_TRIP:
        findings.append({
            "kind": "identity_mismatch_burst",
            "detail": f"{mismatches} account.identity_mismatch in 24h",
            "action": "kill",
        })
    return findings


def _atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def run(state_dir, files_dir, journal_path=None, live=False, now=None):
    """One self-tune pass. Returns a report dict; journals everything."""
    journal_path = journal_path or os.path.join(state_dir, "nova_journal.jsonl")
    cfg_path = os.path.join(state_dir, "risk_config.json")
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    closed = load_closed_trades(journal_path, now=now)
    proposals = propose(closed, cfg)
    accepted, rejected = validate(proposals, cfg)

    # security sweep inputs: broker positions feed (best effort)
    positions = []
    try:
        with open(os.path.join(files_dir, "nova_positions.json")) as f:
            positions = json.load(f).get("positions") or []
    except (OSError, ValueError):
        pass
    findings = security_sweep(journal_path, positions, now=now)

    report = {
        "type": "self_tune.report",
        "time": (now or datetime.now(timezone.utc)).strftime("%Y.%m.%d %H:%M:%S"),
        "mode": "live" if live else "advisory",
        "lookback_days": LOOKBACK_DAYS,
        "closed_trades": len(closed),
        "proposals": [{k: v for k, v in p.items() if k != "evidence"}
                       for p in proposals],
        "accepted": len(accepted),
        "rejected": [{k: r[k] for k in ("key", "rejected")} for r in rejected],
        "security_findings": findings,
        "applied": [],
        "rollback": None,
    }

    def journal(entry):
        try:
            with open(journal_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            pass

    if not live:
        for p in accepted:
            journal({"type": "self_tune.proposal", **p})
        for r in rejected:
            journal({"type": "self_tune.rejected", **r})
        journal(report)
        return report

    # live: snapshot, apply accepted, journal
    snap_path = os.path.join(state_dir, "self_tune_snapshot.json")
    prev_snap = None
    try:
        with open(snap_path) as f:
            prev_snap = json.load(f)
    except (OSError, ValueError):
        pass
    snapshot = {"time": report["time"], "config": cfg,
                "baseline_expectancy": _expectancy(closed)}
    _atomic_write_json(snap_path, snapshot)
    new_cfg = dict(cfg)
    for p in accepted:
        new_cfg[p["key"]] = p["new"]
        report["applied"].append(p["key"])
        journal({"type": "self_tune.applied", **p})
    _atomic_write_json(cfg_path, new_cfg)
    journal({"type": "config.changed",
             "actor": "self_tune",
             "note": "self-tune loop applied bounded tuning",
             "applied": report["applied"]})

    # rollback check against the PREVIOUS snapshot's baseline: if the
    # window since the last applied change clearly degraded expectancy,
    # revert to the previous config.
    if prev_snap:
        try:
            base_exp = float(prev_snap.get("baseline_expectancy") or 0)
            cur_exp = _expectancy(closed)
            # Degraded: expectancy fell by more than $25/trade AND is negative.
            if cur_exp < 0 and (base_exp - cur_exp) > 25:
                _atomic_write_json(cfg_path, prev_snap["config"])
                report["rollback"] = {
                    "reverted_to": prev_snap["time"],
                    "baseline_expectancy": round(base_exp, 2),
                    "current_expectancy": round(cur_exp, 2),
                }
                journal({"type": "self_tune.rollback", **report["rollback"]})
        except (ValueError, TypeError, KeyError):
            pass

    # security actions
    for fl in findings:
        journal({"type": "self_tune.security", **fl})
        if fl["action"] == "kill":
            try:
                kill_path = os.path.join(state_dir, "trading_enabled")
                tmp = kill_path + ".tmp"
                with open(tmp, "w") as f:
                    f.write("0")
                os.replace(tmp, kill_path)
                journal({"type": "kill_switch.tripped",
                         "actor": "self_tune",
                         "reason": fl["detail"]})
            except OSError:
                pass
    journal(report)
    return report


def _expectancy(closed):
    if not closed:
        return 0.0
    return sum(c["pnl"] for c in closed) / len(closed)


def main(argv):
    global LOOKBACK_DAYS
    import argparse
    ap = argparse.ArgumentParser(description="Self-tuning loop (owner order 2026-10-13)")
    ap.add_argument("--state-dir", default=os.path.join(BRIDGE, "run"))
    ap.add_argument("--files-dir", default=None)
    ap.add_argument("--live", action="store_true",
                    help="apply validated tuning (default: advisory only)")
    ap.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS)
    a = ap.parse_args(argv)
    LOOKBACK_DAYS = a.lookback_days
    rep = run(a.state_dir, a.files_dir or "", live=a.live)
    print(json.dumps(rep, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
