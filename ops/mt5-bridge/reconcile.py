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


# ----------------------------------------------------------------------
# Durable open/close history rebuild (issue 3, 2026-09-23).
#
# Derives authoritative open/close ticket sets PURELY from the two
# durable logs -- the EA's nova_trades.jsonl and the executor's
# nova_journal.jsonl -- with no reliance on in-memory state. Used by
# ticket_tracker.audit_seen_file() and by any audit that must answer
# "which tickets are really open?" after a restart.
#
# Also verifies EVERY journaled close: genuine deal/order IDs present,
# gross_profit / swap / commission / net_profit present, and
# net == gross + swap + commission within CLOSE_MATH_TOL. Closes that
# fail are reported as findings -- missing values are NEVER invented.
# ----------------------------------------------------------------------

CLOSE_MATH_TOL = 0.05  # |net_profit - (gross+swap+commission)| tolerance

_CLOSE_ID_KEYS = ("deal_id", "deal_in", "deal_out", "order_id",
                  "position_id", "deal")
_CLOSE_COMPONENT_KEYS = ("gross_profit", "swap", "commission", "net_profit")


def _iter_log_events(path):
    """Yield (line_no, event_dict) for parseable JSON-object lines.

    Returns (events, bad_lines): bad_lines counts blank/unparseable lines
    so callers can tell "no events" apart from "unreadable file".
    """
    events, bad = [], 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return events, bad
    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(ev, dict):
            events.append((i, ev))
        else:
            bad += 1
    return events, bad


def _ticket_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def verify_close(ev, tolerance=CLOSE_MATH_TOL):
    """Verify one journaled trade.closed event.

    Returns a JSON-safe dict:
      status "pass"       - deal/order IDs present, all four P&L
                            components present, net == gross+swap+
                            commission within tolerance.
      status "incomplete" - anything missing (ids or components), or an
                            honest unknown/under-reconciliation close
                            whose P&L is not yet known. missing_fields
                            lists exactly what is absent.
      status "mismatch"   - components complete but the arithmetic fails.
    """
    ticket = _ticket_int(ev.get("ticket"))
    out = {"ticket": ticket, "status": "incomplete",
           "deal_ids": {}, "missing_fields": [], "math": None, "note": ""}
    if ev.get("profit_status") == "unknown" or (
            ev.get("exit_price") is None and ev.get("profit") is None):
        out["note"] = ("honest unknown/under-reconciliation close: "
                       "P&L not yet known, never invented")
        out["missing_fields"] = ["exit_price", "profit", "gross_profit",
                                 "swap", "commission", "net_profit"]
        return out
    for key in _CLOSE_ID_KEYS:
        if ev.get(key) is not None:
            out["deal_ids"][key] = ev.get(key)
    if not out["deal_ids"]:
        out["missing_fields"].append("deal/order ids")
    comps = {}
    for key in _CLOSE_COMPONENT_KEYS:
        v = ev.get(key)
        if v is None:
            out["missing_fields"].append(key)
        else:
            try:
                comps[key] = float(v)
            except (TypeError, ValueError):
                out["missing_fields"].append(key + " (non-numeric)")
    if out["missing_fields"]:
        return out
    expected = comps["gross_profit"] + comps["swap"] + comps["commission"]
    diff = comps["net_profit"] - expected
    out["math"] = {"gross_profit": comps["gross_profit"],
                   "swap": comps["swap"],
                   "commission": comps["commission"],
                   "net_profit": comps["net_profit"],
                   "expected_net": round(expected, 2),
                   "diff": round(diff, 2)}
    if abs(diff) <= tolerance:
        out["status"] = "pass"
    else:
        out["status"] = "mismatch"
        out["note"] = (f"net_profit != gross+swap+commission "
                       f"(diff={diff:.2f} > tol={tolerance})")
    return out


def rebuild_open_close_sets(trades_path, journal_path,
                            tolerance=CLOSE_MATH_TOL):
    """Rebuild authoritative open/close ticket sets from durable logs.

    Sources: EA nova_trades.jsonl (trades_path) and executor
    nova_journal.jsonl (journal_path). No memory state is consulted.

    Returns a JSON-safe dict:
      opened_tickets_ea / opened_tickets_journal - sorted ticket lists
      closed_tickets_ea / closed_tickets_journal  - sorted ticket lists
      reconciled_tickets  - broker-confirmed-gone (positions.reconciled)
      open_tickets        - opened (either source) minus closed (any source)
      closed_tickets      - union of all close evidence
      phantoms            - closed w/o any open record (either source)
      mirror_gaps         - EA-opened tickets with no journal trade.opened
      journal_extra_opens - journal-opened tickets with no EA trade.opened
      close_verification  - [verify_close(ev)] for every journal close
      bad_lines           - {"trades": n, "journal": n}
    """
    trades_events, trades_bad = _iter_log_events(trades_path)
    journal_events, journal_bad = _iter_log_events(journal_path)

    opened_ea, closed_ea = {}, []
    for _ln, ev in trades_events:
        t = ev.get("type")
        ticket = _ticket_int(ev.get("ticket"))
        if ticket is None:
            continue
        if t == "trade.opened":
            opened_ea[ticket] = ev
        elif t in ("trade.closed", "trade.close_corrected"):
            # Repair round 2 (2026-09-24): a trade.close_corrected revision
            # is close evidence too. A ticket whose only close record is a
            # correction (provisional close lost across a restart) must not
            # look open forever.
            closed_ea.append(ev)

    opened_j, closed_j, reconciled = {}, [], set()
    for _ln, ev in journal_events:
        t = ev.get("type")
        if t == "positions.reconciled":
            for x in ev.get("tickets_closed") or []:
                ticket = _ticket_int((x or {}).get("ticket"))
                if ticket is not None:
                    reconciled.add(ticket)
            continue
        ticket = _ticket_int(ev.get("ticket"))
        if ticket is None:
            continue
        if t == "trade.opened":
            opened_j[ticket] = ev
        elif t in ("trade.closed", "trade.close_corrected"):
            # Repair round 2 (2026-09-24): see the EA-log loop above --
            # corrections are close evidence in the journal too.
            closed_j.append(ev)

    closed_ea_t = {_ticket_int(ev.get("ticket")) for ev in closed_ea}
    closed_j_t = {_ticket_int(ev.get("ticket")) for ev in closed_j}
    closed_ea_t.discard(None)
    closed_j_t.discard(None)
    closed_all = closed_ea_t | closed_j_t | reconciled
    opened_all = set(opened_ea) | set(opened_j)

    out = {
        "opened_tickets_ea": sorted(opened_ea),
        "opened_tickets_journal": sorted(opened_j),
        "closed_tickets_ea": sorted(closed_ea_t),
        "closed_tickets_journal": sorted(closed_j_t),
        "reconciled_tickets": sorted(reconciled),
        "open_tickets": sorted(opened_all - closed_all),
        "closed_tickets": sorted(closed_all),
        "phantoms": sorted(closed_all - opened_all),
        "mirror_gaps": sorted(set(opened_ea) - set(opened_j)),
        "journal_extra_opens": sorted(set(opened_j) - set(opened_ea)),
        "close_verification": [verify_close(ev, tolerance)
                               for ev in closed_j],
        "bad_lines": {"trades": trades_bad, "journal": journal_bad},
    }
    return out
