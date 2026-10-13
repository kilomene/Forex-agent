"""Regression tests for the 2026-10-13 reconcile_deferred journal flood.

Root cause: a broker-rejected trade.open stayed in idempotency status
"unknown" forever. _reconcile_entry() only adopted trade.opened evidence,
and _proves_no_execution() treated trade.rejected as blocking proof, so
sweep_timeouts() (every 5s main-loop cycle) re-journaled
execution.reconcile_deferred for every stuck entry on every cycle --
117k+ journal lines of pure noise from 17 stuck entries.

Fix pins:
  * trade.rejected with the command_id -> terminal status "rejected",
    exactly one execution.rejected_confirmed journal line, never resent.
  * genuinely-inconclusive deferrals journal at most once per entry per
    hour (status stays "unknown", reconcile still retried every cycle).
  * _proves_no_execution() no longer treats a rejection as blocking.

Hermetic: tmp dirs only, injectable providers, no MT5, no network.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import trade_executor as te  # noqa: E402


@pytest.fixture()
def eng(tmp_path):
    files = tmp_path / "files"
    run = tmp_path / "run"
    files.mkdir()
    run.mkdir()
    e = te.TraderEngine(files_dir=str(files), state_dir=str(run))
    return e, files, run


def read_journal(run):
    p = run / "nova_journal.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def claim_unknown(e, key="k1", command_id="cmd-1", cmd_type="trade.open"):
    e.idem.check_or_claim(key, {
        "command_id": command_id, "cmd_type": cmd_type,
        "symbol": "EURJPY", "direction": "SELL", "volume": 1.0,
        "sent_at": (datetime.now(timezone.utc)
                    - timedelta(hours=11)).isoformat(),
        "status": "unknown",
    })
    return e.idem.get(key)


def test_rejected_command_becomes_terminal(eng):
    e, files, run = eng
    rec = claim_unknown(e)
    trades = [{"type": "trade.rejected", "command_id": "cmd-1",
               "reason": "invalid volume"}]
    out = e._reconcile_entry("k1", rec,
                             positions_provider=lambda: ([], True),
                             trades_provider=lambda: trades)
    assert out == "rejected"
    assert e.idem.get("k1")["status"] == "rejected"
    types = [j["type"] for j in read_journal(run)]
    assert types.count("execution.rejected_confirmed") == 1
    assert "execution.reconcile_deferred" not in types
    # Second sweep: terminal, nothing re-journaled.
    e._reconcile_entry("k1", e.idem.get("k1"),
                       positions_provider=lambda: ([], True),
                       trades_provider=lambda: trades)
    types2 = [j["type"] for j in read_journal(run)]
    assert types2.count("execution.rejected_confirmed") == 1


def test_inconclusive_deferral_journaled_once_per_hour(eng):
    e, files, run = eng
    rec = claim_unknown(e)
    # No trades evidence, STALE positions feed -> genuinely inconclusive.
    kw = dict(positions_provider=lambda: ([], False),
              trades_provider=lambda: [])
    assert e._reconcile_entry("k1", rec, **kw) == "deferred"
    assert e.idem.get("k1")["status"] == "unknown"
    n1 = len(read_journal(run))
    assert n1 == 1
    # Immediate re-sweep: still deferred, but nothing new journaled.
    e._reconcile_entry("k1", e.idem.get("k1"), **kw)
    assert len(read_journal(run)) == n1
    # After an hour the deferral may be journaled again (liveness signal).
    e.idem.update("k1", last_deferral_journaled_at=(
        datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
    e._reconcile_entry("k1", e.idem.get("k1"), **kw)
    assert len(read_journal(run)) == n1 + 1


def test_proves_no_execution_ignores_rejection_for_open(eng):
    e, files, run = eng
    rec = {"command_id": "cmd-1", "cmd_type": "trade.open",
           "symbol": "EURJPY", "direction": "SELL", "volume": 1.0,
           "sent_at": datetime.now(timezone.utc).isoformat()}
    trades = [{"type": "trade.rejected", "command_id": "cmd-1"}]
    assert e._proves_no_execution(rec, [], trades, True) is True


def test_opened_still_blocks_proof(eng):
    e, files, run = eng
    rec = {"command_id": "cmd-1", "cmd_type": "trade.open",
           "symbol": "EURJPY", "direction": "SELL", "volume": 1.0,
           "sent_at": datetime.now(timezone.utc).isoformat()}
    trades = [{"type": "trade.opened", "command_id": "cmd-1",
               "ticket": "123"}]
    assert e._proves_no_execution(rec, [], trades, True) is False
