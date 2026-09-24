"""Issue 6 regression tests: trade.opened journal events carry sl/tp.

_open_info_for() must attach sl/tp to every newly journaled trade.opened,
sourced from the EA line when present, else from the matching command.sent
(by command_id via cmd_index), with sl_tp_source noting provenance.
Historical opens (pre-fix) carry neither -- that gap is documented, not
backfilled.
"""
import os
import sys

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import trade_executor as te  # noqa: E402


def make_eng(tmp_path, cmd_index=None):
    eng = te.TraderEngine(files_dir=str(tmp_path / "files"),
                          state_dir=str(tmp_path / "state"))
    eng.state["cmd_index"] = cmd_index or {}
    return eng


def ea_opened(cmd_id="cmd-1", sl=None, tp=None):
    ev = {"type": "trade.opened", "command_id": cmd_id, "ticket": 4242,
          "deal": 8181, "fill_price": 1.0950, "time": "2026.09.23 10:00:00",
          "signal_id": "sig-1", "symbol": "EURUSD", "direction": "BUY",
          "volume": 1.0}
    if sl is not None:
        ev["sl"] = sl
    if tp is not None:
        ev["tp"] = tp
    return ev


def cmd_entry(sl=1.0850, tp=1.1050):
    return {"signal_id": "sig-1", "symbol": "EURUSD", "direction": "BUY",
            "volume": 1.0, "sl": sl, "tp": tp, "entry_price": 1.09}


def test_sl_tp_from_command_index(tmp_path):
    eng = make_eng(tmp_path, {"cmd-1": cmd_entry()})
    info = eng._open_info_for(ea_opened())
    assert info["sl"] == 1.0850
    assert info["tp"] == 1.1050
    assert info["sl_tp_source"] == "command"


def test_sl_tp_ea_line_wins(tmp_path):
    eng = make_eng(tmp_path, {"cmd-1": cmd_entry(sl=9.9, tp=9.99)})
    info = eng._open_info_for(ea_opened(sl=1.0850, tp=1.1050))
    assert info["sl"] == 1.0850
    assert info["tp"] == 1.1050
    assert info["sl_tp_source"] == "ea_line"


def test_sl_tp_mixed_provenance_prefers_ea(tmp_path):
    eng = make_eng(tmp_path, {"cmd-1": cmd_entry(sl=1.0850, tp=1.1050)})
    info = eng._open_info_for(ea_opened(sl=1.0800))  # EA has sl only
    assert info["sl"] == 1.0800
    assert info["tp"] == 1.1050
    assert info["sl_tp_source"] == "ea_line"


def test_sl_tp_missing_when_no_source(tmp_path):
    eng = make_eng(tmp_path, {})
    info = eng._open_info_for(ea_opened(cmd_id="cmd-unknown"))
    assert info["sl"] is None
    assert info["tp"] is None
    assert info["sl_tp_source"] == "missing"


def test_historical_open_shape_has_no_sl_tp(tmp_path):
    # The exact shape of the 40 pre-fix journaled trade.opened events:
    # sl/tp are absent and must NOT be invented.
    eng = make_eng(tmp_path, {})
    ev = {"type": "trade.opened", "ticket": 10649170114, "deal": 10379628907,
          "symbol": "USDHKD", "direction": "BUY", "volume": 26.14,
          "entry_price": 7.84358, "time": "2026.09.23 23:30:03",
          "signal_id": "USDHKD_M15_BUY_1790205300",
          "command_id": "cmd-USDHKD_M15_BUY_1790205300"}
    info = eng._open_info_for(ev)
    assert info["sl"] is None and info["tp"] is None
    assert info["sl_tp_source"] == "missing"
    # and the original event dict is untouched (no backfill mutation)
    assert "sl" not in ev and "tp" not in ev


def test_other_open_fields_unchanged(tmp_path):
    eng = make_eng(tmp_path, {"cmd-1": cmd_entry()})
    info = eng._open_info_for(ea_opened())
    assert info["ticket"] == 4242
    assert info["deal"] == 8181
    assert info["entry_price"] == 1.0950
    assert info["command_id"] == "cmd-1"
