"""Worker C (2026-09-23): AI engine tests.

Covers: NO_TRADE as a first-class allowed outcome, validate_decision()
rejecting invented prices / missing SL / wrong-sided SL / non-finite
numbers, ml_status() honestly reporting ML_UNAVAILABLE, and the pipeline
stages running in the documented order.
"""
import os
import sys

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import ai_engine as ae  # noqa: E402
from ai_engine import Decision  # noqa: E402


def trade_decision(**over):
    d = dict(
        decision="TRADE", symbol="XPDUSD", direction="BUY",
        entry=1310.914, stop_loss=1306.344, take_profit=1320.054,
        invalidation="close below 1306.344", time_horizon="M15",
        thesis="ensemble long", evidence=["e1"], timeframes=["M15"],
        risk_notes="capped at $100", confidence=0.6, expected_R=2.0,
        valid_until="2026-09-23T12:00:00+00:00",
    )
    d.update(over)
    return Decision(**d)


# ---------------------------------------------------------------- NO_TRADE

def test_no_trade_allowed_minimal():
    dec = Decision(decision="NO_TRADE", symbol="XPDUSD",
                   thesis="insufficient data")
    ok, errors = ae.validate_decision(dec, 1310.9)
    assert ok is True
    assert errors == []


def test_no_trade_needs_no_prices():
    dec = Decision(decision="NO_TRADE")
    ok, _ = ae.validate_decision(dec, None)
    assert ok is True


def test_bad_decision_value_rejected():
    dec = Decision(decision="MAYBE", symbol="XPDUSD")
    ok, errors = ae.validate_decision(dec, 1310.9)
    assert ok is False
    assert errors


# ---------------------------------------------------------------- validation

def test_validate_accepts_good_trade():
    ok, errors = ae.validate_decision(trade_decision(), 1310.914)
    assert ok is True, errors


def test_validate_rejects_invented_price():
    # Entry 5% away from the real quote: the AI invented a price.
    dec = trade_decision(entry=1376.0, stop_loss=1370.0, take_profit=1388.0)
    ok, errors = ae.validate_decision(dec, 1310.914)
    assert ok is False
    assert any("invented price" in e for e in errors)


def test_validate_allows_entry_at_tolerance_edge():
    # 0.4% away: inside the 0.5% tolerance.
    ref = 1310.914
    entry = ref * 1.004
    sl = entry - (ref - 1306.344)
    tp = entry + (1320.054 - ref)
    dec = trade_decision(entry=entry, stop_loss=sl, take_profit=tp,
                         expected_R=2.0)
    ok, errors = ae.validate_decision(dec, ref)
    assert ok is True, errors


def test_validate_rejects_missing_sl():
    ok, errors = ae.validate_decision(trade_decision(stop_loss=None),
                                      1310.914)
    assert ok is False
    assert any("stop_loss" in e for e in errors)


def test_validate_rejects_missing_tp():
    ok, errors = ae.validate_decision(trade_decision(take_profit=None),
                                      1310.914)
    assert ok is False


def test_validate_rejects_wrong_sided_buy():
    ok, errors = ae.validate_decision(
        trade_decision(direction="BUY", stop_loss=1315.0), 1310.914)
    assert ok is False
    assert any("mis-sided" in e for e in errors)


def test_validate_rejects_wrong_sided_sell():
    ok, errors = ae.validate_decision(
        trade_decision(direction="SELL", entry=1310.914,
                       stop_loss=1306.344, take_profit=1320.054,
                       expected_R=2.0), 1310.914)
    assert ok is False
    assert any("mis-sided" in e for e in errors)


def test_validate_rejects_non_finite():
    ok, _ = ae.validate_decision(trade_decision(entry=float("inf")),
                                 1310.914)
    assert ok is False
    ok, _ = ae.validate_decision(trade_decision(entry=float("nan")),
                                 1310.914)
    assert ok is False


def test_validate_rejects_bad_confidence():
    ok, errors = ae.validate_decision(trade_decision(confidence=1.5),
                                      1310.914)
    assert ok is False
    assert any("confidence" in e for e in errors)


