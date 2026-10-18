"""Tests for performance.py -- honest Freqtrade-style performance metrics.

Covers every metric with hand-computed fixtures, the single dedup rule
(correction-wins regardless of order, ticketless quarantine), and the
duplication guard (fires on a duplicated journal, stays quiet on clean).
"""
import json
import math
import os
import sys

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import performance as pf  # noqa: E402


# ---------------------------------------------------------------- helpers

def C(ticket, profit, **kw):
    e = {"type": "trade.closed", "ticket": ticket, "profit": profit,
         "symbol": "EURUSD", "direction": "BUY",
         "time": "2026-10-10 10:00:00"}
    e.update(kw)
    return e


def CORR(ticket, net, prov, **kw):
    e = {"type": "trade.close_corrected", "ticket": ticket,
         "profit": prov, "net_profit": net,
         "time": "2026-10-10 10:05:00", "symbol": "EURUSD"}
    e.update(kw)
    return e


# ---------------------------------------------------------------- sharpe

def test_sharpe_hand_computed():
    # [100, -50, 200, -50]: mean 50, pop-var 11250, std 106.066...
    # Sharpe = 50 / 106.066... = 0.47140...
    got = pf.sharpe_ratio([100, -50, 200, -50])
    assert got == pytest.approx(0.47140, abs=1e-4)


def test_sharpe_empty_and_short():
    assert pf.sharpe_ratio([]) == 0.0
    assert pf.sharpe_ratio([100]) == 0.0


def test_sharpe_zero_variance():
    assert pf.sharpe_ratio([100, 100, 100]) == 0.0


def test_sharpe_all_losers_negative():
    # [-100, -200]: mean -150, std 50 -> -3.0
    assert pf.sharpe_ratio([-100, -200]) == pytest.approx(-3.0)


# ---------------------------------------------------------------- sortino

def test_sortino_hand_computed():
    # [100, -50, 200, -50]: mean 50; downside over [-50,-50] = 50
    # Sortino = 50/50 = 1.0
    assert pf.sortino_ratio([100, -50, 200, -50]) == pytest.approx(1.0)


def test_sortino_undefined_cases():
    assert pf.sortino_ratio([]) is None
    assert pf.sortino_ratio([100]) is None
    assert pf.sortino_ratio([100, 200]) is None  # no losers
    assert pf.sortino_ratio([100, 0, 200]) is None  # zero downside variance


# ---------------------------------------------------------- max drawdown

def test_max_drawdown_hand_computed():
    # cum: 100, 70, -10, 190, -110 -> worst decline 190 -> -110 = 300
    dd = pf.max_drawdown([100, -30, -80, 200, -300])
    assert dd["max_drawdown"] == pytest.approx(300.0)
    assert dd["peak"] == pytest.approx(190.0)
    assert dd["trough"] == pytest.approx(-110.0)
    assert dd["peak_index"] == 3
    assert dd["trough_index"] == 4


def test_max_drawdown_empty_and_no_decline():
    dd = pf.max_drawdown([])
    assert dd["max_drawdown"] == 0.0
    assert dd["peak_index"] is None and dd["trough_index"] is None
    dd2 = pf.max_drawdown([100, 200, 300])
    assert dd2["max_drawdown"] == 0.0


def test_max_drawdown_needs_chronological_order():
    # same trades in profit order vs chronological order differ
    assert pf.max_drawdown([300, -300])["max_drawdown"] == pytest.approx(300.0)
    assert pf.max_drawdown([-300, 300])["max_drawdown"] == pytest.approx(300.0)


# ---------------------------------------------------------- profit factor

def test_profit_factor_hand_computed():
    assert pf.profit_factor([100, -50, 200, -50]) == pytest.approx(3.0)


def test_profit_factor_undefined_not_infinite():
    assert pf.profit_factor([]) is None
    assert pf.profit_factor([100, 200]) is None  # no losses -> None, not inf
    assert pf.profit_factor([-100]) == pytest.approx(0.0)  # no wins -> 0.0
    assert pf.profit_factor([-100, -50]) == pytest.approx(0.0)


