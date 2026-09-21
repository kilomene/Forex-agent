"""Tests for storage.Store: CRUD, persistence across close/reopen, kill-switch,
event queue ordering, audit, journal filters."""

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from storage import Store


def _tmpdb():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # Store creates it fresh
    return path


def test_risk_state_crud_and_persistence():
    path = _tmpdb()
    s = Store(path)
    assert s.get_risk_state() == {}
    s.set_risk_state({"daily_loss_baseline": 10000.0, "loss_streak": 2})
    assert s.get_risk_state() == {"daily_loss_baseline": 10000.0, "loss_streak": 2}
    s.set_risk_state({"loss_streak": 3})  # partial upsert
    assert s.get_risk_state()["loss_streak"] == 3
    assert s.get_risk_state()["daily_loss_baseline"] == 10000.0
    s.close()

    # State survives close/reopen (the restart hole the original in-memory risk had).
    s2 = Store(path)
    assert s2.get_risk_state() == {"daily_loss_baseline": 10000.0, "loss_streak": 3}
    s2.close()


def test_kill_switch_round_trip():
    path = _tmpdb()
    s = Store(path)
    assert s.get_kill_switch() == {"engaged": False, "source": None, "ts": None}
    s.set_kill_switch(True, "local")
    ks = s.get_kill_switch()
    assert ks["engaged"] is True
    assert ks["source"] == "local"
    assert ks["ts"] is not None
    s.set_kill_switch(False, "agent")
    ks = s.get_kill_switch()
    assert ks["engaged"] is False
    assert ks["source"] == "agent"
    s.close()


def test_event_enqueue_dequeue_ordering():
    path = _tmpdb()
    s = Store(path)
    for i in range(5):
        s.enqueue_event({"event": "signal.detected", "seq": i})
    got = s.dequeue_events(limit=3)
    assert [e["seq"] for e in got] == [0, 1, 2]  # FIFO
    assert all("_queue_id" in e and "_ts" in e for e in got)
    got2 = s.dequeue_events()
    assert [e["seq"] for e in got2] == [3, 4]  # dequeued items are gone
    assert s.dequeue_events() == []
    s.close()


def test_audit_append_and_query():
    path = _tmpdb()
    s = Store(path)
    s.audit({"event": "trade.executed", "ticket": "1"})
    s.audit({"event": "risk.blocked", "reason": "DAILY_LOSS_LIMIT"})
    rows = s.query_audit()
    assert len(rows) == 2
    assert rows[0]["event"] == "risk.blocked"  # newest first
    assert rows[1]["event"] == "trade.executed"
    since = rows[1]["_ts"]
    assert len(s.query_audit(since=since)) == 2
    s.close()


def test_journal_add_and_filters():
    path = _tmpdb()
    s = Store(path)
    s.journal_add({"kind": "trade", "symbol": "EURUSD", "direction": "BUY", "outcome": "win", "pnl": 42.0})
    s.journal_add({"kind": "trade", "symbol": "GBPUSD", "direction": "SELL", "outcome": "loss"})
    s.journal_add({"kind": "reflection", "symbol": "EURUSD"})
    assert len(s.journal_query()) == 3
    assert len(s.journal_query(kind="trade")) == 2
    assert len(s.journal_query(symbol="EURUSD")) == 2
    assert len(s.journal_query(outcome="win")) == 1
    assert len(s.journal_query(kind="trade", symbol="GBPUSD")) == 1
    try:
        s.journal_query(bogus="x")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown journal filter must raise ValueError")
    try:
        s.journal_add({"symbol": "EURUSD"})
    except ValueError:
        pass
    else:
        raise AssertionError("journal entry without kind must raise ValueError")
    s.close()


def test_thread_safety_smoke():
    path = _tmpdb()
    s = Store(path)

    def worker(n):
        for i in range(50):
            s.enqueue_event({"worker": n, "i": i})
            s.audit({"worker": n, "i": i})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(s.dequeue_events(limit=1000)) == 200
    assert len(s.query_audit(limit=1000)) == 200
    s.close()
