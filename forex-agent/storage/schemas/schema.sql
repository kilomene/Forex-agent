-- Local SQLite schema for the forex-agent subsystem.
--
-- This is the LOCAL store. It is NOT the Worker D1 schema.
-- Local holds: risk state, kill-switch latch, event queue, audit log, journal.
-- Design rule (TARGET_ARCHITECTURE.md §5): local safety state must survive a
-- Worker/cloud outage, so everything safety-critical lives here, never only in D1.

CREATE TABLE IF NOT EXISTS risk_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL            -- JSON-encoded value
);

CREATE TABLE IF NOT EXISTS kill_switch (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    engaged INTEGER NOT NULL DEFAULT 0,   -- 0/1
    source  TEXT,                          -- 'agent' | 'worker' | 'local' | ...
    ts      TEXT                           -- UTC ISO-8601 of last change
);

CREATE TABLE IF NOT EXISTS event_queue (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    TEXT NOT NULL,           -- UTC ISO-8601 enqueue time
    event TEXT NOT NULL            -- JSON-encoded event dict
);
CREATE INDEX IF NOT EXISTS idx_event_queue_id ON event_queue(id);

CREATE TABLE IF NOT EXISTS audit_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,          -- UTC ISO-8601
    record TEXT NOT NULL           -- JSON-encoded audit record
);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS journal (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,       -- UTC ISO-8601
    kind      TEXT NOT NULL,       -- trade|setup|regime|strategy|symbol|failure|pattern|reflection
    symbol    TEXT,
    direction TEXT,                -- BUY|SELL
    outcome   TEXT,                -- win|loss|breakeven|...
    payload   TEXT NOT NULL        -- JSON-encoded full entry
);
CREATE INDEX IF NOT EXISTS idx_journal_kind ON journal(kind);
CREATE INDEX IF NOT EXISTS idx_journal_symbol ON journal(symbol);
CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal(ts);
