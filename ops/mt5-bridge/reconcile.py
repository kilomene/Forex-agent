#!/usr/bin/env python3
"""Continuous local-vs-broker reconciliation for the Nova MT5 stack.

Compares three views of reality:
  * broker_positions - nova_positions.json (broker-authoritative, EA feed)
  * journal_opens    - journal/state record of trade.opened events
  * broker_deals     - nova_trades.jsonl trade.opened / trade.closed events

Broker state is authoritative. Findings NEVER mark a position closed;
only a broker trade.closed / closing deal may do that. Findings are
journaled as {"type": "reconcile.finding", ...}; critical ones
(external_position, duplicate_execution) also raise an alert flag for the
caller to surface.

Finding types:
  external_position   broker position with no journal open record (critical)
  external_close      broker shows a close the journal still treats as open
  partial_fill        broker volume < journal open volume, no reduction deal
  partial_close       position shrank vs last known broker volume
  missing_position    journal says open, broker has no position and no close
  duplicate_execution one command_id produced >1 distinct broker ticket (critical)
  sl_mismatch         journal/broker SL differ beyond tolerance
  tp_mismatch         journal/broker TP differ beyond tolerance
  volume_mismatch     journal/broker volume differ (not a partial fill/close)

Pure/testable core: reconcile(...). Thin loop hook: integrate(engine).

One-line integration (for Worker B / integrator, inside TraderEngine.run_once):
    from reconcile import integrate as integrate_reconcile; integrate_reconcile(self)

This module is NOT wired into the live loop by default.
"""

import json
import os
import sys
import time

BRIDGE = os.path.dirname(os.path.abspath(__file__))
if BRIDGE not in sys.path:
    sys.path.insert(0, BRIDGE)

CRITICAL_TYPES = {"external_position", "duplicate_execution"}

PRICE_TOL = 1e-9   # exact float compare for SL/TP is unreliable; use epsilon
VOL_TOL = 1e-9
EXTERNAL_GRACE_S = 180  # grace for a fresh fill before it must be journaled


def _finding(ftype, severity, ticket=None, symbol=None, detail=None):
    f = {"type": f"reconcile.{ftype}" if "." not in ftype else ftype,
         "severity": severity}
    if ticket is not None:
        f["ticket"] = ticket
    if symbol is not None:
        f["symbol"] = symbol
    if detail:
        f["detail"] = detail
    return f


def _close_tickets(broker_deals):
    """Tickets the broker has closed (trade.closed events)."""
    closed = {}
    for d in broker_deals or []:
        if d.get("type") == "trade.closed" and d.get("ticket") is not None:
            closed[str(d["ticket"])] = d
    return closed


def _open_deals(broker_deals):
    """ticket -> trade.opened broker deal record."""
    opened = {}
    for d in broker_deals or []:
        if d.get("type") == "trade.opened" and d.get("ticket") is not None:
            opened[str(d["ticket"])] = d
    return opened


