#!/usr/bin/env python3
"""
Nova AI decision engine (advisory).

Worker C (2026-09-23): part of the Forex trading system upgrade.

The engine is a STRUCTURED pipeline: every stage is a pure function that
takes a dict and returns an annotated dict, so each stage is independently
testable. Stages run in a fixed order:

    market_data -> multi_timeframe -> market_structure -> trend ->
    momentum -> volatility -> liquidity -> session -> news_event_risk ->
    ensemble -> ai_reasoning -> trade_plan -> risk -> execution_proposal

Honest limits (read before wiring anything to real money):
  * ml_status() returns "ML_UNAVAILABLE": there is NO trained model in this
    system. The "ai_reasoning" stage is a deterministic rules-based
    synthesizer, not a neural network. It must never be presented as ML.
  * The engine NEVER invents prices: entry must come from the real quote
    passed in as ``price_reference``. validate_decision() rejects any
    TRADE whose entry is more than ENTRY_PRICE_TOLERANCE_PCT away from the
    supplied reference.
  * NO_TRADE is a first-class, always-allowed outcome. With no indicator
    data the engine deterministically returns NO_TRADE ("insufficient
    data") -- it will not hallucinate a setup.
  * The engine is ADVISORY. Wiring (see trade_executor._ai_advise) journals
    every decision as {"type": "ai.decision"}; it may VETO a signal the
    pipeline would otherwise take (config ai_veto_enabled, default OFF)
    but it can NEVER force a trade the existing pipeline would not take.

ML contract for future work (when a real model is trained):
  * Train ONLY on real broker history (ticks/deals from the account's own
    broker feed). No synthetic fills, no simulated spreads.
  * No lookahead: features at decision time t may only use data with
    timestamps <= t. Walk-forward validation (train on window W, test on
    W+1, roll forward); no shuffling across time.
  * Costs modeled explicitly: spread (points), commission, swap, and a
    slippage distribution fitted from the account's own fill history.
  * Model artifacts versioned (ai_version string) and recorded on every
    decision; a model is promoted only after beating the no-model baseline
    on out-of-sample data. Until then ml_status() stays ML_UNAVAILABLE.
"""

import math
from dataclasses import dataclass, field, asdict

AI_VERSION = "ai-engine-0.1-advisory"

# A TRADE whose entry differs from the supplied real quote by more than
# this is treated as an invented price and rejected.
ENTRY_PRICE_TOLERANCE_PCT = 0.5

# Hard per-trade planned-risk ceiling (USD). Mirrors the risk engine's
# MAX_RISK_PER_TRADE_USD; the risk engine enforces it, the AI validates
# its own numbers against it.
MAX_RISK_PER_TRADE_USD = 100.0


def ml_status():
    """Model availability. Honest: there is no trained model.

    Returns a dict with status "ML_UNAVAILABLE" until a real model is
    trained under the contract documented in this module's docstring.
    """
    return {
        "status": "ML_UNAVAILABLE",
        "reason": ("no trained model exists in this system; the "
                   "ai_reasoning stage is a deterministic rules-based "
                   "synthesizer, not machine learning"),
        "ai_version": AI_VERSION,
        "contract": {
            "data": "real broker history only (own broker feed)",
            "lookahead": "forbidden; walk-forward validation",
            "costs": "spread, commission, swap, slippage modeled",
            "promotion": "out-of-sample beat of no-model baseline required",
        },
    }


# ---------------------------------------------------------------- decision

@dataclass
class Decision:
    """Structured AI decision. NO_TRADE is always allowed."""
    decision: str                      # "TRADE" | "NO_TRADE"
    symbol: str = ""
    direction: str = None              # "BUY" | "SELL" | None
    entry: float = None
    stop_loss: float = None
    take_profit: float = None
    invalidation: str = None           # what proves the idea wrong
    time_horizon: str = None           # e.g. "M15", "H1", "4H"
    thesis: str = ""
    evidence: list = field(default_factory=list)
    timeframes: list = field(default_factory=list)
    risk_notes: str = ""
    confidence: float = 0.0            # 0..1
    expected_R: float = None           # reward:risk multiple
    valid_until: str = None            # ISO timestamp

    def to_dict(self):
        return asdict(self)


