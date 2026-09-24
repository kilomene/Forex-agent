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
# The advisory call in the signal path journals ai.decision and ALWAYS
# enforces the veto (fail-closed): an honest NO_TRADE vetoes a commanded
# signal. The only way through is a one-shot founder approval file.

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


def test_veto_blocks_commanded_by_default(tmp_path):
    # Repair round 2 (2026-09-24): the advisory veto is ALWAYS enforced,
    # fail-closed. The old ai_veto_enabled opt-in is gone: with it off
    # (the default), an honest AI NO_TRADE was silently overridden and
    # commanded anyway (EURDKK 2026-09-23, -$117.73). Default config, no
    # approval file -> the abstention vetoes the trade.
    eng, cfg, specs, sig, run, files = _wiring_sandbox(tmp_path)
    cfg.update({"trading_enabled": True, "dry_run": False,
                "capital_basis": 200000.0})
    assert "ai_veto_enabled" not in cfg  # the opt-in no longer exists
    # No indicator data -> the honest AI abstains (NO_TRADE) -> veto.
    d = eng.handle_signal(sig, cfg, True, specs, True, {}, 0.0,
                          "2026-09-23", market_open=True)
    assert d == "skipped:advisory_no_trade", d
    assert d != "commanded"  # a vetoed signal writes no command
    assert _os.path.getsize(_os.path.join(files, "nova_commands.jsonl")) == 0
    types = _journal_types(run)
    assert "ai.decision" in types
    assert "trade.decision" in types


def test_veto_override_requires_one_shot_founder_file(tmp_path):
    # The ONLY way through a veto is an explicit, one-shot founder
    # approval file: run/ai_override_<signal_id> holding a non-empty ref.
    # It is consumed on first use and journaled as {"type": "ai.override"}.
    eng, cfg, specs, sig, run, files = _wiring_sandbox(tmp_path)
    cfg.update({"trading_enabled": True, "dry_run": False,
                "capital_basis": 200000.0})
    appr_path = _os.path.join(run, "ai_override_X1")
    with open(appr_path, "w") as f:
        f.write("founder:2026-09-24:manual-review")
    d = eng.handle_signal(sig, cfg, True, specs, True, {}, 0.0,
                          "2026-09-23", market_open=True)
    assert d == "commanded", d
    assert not _os.path.exists(appr_path)  # consumed: cannot linger
    types = _journal_types(run)
    assert "ai.override" in types


def test_veto_override_file_single_use(tmp_path):
    # A consumed approval cannot be reused: the second signal with the
    # same id is vetoed again (the file is gone).
    eng, cfg, specs, sig, run, files = _wiring_sandbox(tmp_path)
    cfg.update({"trading_enabled": True, "dry_run": False,
                "capital_basis": 200000.0})
    with open(_os.path.join(run, "ai_override_X1"), "w") as f:
        f.write("founder:one-shot")
    d1 = eng.handle_signal(sig, cfg, True, specs, True, {}, 0.0,
                           "2026-09-23", market_open=True)
    assert d1 == "commanded", d1
    d2 = eng.handle_signal(sig, cfg, True, specs, True, {}, 0.0,
                           "2026-09-23", market_open=True)
    assert d2 == "skipped:advisory_no_trade", d2


def test_veto_empty_approval_file_is_no_approval(tmp_path):
    # An empty approval file is not an approval.
    eng, cfg, specs, sig, run, files = _wiring_sandbox(tmp_path)
    cfg.update({"trading_enabled": True, "dry_run": False,
                "capital_basis": 200000.0})
    with open(_os.path.join(run, "ai_override_X1"), "w") as f:
        f.write("   ")
    d = eng.handle_signal(sig, cfg, True, specs, True, {}, 0.0,
                          "2026-09-23", market_open=True)
    assert d == "skipped:advisory_no_trade", d


def test_veto_never_forces_a_trade(tmp_path):
    # A signal the gates refuse stays refused; the AI can only ever
    # remove a trade, never create one.
    eng, cfg, specs, sig, run, files = _wiring_sandbox(tmp_path)
    cfg.update({"trading_enabled": True, "dry_run": False,
                "capital_basis": 200000.0})
    sig = dict(sig, stop_loss=None)  # gate refuses: missing SL
    d = eng.handle_signal(sig, cfg, True, specs, True, {}, 0.0,
                          "2026-09-23", market_open=True)
    assert d == "skipped:missing_sl_tp"
    assert _os.path.getsize(_os.path.join(files, "nova_commands.jsonl")) == 0


