"""Local SQLite persistence for the forex-agent subsystem.

This is the LOCAL store (TARGET_ARCHITECTURE.md §5) — it is NOT the Worker
D1 schema. It holds risk state, the kill-switch latch, the event queue, the
audit log, and the journal. It must survive Worker/cloud outage: local risk
controls keep working with no network.

Contract (exact):
    Store(path)
      .get_risk_state() -> dict            / .set_risk_state(dict)
      .get_kill_switch() -> {"engaged": bool, "source": str|None, "ts": str|None}
      .set_kill_switch(engaged, source)
      .enqueue_event(dict)                / .dequeue_events(limit=100) -> list
      .audit(dict)                        / .query_audit(limit=100, since=None) -> list
      .journal_add(dict)                  / .journal_query(**filters) -> list

Thread-safe: a single connection guarded by a re-entrant lock, opened with
check_same_thread=False so daemon threads can share one Store.

Default path: <install_dir>/storage/local.db, i.e. forex-agent/storage/local.db
next to this module. Override with the FOREX_AGENT_STORAGE env var.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "schema.sql"
DEFAULT_DB_PATH = Path(
    os.environ.get("FOREX_AGENT_STORAGE", Path(__file__).resolve().parent / "local.db")
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """Thread-safe local SQLite store. See module docstring for the contract."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else Path(DEFAULT_DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
                self._conn.executescript(f.read())
            self._conn.commit()

    # -- lifecycle ------------------------------------------------------
    def close(self):
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # -- risk state (kv) --------------------------------------------------
    def get_risk_state(self) -> dict:
        """Return the whole persisted risk-state dict ({} if never set)."""
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM risk_state").fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def set_risk_state(self, state: dict) -> None:
        """Upsert every key of `state` (values JSON-encoded)."""
        if not isinstance(state, dict):
            raise TypeError("risk state must be a dict")
        with self._lock:
            self._conn.executemany(
                "INSERT INTO risk_state(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(k, json.dumps(v)) for k, v in state.items()],
            )
            self._conn.commit()

    # -- kill switch --------------------------------------------------------
    def get_kill_switch(self) -> dict:
        """{"engaged": bool, "source": str|None, "ts": str|None}."""
        with self._lock:
            row = self._conn.execute(
                "SELECT engaged, source, ts FROM kill_switch WHERE id = 1"
            ).fetchone()
        if row is None:
            return {"engaged": False, "source": None, "ts": None}
        return {
            "engaged": bool(row["engaged"]),
            "source": row["source"],
            "ts": row["ts"],
        }

    def set_kill_switch(self, engaged: bool, source=None) -> None:
        """Latch the kill switch. `source`: 'agent' | 'worker' | 'local' | ..."""
        ts = _utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO kill_switch(id, engaged, source, ts) VALUES (1, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET engaged = excluded.engaged, "
                "source = excluded.source, ts = excluded.ts",
                (1 if engaged else 0, source, ts),
            )
            self._conn.commit()

    # -- event queue (FIFO) ---------------------------------------------------
    def enqueue_event(self, event: dict) -> int:
        """Append an event dict; returns the queue id."""
        if not isinstance(event, dict):
            raise TypeError("event must be a dict")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO event_queue(ts, event) VALUES (?, ?)",
                (_utcnow(), json.dumps(event)),
            )
            self._conn.commit()
            return cur.lastrowid

    def dequeue_events(self, limit: int = 100) -> list:
        """Pop up to `limit` oldest events (FIFO). Each returned dict is the
        stored event plus "_queue_id" and "_ts" metadata keys."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, event FROM event_queue ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                self._conn.execute(
                    f"DELETE FROM event_queue WHERE id IN ({','.join('?' * len(ids))})",
                    ids,
                )
                self._conn.commit()
        out = []
        for r in rows:
            entry = json.loads(r["event"])
            entry["_queue_id"] = r["id"]
            entry["_ts"] = r["ts"]
            out.append(entry)
        return out

    # -- audit log (append-only) ------------------------------------------------
    def audit(self, record: dict) -> int:
        """Append an audit record dict; returns the row id."""
        if not isinstance(record, dict):
            raise TypeError("audit record must be a dict")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO audit_log(ts, record) VALUES (?, ?)",
                (_utcnow(), json.dumps(record)),
            )
            self._conn.commit()
            return cur.lastrowid

    def query_audit(self, limit: int = 100, since=None) -> list:
        """Newest-first audit records. `since`: ISO-8601 lower bound on ts."""
        with self._lock:
            if since is None:
                rows = self._conn.execute(
                    "SELECT id, ts, record FROM audit_log ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, ts, record FROM audit_log WHERE ts >= ? "
                    "ORDER BY id DESC LIMIT ?",
                    (since, limit),
                ).fetchall()
        out = []
        for r in rows:
            entry = json.loads(r["record"])
            entry["_audit_id"] = r["id"]
            entry["_ts"] = r["ts"]
            out.append(entry)
        return out

    # -- journal (experience / reflections backing store) -------------------------
    def journal_add(self, entry: dict) -> int:
        """Add a journal entry. Top-level `kind` is required; `symbol`,
        `direction`, `outcome` are indexed when present. The whole dict is
        kept as the JSON payload. Returns the row id."""
        if not isinstance(entry, dict):
            raise TypeError("journal entry must be a dict")
        if not entry.get("kind"):
            raise ValueError("journal entry requires a 'kind'")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO journal(ts, kind, symbol, direction, outcome, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _utcnow(),
                    entry.get("kind"),
                    entry.get("symbol"),
                    entry.get("direction"),
                    entry.get("outcome"),
                    json.dumps(entry),
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def journal_query(self, limit: int = 100, **filters) -> list:
        """Query journal entries, newest first.

        Supported filter keys: kind, symbol, direction, outcome, since
        (ISO-8601 lower bound on ts), until (ISO-8601 upper bound on ts).
        Unknown filter keys raise ValueError. Each returned dict is the
        stored entry plus "_journal_id" and "_ts".
        """
        allowed = {"kind", "symbol", "direction", "outcome", "since", "until"}
        unknown = set(filters) - allowed
        if unknown:
            raise ValueError(f"Unsupported journal filters: {sorted(unknown)}")

        where, params = [], []
        for key in ("kind", "symbol", "direction", "outcome"):
            if key in filters:
                where.append(f"{key} = ?")
                params.append(filters[key])
        if "since" in filters:
            where.append("ts >= ?")
            params.append(filters["since"])
        if "until" in filters:
            where.append("ts <= ?")
            params.append(filters["until"])

        sql = "SELECT id, ts, payload FROM journal"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            entry = json.loads(r["payload"])
            entry["_journal_id"] = r["id"]
            entry["_ts"] = r["ts"]
            out.append(entry)
        return out