def _finite_positive(x):
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x) and x > 0)


def validate_decision(dec, price_reference, max_risk_usd=MAX_RISK_PER_TRADE_USD):
    """Independently validate a Decision against the real market reference.

    Checks:
      * decision is TRADE or NO_TRADE (NO_TRADE always passes with a
        warning-free result -- it needs no prices).
      * for TRADE: symbol, direction, entry/SL/TP present, finite,
        positive numbers; SL/TP correctly sided (BUY: SL < entry < TP;
        SELL: TP < entry < SL).
      * entry within ENTRY_PRICE_TOLERANCE_PCT of price_reference --
        the AI must never invent prices.
      * expected_R (when given) consistent with the TP/SL geometry;
        expected_R > 0.
      * confidence in [0, 1].
    Returns (ok: bool, errors: list[str]).
    """
    errors = []
    d = dec.decision if isinstance(dec, Decision) else dec.get("decision")
    if d not in ("TRADE", "NO_TRADE"):
        return False, [f"bad decision value: {d!r}"]
    if d == "NO_TRADE":
        return True, []
    get = (lambda k: getattr(dec, k, None)) if isinstance(dec, Decision) \
        else (lambda k: dec.get(k))
    symbol, direction = get("symbol"), get("direction")
    entry, sl, tp = get("entry"), get("stop_loss"), get("take_profit")
    if not symbol:
        errors.append("missing symbol")
    if direction not in ("BUY", "SELL"):
        errors.append(f"bad direction: {direction!r}")
    for name, val in (("entry", entry), ("stop_loss", sl),
                      ("take_profit", tp)):
        if not _finite_positive(val):
            errors.append(f"{name} must be a finite positive number, "
                          f"got {val!r}")
    if errors:
        return False, errors
    if direction == "BUY" and not (sl < entry < tp):
        errors.append(f"BUY mis-sided: need SL < entry < TP, got "
                      f"{sl}/{entry}/{tp}")
    if direction == "SELL" and not (tp < entry < sl):
        errors.append(f"SELL mis-sided: need TP < entry < SL, got "
                      f"{tp}/{entry}/{sl}")
    # Never invent prices: entry must track the real supplied quote.
    if _finite_positive(price_reference):
        dev = abs(entry - price_reference) / price_reference * 100.0
        if dev > ENTRY_PRICE_TOLERANCE_PCT:
            errors.append(
                f"entry {entry} deviates {dev:.3f}% from market reference "
                f"{price_reference} (limit {ENTRY_PRICE_TOLERANCE_PCT}%) -- "
                f"invented price rejected")
    else:
        errors.append("no valid market price_reference supplied; cannot "
                      "verify the entry came from a real quote")
    # R-multiple geometry consistency.
    exp_r = get("expected_R")
    risk_dist = abs(entry - sl)
    if risk_dist <= 0:
        errors.append("zero stop distance")
    else:
        geom_r = abs(tp - entry) / risk_dist
        if exp_r is not None:
            try:
                er = float(exp_r)
            except (TypeError, ValueError):
                er = None
            if er is None or not math.isfinite(er) or er <= 0:
                errors.append(f"expected_R must be a positive number, "
                              f"got {exp_r!r}")
            elif abs(er - geom_r) / max(geom_r, 1e-12) > 0.05:
                errors.append(f"expected_R {er} inconsistent with TP/SL "
                              f"geometry ({geom_r:.3f}R)")
    conf = get("confidence")
    try:
        c = float(conf)
    except (TypeError, ValueError):
        c = None
    if c is None or not (0.0 <= c <= 1.0):
        errors.append(f"confidence must be in [0,1], got {conf!r}")
    return (len(errors) == 0), errors


