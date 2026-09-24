"""Hermetic unit tests for daily_summary.py P&L accounting.

Track 6 (2026-09-23) forensic finding: the daily summary computed day P&L
from the bare ``profit`` field of trade.closed events, ignoring both the
``net_profit`` field (gross + commission + swap) and authoritative
``trade.close_corrected`` revisions. The executor's own gate accounting
(eff_profit in trade_executor.load_trade_state) and the experiment record
(experiment.compute_metrics) both prefer net_profit and let a
close_corrected revision win -- the daily summary must agree with them.

Concrete case from the live journal: ticket 10605494223 (GBPAUD) was first
journaled as trade.closed with profit -1495.22 (swap missing); the
broker-history reconciliation later journaled trade.close_corrected with
swap -520.74 and net_profit -2016.15. The old summary would report
-1495.22 for that close; the corrected, commission/swap-inclusive figure
is -2016.15.
"""
import json
import os
import sys

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import daily_summary as ds  # noqa: E402


def write_journal(path, events):
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def read_summaries(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "daily_summary":
                out.append(e)
    return out


# ---------------------------------------------------------------- core rule


def test_close_corrected_supersedes_provisional_close():
    journal = [
        {"type": "trade.closed", "ticket": 10605494223, "symbol": "GBPAUD",
         "profit": -1495.22, "time": "2026.09.21 21:00:42"},
        {"type": "trade.close_corrected", "ticket": 10605494223,
         "symbol": "GBPAUD", "profit": -1495.41, "commission": 0.0,
         "swap": -520.74, "net_profit": -2016.15,
         "exit_time_broker": "2026.09.22 00:00:43",
         "time": "2026-09-22 04:06:03"},
    ]
    pnl, profits = ds.summarize_day(journal, "2026-09-22")
    assert pnl == pytest.approx(-2016.15)
    assert len(profits) == 1  # one ticket counts once, not twice


def test_net_profit_preferred_over_bare_profit():
    # Broker-confirmed close: profit field already net here, but the rule
    # must hold even when they differ (swap/commission drag).
    journal = [
        {"type": "trade.closed", "ticket": 10628042239, "symbol": "GBPCAD",
         "profit": -105.0, "commission": 0.0, "swap": -5.82,
         "net_profit": -110.82, "time": "2026.09.23 00:00:32"},
    ]
    pnl, profits = ds.summarize_day(journal, "2026-09-23")
    assert pnl == pytest.approx(-110.82)
    assert profits == pytest.approx([-110.82])


def test_plain_close_without_components_unchanged():
    # EA position-watch closes carry profit only (already net per the EA:
    # profit = DEAL_PROFIT + DEAL_SWAP + DEAL_COMMISSION). No correction,
    # no components -> profit is used as-is.
    journal = [
        {"type": "trade.closed", "ticket": 10639811301, "symbol": "EURDKK",
         "profit": -117.73, "time": "2026.09.23 15:33:14"},
    ]
    pnl, profits = ds.summarize_day(journal, "2026-09-23")
    assert pnl == pytest.approx(-117.73)
    assert profits == pytest.approx([-117.73])


def test_correction_dated_by_authoritative_broker_exit_time():
    # The correction carries the broker's exit time; the summary must
    # attribute the corrected P&L to that day, matching the executor's
    # eff_profit() dating (exit_time_broker wins over journal time).
    journal = [
        {"type": "trade.closed", "ticket": 10605494223, "symbol": "GBPAUD",
         "profit": -1495.22, "time": "2026.09.21 21:00:42"},
        {"type": "trade.close_corrected", "ticket": 10605494223,
         "symbol": "GBPAUD", "net_profit": -2016.15,
         "exit_time_broker": "2026.09.22 00:00:43",
         "time": "2026-09-22 04:06:03"},
    ]
    pnl_old_day, _ = ds.summarize_day(journal, "2026-09-21")
    pnl_new_day, _ = ds.summarize_day(journal, "2026-09-22")
    assert pnl_old_day == pytest.approx(0.0)
    assert pnl_new_day == pytest.approx(-2016.15)


def test_correction_wins_regardless_of_journal_order():
    # Repair round 2 (2026-09-24): correction precedence must not depend
    # on journal order. A late provisional trade.closed must never clobber
    # an earlier trade.close_corrected.
    journal = [
        {"type": "trade.close_corrected", "ticket": 4242, "symbol": "EURUSD",
         "net_profit": -2016.15, "exit_time_broker": "2026.09.22 00:00:43",
         "time": "2026-09-22 04:06:03"},
        {"type": "trade.closed", "ticket": 4242, "symbol": "EURUSD",
         "profit": -1495.22, "time": "2026-09-22 05:00:00"},
    ]
    pnl, profits = ds.summarize_day(journal, "2026-09-22")
    assert pnl == pytest.approx(-2016.15)
    assert len(profits) == 1


def test_two_provisionals_last_wins_unchanged():
    # Two provisional closes for one ticket (shouldn't happen): keep the
    # old last-wins behavior; only corrections are order-independent.
    journal = [
        {"type": "trade.closed", "ticket": 4343, "profit": -100.0,
         "time": "2026-09-22 01:00:00"},
        {"type": "trade.closed", "ticket": 4343, "profit": -110.0,
         "time": "2026-09-22 02:00:00"},
    ]
    pnl, profits = ds.summarize_day(journal, "2026-09-22")
    assert pnl == pytest.approx(-110.0)
    assert len(profits) == 1


def test_ticketless_closes_never_merge():
    # Repair round 2 (2026-09-24): closes with ticket=None must not
    # collapse into one "None" bucket (old code: str(None) key merged
    # them, so -30 became -20 when two ticketless closes existed).
    journal = [
        {"type": "trade.closed", "ticket": None, "symbol": "EURUSD",
         "profit": -30.0, "time": "2026-09-22 01:00:00"},
        {"type": "trade.closed", "ticket": None, "symbol": "GBPUSD",
         "profit": -20.0, "time": "2026-09-22 02:00:00"},
    ]
    pnl, profits = ds.summarize_day(journal, "2026-09-22")
    assert pnl == pytest.approx(-50.0)
    assert len(profits) == 2


def test_unknown_close_counts_zero_not_crash():
    journal = [
        {"type": "trade.closed", "ticket": 999, "symbol": "XAUUSD",
         "profit": None, "profit_status": "unknown",
         "time": "2026-09-23 10:00:00"},
    ]
    pnl, profits = ds.summarize_day(journal, "2026-09-23")
    assert pnl == pytest.approx(0.0)
    assert profits == pytest.approx([0.0])


# ------------------------------------------------------------- end to end


def test_main_reports_corrected_net_pnl(tmp_path):
    journal_path = tmp_path / "nova_journal.jsonl"
    write_journal(str(journal_path), [
        {"type": "trade.closed", "ticket": 10605494223, "symbol": "GBPAUD",
         "profit": -1495.22, "time": "2026.09.21 21:00:42"},
        {"type": "trade.close_corrected", "ticket": 10605494223,
         "symbol": "GBPAUD", "profit": -1495.41, "commission": 0.0,
         "swap": -520.74, "net_profit": -2016.15,
         "exit_time_broker": "2026.09.22 00:00:43",
         "time": "2026-09-22 04:06:03"},
    ])
    rc = ds.main(journal_path=str(journal_path), today="2026-09-22",
                 memory_dir=str(tmp_path / "mem"))
    assert rc == 0
    summaries = read_summaries(str(journal_path))
    assert len(summaries) == 1
    s = summaries[0]
    assert s["date"] == "2026-09-22"
    assert s["trades_closed"] == 1
    assert s["day_pnl"] == pytest.approx(-2016.15)
    assert s["losses"] == 1
    assert s["wins"] == 0