def test_validate_rejects_inconsistent_R():
    ok, errors = ae.validate_decision(trade_decision(expected_R=9.9),
                                      1310.914)
    assert ok is False
    assert any("expected_R" in e for e in errors)


def test_validate_needs_price_reference():
    ok, errors = ae.validate_decision(trade_decision(), None)
    assert ok is False
    assert any("price_reference" in e for e in errors)


# ---------------------------------------------------------------- ml_status

def test_ml_status_unavailable():
    st = ae.ml_status()
    assert st["status"] == "ML_UNAVAILABLE"
    assert "no trained model" in st["reason"]
    assert st["contract"]["lookahead"] == "forbidden; walk-forward validation"


# ---------------------------------------------------------------- pipeline

def test_stages_run_in_documented_order():
    ctx = ae.run_pipeline({"symbol": "XPDUSD", "price_reference": 1310.9})
    expected = [fn.__name__ for fn in ae.STAGES]
    assert ctx["stages_run"] == expected
    assert expected[0] == "stage_market_data"
    assert expected[-1] == "stage_execution_proposal"


def test_each_stage_annotates():
    ctx = ae.run_pipeline({"symbol": "XPDUSD", "price_reference": 1310.9})
    for fn in ae.STAGES:
        key = fn.__name__.replace("stage_", "", 1)
        assert key in ctx, fn.__name__


def test_pipeline_abstains_without_data():
    # No indicators at all -> ensemble abstains -> NO_TRADE, never a
    # hallucinated TRADE.
    ctx = ae.run_pipeline({"symbol": "XPDUSD", "price_reference": 1310.9,
                           "signal_direction": "BUY"})
    dec = ctx["ai_reasoning"]["decision"]
    assert dec["decision"] == "NO_TRADE"
    assert ctx["ensemble"]["verdict"] == "none"


def test_pipeline_trades_with_aligned_data():
    ctx = ae.run_pipeline({
        "symbol": "XPDUSD", "price_reference": 1310.9,
        "signal_direction": "BUY",
        "indicators": {"ema_fast": 1312.0, "ema_slow": 1308.0,
                       "rsi": 55.0, "atr": 2.0, "higher_highs": True},
    })
    dec = ctx["ai_reasoning"]["decision"]
    assert dec["decision"] == "TRADE"
    assert dec["direction"] == "BUY"
    assert dec["entry"] == 1310.9  # real quote, not invented
    ok, errors = ae.validate_decision(Decision(**dec), 1310.9)
    assert ok is True, errors


def test_pipeline_vetoes_on_news_blackout():
    ctx = ae.run_pipeline({
        "symbol": "XPDUSD", "price_reference": 1310.9,
        "signal_direction": "BUY", "news_blackout": True,
        "indicators": {"ema_fast": 1312.0, "ema_slow": 1308.0},
    })
    assert ctx["ai_reasoning"]["decision"]["decision"] == "NO_TRADE"


def test_pipeline_rejects_disagreeing_signal():
    ctx = ae.run_pipeline({
        "symbol": "XPDUSD", "price_reference": 1310.9,
        "signal_direction": "SELL",
        "indicators": {"ema_fast": 1312.0, "ema_slow": 1308.0,
                       "higher_highs": True},
    })
    dec = ctx["ai_reasoning"]["decision"]
    assert dec["decision"] == "NO_TRADE"
    assert "disagrees" in dec["thesis"]


# ---------------------------------------------------------------- advise

def test_advise_never_raises_and_reports_ml_status():
    out = ae.advise({"symbol": "XPDUSD", "direction": "BUY",
                     "entry_price": 1310.914, "timeframe": "M15"},
                    {"session": "london"})
    assert out["ml_status"] == "ML_UNAVAILABLE"
    assert out["ai_version"] == ae.AI_VERSION
    assert out["decision"].decision in ("TRADE", "NO_TRADE")