# ---------------------------------------------------------------- stages
# Each stage: pure function, dict in -> dict out (annotated copy).

def _annotate(ctx, key, value):
    out = dict(ctx)
    out[key] = value
    return out


def stage_market_data(ctx):
    """Validate the market input; the price reference is mandatory."""
    ref = ctx.get("price_reference")
    ok = _finite_positive(ref)
    return _annotate(ctx, "market_data", {
        "price_reference": ref,
        "symbol": ctx.get("symbol"),
        "data_available": ok,
        "note": ("real quote supplied" if ok else
                 "MISSING price_reference -- downstream stages must "
                 "not invent prices"),
    })


def stage_multi_timeframe(ctx):
    tfs = ctx.get("timeframes") or [ctx.get("timeframe") or "M15"]
    tfs = [t for t in tfs if t]
    return _annotate(ctx, "multi_timeframe", {
        "timeframes": tfs,
        "note": ("timeframes under review: " + ", ".join(tfs)) if tfs
                else "no timeframe context",
    })


def stage_market_structure(ctx):
    ind = ctx.get("indicators") or {}
    struct = {"regime": "unknown", "data_available": False, "note": ""}
    hh = ind.get("higher_highs")
    if hh is True:
        struct.update(regime="uptrend_structure", data_available=True,
                      note="higher highs / higher lows")
    elif hh is False:
        struct.update(regime="downtrend_structure", data_available=True,
                      note="lower highs / lower lows")
    else:
        struct["note"] = "no swing-point data; structure unknown"
    return _annotate(ctx, "market_structure", struct)


def stage_trend(ctx):
    ind = ctx.get("indicators") or {}
    fast, slow = ind.get("ema_fast"), ind.get("ema_slow")
    trend = {"direction": "unknown", "data_available": False, "note": ""}
    if _finite_positive(fast) and _finite_positive(slow):
        if fast > slow:
            trend.update(direction="up", data_available=True,
                         note="ema_fast > ema_slow")
        elif fast < slow:
            trend.update(direction="down", data_available=True,
                         note="ema_fast < ema_slow")
        else:
            trend.update(direction="flat", data_available=True,
                         note="emas equal")
    else:
        trend["note"] = "no EMA data; trend unknown"
    return _annotate(ctx, "trend", trend)


def stage_momentum(ctx):
    ind = ctx.get("indicators") or {}
    rsi = ind.get("rsi")
    mom = {"state": "unknown", "rsi": rsi, "data_available": False,
           "note": ""}
    try:
        r = float(rsi)
    except (TypeError, ValueError):
        r = None
    if r is not None and math.isfinite(r) and 0 <= r <= 100:
        mom["data_available"] = True
        if r >= 70:
            mom.update(state="overbought",
                       note=f"RSI {r:.1f} >= 70")
        elif r <= 30:
            mom.update(state="oversold", note=f"RSI {r:.1f} <= 30")
        else:
            mom.update(state="neutral", note=f"RSI {r:.1f} in range")
    else:
        mom["note"] = "no RSI data; momentum unknown"
    return _annotate(ctx, "momentum", mom)


def stage_volatility(ctx):
    ind = ctx.get("indicators") or {}
    atr = ind.get("atr")
    vol = {"state": "unknown", "atr": atr, "data_available": False,
           "note": ""}
    if _finite_positive(atr):
        vol.update(data_available=True, state="measured",
                   note=f"ATR {atr}")
    else:
        vol["note"] = "no ATR data; volatility unknown"
    return _annotate(ctx, "volatility", vol)