# ---------------------------------------------------------------- hard advisory veto
# Track 5 (2026-09-23): an advisory NO_TRADE must never be silently
# overridden. Regression for the EURDKK incident (signal
# EURDKK_M15_BUY_1790176500): ai.decision recorded NO_TRADE / confidence
# 0.0, yet the executor commanded the trade anyway (fill, then closed
# -$117.73) because the veto was opt-in and off.


def _eurdkk_signal():
    # Shape of the real EURDKK signal: the executor forwards no indicator
    # data to the engine (only spread/session), so the honest engine
    # abstains with 0.0 confidence -- exactly the journaled record.
    return {"id": "EURDKK_M15_BUY_1790176500", "symbol": "EURDKK",
            "direction": "BUY", "entry_price": 7.4755,
            "stop_loss": 7.47483, "take_profit": 7.47683,
            "timeframe": "M15", "rsi": 64.1, "atr": 0.00044}


def test_hard_veto_no_trade_blocks_commanded_signal():
    ae.clear_advisory_records()
    sig = _eurdkk_signal()
    advisory = ae.advise(sig, {"spread_points": 12, "session": "newyork"})
    assert advisory["decision"].decision == "NO_TRADE"
    assert advisory["decision"].confidence == 0.0
    assert advisory["valid"] is True
    ae.record_advisory(sig["id"], advisory)
    decision, reason = ae.apply_advisory_veto(sig["id"], "commanded")
    assert decision == "skipped:advisory_no_trade", reason
    assert decision != "commanded"  # a skipped signal writes no command
    allowed, why = ae.advisory_allows_trade(sig["id"])
    assert allowed is False
    assert why.startswith("advisory_no_trade")


def test_hard_veto_missing_record_fails_closed():
    ae.clear_advisory_records()
    decision, reason = ae.apply_advisory_veto("no_such_signal", "commanded")
    assert decision == "skipped:advisory_unavailable", reason


def test_hard_veto_none_advisory_fails_closed():
    ae.clear_advisory_records()
    ae.record_advisory("sig_x", None)  # engine call failed / raised
    decision, reason = ae.apply_advisory_veto("sig_x", "commanded")
    assert decision.startswith("skipped:advisory_"), reason
    assert decision != "commanded"


def test_hard_veto_invalid_trade_advisory_fails_closed():
    ae.clear_advisory_records()
    sig = _eurdkk_signal()
    advisory = ae.advise(sig, {"spread_points": 12, "session": "newyork"})
    advisory = dict(advisory, valid=False,
                    validation_errors=["tampered"])
    ae.record_advisory(sig["id"], advisory)
    decision, reason = ae.apply_advisory_veto(sig["id"], "commanded")
    assert decision != "commanded", reason


def test_hard_veto_override_requires_explicit_approval():
    ae.clear_advisory_records()
    sig = _eurdkk_signal()
    ae.record_advisory(sig["id"], ae.advise(sig, {}))
    # Default: no approval -> blocked.
    d, _ = ae.apply_advisory_veto(sig["id"], "commanded")
    assert d == "skipped:advisory_no_trade"
    # Empty / garbage approval -> still blocked.
    for bad in (None, {}, {"ref": ""}, " ", 0):
        d, _ = ae.apply_advisory_veto(sig["id"], "commanded",
                                      operator_approval=bad)
        assert d == "skipped:advisory_no_trade", bad
    # Explicit approval -> allowed; the ref is carried in the reason so
    # the caller can journal it as {"type": "ai.override"}.
    d, reason = ae.apply_advisory_veto(
        sig["id"], "commanded",
        operator_approval={"by": "zenas", "ref": "op-20260923-001",
                           "reason": "manual review"})
    assert d == "commanded", reason
    assert "op-20260923-001" in reason


def test_hard_veto_never_forces_a_refused_trade():
    ae.clear_advisory_records()
    sig = _eurdkk_signal()
    ae.record_advisory(sig["id"], ae.advise(sig, {}))
    d, reason = ae.apply_advisory_veto(
        sig["id"], "skipped:spread_too_wide",
        operator_approval={"by": "zenas", "ref": "op-1"})
    assert d == "skipped:spread_too_wide", reason


def test_hard_veto_validated_trade_advisory_allows():
    ae.clear_advisory_records()
    sig = dict(_eurdkk_signal(), id="OK_M15_BUY_1")
    ctx = {"spread_points": 10, "session": "london",
           "indicators": {"ema_fast": 1.10, "ema_slow": 1.09,
                          "rsi": 55.0, "atr": 0.001}}
    advisory = ae.advise(sig, ctx)
    assert advisory["decision"].decision == "TRADE", \
        advisory["decision"].thesis
    assert advisory["valid"] is True
    ae.record_advisory(sig["id"], advisory)
    d, reason = ae.apply_advisory_veto(sig["id"], "commanded")
    assert d == "commanded", reason
    assert reason == "advisory_ok"
