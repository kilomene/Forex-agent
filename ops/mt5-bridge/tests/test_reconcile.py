"""Hermetic unit tests for reconcile.py (no MT5, no network)."""
import os
import sys

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import reconcile  # noqa: E402

NOW = 1_790_160_000.0

KNOWN_TYPES = {
    "reconcile.external_position", "reconcile.external_close",
    "reconcile.partial_fill", "reconcile.partial_close",
    "reconcile.missing_position", "reconcile.duplicate_execution",
    "reconcile.sl_mismatch", "reconcile.tp_mismatch",
    "reconcile.volume_mismatch",
}


def bpos(ticket=1, symbol="EURUSD", direction="BUY", volume=1.0, sl=1.09,
         tp=1.12):
    return {"ticket": ticket, "symbol": symbol, "direction": direction,
            "volume": volume, "open_price": 1.10, "current_price": 1.105,
            "profit": 5.0, "sl": sl, "tp": tp, "open_time": NOW - 600}


def jopen(ticket=1, symbol="EURUSD", direction="BUY", volume=1.0, sl=1.09,
          tp=1.12, command_id="cmd-1"):
    return {"ticket": ticket, "symbol": symbol, "direction": direction,
            "volume": volume, "sl": sl, "tp": tp, "command_id": command_id}


def types(findings):
    return [f["type"] for f in findings]


def test_clean_state_no_findings():
    out = reconcile.reconcile([bpos()], [jopen()], [], now=NOW)
    assert out == []


def test_external_position_critical():
    out = reconcile.reconcile([bpos(ticket=99)], [], [], now=NOW)
    assert types(out) == ["reconcile.external_position"]
    assert out[0]["severity"] == "critical"
    assert out[0]["ticket"] == 99


def test_external_position_grace_for_fresh_fill():
    from datetime import datetime, timezone
    ts = datetime.fromtimestamp(NOW - 30, tz=timezone.utc).strftime(
        "%Y.%m.%d %H:%M:%S")
    deals = [{"type": "trade.opened", "ticket": 50, "command_id": "cmd-x",
              "time": ts}]
    out = reconcile.reconcile([bpos(ticket=50)], [], deals, now=NOW)
    assert out == []  # within the 180s grace window


def test_external_close():
    deals = [{"type": "trade.closed", "ticket": 5, "deal": 777,
              "profit": -12.5, "time": "2026.09.23 10:00:00"}]
    out = reconcile.reconcile([], [jopen(ticket=5)], deals, now=NOW)
    assert types(out) == ["reconcile.external_close"]
    assert out[0]["detail"]["profit"] == -12.5


def test_missing_position():
    out = reconcile.reconcile([], [jopen(ticket=6)], [], now=NOW)
    assert types(out) == ["reconcile.missing_position"]
    # The finding must NOT claim the position is closed.
    assert "not" in out[0]["detail"]["note"].lower() or \
        "NOT" in out[0]["detail"]["note"]


def test_partial_fill():
    out = reconcile.reconcile([bpos(volume=1.0)], [jopen(volume=2.0)], [],
                              now=NOW)
    assert types(out) == ["reconcile.partial_fill"]
    assert out[0]["detail"]["journal_volume"] == 2.0
    assert out[0]["detail"]["broker_volume"] == 1.0


def test_partial_close():
    out = reconcile.reconcile([bpos(ticket=7, volume=1.0)],
                              [jopen(ticket=7, volume=2.0)], [], now=NOW,
                              last_known_volumes={"7": 2.0})
    assert types(out) == ["reconcile.partial_close"]


def test_duplicate_execution_critical():
    deals = [
        {"type": "trade.opened", "ticket": 11, "command_id": "cmd-dup",
         "time": "2026.09.23 10:00:00"},
        {"type": "trade.opened", "ticket": 12, "command_id": "cmd-dup",
         "time": "2026.09.23 10:00:05"},
    ]
    out = reconcile.reconcile([], [], deals, now=NOW)
    assert "reconcile.duplicate_execution" in types(out)
    dup = next(f for f in out if f["type"] == "reconcile.duplicate_execution")
    assert dup["severity"] == "critical"
    assert sorted(dup["detail"]["tickets"]) == ["11", "12"]


def test_sl_mismatch():
    out = reconcile.reconcile([bpos(sl=1.085)], [jopen(sl=1.09)], [], now=NOW)
    assert types(out) == ["reconcile.sl_mismatch"]


def test_tp_mismatch():
    out = reconcile.reconcile([bpos(tp=1.115)], [jopen(tp=1.12)], [], now=NOW)
    assert types(out) == ["reconcile.tp_mismatch"]


def test_volume_mismatch_broker_larger():
    out = reconcile.reconcile([bpos(volume=3.0)], [jopen(volume=2.0)], [],
                              now=NOW)
    assert types(out) == ["reconcile.volume_mismatch"]


def test_no_false_closed_claims():
    # Across every scenario, reconcile() only emits findings — it never
    # emits a trade.closed claim. Broker confirmation is the only closer.
    scenarios = [
        ([bpos(ticket=99)], [], []),
        ([], [jopen(ticket=5)],
         [{"type": "trade.closed", "ticket": 5, "time": "2026.09.23 10:00:00"}]),
        ([], [jopen(ticket=6)], []),
        ([bpos(volume=1.0)], [jopen(volume=2.0)], []),
    ]
    for bp, jo, deals in scenarios:
        for f in reconcile.reconcile(bp, jo, deals, now=NOW):
            assert f["type"] in KNOWN_TYPES
            assert f["type"] != "trade.closed"


def test_journal_findings_alert_flag(tmp_path):
    jp = tmp_path / "j.jsonl"
    crit = [{"type": "reconcile.external_position", "severity": "critical",
             "ticket": 1}]
    assert reconcile.journal_findings(str(jp), crit) is True
    warn = [{"type": "reconcile.sl_mismatch", "severity": "warning",
             "ticket": 1}]
    assert reconcile.journal_findings(str(jp), warn) is False
    lines = jp.read_text().strip().split("\n")
    assert len(lines) == 2
    import json
    assert json.loads(lines[0])["type"] == "reconcile.external_position"