def stage_liquidity(ctx):
    spread = ctx.get("spread_points")
    liq = {"state": "unknown", "spread_points": spread,
           "data_available": False, "note": ""}
    try:
        s = float(spread)
    except (TypeError, ValueError):
        s = None
    if s is not None and math.isfinite(s) and s >= 0:
        liq.update(data_available=True,
                   state="wide" if s > 50 else "normal",
                   note=f"spread {s:g} points")
    else:
        liq["note"] = "no spread data; liquidity unknown"
    return _annotate(ctx, "liquidity", liq)


def stage_session(ctx):
    sess = ctx.get("session")
    return _annotate(ctx, "session", {
        "session": sess or "unknown",
        "data_available": sess is not None,
        "note": f"session: {sess}" if sess else "no session context",
    })


def stage_news_event_risk(ctx):
    blackout = bool(ctx.get("news_blackout"))
    return _annotate(ctx, "news_event_risk", {
        "blackout": blackout,
        "data_available": True,
        "note": ("NEWS BLACKOUT -- no new entries"
                 if blackout else "no scheduled high-impact news flagged"),
    })


def stage_ensemble(ctx):
    """Combine stage outputs into a single directional read.

    With no usable data the ensemble abstains (vote: none). It never
    manufactures conviction from missing inputs.
    """
    votes = []
    trend = (ctx.get("trend") or {}).get("direction")
    if trend in ("up", "down"):
        votes.append(1 if trend == "up" else -1)
    mom = (ctx.get("momentum") or {}).get("state")
    if mom == "oversold":
        votes.append(1)
    elif mom == "overbought":
        votes.append(-1)
    struct = (ctx.get("market_structure") or {}).get("regime")
    if struct == "uptrend_structure":
        votes.append(1)
    elif struct == "downtrend_structure":
        votes.append(-1)
    score = sum(votes)
    if ctx.get("news_event_risk", {}).get("blackout"):
        verdict, note = "none", "news blackout overrides all votes"
    elif not votes:
        verdict, note = "none", "no data to vote on -- abstain"
    elif score > 0:
        verdict, note = "long", f"net vote +{score}"
    elif score < 0:
        verdict, note = "short", f"net vote {score}"
    else:
        verdict, note = "none", "votes cancel out"
    return _annotate(ctx, "ensemble", {
        "votes": votes, "score": score, "verdict": verdict, "note": note,
    })