def test_advise_minimal_input_abstains():
    out = ae.advise({"symbol": "XPDUSD", "direction": "BUY"})
    # No entry_price -> no price reference -> must not invent one.
    assert out["decision"].decision == "NO_TRADE"
    assert out["valid"] is True  # NO_TRADE is always valid


def test_advise_pipeline_error_abstains():
    out = ae.advise(None, None)
    assert out["decision"].decision == "NO_TRADE"


# ---------------------------------------------------------------- wiring
# The advisory call in the signal path is ADDITIVE ONLY: it journals
# ai.decision and never changes the gate decision unless the operator
# explicitly enables ai_veto_enabled.

import json as _json  # noqa: E402
import os as _os  # noqa: E402
import trade_executor as _te  # noqa: E402


def _wiring_sandbox(tmp_path):
    run = str(tmp_path / "run")
    files = str(tmp_path / "files")
    _os.makedirs(run)
    _os.makedirs(files)
    eng = _te.TraderEngine(files_dir=files, state_dir=run)
    cfg = dict(_te.DEFAULT_CONFIG)
    specs = {"symbols": {"XPDUSD": {
        "tick_value": 1.0, "tick_size": 0.001, "volume_min": 0.01,
        "volume_max": 100.0, "volume_step": 0.01,
        "stops_level_points": 0, "spread_points": 20, "digits": 3,
        "point": 0.001}},
        "account": {"server": "MetaQuotes-Demo", "equity": 200000.0,
                    "balance": 200000.0}}
    sig = {"id": "X1", "type": "signal.detected", "symbol": "XPDUSD",
           "timeframe": "M15", "direction": "BUY", "entry_price": 1310.914,
           "stop_loss": 1306.344, "take_profit": 1320.054}
    return eng, cfg, specs, sig, run, files


def _journal_types(run):
    jp = _os.path.join(run, "nova_journal.jsonl")
    return [_json.loads(l).get("type") for l in open(jp) if l.strip()]


def test_advisory_is_additive_by_default(tmp_path):
    eng, cfg, specs, sig, run, files = _wiring_sandbox(tmp_path)
    cfg.update({"trading_enabled": True, "capital_basis": 200000.0})
    # dry_run stays True: decision would be "intended"; advisory must not change it,
    # but must journal its own ai.decision line.
    d = eng.handle_signal(sig, cfg, True, specs, True, {}, 0.0,
                          "2026-09-23", market_open=True)
    assert d == "intended"
    assert "ai.decision" in _journal_types(run)
    assert _os.path.getsize(_os.path.join(files, "nova_commands.jsonl")) == 0


def test_veto_blocks_commanded_only_when_enabled(tmp_path):
    eng, cfg, specs, sig, run, files = _wiring_sandbox(tmp_path)
    cfg.update({"trading_enabled": True, "dry_run": False,
                "capital_basis": 200000.0, "ai_veto_enabled": True})
    # No indicator data -> the honest AI abstains (NO_TRADE) -> veto.
    d = eng.handle_signal(sig, cfg, True, specs, True, {}, 0.0,
                          "2026-09-23", market_open=True)
    assert d == "skipped:ai_veto"
    assert _os.path.getsize(_os.path.join(files, "nova_commands.jsonl")) == 0
    types = _journal_types(run)
    assert "ai.decision" in types
    assert "trade.decision" in types


def test_veto_never_forces_a_trade(tmp_path):
    # A signal the gates refuse stays refused even with veto enabled;
    # the AI can only ever remove a trade, never create one.
    eng, cfg, specs, sig, run, files = _wiring_sandbox(tmp_path)
    cfg.update({"trading_enabled": True, "dry_run": False,
                "capital_basis": 200000.0, "ai_veto_enabled": True})
    sig = dict(sig, stop_loss=None)  # gate refuses: missing SL
    d = eng.handle_signal(sig, cfg, True, specs, True, {}, 0.0,
                          "2026-09-23", market_open=True)
    assert d == "skipped:missing_sl_tp"
    assert _os.path.getsize(_os.path.join(files, "nova_commands.jsonl")) == 0