# ---------------------------------------------------------- expectancy

def test_expectancy():
    assert pf.expectancy([100, -50, 200, -50]) == pytest.approx(50.0)
    assert pf.expectancy([]) == 0.0


# ---------------------------------------------------------- compute_metrics

def test_compute_metrics_json_safe():
    m = pf.compute_metrics([100, -50, 200, -50])
    assert m["n_trades"] == 4
    assert m["sharpe_ratio"] == pytest.approx(0.47140, abs=1e-4)
    assert m["sortino_ratio"] == pytest.approx(1.0)
    # cum: 100, 50, 250, 200 -> worst decline 50 (100->50 and 250->200 tie,
    # first wins: peak 100 at idx 0, trough 50 at idx 1)
    assert m["max_drawdown"]["max_drawdown"] == pytest.approx(50.0)
    assert m["max_drawdown"]["peak"] == pytest.approx(100.0)
    assert m["max_drawdown"]["trough"] == pytest.approx(50.0)
    assert m["profit_factor"] == pytest.approx(3.0)
    assert m["expectancy"] == pytest.approx(50.0)
    # must serialize cleanly: no NaN, no inf anywhere
    s = json.dumps(m)
    assert "NaN" not in s and "Infinity" not in s


def test_compute_metrics_empty_never_nan():
    m = pf.compute_metrics([])
    s = json.dumps(m)
    assert "NaN" not in s and "Infinity" not in s
    for v in (m["sharpe_ratio"], m["expectancy"]):
        assert v == 0.0 and math.isfinite(v)
    assert m["sortino_ratio"] is None and m["profit_factor"] is None


# ---------------------------------------------------------- dedup rule

def test_dedupe_collapses_mirrors():
    journal = [C(111, -100.0), C(111, -100.0, reason="broker"),
               C(111, -100.0, reconciled=True),
               C(222, 250.0), C(222, 250.0, reason="broker")]
    uniq, raw = pf.dedupe_closes(journal)
    assert raw == 5
    assert len(uniq) == 2
    assert sorted(e["ticket"] for e in uniq) == [111, 222]


def test_dedupe_correction_wins_regardless_of_order():
    journal = [CORR(111, -160.74, -100.0), C(111, -100.0)]
    uniq, _ = pf.dedupe_closes(journal)
    assert len(uniq) == 1
    assert uniq[0]["type"] == "trade.close_corrected"
    assert pf.effective_profit(uniq[0]) == pytest.approx(-160.74)
    # reversed: provisional first, correction later -- same result
    uniq2, _ = pf.dedupe_closes(list(reversed(journal)))
    assert len(uniq2) == 1
    assert uniq2[0]["type"] == "trade.close_corrected"


def test_dedupe_provisional_last_wins_over_provisional():
    journal = [C(111, -100.0), C(111, -90.0)]
    uniq, _ = pf.dedupe_closes(journal)
    assert len(uniq) == 1
    assert pf.effective_profit(uniq[0]) == pytest.approx(-90.0)


def test_dedupe_ticketless_quarantine():
    journal = [C(None, -10.0), C(None, -20.0), C("", -30.0)]
    uniq, raw = pf.dedupe_closes(journal)
    assert raw == 3
    assert len(uniq) == 3  # never merged into one bucket


def test_dedupe_ignores_non_close_events():
    journal = [C(111, -100.0), {"type": "signal.received", "profit": 9999},
               {"type": "daily_summary", "profit": 9999}]
    uniq, raw = pf.dedupe_closes(journal)
    assert raw == 1 and len(uniq) == 1


# ---------------------------------------------------------- duplication guard

def test_duplication_flag_fires_on_ratio():
    # ticket 111's winner mirrored 3x: raw |650| vs deduped |150| -> 4.33
    # (divergence is exactly 500, so the ratio branch must be the trigger)
    journal = [C(111, 250.0), C(111, 250.0, reason="broker"),
               C(111, 250.0, reconciled=True),
               C(222, -100.0)]
    chk = pf.duplication_check(journal)
    assert chk["raw_close_events"] == 4
    assert chk["deduped_close_events"] == 2
    assert chk["raw_total_pnl"] == pytest.approx(650.0)
    assert chk["deduped_total_pnl"] == pytest.approx(150.0)
    assert chk["raw_to_deduped_ratio"] == pytest.approx(4.333, abs=1e-3)
    assert chk["absolute_divergence"] == pytest.approx(500.0)
    assert chk["flagged"] is True
    assert chk["status"] == "flagged"