def stage_ai_reasoning(ctx):
    """Deterministic rules-based synthesis (NOT a trained model).

    Produces the Decision. Requires: a valid price_reference, an
    ensemble verdict, no news blackout, and a proposed direction that --
    when a signal direction was supplied -- agrees with the ensemble.
    Anything less -> NO_TRADE with the reason spelled out.
    """
    md = ctx.get("market_data") or {}
    ens = ctx.get("ensemble") or {}
    sig_dir = (ctx.get("signal_direction") or "").upper()
    symbol = ctx.get("symbol") or ""
    reasons = []
    if not md.get("data_available"):
        reasons.append("no valid market price reference")
    if ens.get("verdict") not in ("long", "short"):
        reasons.append(f"ensemble abstains ({ens.get('note')})")
    want = {"long": "BUY", "short": "SELL"}.get(ens.get("verdict"))
    if sig_dir and want and sig_dir != want:
        reasons.append(f"signal {sig_dir} disagrees with ensemble {want}")
    if sig_dir and sig_dir not in ("BUY", "SELL"):
        reasons.append(f"unusable signal direction {sig_dir!r}")
    direction = want if not sig_dir else sig_dir
    if reasons or not direction:
        if not direction and not reasons:
            reasons.append("no direction resolved")
        dec = Decision(
            decision="NO_TRADE", symbol=symbol,
            thesis="; ".join(reasons) or "no trade",
            evidence=reasons,
            timeframes=(ctx.get("multi_timeframe") or {}).get(
                "timeframes", []),
            risk_notes="no position taken",
            confidence=0.0)
        return _annotate(ctx, "ai_reasoning", {"decision": dec.to_dict(),
                                               "note": "NO_TRADE"})
    ref = md["price_reference"]
    atr = (ctx.get("volatility") or {}).get("atr")
    try:
        atr_f = float(atr)
        atr_ok = math.isfinite(atr_f) and atr_f > 0
    except (TypeError, ValueError):
        atr_ok = False
    # Stop/TP geometry: prefer ATR-based; fall back to a 0.5%/1.0% rule so
    # the plan is always explicit about its distances.
    if atr_ok:
        sl_d, tp_d = 1.5 * atr_f, 3.0 * atr_f
        dist_note = "1.5x/3.0x ATR"
    else:
        sl_d, tp_d = ref * 0.005, ref * 0.010
        dist_note = "fallback 0.5%/1.0% (no ATR)"
    if direction == "BUY":
        sl, tp = ref - sl_d, ref + tp_d
    else:
        sl, tp = ref + sl_d, ref - tp_d
    exp_r = abs(tp - ref) / abs(ref - sl)
    dec = Decision(
        decision="TRADE", symbol=symbol, direction=direction,
        entry=ref, stop_loss=sl, take_profit=tp,
        invalidation=(f"price closes beyond {sl} ({dist_note})"),
        time_horizon=(ctx.get("multi_timeframe") or {}).get(
            "timeframes", ["M15"])[0],
        thesis=(f"ensemble {ens.get('verdict')} ({ens.get('note')}); "
                f"signal direction {sig_dir or 'n/a'} aligned"),
        evidence=[f"ensemble: {ens.get('note')}",
                  f"trend: {(ctx.get('trend') or {}).get('note')}",
                  f"momentum: {(ctx.get('momentum') or {}).get('note')}",
                  f"distances: {dist_note}"],
        timeframes=(ctx.get("multi_timeframe") or {}).get("timeframes", []),
        risk_notes=(f"planned risk capped at ${MAX_RISK_PER_TRADE_USD:.0f} "
                    f"by the risk engine (M4 gate); stop distance "
                    f"{sl_d} price units"),
        confidence=min(0.9, 0.35 + 0.15 * len(ens.get("votes", []))),
        expected_R=round(exp_r, 3),
        valid_until=ctx.get("valid_until"))
    return _annotate(ctx, "ai_reasoning", {"decision": dec.to_dict(),
                                           "note": f"TRADE {direction}"})


def stage_trade_plan(ctx):
    air = ctx.get("ai_reasoning") or {}
    dec = air.get("decision") or {}
    plan = {"action": dec.get("decision", "NO_TRADE")}
    if dec.get("decision") == "TRADE":
        plan.update({
            "symbol": dec.get("symbol"), "direction": dec.get("direction"),
            "entry": dec.get("entry"), "stop_loss": dec.get("stop_loss"),
            "take_profit": dec.get("take_profit"),
            "invalidation": dec.get("invalidation"),
            "time_horizon": dec.get("time_horizon"),
        })
    return _annotate(ctx, "trade_plan", plan)


def stage_risk(ctx):
    plan = ctx.get("trade_plan") or {}
    air = ctx.get("ai_reasoning") or {}
    dec = air.get("decision") or {}
    if plan.get("action") != "TRADE":
        return _annotate(ctx, "risk", {"action": "none",
                                       "note": "no position, no risk"})
    entry, sl = dec.get("entry"), dec.get("stop_loss")
    ok = (_finite_positive(entry) and _finite_positive(sl)
          and abs(entry - sl) > 0)
    return _annotate(ctx, "risk", {
        "action": "size_to_fixed_fractional",
        "max_planned_risk_usd": MAX_RISK_PER_TRADE_USD,
        "stop_distance": abs(entry - sl) if ok else None,
        "checks": ["sl_present_and_sided (risk engine M3)",
                   f"risk <= ${MAX_RISK_PER_TRADE_USD:.0f} (risk engine M4)",
                   "broker stop/freeze levels (risk engine M3)"],
        "note": "sizing itself is done by the risk engine; the AI only "
                "constrains the plan",
    })