def reconcile(broker_positions, journal_opens, broker_deals, now=None,
              last_known_volumes=None):
    """Pure reconciliation. Returns a list of finding dicts.

    Args:
        broker_positions:  list of broker position dicts (nova_positions.json).
        journal_opens:     list of dicts the journal/state believes are open:
                           {ticket, symbol, direction, volume, sl, tp,
                            command_id, open_time}.
        broker_deals:      list of trade.opened/trade.closed dicts from
                           nova_trades.jsonl.
        now:               epoch seconds (default time.time()).
        last_known_volumes:optional {ticket: volume} from the previous cycle,
                           used to detect partial closes.

    Never asserts a close: findings only describe mismatches.
    """
    now_s = now if now is not None else time.time()
    last_known = {str(k): float(v) for k, v in (last_known_volumes or {}).items()}
    findings = []

    broker_by_ticket = {}
    for p in broker_positions or []:
        if p.get("ticket") is not None:
            broker_by_ticket[str(p["ticket"])] = p
    journal_by_ticket = {}
    for j in journal_opens or []:
        if j.get("ticket") is not None:
            journal_by_ticket[str(j["ticket"])] = j
    closed = _close_tickets(broker_deals)
    broker_opened = _open_deals(broker_deals)

    # --- duplicate execution: one command_id -> >1 distinct broker tickets
    cmd_tickets = {}
    for d in broker_deals or []:
        if d.get("type") == "trade.opened" and d.get("command_id"):
            cmd_tickets.setdefault(str(d["command_id"]), set()).add(
                str(d["ticket"]))
    for cmd_id, tickets in cmd_tickets.items():
        if len(tickets) > 1:
            findings.append(_finding(
                "duplicate_execution", "critical", symbol=None,
                detail={"command_id": cmd_id,
                        "tickets": sorted(tickets)}))

    # --- broker-side scan
    for key, pos in broker_by_ticket.items():
        j = journal_by_ticket.get(key)
        if j is None:
            # Grace: a fill seconds old may not be journaled yet.
            opened_at = None
            bd = broker_opened.get(key)
            if bd and bd.get("time"):
                opened_at = _parse_broker_time(bd.get("time"))
            if opened_at is not None and now_s - opened_at < EXTERNAL_GRACE_S:
                continue
            findings.append(_finding(
                "external_position", "critical", ticket=pos.get("ticket"),
                symbol=pos.get("symbol"),
                detail={"direction": pos.get("direction"),
                        "volume": pos.get("volume"),
                        "note": "broker position has no journal open record"}))
            continue

        # volume checks (broker authoritative)
        try:
            bv = float(pos.get("volume") or 0)
            jv = float(j.get("volume") or 0)
        except (TypeError, ValueError):
            bv, jv = 0.0, 0.0
        lk = last_known.get(key)
        if lk is not None and bv < lk - VOL_TOL:
            findings.append(_finding(
                "partial_close", "warning", ticket=pos.get("ticket"),
                symbol=pos.get("symbol"),
                detail={"last_known_volume": lk, "broker_volume": bv}))
        elif abs(bv - jv) > VOL_TOL:
            if bv < jv:
                findings.append(_finding(
                    "partial_fill", "warning", ticket=pos.get("ticket"),
                    symbol=pos.get("symbol"),
                    detail={"journal_volume": jv, "broker_volume": bv}))
            else:
                findings.append(_finding(
                    "volume_mismatch", "warning", ticket=pos.get("ticket"),
                    symbol=pos.get("symbol"),
                    detail={"journal_volume": jv, "broker_volume": bv,
                            "note": "broker volume exceeds journal record"}))

        # SL / TP checks against the journal's commanded values
        for field, ftype in (("sl", "sl_mismatch"), ("tp", "tp_mismatch")):
            try:
                bval = float(pos.get(field) or 0)
                jval = float(j.get(field) or 0)
            except (TypeError, ValueError):
                continue
            if jval and abs(bval - jval) > PRICE_TOL * max(1.0, abs(jval)):
                findings.append(_finding(
                    ftype, "warning", ticket=pos.get("ticket"),
                    symbol=pos.get("symbol"),
                    detail={f"journal_{field}": jval,
                            f"broker_{field}": bval}))

    # --- journal-side scan: opens the broker no longer shows
    for key, j in journal_by_ticket.items():
        if key in broker_by_ticket:
            continue
        if key in closed:
            findings.append(_finding(
                "external_close", "warning", ticket=j.get("ticket"),
                symbol=j.get("symbol"),
                detail={"note": "broker closed this ticket but the journal "
                                "still treats it as open; journal must be "
                                "backfilled from the broker deal",
                        "broker_deal": closed[key].get("deal"),
                        "profit": closed[key].get("profit")}))
        else:
            findings.append(_finding(
                "missing_position", "warning", ticket=j.get("ticket"),
                symbol=j.get("symbol"),
                detail={"note": "journal says open; broker shows no position "
                                "and no close record (EA lag or dropped "
                                "event); position is NOT marked closed"}))

    return findings