def test_duplication_flag_fires_on_absolute_divergence():
    # ratio 160000/110000 = 1.4545 (under 1.5) but divergence 50000 > 500
    journal = [C(1, 60000.0),
               C(2, 50000.0), C(2, 50000.0, reason="broker")]
    chk = pf.duplication_check(journal)
    assert chk["raw_to_deduped_ratio"] == pytest.approx(1.4545, abs=1e-3)
    assert chk["absolute_divergence"] == pytest.approx(50000.0)
    assert chk["flagged"] is True


def test_duplication_flag_quiet_on_clean_journal():
    journal = [C(111, 250.0), C(222, -100.0), C(333, 50.0)]
    chk = pf.duplication_check(journal)
    assert chk["flagged"] is False
    assert chk["status"] == "clean"
    assert chk["raw_to_deduped_ratio"] == pytest.approx(1.0)
    assert chk["absolute_divergence"] == pytest.approx(0.0)
    json.dumps(chk)  # serializable


def test_duplication_flag_quiet_on_small_correction():
    # one corrected ticket: divergence $100 < $500, ratio under 1.5
    journal = [C(t, 100.0) for t in range(10)]
    journal += [C(99, -100.0), CORR(99, -160.74, -100.0)]
    chk = pf.duplication_check(journal)
    assert chk["flagged"] is False


def test_duplication_flag_empty_journal():
    chk = pf.duplication_check([])
    assert chk["flagged"] is False
    assert chk["raw_to_deduped_ratio"] is None


# ---------------------------------------------------------- benchmark

def _bench_journal():
    return [
        {"type": "signal.received", "signal_id": "s1", "symbol": "EURUSD",
         "entry_price": 1.10000, "time": "2026-10-10 08:00:00"},
        {"type": "trade.opened", "ticket": 111, "symbol": "EURUSD",
         "entry_price": 1.10010, "time": "2026-10-10 08:01:00",
         "signal_id": "s1"},
        # close carries no symbol -- must resolve via ticket
        {"type": "trade.closed", "ticket": 111, "symbol": None,
         "exit_price": 1.11000, "profit": 90.0,
         "time": "2026-10-10 09:00:00"},
        # symbol with no usable price data
        {"type": "trade.closed", "ticket": 222, "symbol": "XAUCHF",
         "exit_price": None, "profit": -50.0,
         "time": "2026-10-10 09:30:00"},
    ]


def test_benchmark_return_hand_computed():
    b = pf.benchmark_comparison(_bench_journal())
    eur = b["EURUSD"]
    # (1.11000 - 1.10000) / 1.10000 * 100 = 0.90909...%
    assert eur["benchmark_return_pct"] == pytest.approx(0.909, abs=1e-3)
    assert eur["first_price"] == pytest.approx(1.10000)
    assert eur["last_price"] == pytest.approx(1.11000)
    assert eur["n_price_points"] == 3


def test_benchmark_no_data_reported_honestly():
    b = pf.benchmark_comparison(_bench_journal())
    assert b["XAUCHF"] == {"benchmark_data": "none"}


def test_benchmark_empty_journal():
    assert pf.benchmark_comparison([]) == {}


def test_benchmark_dotted_timestamps_sort():
    journal = [
        {"type": "trade.opened", "ticket": 1, "symbol": "GBPUSD",
         "entry_price": 1.20, "time": "2026.10.10 10:00:00"},
        {"type": "trade.closed", "ticket": 1, "symbol": "GBPUSD",
         "exit_price": 1.26, "time": "2026-10-10 11:00:00"},
    ]
    b = pf.benchmark_comparison(journal)
    assert b["GBPUSD"]["benchmark_return_pct"] == pytest.approx(5.0)


