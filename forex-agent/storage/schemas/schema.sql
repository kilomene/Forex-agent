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

-- Agent event journal (Phase 4, owned by the events builder).
-- The push/streaming event channel's source of truth: every published
-- event is recorded here with its unique event_id, so subscribers can
-- reconnect with resume_from=<event_id> and replay missed events in
-- order. Duplicates are impossible: event_id is UNIQUE, so a retried
-- publish of the same event is ignored (INSERT OR IGNORE) and the
-- agent can always dedup on event_id.
-- NOTE: this is the DELIVERY journal, not the Worker sync queue
-- (event_queue, destructive FIFO) nor the generic experience journal.
CREATE TABLE IF NOT EXISTS event_journal (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,   -- 'evt_' + uuid4 hex, assigned by agent/events/bus
    event    TEXT NOT NULL,          -- dotted event type, e.g. 'signal.detected'
    severity TEXT NOT NULL DEFAULT 'INFO',  -- INFO|NOTICE|WARNING|CRITICAL
    ts       TEXT NOT NULL,          -- UTC ISO-8601 event timestamp
    payload  TEXT NOT NULL,          -- JSON-encoded full normalized event
    acked    INTEGER NOT NULL DEFAULT 0,     -- 1 once an agent acknowledged receipt
    acked_ts TEXT                     -- UTC ISO-8601 of acknowledgement
);
CREATE INDEX IF NOT EXISTS idx_event_journal_event_id ON event_journal(event_id);
CREATE INDEX IF NOT EXISTS idx_event_journal_ts ON event_journal(ts);