def _parse_broker_time(s):
    """Parse broker 'YYYY.MM.DD HH:MM:SS' to epoch (None on failure)."""
    if not s:
        return None
    try:
        from datetime import datetime
        return datetime.strptime(str(s), "%Y.%m.%d %H:%M:%S").timestamp()
    except (ValueError, TypeError):
        return None


def journal_findings(journal_path, findings):
    """Append findings as {"type": "reconcile.finding", ...} lines.

    Returns True when any finding is critical (alert flag for the caller).
    """
    alert = False
    try:
        with open(journal_path, "a") as f:
            for fd in findings:
                entry = {"type": "reconcile.finding"}
                entry.update(fd)
                entry.setdefault("time", time.strftime("%Y-%m-%d %H:%M:%S"))
                f.write(json.dumps(entry) + "\n")
                if fd.get("type", "").split(".")[-1] in CRITICAL_TYPES \
                        or fd.get("severity") == "critical":
                    alert = True
    except OSError:
        return False
    return alert


# --------------------------------------------------------------------------
# Thin executor-loop hook (NOT wired in by default)
# --------------------------------------------------------------------------

def _load_broker_deals(trades_path, tail_lines=5000):
    """Last N lines of nova_trades.jsonl parsed as dicts."""
    deals = []
    try:
        with open(trades_path) as f:
            lines = f.readlines()
    except OSError:
        return deals
    for line in lines[-tail_lines:]:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            deals.append(json.loads(line))
        except ValueError:
            continue
    return deals


def integrate(engine):
    """One executor-cycle hook. Call from TraderEngine.run_once():

        from reconcile import integrate as integrate_reconcile; integrate_reconcile(self)

    Reconciles broker positions vs journal opens vs broker deals, journals
    findings, and returns {"status", "findings", "alert"}. Findings never
    mark positions closed; only broker trade.closed events do that.
    """
    import trade_executor as te

    paths = engine.paths
    positions, fresh = te.load_broker_positions(paths["files_dir"])
    if not fresh:
        return {"status": "skipped", "reason": "stale_positions"}

    # journal_opens: engine open_map enriched with commanded sl/tp from
    # the journal's command.sent lines (tail scan, bounded).
    journal_opens = []
    open_map = engine.state.get("open_map") or {}
    cmd_sl_tp = {}
    try:
        with open(paths["journal"]) as f:
            jlines = f.readlines()
    except OSError:
        jlines = []
    for line in jlines[-5000:]:
        line = line.strip()
        if '"command.sent"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        cmd = ev.get("command") or {}
        if cmd.get("type") == "trade.open":
            cmd_sl_tp[cmd.get("id")] = (cmd.get("sl"), cmd.get("tp"))
    for t, info in open_map.items():
        sl, tp = cmd_sl_tp.get(info.get("command_id"), (None, None))
        journal_opens.append({
            "ticket": info.get("ticket"), "symbol": info.get("symbol"),
            "direction": info.get("direction"), "volume": info.get("volume"),
            "sl": sl, "tp": tp, "command_id": info.get("command_id"),
        })

    deals = _load_broker_deals(paths["trades"])
    findings = reconcile(positions, journal_opens, deals,
                         last_known_volumes=engine.state.get(
                             "reconcile_last_volumes"))
    # Dedup: journal each distinct finding once; retire keys that resolve so
    # a re-occurrence re-alerts. Without this the journal gets one line per
    # finding per 5s cycle.
    reported = engine.state.get("reconcile_reported") or {}
    current_keys = set()
    new_findings = []
    for fd in findings:
        key = "%s:%s" % (fd.get("type"), fd.get("ticket"))
        current_keys.add(key)
        if key not in reported:
            new_findings.append(fd)
    alert = journal_findings(paths["journal"], new_findings)
    engine.state["reconcile_reported"] = {k: True for k in current_keys}
    engine.state["reconcile_last_volumes"] = {
        str(p.get("ticket")): p.get("volume") for p in positions
        if p.get("ticket") is not None}
    engine.save()
    if alert:
        te.log("!!! RECONCILE ALERT: critical broker/journal mismatch - "
               "see journal reconcile.finding lines")
    return {"status": "ok", "findings": findings, "alert": alert}