# ---------------------------------------------------------- effective_profit

def test_effective_profit_prefers_net():
    assert pf.effective_profit(CORR(1, -160.74, -100.0)) == pytest.approx(-160.74)
    assert pf.effective_profit(C(1, 250.0)) == pytest.approx(250.0)
    assert pf.effective_profit({"type": "trade.closed"}) == 0.0
    assert pf.effective_profit({"type": "trade.closed", "profit": "bad"}) == 0.0


# ---------------------------------------------------------- close_time_key

def test_close_time_key_prefers_broker_exit():
    e = CORR(1, -160.74, -100.0)
    e["exit_time_broker"] = "2026.10.10 07:00:00"
    e["time"] = "2026-10-10 10:05:00"
    assert pf.close_time_key(e) == "2026-10-10 07:00:00"
    assert pf.close_date(e) == "2026-10-10"
    assert pf.close_date(C(1, 1.0)) == "2026-10-10"


# ---------------------------------------------------------- wiring: daily_summary

def _write(path, events):
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def test_daily_summary_event_carries_metrics_and_dup(tmp_path):
    import daily_summary as ds
    jp = str(tmp_path / "j.jsonl")
    mem = str(tmp_path / "mem")
    journal = [
        {"type": "signal.received", "time": "2026-10-17 08:00:00",
         "symbol": "EURUSD"},
        {"type": "trade.decision", "time": "2026-10-17 08:01:00",
         "signal_id": "s1"},
        C(111, 250.0, time="2026-10-17 09:00:00"),
        C(111, 250.0, reason="broker", time="2026-10-17 09:00:01"),
        C(111, 250.0, reconciled=True, time="2026-10-17 09:00:02"),
        C(222, -100.0, time="2026-10-17 10:00:00"),
    ]
    _write(jp, journal)
    rc = ds.main(journal_path=jp, today="2026-10-17", memory_dir=mem)
    assert rc == 0
    events = [json.loads(l) for l in open(jp) if l.strip()]
    ev = [e for e in events if e.get("type") == "daily_summary"][0]
    assert ev["trades_closed"] == 2  # deduped
    assert "performance" in ev and "duplication_check" in ev
    assert ev["performance"]["n_trades"] == 2
    assert ev["performance"]["profit_factor"] == pytest.approx(2.5)
    assert ev["duplication_check"]["flagged"] is True  # mirrored 111
    json.dumps(ev)  # valid JSON, no inf/NaN
    memline = open(os.path.join(mem, "2026-10-17.md")).read()
    assert "Sharpe" in memline
    assert "DUP-FLAGGED" in memline  # clean journals print "dup-check clean"


# ---------------------------------------------------------- wiring: weekend_reflection

def test_reflection_event_and_md_carry_metrics(tmp_path, monkeypatch):
    import weekend_reflection as wr
    jp = str(tmp_path / "j.jsonl")
    journal = [
        {"type": "trade.decision", "signal_id": "s1", "symbol": "EURUSD",
         "direction": "BUY", "time": "2026-10-17 08:01:00",
         "context": {"session": "london", "weekday": "Friday"}},
        C(111, 250.0, time="2026-10-17 09:00:00", signal_id="s1"),
        C(222, -100.0, time="2026-10-17 10:00:00", signal_id="s1"),
    ]
    _write(jp, journal)
    monkeypatch.setattr(wr, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(wr, "JOURNAL_PATH", jp)
    rc = wr.main()
    assert rc == 0
    events = [json.loads(l) for l in open(jp) if l.strip()]
    ev = [e for e in events if e.get("type") == "reflection"][0]
    assert "performance" in ev and "duplication_check" in ev
    assert "benchmark" in ev
    assert ev["performance"]["n_trades"] == 2
    md = open(str(tmp_path / f"reflection_{ev['date']}.md")).read()
    assert "Sharpe" in md
    assert "Duplication check:" in md
    assert "Benchmark" in md
    # per-symbol columns
    assert "EURUSD" in md and "maxDD" in md
