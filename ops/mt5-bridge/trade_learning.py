#!/usr/bin/env python3
"""
Nova trade-learning database
============================

USAGE NOTE (read this first)
----------------------------
This module is the durable learning database for the 30-day autonomous
demo-trading experiment (2026-09-21 -> 2026-10-21, MetaQuotes-Demo).

* Owner order: EVERY trade -- profit or loss -- must be noted and learned
  from. Nothing is discarded: signals, gate decisions, rejections,
  commands, broker acknowledgements, fills, closes, P&L, outages, and
  per-trade lessons all land here.
* The JSONL journals (nova_journal.jsonl, nova_trades.jsonl,
  nova_signals.jsonl) remain the raw evidence. This SQLite DB is the
  *queryable* layer built on top of them -- it never edits the sources.
* Honesty rules (non-negotiable):
  - Never invent P&L. Trades without a close record are stored with
    outcome explicitly marked unknown/provisional.
  - Never claim vanished positions' floating gains were banked.
  - Account/login numbers, tokens and credentials are NEVER stored here.
* Ingestion is idempotent: run it as often as you like; re-ingesting the
  same files never duplicates rows (upserts keyed by signal id / trade
  ticket, plus a per-file offset cursor in ingest_state).

Quick start::

    from trade_learning import connect, init_schema, ingest_all, daily_aggregate

    conn = connect()            # default: ~/workspace/mt5/data/trade_learning.db
    init_schema(conn)
    counts = ingest_all(conn)   # reads journal + MT5 files, reconciles
    print(daily_aggregate(conn, "2026-09-22"))

CLI::

    python3 trade_learning.py ingest      # ingest + reconcile + gap detect
    python3 trade_learning.py stats       # row counts per table
    python3 trade_learning.py review --ticket 10605494223
    python3 trade_learning.py lesson --ticket 10605494223 \
        --why-entered "..." --why-exited "..." --worked "..." \
        --failed "..." --change "..."

Environment overrides (useful for tests)::

    TRADE_LEARNING_DB   path to the sqlite file
    MT5_FILES_DIR       dir with nova_signals.jsonl / nova_trades.jsonl /
                        nova_positions.json
    TRADER_STATE_DIR    dir holding nova_journal.jsonl (default: bridge/run)
"""

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.expanduser("~/workspace/mt5/data/trade_learning.db")
FILES_DIR = os.environ.get(
    "MT5_FILES_DIR",
    os.path.expanduser(
        "~/workspace/mt5/prefix/drive_c/Program Files/MetaTrader 5/MQL5/Files"
    ),
)
STATE_DIR = os.environ.get("TRADER_STATE_DIR", os.path.join(BASE, "run"))

JOURNAL_PATH = os.path.join(STATE_DIR, "nova_journal.jsonl")
TRADES_PATH = os.path.join(FILES_DIR, "nova_trades.jsonl")
SIGNALS_PATH = os.path.join(FILES_DIR, "nova_signals.jsonl")
POSITIONS_PATH = os.path.join(FILES_DIR, "nova_positions.json")
AUDIT_PATH = os.path.join(STATE_DIR, "safety_audit.jsonl")

SCHEMA_VERSION = 1

# Tickets known to have vanished from the broker view without a clean close
# record (host-reboot / terminal-downtime windows). Their outcomes are
# UNKNOWN -- never claim the floating gains were banked.
KNOWN_VANISHED_TICKETS = {
    10607517845: "GBPJPY",
    10606155417: "XAUEUR",
}

# Keys that must never be persisted (account identifiers, secrets).
FORBIDDEN_KEYS = {
    "account", "login", "account_number", "acct", "token", "password",
    "secret", "api_key", "apikey", "auth", "credential",
}


# ---------------------------------------------------------------------------
# connection / schema
# ---------------------------------------------------------------------------

