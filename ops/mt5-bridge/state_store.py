#!/usr/bin/env python3
"""Durable program state in SQLite.

Replaces the scattered run/*.state.json cursor files and the
run/trading_enabled kill-switch file with a single transactional database:
<state_dir>/state.db.

Why: after a platform container restart the daemons must resume exactly where
they left off. Per-daemon JSON cursor files worked, but every daemon invented
its own load/save, the dry-run hooks had to back up and restore JSON files
around probe runs, and nothing was atomic. SQLite gives one store, atomic
commits, and true dry-run (writes are simply skipped).

Tables:
  kv    key -> scalar value (cursors, offsets, flags, config signature)
  seen  (set_name, item) dedup sets, insertion-ordered for pruning
  docs  name -> JSON document (cmd_index, open_map, config_applied, ...)

Legacy migration: the first time a component's state is loaded and the DB has
nothing for it, the legacy JSON file (if present) is imported automatically,
then renamed to <name>.migrated so a stale file can never silently come back.

Components: executor | signal_bridge | chat_notify | trade_notify |
            tg_subscribers | siggen

Small CLI for humans (kill switch etc.):
  python3 state_store.py get <key>
  python3 state_store.py set <key> <value>
  python3 state_store.py show
"""

import json
import os
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE, "run", "state.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  set_name TEXT NOT NULL,
  item TEXT NOT NULL,
  added_at REAL NOT NULL,
  UNIQUE (set_name, item)
);
CREATE TABLE IF NOT EXISTS docs (
  name TEXT PRIMARY KEY,
  doc TEXT NOT NULL,
  updated_at REAL NOT NULL
);
"""

# component -> legacy JSON filename (relative to the state dir) and the
# mapping of legacy dict keys to store locations.
# locations: ("kv", key) | ("seen", set_name) | ("doc", name)
COMPONENTS = {
    "executor": {
        "legacy": "trade_executor.state.json",
        "map": {
            "offset": ("kv", "exec:signals_offset"),
            "seen": ("seen", "exec:signals_seen"),
            "trades_offset": ("kv", "exec:trades_offset"),
            "trades_seen": ("seen", "exec:trades_seen"),
            "cmd_index": ("doc", "exec:cmd_index"),
            "open_map": ("doc", "exec:open_map"),
            "config_sig": ("kv", "exec:config_sig"),
            "config_applied": ("doc", "exec:config_applied"),
        },
        "defaults": {"offset": 0, "seen": [], "trades_offset": 0,
                     "trades_seen": []},
    },
    "signal_bridge": {
        "legacy": "signal_bridge.state.json",
        "map": {"offset": ("kv", "bridge:offset"),
                "seen": ("seen", "bridge:seen")},
        "defaults": {"offset": 0, "seen": []},
    },
    "chat_notify": {
        "legacy": "chat_notify.state.json",
        "map": {"offset": ("kv", "chat_notify:offset"),
                "seen": ("seen", "chat_notify:seen")},
        "defaults": {"offset": 0, "seen": []},
    },
    "trade_notify": {
        "legacy": "trade_notify.state.json",
        "map": {"offset": ("kv", "trade_notify:offset"),
                "seen": ("seen", "trade_notify:seen")},
        "defaults": {"offset": 0, "seen": []},
    },
    "tg_subscribers": {
        "legacy": "subscribers.state.json",
        "map": {"offset": ("kv", "tg:offset")},
        "defaults": {"offset": 0},
    },
    "siggen": {
        "legacy": "siggen_state.json",
        "map": {"__doc__": ("doc", "siggen")},
        "defaults": {},
    },
}

SEEN_KEEP = 500  # mirror the legacy "keep last 500" dedup cap


def _now():
    return time.time()


class StateStore:
    """Thin wrapper around the state database."""

    def __init__(self, db_path=None, dry_run=False):
        self.db_path = db_path or DEFAULT_DB
        self.dry_run = dry_run
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, timeout=30)
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    # -- low-level ------------------------------------------------------
    def _has_any(self, component):
        spec = COMPONENTS[component]
        locs = set(spec["map"].values())
        for kind, name in locs:
            if kind == "kv":
                row = self._conn.execute(
                    "SELECT 1 FROM kv WHERE key=?", (name,)).fetchone()
            elif kind == "seen":
                row = self._conn.execute(
                    "SELECT 1 FROM seen WHERE set_name=? LIMIT 1",
                    (name,)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT 1 FROM docs WHERE name=?", (name,)).fetchone()
            if row:
                return True
        return False

    def get_int(self, key, default=0):
        row = self._conn.execute(
            "SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return default

    def set_int(self, key, value):
        if self.dry_run:
            return
        self._conn.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, str(int(value)), _now()))
        self._conn.commit()

    def get_text(self, key, default=None):
        row = self._conn.execute(
            "SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_text(self, key, value):
        if self.dry_run:
            return
        self._conn.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, str(value), _now()))
        self._conn.commit()

    def seen_add(self, set_name, item):
        """Add item; return True if it was new."""
        if self.dry_run:
            row = self._conn.execute(
                "SELECT 1 FROM seen WHERE set_name=? AND item=?",
                (set_name, item)).fetchone()
            return row is None
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO seen(set_name, item, added_at) "
            "VALUES(?,?,?)", (set_name, item, _now()))
        self._conn.commit()
        return cur.rowcount == 1

    def seen_items(self, set_name):
        return [r[0] for r in self._conn.execute(
            "SELECT item FROM seen WHERE set_name=? ORDER BY id", (set_name,))]

    def seen_replace(self, set_name, items):
        """Replace the whole set (used when persisting a pruned in-memory set)."""
        if self.dry_run:
            return
        self._conn.execute("DELETE FROM seen WHERE set_name=?", (set_name,))
        now = _now()
        self._conn.executemany(
            "INSERT OR IGNORE INTO seen(set_name, item, added_at) "
            "VALUES(?,?,?)",
            [(set_name, it, now) for it in items])
        self._conn.commit()

    def seen_prune(self, set_name, keep=SEEN_KEEP):
        if self.dry_run:
            return
        self._conn.execute(
            "DELETE FROM seen WHERE set_name=? AND id NOT IN "
            "(SELECT id FROM seen WHERE set_name=? ORDER BY id DESC LIMIT ?)",
            (set_name, set_name, keep))
        self._conn.commit()

    def get_doc(self, name):
        row = self._conn.execute(
            "SELECT doc FROM docs WHERE name=?", (name,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def set_doc(self, name, doc):
        if self.dry_run:
            return
        self._conn.execute(
            "INSERT INTO docs(name, doc, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET doc=excluded.doc, "
            "updated_at=excluded.updated_at",
            (name, json.dumps(doc), _now()))
        self._conn.commit()

    # -- component-level (dict shaped like the legacy JSON) --------------
    def _migrate_legacy(self, component, state_dir):
        """One-time import of the legacy JSON file into the DB."""
        spec = COMPONENTS[component]
        legacy_path = os.path.join(state_dir, spec["legacy"])
        if not os.path.exists(legacy_path):
            return False
        try:
            with open(legacy_path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            data = {}
        for legacy_key, (kind, name) in spec["map"].items():
            if legacy_key == "__doc__":
                self.set_doc(name, data)
                continue
            if legacy_key not in data:
                continue
            val = data[legacy_key]
            if kind == "kv":
                self.set_text(name, val)
            elif kind == "seen":
                items = []
                for k in val or []:
                    if isinstance(k, (list, tuple)):
                        k = "|".join("" if v is None else str(v) for v in k)
                    items.append(k)
                self.seen_replace(name, items)
                self.seen_prune(name)
            elif kind == "doc":
                self.set_doc(name, val if isinstance(val, dict) else {})
        try:
            os.replace(legacy_path, legacy_path + ".migrated")
        except OSError:
            pass
        return True

    def load_component(self, component, state_dir=None):
        """Return the component's state as a dict like the legacy JSON."""
        state_dir = state_dir or os.path.dirname(self.db_path)
        if not self._has_any(component):
            self._migrate_legacy(component, state_dir)
        spec = COMPONENTS[component]
        out = dict(spec["defaults"])
        for legacy_key, (kind, name) in spec["map"].items():
            if legacy_key == "__doc__":
                doc = self.get_doc(name)
                if doc is not None:
                    out = doc
                continue
            if kind == "kv":
                default = spec["defaults"].get(legacy_key)
                if isinstance(default, int):
                    out[legacy_key] = self.get_int(name, default)
                else:
                    v = self.get_text(name)
                    out[legacy_key] = v if v is not None else default
            elif kind == "seen":
                out[legacy_key] = self.seen_items(name)
            elif kind == "doc":
                doc = self.get_doc(name)
                out[legacy_key] = doc if isinstance(doc, dict) else {}
        return out

    def save_component(self, component, state):
        """Persist a legacy-shaped dict into the DB."""
        if self.dry_run:
            return
        spec = COMPONENTS[component]
        for legacy_key, (kind, name) in spec["map"].items():
            if legacy_key == "__doc__":
                self.set_doc(name, state if isinstance(state, dict) else {})
                continue
            if legacy_key not in state:
                continue
            val = state[legacy_key]
            if kind == "kv":
                self.set_text(name, val)
            elif kind == "seen":
                items = []
                for k in val or []:
                    if isinstance(k, (list, tuple)):
                        k = "|".join("" if v is None else str(v) for v in k)
                    items.append(str(k))
                self.seen_replace(name, items[-SEEN_KEEP:])
            elif kind == "doc":
                self.set_doc(name, val if isinstance(val, dict) else {})

    # -- kill switch ----------------------------------------------------
    def kill_switch_on(self, state_dir=None):
        """DB-canonical kill switch. A leftover legacy file is imported once."""
        state_dir = state_dir or os.path.dirname(self.db_path)
        if self.get_text("trading_enabled") is None:
            legacy = os.path.join(state_dir, "trading_enabled")
            try:
                with open(legacy) as f:
                    val = f.read().strip()
                if val in ("0", "1") and not self.dry_run:
                    self.set_text("trading_enabled", val)
                    try:
                        os.replace(legacy, legacy + ".migrated")
                    except OSError:
                        pass
            except OSError:
                pass
        return self.get_text("trading_enabled", "0") == "1"

    def set_kill_switch(self, on):
        if self.dry_run:
            return
        self.set_text("trading_enabled", "1" if on else "0")


def _cli():
    if len(sys.argv) < 3:
        print("usage: state_store.py get <key> | set <key> <value> | show")
        return 2
    store = StateStore()
    try:
        cmd = sys.argv[1]
        if cmd == "get":
            print(store.get_text(sys.argv[2]))
        elif cmd == "set":
            store.set_text(sys.argv[2], sys.argv[3])
            print("ok")
        elif cmd == "show":
            for k, v, u in store._conn.execute(
                    "SELECT key, value, updated_at FROM kv ORDER BY key"):
                print(f"{k}={v}")
        else:
            print("unknown command")
            return 2
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
