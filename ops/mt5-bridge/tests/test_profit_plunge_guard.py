"""Hermetic unit tests for the profit-plunge guard (owner order 2026-10-13).

The guard tracks peak unrealized profit per bot position (BOT_MAGIC) from
the broker positions feed. When a position that peaked at or above
profit_plunge_arm_usd falls to profit_plunge_keep_pct (or less) of that
peak, the executor must immediately send a trade.close command
(reason "profit_plunge"). Protective: never gated by kill switch /
trading_enabled; skips on stale feed; never touches foreign magic.
"""
import json
import os

import pytest

import trade_executor as te


def make_eng(tmp_path, **cfg_over):
    files = tmp_path / "files"
    state = tmp_path / "run"
    files.mkdir()
    state.mkdir()
    cfg = {
        "trading_enabled": True,
        "dry_run": False,
        "profit_plunge_guard": True,
        "profit_plunge_arm_usd": 100.0,
        "profit_plunge_keep_pct": 0.5,
    }
    cfg.update(cfg_over)
    (state / "risk_config.json").write_text(json.dumps(cfg))
    (state / "trading_enabled").write_text("1")
    eng = te.TraderEngine(files_dir=str(files), state_dir=str(state))
    return eng, files, state


def write_positions(files, positions):
    (files / "nova_positions.json").write_text(json.dumps({
        "time": "2026.10.13 20:30:00",
        "server_time": "2026.10.13 20:30:00",
        "account": 112975129,
        "positions": positions,
    }))


def pos(ticket, profit, symbol="GBPJPY", magic=te.BOT_MAGIC, ptype="BUY"):
    return {"ticket": ticket, "symbol": symbol, "volume": 0.95,
            "type": ptype, "magic": magic, "open_price": 209.81,
            "current_price": 210.0, "profit": profit,
            "sl": 209.0, "tp": 211.0, "open_time": 1791910000}


def journal_types(state):
    out = []
    jp = state / "nova_journal.jsonl"
    if jp.exists():
        for line in jp.read_text().splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line).get("type"))
    return out


def commands(files):
    cp = files / "nova_commands.jsonl"
    if not cp.exists():
        return []
    return [json.loads(l) for l in cp.read_text().splitlines() if l.strip()]


def test_plunge_triggers_close(tmp_path):
    eng, files, state = make_eng(tmp_path)
    write_positions(files, [pos(111, 120.0)])
    assert eng.sweep_profit_plunge() == []          # peak recorded, armed
    write_positions(files, [pos(111, 40.0)])        # 33% of peak -> plunge
    closed = eng.sweep_profit_plunge()
    assert closed == ["111"]
    cmds = commands(files)
    assert len(cmds) == 1
    assert cmds[0]["type"] == "trade.close"
    assert cmds[0]["position_id"] == 111
    assert cmds[0]["reason"] == "profit_plunge"
    assert "trade.profit_plunge" in journal_types(state)


def test_no_trigger_below_arm(tmp_path):
    eng, files, state = make_eng(tmp_path)
    write_positions(files, [pos(112, 80.0)])       # peak under $100 arm
    assert eng.sweep_profit_plunge() == []
    write_positions(files, [pos(112, 10.0)])       # fell, but never armed
    assert eng.sweep_profit_plunge() == []
    assert commands(files) == []


def test_no_trigger_above_keep_line(tmp_path):
    eng, files, state = make_eng(tmp_path)
    write_positions(files, [pos(113, 120.0)])
    assert eng.sweep_profit_plunge() == []
    write_positions(files, [pos(113, 70.0)])       # 58% of peak: dip, not plunge
    assert eng.sweep_profit_plunge() == []
    assert commands(files) == []


def test_ignores_foreign_magic(tmp_path):
    eng, files, state = make_eng(tmp_path)
    write_positions(files, [pos(114, 500.0, magic=99999)])
    assert eng.sweep_profit_plunge() == []
    write_positions(files, [pos(114, 10.0, magic=99999)])
    assert eng.sweep_profit_plunge() == []
    assert commands(files) == []
    assert "trade.profit_plunge" not in journal_types(state)


def test_stale_feed_skips(tmp_path):
    eng, files, state = make_eng(tmp_path)
    write_positions(files, [pos(115, 5.0)])
    p = files / "nova_positions.json"
    old = os.path.getmtime(p) - 3600
    os.utime(p, (old, old))                        # feed older than 60 s
    assert eng.sweep_profit_plunge() == []
    assert commands(files) == []


def test_guard_disabled_is_noop(tmp_path):
    eng, files, state = make_eng(tmp_path,
                                 profit_plunge_guard=False)
    write_positions(files, [pos(116, 500.0)])
    assert eng.sweep_profit_plunge() == []
    write_positions(files, [pos(116, 1.0)])
    assert eng.sweep_profit_plunge() == []
    assert commands(files) == []


def test_dry_run_journals_intended_only(tmp_path):
    eng, files, state = make_eng(tmp_path, dry_run=True)
    write_positions(files, [pos(117, 150.0)])
    assert eng.sweep_profit_plunge() == []
    write_positions(files, [pos(117, 40.0)])
    assert eng.sweep_profit_plunge() == []         # nothing sent
    assert commands(files) == []
    types = journal_types(state)
    assert "trade.profit_plunge" in types
    assert "trade.profit_plunge_intended" in types


def test_no_double_command_while_closing(tmp_path):
    eng, files, state = make_eng(tmp_path)
    write_positions(files, [pos(118, 150.0)])
    eng.sweep_profit_plunge()
    write_positions(files, [pos(118, 20.0)])
    assert eng.sweep_profit_plunge() == ["118"]
    assert len(commands(files)) == 1
    write_positions(files, [pos(118, 5.0)])        # still open, close in flight
    assert eng.sweep_profit_plunge() == []         # no second command
    assert len(commands(files)) == 1


def test_peak_pruned_after_broker_close(tmp_path):
    eng, files, state = make_eng(tmp_path)
    write_positions(files, [pos(119, 150.0)])
    eng.sweep_profit_plunge()
    peaks_file = state / "profit_plunge_peaks.json"
    assert peaks_file.exists()
    write_positions(files, [])                     # fresh feed, position gone
    eng.sweep_profit_plunge()
    assert json.loads(peaks_file.read_text()) == {}


def test_negative_plunge_still_closes(tmp_path):
    # Peak $120, now -$10: fully "depreciated" -- still closes.
    eng, files, state = make_eng(tmp_path)
    write_positions(files, [pos(120, 120.0)])
    eng.sweep_profit_plunge()
    write_positions(files, [pos(120, -10.0)])
    assert eng.sweep_profit_plunge() == ["120"]
    assert commands(files)[0]["reason"] == "profit_plunge"