def connect(db_path=None):
    """Open (creating dirs as needed) the learning DB."""
    path = db_path or os.environ.get("TRADE_LEARNING_DB", DEFAULT_DB)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn):
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS meta(
               key TEXT PRIMARY KEY, value TEXT)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS signals(
               signal_id TEXT PRIMARY KEY,
               symbol TEXT, timeframe TEXT, direction TEXT,
               strategy TEXT, setup_class TEXT,
               entry_price REAL, stop_loss REAL, take_profit REAL,
               ema_fast REAL, ema_slow REAL, rsi REAL, atr REAL,
               candle_time TEXT, server_time TEXT,
               trigger TEXT, received_at TEXT,
               raw_json TEXT, ingested_at TEXT)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS decisions(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               signal_id TEXT, decision TEXT,
               volume REAL, risk_amount REAL, capital_basis REAL,
               command_id TEXT, context_json TEXT,
               time TEXT, raw_json TEXT,
               UNIQUE(signal_id, decision, time))"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS commands(
               command_id TEXT PRIMARY KEY,
               signal_id TEXT, symbol TEXT, direction TEXT,
               volume REAL, sl REAL, tp REAL, time TEXT,
               ack_status TEXT, reject_reason TEXT,
               fill_ticket INTEGER, fill_deal INTEGER, fill_price REAL,
               raw_json TEXT)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS trades(
               ticket INTEGER PRIMARY KEY,
               signal_id TEXT, command_id TEXT,
               symbol TEXT, direction TEXT, volume REAL,
               entry_price REAL, fill_price REAL, deal INTEGER,
               opened_server_time TEXT, opened_local_time TEXT,
               status TEXT,                       -- open|closed|unknown
               exit_server_time TEXT, exit_price REAL,
               profit REAL, commission REAL, swap REAL,
               exit_reason TEXT,
               broker_confirmed INTEGER DEFAULT 0, -- 1 = close came from broker history
               provisional INTEGER DEFAULT 0,     -- 1 = estimate / incomplete
               outcome_note TEXT,
               raw_json TEXT, updated_at TEXT)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS signal_outcomes(
               signal_id TEXT PRIMARY KEY,
               symbol TEXT, direction TEXT,
               outcome TEXT,                        -- TP|SL|OPEN|...
               hit_at TEXT, snapshots INTEGER, time TEXT)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS lessons(
               ref TEXT PRIMARY KEY,                -- ticket or signal_id
               kind TEXT,                           -- trade|signal
               why_entered TEXT, why_exited TEXT,
               what_worked TEXT, what_failed TEXT, what_to_change TEXT,
               author TEXT, created_at TEXT, updated_at TEXT)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS notes(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               kind TEXT,                           -- daily|weekly|config|strategy|safety|daily_auto|experiment
               ref_date TEXT, title TEXT, body TEXT,
               stats_json TEXT, auto INTEGER DEFAULT 0,
               created_at TEXT,
               UNIQUE(kind, ref_date, title))"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS outages(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               kind TEXT,                           -- detected_gap|restart|recovery|manual
               started_at TEXT, ended_at TEXT,
               note TEXT, source TEXT, created_at TEXT,
               UNIQUE(kind, started_at))"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS ingest_state(
               source TEXT PRIMARY KEY,
               last_offset INTEGER DEFAULT 0,
               last_mtime REAL DEFAULT 0,
               ingested_at TEXT)"""
    )
    c.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    for tbl, col in (("decisions", "signal_id"), ("commands", "signal_id"),
                     ("trades", "signal_id"), ("trades", "status"),
                     ("notes", "ref_date"), ("outages", "started_at")):
        c.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{tbl}_{col} ON {tbl}({col})"
        )
    conn.commit()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_local(s):
    """'2026-09-21 20:09:23' -> ISO-ish string or None."""
    if not s or not isinstance(s, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def _parse_server(s):
    """MT5 '2026.09.21 23:30:28' -> ISO-ish string or None."""
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.strptime(s.strip(), "%Y.%m.%d %H:%M:%S").strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        return None


def _date_of(ts):
    return ts[:10] if ts and len(ts) >= 10 else None


def _sanitize(obj):
    """Recursively drop forbidden keys (account ids, secrets) from a structure."""
    if isinstance(obj, dict):
        return {
            k: _sanitize(v)
            for k, v in obj.items()
            if k.lower() not in FORBIDDEN_KEYS
        }
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


def _raw(e):
    return json.dumps(_sanitize(e), ensure_ascii=False)


def _setup_class(strategy, trigger):
    """Heuristic setup classification from strategy + trigger text."""
    t = (trigger or "").lower()
    s = (strategy or "").lower()
    if "crossed above" in t:
        return "trend_continuation_bull"
    if "crossed below" in t:
        return "trend_continuation_bear"
    if "oversold" in t or "rsi" in t and "below" in t:
        return "mean_reversion_bull"
    if "overbought" in t or "rsi" in t and "above" in t:
        return "mean_reversion_bear"
    if "ema_rsi" in s:
        return "ema_rsi_signal"
    return s or "unknown"


def _get_cursor_offset(conn, source):
    row = conn.execute(
        "SELECT last_offset FROM ingest_state WHERE source=?", (source,)
    ).fetchone()
    return row["last_offset"] if row else 0


def _set_cursor_offset(conn, source, offset):
    conn.execute(
        """INSERT INTO ingest_state(source, last_offset, last_mtime, ingested_at)
           VALUES(?,?,?,?)
           ON CONFLICT(source) DO UPDATE SET last_offset=excluded.last_offset,
               last_mtime=excluded.last_mtime, ingested_at=excluded.ingested_at""",
        (source, offset, os.path.getmtime(source) if os.path.isfile(source) else 0,
         _utcnow()),
    )


def _iter_new_lines(conn, source, path):
    """Yield (dict_event, new_offset) for lines appended since last ingest."""
    if not os.path.isfile(path):
        return
    offset = _get_cursor_offset(conn, source)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        while True:
            pos = f.tell()
            line = f.readline()
            if not line:
                _set_cursor_offset(conn, source, pos)
                break
            _set_cursor_offset(conn, source, f.tell())
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line), f.tell()
            except json.JSONDecodeError:
                continue
    conn.commit()


# ---------------------------------------------------------------------------
# ingestion — journal
# ---------------------------------------------------------------------------

def _upsert_signal(conn, e, received_at=None):
    sid = e.get("signal_id") or e.get("id")
    if not sid:
        return
    conn.execute(
        """INSERT INTO signals(signal_id, symbol, timeframe, direction, strategy,
               setup_class, entry_price, stop_loss, take_profit,
               ema_fast, ema_slow, rsi, atr,
               candle_time, server_time, trigger, received_at, raw_json, ingested_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(signal_id) DO UPDATE SET
               symbol=COALESCE(excluded.symbol, signals.symbol),
               timeframe=COALESCE(excluded.timeframe, signals.timeframe),
               direction=COALESCE(excluded.direction, signals.direction),
               strategy=COALESCE(excluded.strategy, signals.strategy),
               setup_class=COALESCE(excluded.setup_class, signals.setup_class),
               entry_price=COALESCE(excluded.entry_price, signals.entry_price),
               stop_loss=COALESCE(excluded.stop_loss, signals.stop_loss),
               take_profit=COALESCE(excluded.take_profit, signals.take_profit),
               ema_fast=COALESCE(excluded.ema_fast, signals.ema_fast),
               ema_slow=COALESCE(excluded.ema_slow, signals.ema_slow),
               rsi=COALESCE(excluded.rsi, signals.rsi),
               atr=COALESCE(excluded.atr, signals.atr),
               candle_time=COALESCE(excluded.candle_time, signals.candle_time),
               server_time=COALESCE(excluded.server_time, signals.server_time),
               trigger=COALESCE(excluded.trigger, signals.trigger),
               received_at=COALESCE(excluded.received_at, signals.received_at),
               raw_json=excluded.raw_json""",
        (
            sid, e.get("symbol"), e.get("timeframe"), e.get("direction"),
            e.get("strategy"), _setup_class(e.get("strategy"), e.get("trigger")),
            e.get("entry_price"), e.get("stop_loss"), e.get("take_profit"),
            e.get("ema_fast"), e.get("ema_slow"),
            e.get("rsi") if e.get("rsi") is not None else e.get("rsi_value"),
            e.get("atr") if e.get("atr") is not None else e.get("atr_value"),
            _parse_server(e.get("candle_time")) or e.get("candle_time"),
            _parse_server(e.get("server_time")) or e.get("server_time"),
            e.get("trigger"),
            received_at or _parse_local(e.get("time")),
            _raw(e), _utcnow(),
        ),
    )


def _upsert_trade_open(conn, e, local_time=None, from_broker=False):
    ticket = e.get("ticket")
    if not ticket:
        return
    row = conn.execute("SELECT * FROM trades WHERE ticket=?", (ticket,)).fetchone()
    if row and row["status"] == "closed":
        # A closed trade stays closed; only enrich null fields from broker.
        fills = {}
        for k, src in (("symbol", "symbol"), ("direction", "direction"),
                       ("volume", "volume"), ("entry_price", "entry_price"),
                       ("signal_id", "signal_id"), ("command_id", "command_id")):
            if not row[k] and e.get(src) is not None:
                fills[k] = e.get(src)
        if fills:
            conn.execute(
                "UPDATE trades SET %s, updated_at=? WHERE ticket=?" % ",".join(
                    f"{k}=?" for k in fills),
                (*fills.values(), _utcnow(), ticket),
            )
        return
    conn.execute(
        """INSERT INTO trades(ticket, signal_id, command_id, symbol, direction,
               volume, entry_price, fill_price, deal,
               opened_server_time, opened_local_time, status,
               raw_json, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(ticket) DO UPDATE SET
               signal_id=COALESCE(excluded.signal_id, trades.signal_id),
               command_id=COALESCE(excluded.command_id, trades.command_id),
               symbol=COALESCE(excluded.symbol, trades.symbol),
               direction=COALESCE(excluded.direction, trades.direction),
               volume=COALESCE(excluded.volume, trades.volume),
               entry_price=COALESCE(excluded.entry_price, trades.entry_price),
               fill_price=COALESCE(excluded.fill_price, trades.fill_price),
               deal=COALESCE(excluded.deal, trades.deal),
               opened_server_time=COALESCE(excluded.opened_server_time,
                                           trades.opened_server_time),
               opened_local_time=COALESCE(excluded.opened_local_time,
                                          trades.opened_local_time),
               status=CASE WHEN trades.status='closed' THEN 'closed'
                           ELSE excluded.status END,
               raw_json=excluded.raw_json, updated_at=excluded.updated_at""",
        (
            ticket, e.get("signal_id"), e.get("command_id"),
            e.get("symbol"), e.get("direction"), e.get("volume"),
            e.get("entry_price"), e.get("fill_price") or e.get("entry_price"),
            e.get("deal"),
            _parse_server(e.get("time")), local_time,
            "open", _raw(e), _utcnow(),
        ),
    )


def _apply_trade_close(conn, e, from_broker=False, local_time=None):
    """Record a close. Broker-history closes are authoritative for P&L."""
    ticket = e.get("ticket")
    if not ticket:
        return
    profit = e.get("profit")
    reason = (e.get("reason") or "").lower()
    note = e.get("note") or ""
    # Provisional if it says reconciled/estimated, or profit is missing.
    provisional = 1 if ("reconcil" in reason or "reconcil" in note.lower()
                        or "estimat" in note.lower() or profit is None) else 0
    # Broker history (nova_trades.jsonl) or a journal close whose reason is
    # exactly "broker" is broker-confirmed -- unless the record itself says
    # the numbers were reconciled/estimated, in which case it stays
    # provisional and broker_confirmed stays 0.
    broker_confirmed = 1 if (from_broker or reason == "broker") \
        and not provisional else 0
    row = conn.execute("SELECT * FROM trades WHERE ticket=?", (ticket,)).fetchone()
    exit_ts = _parse_server(e.get("time")) or local_time
    if row and row["broker_confirmed"] and not from_broker:
        # Never let a less-authoritative journal close overwrite broker data.
        return
    if row:
        conn.execute(
            """UPDATE trades SET status='closed',
                   exit_server_time=COALESCE(?, exit_server_time),
                   exit_price=COALESCE(?, exit_price),
                   profit=?, commission=COALESCE(?, commission),
                   swap=COALESCE(?, swap), exit_reason=?,
                   broker_confirmed=?, provisional=?,
                   outcome_note=COALESCE(?, outcome_note),
                   raw_json=?, updated_at=? WHERE ticket=?""",
            (exit_ts, e.get("exit_price"), profit, e.get("commission"),
             e.get("swap"), e.get("reason"), broker_confirmed, provisional,
             note or None, _raw(e), _utcnow(), ticket),
        )
    else:
        # Close seen without an open record (e.g. opened before journaling
        # started): create the row, marked closed.
        conn.execute(
            """INSERT INTO trades(ticket, signal_id, command_id, symbol, direction,
                   volume, status, exit_server_time, exit_price, profit,
                   commission, swap, exit_reason, broker_confirmed, provisional,
                   outcome_note, raw_json, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticket, e.get("signal_id"), e.get("command_id"), e.get("symbol"),
             e.get("direction"), e.get("volume"), "closed", exit_ts,
             e.get("exit_price"), profit, e.get("commission"), e.get("swap"),
             e.get("reason"), broker_confirmed, provisional, note or None,
             _raw(e), _utcnow()),
        )


def ingest_journal(conn, path=JOURNAL_PATH):
    """Ingest nova_journal.jsonl events (idempotent via per-file cursor)."""
    counts = {}
    for e, _pos in _iter_new_lines(conn, "journal", path):
        if not isinstance(e, dict):
            continue
        t = e.get("type")
        counts[t] = counts.get(t, 0) + 1
        local_time = _parse_local(e.get("time"))
        if t == "signal.received":
            _upsert_signal(conn, e, received_at=local_time)
        elif t == "trade.decision":
            _upsert_signal(conn, e, received_at=local_time)
            ctx = e.get("context") or {}
            conn.execute(
                """INSERT OR IGNORE INTO decisions(signal_id, decision, volume,
                       risk_amount, capital_basis, command_id, context_json,
                       time, raw_json)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (e.get("signal_id"), e.get("decision"), e.get("volume"),
                 e.get("risk_amount"),
                 (e.get("context") or {}).get("capital_basis"),
                 e.get("command_id"),
                 json.dumps(_sanitize(ctx), ensure_ascii=False),
                 local_time, _raw(e)),
            )
        elif t == "command.sent":
            cmd = e.get("command") or {}
            conn.execute(
                """INSERT INTO commands(command_id, signal_id, symbol, direction,
                       volume, sl, tp, time, ack_status, raw_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(command_id) DO UPDATE SET
                       ack_status=COALESCE(commands.ack_status, excluded.ack_status),
                       raw_json=excluded.raw_json""",
                (cmd.get("id"), cmd.get("signal_id"), cmd.get("symbol"),
                 cmd.get("direction"), cmd.get("volume"), cmd.get("sl"),
                 cmd.get("tp"), local_time, "sent", _raw(e)),
            )
        elif t == "trade.update":
            _upsert_trade_open(conn, e, local_time=local_time)
            cmd_id = e.get("command_id")
            if cmd_id:
                conn.execute(
                    """INSERT INTO commands(command_id, ack_status, fill_ticket,
                               fill_deal, fill_price, signal_id)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(command_id) DO UPDATE SET
                           ack_status='filled',
                           fill_ticket=COALESCE(excluded.fill_ticket,
                                                commands.fill_ticket),
                           fill_deal=COALESCE(excluded.fill_deal, commands.fill_deal),
                           fill_price=COALESCE(excluded.fill_price,
                                               commands.fill_price)""",
                    (cmd_id, "filled", e.get("ticket"), e.get("deal"),
                     e.get("fill_price"), e.get("signal_id")),
                )
        elif t == "trade.opened":
            _upsert_trade_open(conn, e, local_time=local_time)
            cmd_id = e.get("command_id")
            if cmd_id:
                conn.execute(
                    """INSERT INTO commands(command_id, ack_status, fill_ticket,
                               fill_deal, fill_price, signal_id)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(command_id) DO UPDATE SET ack_status='filled',
                           fill_ticket=COALESCE(excluded.fill_ticket, commands.fill_ticket),
                           fill_deal=COALESCE(excluded.fill_deal, commands.fill_deal),
                           fill_price=COALESCE(excluded.fill_price, commands.fill_price)""",
                    (cmd_id, "filled", e.get("ticket"), e.get("deal"),
                     e.get("fill_price") or e.get("entry_price"),
                     e.get("signal_id")),
                )
        elif t == "trade.closed":
            _apply_trade_close(conn, e, from_broker=False, local_time=local_time)
        elif t == "trade.reconciled":
            ticket = e.get("ticket")
            note = e.get("note") or ""
            if ticket:
                conn.execute(
                    """UPDATE trades SET provisional=1,
                           outcome_note=COALESCE(outcome_note,'') || ?,
                           updated_at=? WHERE ticket=?""",
                    ("\n[reconciled] " + note, _utcnow(), ticket),
                )
        elif t == "signal.outcome":
            conn.execute(
                """INSERT INTO signal_outcomes(signal_id, symbol, direction,
                       outcome, hit_at, snapshots, time)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(signal_id) DO UPDATE SET
                       outcome=excluded.outcome, hit_at=excluded.hit_at,
                       snapshots=excluded.snapshots, time=excluded.time""",
                (e.get("signal_id"), e.get("symbol"), e.get("direction"),
                 e.get("outcome"), _parse_local(e.get("hit_at")),
                 e.get("snapshots"), local_time),
            )
        elif t == "config.changed":
            conn.execute(
                """INSERT OR IGNORE INTO notes(kind, ref_date, title, body,
                       stats_json, auto, created_at)
                   VALUES('config',?,?,?,?,?,?)""",
                (_date_of(local_time),
                 "config.changed by %s" % (e.get("actor") or "unknown"),
                 "old=%s new=%s note=%s" % (e.get("old"), e.get("new"),
                                            e.get("note")),
                 json.dumps(_sanitize({"old": e.get("old"),
                                       "new": e.get("new")}), ensure_ascii=False),
                 1, _utcnow()),
            )
        elif t == "strategy.changed":
            conn.execute(
                """INSERT OR IGNORE INTO notes(kind, ref_date, title, body,
                       auto, created_at)
                   VALUES('strategy',?,?,?,1,?)""",
                (_date_of(local_time), "strategy.changed",
                 str(e.get("change"))[:4000], _utcnow()),
            )
        elif t == "daily_summary":
            conn.execute(
                """INSERT OR IGNORE INTO notes(kind, ref_date, title, body,
                       stats_json, auto, created_at)
                   VALUES('daily_auto',?,?,?,?,?,?)""",
                (e.get("date"), "daily summary %s" % e.get("date"),
                 "auto-generated daily summary", _raw(e), 1, _utcnow()),
            )
    conn.commit()
    return counts


def ingest_audit(conn, path=AUDIT_PATH):
    """Ingest run/safety_audit.jsonl (owner overrides, gate deployments)."""
    counts = {"safety.audit": 0}
    for e, _pos in _iter_new_lines(conn, "safety_audit", path):
        if not isinstance(e, dict):
            continue
        counts["safety.audit"] += 1
        d = e.get("details") or {}
        conn.execute(
            """INSERT OR IGNORE INTO notes(kind, ref_date, title, body,
                   auto, created_at)
               VALUES('safety',?,?,?,?,?)""",
            (_date_of(_parse_local(e.get("time"))),
             "safety: %s (%s)" % (e.get("event"), e.get("actor")),
             str(d.get("note") or d)[:4000], 1, _utcnow()),
        )
    conn.commit()
    return counts


# ---------------------------------------------------------------------------
# ingestion — MT5 files (broker side)
# ---------------------------------------------------------------------------

def ingest_trades_file(conn, path=TRADES_PATH):
    """Ingest MQL5 nova_trades.jsonl (broker fills/rejections/closes).

    This is the broker-authoritative source: closes found here but missing
    from the journal are reconciled (same pattern as backup_trading.py's
    reconcile_closes, but written into the DB with broker_confirmed=1).
    """
    counts = {}
    for e, _pos in _iter_new_lines(conn, "trades_file", path):
        if not isinstance(e, dict):
            continue
        t = e.get("type")
        counts[t] = counts.get(t, 0) + 1
        local_time = _parse_local(e.get("journal_time"))
        if t == "trade.opened":
            _upsert_trade_open(conn, e, local_time=local_time, from_broker=True)
            cmd_id = e.get("command_id")
            if cmd_id:
                conn.execute(
                    """INSERT INTO commands(command_id, ack_status, fill_ticket,
                               fill_deal, fill_price, signal_id)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(command_id) DO UPDATE SET ack_status='filled',
                           fill_ticket=COALESCE(excluded.fill_ticket, commands.fill_ticket),
                           fill_deal=COALESCE(excluded.fill_deal, commands.fill_deal),
                           fill_price=COALESCE(excluded.fill_price, commands.fill_price)""",
                    (cmd_id, "filled", e.get("ticket"), e.get("deal"),
                     e.get("fill_price"), e.get("signal_id")),
                )
        elif t == "trade.rejected":
            cmd_id = e.get("command_id")
            if cmd_id:
                conn.execute(
                    """INSERT INTO commands(command_id, ack_status, reject_reason,
                               signal_id, raw_json)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(command_id) DO UPDATE SET
                           ack_status='rejected',
                           reject_reason=COALESCE(excluded.reject_reason,
                                                  commands.reject_reason),
                           raw_json=excluded.raw_json""",
                    (cmd_id, "rejected", e.get("reason"),
                     e.get("signal_id"), _raw(e)),
                )
        elif t == "trade.closed":
            _apply_trade_close(conn, e, from_broker=True, local_time=local_time)
    conn.commit()
    return counts


def ingest_signals_file(conn, path=SIGNALS_PATH):
    """Ingest MQL5 nova_signals.jsonl (full signal snapshots incl. indicators)."""
    counts = {"signal.detected": 0}
    for e, _pos in _iter_new_lines(conn, "signals_file", path):
        if not isinstance(e, dict):
            continue
        counts["signal.detected"] += 1
        e = dict(e)
        e["signal_id"] = e.get("id") or e.get("signal_id")
        _upsert_signal(conn, e, received_at=_parse_server(e.get("server_time")))
    conn.commit()
    return counts


def ingest_positions_snapshot(conn, path=POSITIONS_PATH):
    """Record the current broker open-positions snapshot.

    Positions still open on the broker are marked status='open'. The
    broker account number is NEVER stored (sanitized out).
    Returns the list of open tickets.
    """
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return []
    positions = snap.get("positions") or []
    open_tickets = []
    for p in positions:
        ticket = p.get("ticket")
        if not ticket:
            continue
        open_tickets.append(ticket)
        row = conn.execute("SELECT status FROM trades WHERE ticket=?",
                           (ticket,)).fetchone()
        if row and row["status"] == "closed":
            continue  # broker snapshot stale vs a confirmed close; keep close
        conn.execute(
            """INSERT INTO trades(ticket, symbol, direction, volume,
                   entry_price, status, raw_json, updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(ticket) DO UPDATE SET
                   symbol=COALESCE(excluded.symbol, trades.symbol),
                   direction=COALESCE(excluded.direction, trades.direction),
                   volume=COALESCE(excluded.volume, trades.volume),
                   status=CASE WHEN trades.status='closed' THEN 'closed'
                               ELSE 'open' END,
                   raw_json=excluded.raw_json, updated_at=excluded.updated_at""",
            (ticket, p.get("symbol"), p.get("type"), p.get("volume"),
             p.get("open_price"), "open", _raw(p), _utcnow()),
        )
    conn.commit()
    return open_tickets


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------

def reconcile(conn, open_tickets=()):
    """Reconcile broker history vs journal closes.

    1. Any trade.closed present in nova_trades.jsonl but missing from the
       journal is marked closed + broker_confirmed (pattern reused from
       backup_trading.py reconcile_closes).
    2. Any trade with an open record, no close record, and NOT in the
       current broker positions snapshot is marked status='unknown',
       provisional=1, with an explicit outcome note. The two known
       vanished tickets (GBPJPY 10607517845, XAUEUR 10606155417) get the
       explicit honesty note: outcome UNKNOWN, never claim gains banked.
    Never invents P&L: profit stays NULL for unknown outcomes.
    Returns a dict of what was marked.
    """
    done = {"broker_closed": 0, "unknown": 0, "still_open": 0}
    open_set = set(open_tickets or ())
    for row in conn.execute("SELECT ticket, symbol, status, broker_confirmed"
                            " FROM trades"):
        ticket, symbol, status = row["ticket"], row["symbol"], row["status"]
        if status == "closed":
            continue
        if ticket in open_set:
            if status != "open":
                conn.execute(
                    "UPDATE trades SET status='open', updated_at=? WHERE ticket=?",
                    (_utcnow(), ticket),
                )
            done["still_open"] += 1
            continue
        # Not closed, not in the broker's current open list.
        if ticket in KNOWN_VANISHED_TICKETS:
            note = (
                "Ticket %s (%s) disappeared from the broker view without a "
                "clean close record during a host-reboot / terminal-downtime "
                "window. Outcome UNKNOWN -- the position's floating gain was "
                "NEVER confirmed banked; P&L is NULL, not estimated."
                % (ticket, KNOWN_VANISHED_TICKETS[ticket])
            )
        else:
            note = (
                "No close record and ticket not present in the latest broker "
                "positions snapshot. Outcome UNKNOWN/provisional -- P&L left "
                "NULL until a broker-history close is reconciled."
            )
        conn.execute(
            """UPDATE trades SET status='unknown', provisional=1,
                   outcome_note=?, updated_at=? WHERE ticket=?""",
            (note, _utcnow(), ticket),
        )
        done["unknown"] += 1
    conn.commit()
    # broker_closed count: closes that are broker-confirmed (from file ingest)
    row = conn.execute(
        "SELECT COUNT(*) n FROM trades WHERE status='closed' AND broker_confirmed=1"
    ).fetchone()
    done["broker_closed"] = row["n"]
    return done


def detect_gaps(conn, path=JOURNAL_PATH, threshold_min=45):
    """Detect silent gaps in journal event flow (outages / terminal downtime).

    A gap is recorded as kind='detected_gap' with the honest caveat that it
    is derived from missing journal lines, not a confirmed outage. Idempotent
    via UNIQUE(kind, started_at).
    """
    if not os.path.isfile(path):
        return 0
    times = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_local(e.get("time"))
            if ts:
                times.append(ts)
    times.sort()
    added = 0
    prev = None
    for ts in times:
        if prev:
            gap = (datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                   - datetime.strptime(prev, "%Y-%m-%d %H:%M:%S"))
            if gap > timedelta(minutes=threshold_min):
                cur = conn.execute(
                    """INSERT OR IGNORE INTO outages(kind, started_at, ended_at,
                           note, source, created_at)
                       VALUES('detected_gap',?,?,?, 'journal-time-gap', ?)""",
                    (prev, ts,
                     "No journal events for %d minutes between %s and %s. "
                     "Derived from missing journal lines -- likely terminal "
                     "downtime / host reboot, not confirmed. Trades may have "
                     "opened or closed unseen in this window."
                     % (int(gap.total_seconds() // 60), prev, ts), _utcnow()),
                )
                if cur.rowcount:
                    added += 1
        prev = ts
    conn.commit()
    return added


def ingest_all(conn, files_dir=FILES_DIR, state_dir=STATE_DIR):
    """Full pipeline: journal + audit + broker files + snapshot + reconcile."""
    journal = os.path.join(state_dir, "nova_journal.jsonl")
    audit = os.path.join(state_dir, "safety_audit.jsonl")
    trades_f = os.path.join(files_dir, "nova_trades.jsonl")
    signals_f = os.path.join(files_dir, "nova_signals.jsonl")
    positions_f = os.path.join(files_dir, "nova_positions.json")
    counts = {}
    counts["journal"] = ingest_journal(conn, journal)
    counts["audit"] = ingest_audit(conn, audit)
    counts["trades_file"] = ingest_trades_file(conn, trades_f)
    counts["signals_file"] = ingest_signals_file(conn, signals_f)
    open_tickets = ingest_positions_snapshot(conn, positions_f)
    counts["open_tickets"] = open_tickets
    counts["reconcile"] = reconcile(conn, open_tickets)
    counts["gaps_detected"] = detect_gaps(conn, journal)
    return counts


# ---------------------------------------------------------------------------
# query helpers — per-trade review and aggregates
# ---------------------------------------------------------------------------

def _row_to_dict(row):
    return dict(row) if row is not None else None


def get_trade_review(conn, ticket):
    """Full per-trade dossier: signal, decision, command, trade, lesson.

    Returns None if the ticket is unknown.
    """
    trade = _row_to_dict(
        conn.execute("SELECT * FROM trades WHERE ticket=?", (ticket,)).fetchone()
    )
    if not trade:
        return None
    signal = None
    decision = None
    if trade.get("signal_id"):
        signal = _row_to_dict(conn.execute(
            "SELECT * FROM signals WHERE signal_id=?",
            (trade["signal_id"],)).fetchone())
        decision = _row_to_dict(conn.execute(
            "SELECT * FROM decisions WHERE signal_id=? ORDER BY id DESC LIMIT 1",
            (trade["signal_id"],)).fetchone())
    command = None
    if trade.get("command_id"):
        command = _row_to_dict(conn.execute(
            "SELECT * FROM commands WHERE command_id=?",
            (trade["command_id"],)).fetchone())
    lesson = _row_to_dict(conn.execute(
        "SELECT * FROM lessons WHERE ref=?", (str(ticket),)).fetchone())
    outcome = _row_to_dict(conn.execute(
        "SELECT * FROM signal_outcomes WHERE signal_id=?",
        (trade["signal_id"],)).fetchone()) if trade.get("signal_id") else None
    return {"trade": trade, "signal": signal, "decision": decision,
            "command": command, "lesson": lesson, "signal_outcome": outcome}


def list_trades(conn, status=None):
    q = "SELECT * FROM trades"
    args = ()
    if status:
        q += " WHERE status=?"
        args = (status,)
    q += " ORDER BY ticket"
    return [_row_to_dict(r) for r in conn.execute(q, args)]


def list_open_trades(conn):
    return list_trades(conn, "open")


def list_unknown_outcomes(conn):
    """Trades whose outcome is unknown/provisional -- must stay explicit."""
    return [_row_to_dict(r) for r in conn.execute(
        "SELECT * FROM trades WHERE status='unknown' ORDER BY ticket")]


def _pnl_stats(conn, where, args):
    rows = conn.execute(
        "SELECT profit FROM trades WHERE status='closed' AND profit IS NOT NULL "
        + (" AND " + where if where else ""), args).fetchall()
    pnls = [r["profit"] for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n = len(pnls)
    return {
        "closed_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100.0 * len(wins) / n, 1) if n else 0.0,
        "total_pnl": round(sum(pnls), 2) if pnls else 0.0,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "best": round(max(pnls), 2) if pnls else 0.0,
        "worst": round(min(pnls), 2) if pnls else 0.0,
    }


def daily_aggregate(conn, date_str):
    """Aggregate for one calendar day (YYYY-MM-DD, local journal time)."""
    sig_n = conn.execute(
        "SELECT COUNT(*) n FROM signals WHERE substr(received_at,1,10)=?",
        (date_str,)).fetchone()["n"]
    dec = conn.execute(
        "SELECT decision, COUNT(*) n FROM decisions "
        "WHERE substr(time,1,10)=? GROUP BY decision", (date_str,)).fetchall()
    dec_map = {r["decision"]: r["n"] for r in dec}
    commanded = dec_map.get("commanded", 0)
    opens = conn.execute(
        "SELECT COUNT(*) n FROM trades "
        "WHERE substr(opened_local_time,1,10)=? OR substr(opened_server_time,1,10)=?",
        (date_str, date_str)).fetchone()["n"]
    closes = conn.execute(
        "SELECT COUNT(*) n FROM trades WHERE status='closed' AND "
        "(substr(exit_server_time,1,10)=?)", (date_str,)).fetchone()["n"]
    pnl = _pnl_stats(conn, "substr(exit_server_time,1,10)=?", (date_str,))
    lessons = conn.execute(
        "SELECT COUNT(*) n FROM lessons WHERE substr(created_at,1,10)=?",
        (date_str,)).fetchone()["n"]
    return {
        "date": date_str, "signals": sig_n, "decisions": dec_map,
        "commanded": commanded, "opens": opens, "closes": closes,
        **pnl, "lessons_written": lessons,
    }


def weekly_aggregate(conn, week_start):
    """Aggregate for a 7-day window starting week_start (YYYY-MM-DD)."""
    start = datetime.strptime(week_start, "%Y-%m-%d")
    days = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    agg = {"week_start": week_start, "week_end": days[-1], "days": {}}
    totals = {"signals": 0, "commanded": 0, "opens": 0, "closes": 0,
              "lessons_written": 0}
    all_pnls = []
    for d in days:
        dd = daily_aggregate(conn, d)
        agg["days"][d] = dd
        for k in totals:
            totals[k] += dd.get(k, 0)
    # week-level P&L from trades closed in window
    rows = conn.execute(
        "SELECT profit FROM trades WHERE status='closed' AND profit IS NOT NULL "
        "AND substr(exit_server_time,1,10) BETWEEN ? AND ?",
        (week_start, days[-1])).fetchall()
    all_pnls = [r["profit"] for r in rows]
    wins = [p for p in all_pnls if p > 0]
    agg.update(totals)
    agg.update({
        "closed_trades": len(all_pnls),
        "wins": len(wins),
        "losses": len(all_pnls) - len(wins),
        "win_rate_pct": round(100.0 * len(wins) / len(all_pnls), 1) if all_pnls else 0.0,
        "total_pnl": round(sum(all_pnls), 2) if all_pnls else 0.0,
    })
    return agg


# ---------------------------------------------------------------------------
# lessons and notes
# ---------------------------------------------------------------------------

def add_lesson(conn, ref, kind="trade", why_entered=None, why_exited=None,
               what_worked=None, what_failed=None, what_to_change=None,
               author="agent"):
    """Write or update the per-trade/per-signal lesson (the 'why' record).

    ref: ticket number (kind='trade') or signal_id (kind='signal').
    """
    now = _utcnow()
    conn.execute(
        """INSERT INTO lessons(ref, kind, why_entered, why_exited, what_worked,
               what_failed, what_to_change, author, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(ref) DO UPDATE SET
               why_entered=COALESCE(excluded.why_entered, lessons.why_entered),
               why_exited=COALESCE(excluded.why_exited, lessons.why_exited),
               what_worked=COALESCE(excluded.what_worked, lessons.what_worked),
               what_failed=COALESCE(excluded.what_failed, lessons.what_failed),
               what_to_change=COALESCE(excluded.what_to_change,
                                       lessons.what_to_change),
               updated_at=excluded.updated_at""",
        (str(ref), kind, why_entered, why_exited, what_worked, what_failed,
         what_to_change, author, now, now),
    )
    conn.commit()


def get_lesson(conn, ref):
    return _row_to_dict(conn.execute(
        "SELECT * FROM lessons WHERE ref=?", (str(ref),)).fetchone())


def add_note(conn, kind, ref_date, title, body, stats=None, auto=0):
    conn.execute(
        """INSERT OR IGNORE INTO notes(kind, ref_date, title, body,
               stats_json, auto, created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (kind, ref_date, title, body,
         json.dumps(_sanitize(stats), ensure_ascii=False) if stats else None,
         auto, _utcnow()),
    )
    conn.commit()


def list_notes(conn, kind=None, ref_date=None):
    q = "SELECT * FROM notes"
    conds, args = [], []
    if kind:
        conds.append("kind=?"); args.append(kind)
    if ref_date:
        conds.append("ref_date=?"); args.append(ref_date)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY ref_date, id"
    return [_row_to_dict(r) for r in conn.execute(q, args)]


def table_counts(conn):
    tables = ["signals", "decisions", "commands", "trades", "signal_outcomes",
              "lessons", "notes", "outages", "ingest_state"]
    return {t: conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
            for t in tables}


def export_sanitized(conn):
    """Export the whole DB as sanitized JSON (safe for the GitHub evidence
    repo): account numbers / secrets are stripped from every raw payload."""
    data = {"exported_at": _utcnow(), "schema_version": SCHEMA_VERSION}
    for t in ["signals", "decisions", "commands", "trades", "signal_outcomes",
              "lessons", "notes", "outages"]:
        rows = [_row_to_dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
        for r in rows:
            if r.get("raw_json"):
                try:
                    r["raw_json"] = json.dumps(
                        _sanitize(json.loads(r["raw_json"])), ensure_ascii=False)
                except (ValueError, TypeError):
                    pass
        data[t] = rows
    return data


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_ingest(args):
    conn = connect(args.db)
    init_schema(conn)
    counts = ingest_all(conn)
    print(json.dumps(counts, indent=1, default=str))
    print("table counts:", json.dumps(table_counts(conn)))
    conn.close()


def cmd_stats(args):
    conn = connect(args.db)
    init_schema(conn)
    print(json.dumps(table_counts(conn), indent=1))
    unk = list_unknown_outcomes(conn)
    print("unknown outcomes:", [(r["ticket"], r["symbol"]) for r in unk])
    conn.close()


def cmd_review(args):
    conn = connect(args.db)
    init_schema(conn)
    dossier = get_trade_review(conn, args.ticket)
    print(json.dumps(dossier, indent=1, default=str))
    conn.close()


def cmd_lesson(args):
    conn = connect(args.db)
    init_schema(conn)
    add_lesson(conn, args.ticket, kind="trade",
               why_entered=args.why_entered, why_exited=args.why_exited,
               what_worked=args.worked, what_failed=args.failed,
               what_to_change=args.change, author=args.author)
    print("lesson saved for ticket", args.ticket)
    conn.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Nova trade-learning database")
    ap.add_argument("--db", default=None, help="sqlite path override")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest", help="ingest all sources + reconcile + gap detect")
    sub.add_parser("stats", help="row counts + unknown outcomes")
    p = sub.add_parser("review", help="per-trade dossier")
    p.add_argument("--ticket", type=int, required=True)
    p = sub.add_parser("lesson", help="write a per-trade lesson")
    p.add_argument("--ticket", required=True)
    p.add_argument("--why-entered", default=None)
    p.add_argument("--why-exited", default=None)
    p.add_argument("--worked", default=None)
    p.add_argument("--failed", default=None)
    p.add_argument("--change", default=None)
    p.add_argument("--author", default="agent")
    args = ap.parse_args(argv)
    {"ingest": cmd_ingest, "stats": cmd_stats, "review": cmd_review,
     "lesson": cmd_lesson}[args.cmd](args)


if __name__ == "__main__":
    main()