def stage_execution_proposal(ctx):
    plan = ctx.get("trade_plan") or {}
    risk = ctx.get("risk") or {}
    if plan.get("action") != "TRADE":
        return _annotate(ctx, "execution_proposal",
                         {"proposal": "NO_TRADE",
                          "note": "nothing to execute"})
    return _annotate(ctx, "execution_proposal", {
        "proposal": "AWAIT_RISK_ENGINE",
        "symbol": plan.get("symbol"), "direction": plan.get("direction"),
        "entry": plan.get("entry"), "stop_loss": plan.get("stop_loss"),
        "take_profit": plan.get("take_profit"),
        "note": ("advisory only: the risk engine re-validates everything "
                 "and alone decides whether a command is written"),
    })


STAGES = (
    stage_market_data,
    stage_multi_timeframe,
    stage_market_structure,
    stage_trend,
    stage_momentum,
    stage_volatility,
    stage_liquidity,
    stage_session,
    stage_news_event_risk,
    stage_ensemble,
    stage_ai_reasoning,
    stage_trade_plan,
    stage_risk,
    stage_execution_proposal,
)


def run_pipeline(market_input):
    """Run every stage in order; return the fully annotated context."""
    ctx = dict(market_input or {})
    ran = []
    for fn in STAGES:
        ctx = fn(ctx)
        ran.append(fn.__name__)
    ctx["stages_run"] = ran
    return ctx


def advise(signal, market_context=None):
    """Build a Decision for a signal dict. Never raises.

    market_context may carry: price_reference (defaults to the signal's
    entry_price), timeframe/timeframes, session, spread_points,
    news_blackout, indicators {ema_fast, ema_slow, rsi, atr,
    higher_highs}, valid_until.
    """
    mc = dict(market_context or {})
    sig = signal or {}
    market_input = {
        "symbol": sig.get("symbol"),
        "signal_direction": sig.get("direction"),
        "price_reference": mc.get("price_reference",
                                  sig.get("entry_price")),
        "timeframe": sig.get("timeframe"),
        "timeframes": mc.get("timeframes"),
        "session": mc.get("session"),
        "spread_points": mc.get("spread_points"),
        "news_blackout": mc.get("news_blackout", False),
        "indicators": mc.get("indicators") or {
            k: sig.get(k) for k in ("ema_fast", "ema_slow", "rsi_value",
                                    "atr_value")
            if sig.get(k) is not None
        },
        "valid_until": mc.get("valid_until"),
    }
    # Normalize indicator aliases coming from the signal dict.
    ind = dict(market_input["indicators"])
    if "rsi" not in ind and sig.get("rsi_value") is not None:
        ind["rsi"] = sig.get("rsi_value")
    if "atr" not in ind and sig.get("atr_value") is not None:
        ind["atr"] = sig.get("atr_value")
    market_input["indicators"] = ind
    try:
        ctx = run_pipeline(market_input)
        dec_dict = (ctx.get("ai_reasoning") or {}).get("decision") or {}
    except Exception as e:  # advisory must never break the signal path
        dec_dict = Decision(
            decision="NO_TRADE", symbol=sig.get("symbol") or "",
            thesis=f"ai pipeline error: {e}", evidence=[str(e)],
            risk_notes="pipeline failure -- abstain").to_dict()
        ctx = {"stages_run": [], "error": str(e)}
    valid, errors = validate_decision(
        dec_dict, market_input["price_reference"])
    return {
        "decision": Decision(**{k: dec_dict.get(k)
                                for k in Decision.__dataclass_fields__}),
        "valid": valid,
        "validation_errors": errors,
        "stages_run": ctx.get("stages_run", []),
        "ai_version": AI_VERSION,
        "ml_status": ml_status()["status"],
    }
