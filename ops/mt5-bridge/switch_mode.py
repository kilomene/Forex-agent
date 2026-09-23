#!/usr/bin/env python3
"""switch-mode: the explicit, atomic trading-mode switch (Zenas §8, §9).

Usage:
    python3 switch_mode.py DEMO --by <operator> --reason <text>
    python3 switch_mode.py LIVE --by <operator> --reason <text> --confirm
    python3 switch_mode.py --authorize-live --by <operator> --login <n> \
        --server <s> [--expires-days <n>] [--note <text>]
    python3 switch_mode.py --status

Rules enforced:
  * Only DEMO and LIVE exist. No other mode strings are accepted.
  * DEMO->LIVE requires a valid live authorization on file (see
    --authorize-live) AND --confirm on the first-ever LIVE transition.
  * The switch is atomic: validate -> connect -> verify -> reconcile ->
    confirm risk -> commit. Any failure leaves the previous mode unchanged.
  * The terminal must ALREADY be logged into the target account: the
    verifier reads the live broker account block and the mode record must
    match it exactly (account identity lock). For LIVE this means the
    operator logs the MT5 terminal into the LIVE account first, then runs
    this command.
  * Refused while broker positions are open, unless --allow-with-positions
    (explicit policy override; positions are never carried across modes —
    resolve them first).
  * Switching never resets risk controls, journals, configs, or AI memory.
  * On LIVE failure the mode stays as it was. There is no fallback.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

BRIDGE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BRIDGE)

import trading_mode as tm

FILES_DIR = os.path.join(
    os.path.dirname(BRIDGE), "prefix", "drive_c", "Program Files",
    "MetaTrader 5", "MQL5", "Files")
SPECS_MAX_AGE_S = 120


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_account_block():
    """Read the LIVE broker account block from the EA-published specs file.

    This is the production broker_verifier: it reports what the terminal is
    ACTUALLY logged into right now. Freshness-checked; stale or missing
    data fails closed.
    """
    path = os.path.join(FILES_DIR, "nova_symbol_specs.json")
    try:
        st = os.stat(path)
    except OSError:
        raise tm.ModeError("broker specs file missing: %s" % path)
    age = datetime.now(timezone.utc).timestamp() - st.st_mtime
    if age > SPECS_MAX_AGE_S:
        raise tm.ModeError(
            "broker specs file stale (%.0fs old); refusing to verify identity"
            % age)
    try:
        with open(path, "r", encoding="utf-8") as f:
            specs = json.load(f)
    except (OSError, ValueError) as e:
        raise tm.ModeError("cannot parse broker specs file: %s" % e)
    acct = specs.get("account") or {}
    login = acct.get("login")
    server = acct.get("server")
    acct_type = acct.get("type") or acct.get("account_type")
    if login is None or not server or not acct_type:
        raise tm.ModeError(
            "broker account block incomplete (need login/server/type)")
    capabilities = {
        "equity": acct.get("equity"),
        "currency": acct.get("currency"),
        "specs_time": specs.get("time") or specs.get("server_time"),
    }
    return login, server, acct_type, capabilities


def _open_tickets():
    """Tickets currently open at the broker (authoritative positions file)."""
    path = os.path.join(FILES_DIR, "nova_positions.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return [p.get("ticket") for p in data.get("positions", [])
            if p.get("ticket") is not None]


def cmd_status(_args):
    rec = tm.load_mode()
    print(json.dumps({
        "mode": rec.mode,
        "account_login": rec.account_login,
        "broker_server": rec.broker_server,
        "account_type": rec.account_type,
        "changed_at": rec.changed_at,
        "changed_by": rec.changed_by,
        "reason": rec.reason,
        "version": rec.version,
        "live_authorized": tm.is_live_authorized(),
        "no_live_to_demo_fallback": tm.NO_LIVE_TO_DEMO_FALLBACK,
    }, indent=2))
    return 0


def cmd_authorize_live(args):
    if not args.login or not args.server:
        print("error: --login and --server are required", file=sys.stderr)
        return 2
    expires = _now_iso_dt() + timedelta(days=args.expires_days)
    record = {
        "authorized_by": args.by,
        "live_login": int(args.login),
        "live_server": args.server,
        "authorized_at": _now_iso(),
        "expires_at": expires.isoformat(timespec="seconds"),
        "note": args.note or "",
    }
    if not args.by:
        print("error: --by <operator> is required", file=sys.stderr)
        return 2
    tm.authorize_live(record)
    print("LIVE authorization recorded for %s@%s (expires %s)"
          % (record["live_login"], record["live_server"],
             record["expires_at"]))
    print("This authorizes a future switch only. Mode is unchanged: %s"
          % tm.load_mode().mode)
    return 0


def _now_iso_dt():
    return datetime.now(timezone.utc)


def cmd_switch(args):
    target = args.target.upper()
    try:
        rec = tm.switch_mode(
            target,
            changed_by=args.by,
            reason=args.reason,
            broker_verifier=lambda _profile: _read_account_block(),
            operator_confirm=bool(args.confirm),
            positions_provider=_open_tickets,
            allow_with_positions=bool(args.allow_with_positions),
        )
    except tm.ModeError as e:
        status = getattr(e, "status", None)
        print("SWITCH REFUSED: %s%s"
              % (e, (" [%s]" % status) if status else ""), file=sys.stderr)
        print("Mode unchanged: %s" % tm.load_mode().mode, file=sys.stderr)
        return 1
    print("MODE SWITCHED: %s (version %d)" % (rec.mode, rec.version))
    print(json.dumps({
        "mode": rec.mode,
        "account_login": rec.account_login,
        "broker_server": rec.broker_server,
        "account_type": rec.account_type,
        "changed_at": rec.changed_at,
        "changed_by": rec.changed_by,
        "reason": rec.reason,
    }, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Explicit atomic trading-mode switch (DEMO|LIVE only).")
    ap.add_argument("target", nargs="?",
                    help="DEMO or LIVE")
    ap.add_argument("--by", default="",
                    help="operator identity (required)")
    ap.add_argument("--reason", default="",
                    help="reason for the switch (required)")
    ap.add_argument("--confirm", action="store_true",
                    help="operator confirmation (required for first LIVE transition)")
    ap.add_argument("--allow-with-positions", action="store_true",
                    help="explicit policy override: switch with open positions")
    ap.add_argument("--status", action="store_true",
                    help="print current mode record")
    ap.add_argument("--authorize-live", action="store_true",
                    help="record a LIVE authorization (does not switch mode)")
    ap.add_argument("--login", type=int, default=None,
                    help="LIVE account login (with --authorize-live)")
    ap.add_argument("--server", default=None,
                    help="LIVE broker server (with --authorize-live)")
    ap.add_argument("--expires-days", type=int, default=90,
                    help="authorization validity in days")
    ap.add_argument("--note", default="",
                    help="note for the authorization record")
    args = ap.parse_args(argv)

    if args.status or (not args.target and not args.authorize_live):
        return cmd_status(args)
    if args.authorize_live:
        return cmd_authorize_live(args)
    if not args.by or not args.reason:
        print("error: --by and --reason are required", file=sys.stderr)
        return 2
    return cmd_switch(args)


if __name__ == "__main__":
    sys.exit(main())
