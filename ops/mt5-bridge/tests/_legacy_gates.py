"""Frozen reference copy of trade_executor.check_gates.

Captured verbatim on 2026-10-18 (before the protection-plugin refactor of
check_gates into protections.py filters). Used ONLY by
tests/test_protection_parity.py to prove the refactor preserves behavior
exactly. DO NOT MODIFY -- any behavioral drift here invalidates the parity
proof. Helpers/constants it references are imported from trade_executor
(they were not moved by the refactor).
"""

import math
from datetime import datetime, timezone

from trade_executor import (
    EMERGENCY_DRAWDOWN_PCT,
    FLOATING_DRAWDOWN_PCT,
    MAX_RISK_PER_TRADE_USD,
    TICK_MAX_AGE_S,
    _finite_pos_number,
    _mrs_get,
    compute_volume,
    currency_codes,
    is_demo_server,
    signed_currency_exposure,
    step_decimals,
    validate_stop_loss,
)


def check_gates_legacy(sig, *, enabled, dry_run, specs, specs_fresh,
                open_positions, todays_profit, today_str,
                risk_pct, max_concurrent, max_spread_points,
                max_daily_loss_pct, equity, balance=None,
                risk_basis=None,
                max_total_exposure_lots=0, open_lots=0.0,
                max_consecutive_losses=0, consecutive_losses=0,
                max_correlated_positions=0, require_stop_loss=True,
                market_open=True, tick_age_s=None,
                enforce_correlation_gate=True,
                enforce_spread_gate=True,
                enforce_single_position_per_symbol=True,
                enforce_consecutive_losses_gate=True,
                # Worker C (2026-09-23) mode/integrity checklist. All new
                # checks are opt-in via enforce_mode_checks (default False
                # preserves legacy behavior for direct callers); the LIVE
                # executor path always enables them.
                mode=None, enforce_mode_checks=False, mode_identity_ok=None,
                live_authorized=False, mode_risk_state=None,
                free_margin=None, margin_level=None,
                max_risk_per_trade_usd=MAX_RISK_PER_TRADE_USD,
                emergency_drawdown_pct=EMERGENCY_DRAWDOWN_PCT,
                price_reference=None,
                tick_value_overrides=None,
                blocked_symbols=None,
                max_signal_age_hours=2.0,
                # 2026-10-16: in-flight slot reservation. Commands already
                # emitted (trade.open) but not yet broker-confirmed hold a
                # slot against max_concurrent; default 0 preserves legacy
                # behavior for direct callers (the engine always passes the
                # live count from the idempotency store).
                in_flight_count=0):
    """
    Run the risk gates in order; first failure wins.
    Returns (decision, flags) where flags may carry loud/trip side effects.
    Pure function: no I/O.
    risk_basis: owner-allocated capital (USD). FAIL-CLOSED: None means the
    configured capital or the broker equity was missing, malformed,
    non-finite, or non-positive (see effective_risk_basis); the decision
    is then 'skipped:risk_basis_unavailable'. A valid basis replaces broker
    equity for position sizing and the daily-loss gate. The
    floating-drawdown gate keeps using real balance/equity.
    The enforce_*_gate flags disable the opportunity filters (correlation,
    spread, single-position-per-symbol, consecutive losses) while keeping
    every integrity/safety gate active. Owner order 2026-09-22: all four
    default OFF in live config so nothing blocks trading.
    Mode/integrity checklist (enforce_mode_checks=True), in order:
      M0 mode valid ("DEMO"/"LIVE"), account identity matches the mode
         record, LIVE authorization present when mode == LIVE.
      M1 equity/balance/free-margin/margin-level present and sane.
      M2 spread present in broker data.
      M3 mandatory valid broker-side SL -- ALWAYS enforced, even when
         enforce_mode_checks is False (see the no-SL audit note).
      M4 per-trade planned risk <= max_risk_per_trade_usd ($100).
      M5 per-mode daily loss < 3% of the mode's start-of-day equity;
         blocked entries journal risk.daily_loss_block via the
         trip_mode_daily flag (handled by the engine, which owns I/O).
      M6 per-mode emergency drawdown trips the kill switch
         (trip + trip_mode_kill flags).
    """
    symbol = sig.get("symbol")
    entry = sig.get("entry_price")
    sl = sig.get("stop_loss")

    # 1. not enabled (config or kill switch)
    if not enabled:
        return "skipped:trading_disabled", {}

    # 1a0. Signal freshness (2026-10-15): never execute a signal whose
    # candle_time is older than max_signal_age_hours. The signals file can
    # be re-read from offset 0 after truncation and the 500-entry `seen`
    # dedup ages out old signal IDs, so ancient signals get re-evaluated
    # (2026-10-15: a 2026-09-23 EURDKK signal was executed, -$150.02).
    # candle_time is broker time = UTC (see TZ bug class, 2026-10-12).
    # Unparseable/missing candle_time fails closed: without a verifiable
    # age the signal cannot be proven fresh.
    if max_signal_age_hours and max_signal_age_hours > 0:
        candle_time = sig.get("candle_time")
        try:
            ct = datetime.strptime(
                str(candle_time), "%Y.%m.%d %H:%M:%S").replace(
                    tzinfo=timezone.utc)
        except (ValueError, TypeError, AttributeError):
            return "skipped:stale_signal", {"loud": True}
        age_h = (datetime.now(timezone.utc) - ct).total_seconds() / 3600.0
        if age_h > max_signal_age_hours:
            return "skipped:stale_signal", {"loud": True}

    # 1a. Racheal engagement #3 (2026-10-09): blocked symbols fail closed.
    # Symbols with unverifiable broker tick_values cannot be sized safely.
    blocked = blocked_symbols or []
    if symbol in blocked:
        return "skipped:symbol_blocked", {"loud": True}

    # 1b. owner-allocated capital basis (2026-09-22): fail closed. A basis
    # that is None, non-numeric, non-finite, or non-positive means the
    # trade cannot be sized -- it must not be placed.
    try:
        rb = float(risk_basis)
    except (TypeError, ValueError):
        rb = float("nan")
    if not math.isfinite(rb) or rb <= 0:
        return "skipped:risk_basis_unavailable", {"loud": True}
    sizing_equity = rb

    # The symbol spec is needed by the integrity checklist below.
    spec = (specs.get("symbols") or {}).get(symbol) if specs else None

    # ---- M. pre-entry integrity checklist (Worker C, 2026-09-23) ----
    # M0/M1/M2 run only when enforce_mode_checks is True. The LIVE
    # executor path always enables them; direct callers (unit tests)
    # default to the legacy behavior. M3 (mandatory valid broker-side SL)
    # ALWAYS runs: the no-SL audit found the old `require_stop_loss`
    # config flag could disable the SL gate entirely, so the flag is kept
    # for backward compatibility but can no longer disable the gate.
    if enforce_mode_checks:
        # M0. mode valid, account identity matches the mode record, LIVE
        # authorization present when mode == LIVE.
        if mode not in ("DEMO", "LIVE"):
            return "skipped:invalid_mode", {"loud": True}
        if mode_identity_ok is not True:
            return "skipped:mode_identity_mismatch", {"loud": True}
        if mode == "LIVE" and not live_authorized:
            return "skipped:live_not_authorized", {"loud": True}
        # M1. equity / balance / free margin / margin level present and
        # sane. Fail closed: a trade decision must never rest on missing
        # account data.
        acct = (specs.get("account") or {}) if specs else {}
        fm = acct.get("free_margin") if free_margin is None else free_margin
        ml = (acct.get("margin_level") if margin_level is None
              else margin_level)
        if not (_finite_pos_number(equity) and _finite_pos_number(balance)
                and _finite_pos_number(fm) and _finite_pos_number(ml)):
            return "skipped:margin_data_unavailable", {"loud": True}
        # M2. spread must come from live broker data (fail closed).
        if spec is None or spec.get("spread_points") is None:
            return "skipped:spread_unknown", {"loud": True}

    # M3. mandatory valid broker-side SL -- always enforced (see note).
    # A signal without SL/TP is never traded; a mis-sided SL, an SL
    # inside the broker's stop level, or an SL inside the freeze level is
    # rejected as invalid. Nothing reaching the command writer may lack a
    # valid SL (belt-and-braces re-check in handle_signal).
    if not sig.get("take_profit"):
        return "skipped:missing_sl_tp", {"loud": True}
    sl_ok, sl_reason = validate_stop_loss(
        sig, spec, price_reference=price_reference)
    if not sl_ok:
        if sl_reason == "missing_sl":
            return "skipped:missing_sl_tp", {"loud": True}
        return "skipped:invalid_sl", {"loud": True, "sl_reason": sl_reason}

    # sizing needs specs; in dry_run we still want full computed details,
    # so stale/missing specs and sizing failures journal their skip reason.
    if not specs_fresh or spec is None:
        return "skipped:stale_specs", {}
    acct_ccy = ((specs.get("account") or {}).get("currency") or "USD") \
        if specs else "USD"
    volume, risk_amount, err = compute_volume(
        sizing_equity, risk_pct, entry, sl, spec,
        tick_value_overrides=tick_value_overrides,
        symbol=sig.get("symbol"),
        account_currency=acct_ccy)
    if err:
        return err, {}

    if enforce_mode_checks:
        # M4. per-trade planned risk hard cap (owner risk numbers
        # unchanged: $100/trade on the $200,000 capital basis).
        try:
            cap = float(max_risk_per_trade_usd)
        except (TypeError, ValueError):
            cap = MAX_RISK_PER_TRADE_USD
        if risk_amount > cap + 1e-9:
            return "skipped:max_risk_per_trade", {"loud": True}
        # M5. per-mode daily loss < 3% of the MODE's start-of-day equity.
        # Blocked entries are journaled as risk.daily_loss_block by the
        # engine (trip_mode_daily flag); the mode state owns the flag so
        # it survives restarts and never leaks between DEMO and LIVE.
        sod = _mrs_get(mode_risk_state, "start_of_day_equity")
        if not _finite_pos_number(sod):
            return "skipped:mode_state_unavailable", {"loud": True}
        if _mrs_get(mode_risk_state, "daily_loss_tripped", False):
            return ("skipped:mode_daily_loss",
                    {"loud": True, "trip_mode_daily": True})
        realized = _mrs_get(mode_risk_state, "realized_pl", 0.0) or 0.0
        floating = _mrs_get(mode_risk_state, "floating_pl", 0.0) or 0.0
        if -(realized + floating) >= 0.03 * sod:
            return ("skipped:mode_daily_loss",
                    {"loud": True, "trip_mode_daily": True})
        # M6. per-mode emergency drawdown trips the kill switch. The
        # engine applies the trip (file kill switch + mode-state flag +
        # kill_switch.tripped journal) from these flags.
        try:
            edd = float(emergency_drawdown_pct)
        except (TypeError, ValueError):
            edd = EMERGENCY_DRAWDOWN_PCT
        dd = _mrs_get(mode_risk_state, "current_drawdown", 0.0) or 0.0
        if (_mrs_get(mode_risk_state, "kill_switch_tripped", False)
                or dd >= edd / 100.0):
            return ("skipped:emergency_drawdown",
                    {"loud": True, "trip": True, "trip_mode_kill": True})

    # 1b. total exposure cap: clamp the new trade so bot-wide open volume
    # never exceeds max_total_exposure_lots. Clamp (not reject) keeps
    # trading alive at small size; a trade that cannot fit at all is
    # skipped. NOTE: only bot positions are visible; manual positions
    # cannot be counted until the EA publishes a positions feed.
    if max_total_exposure_lots and max_total_exposure_lots > 0:
        remaining = max_total_exposure_lots - (open_lots or 0.0)
        step = spec.get("volume_step") or 0.01
        if remaining <= 0:
            return "skipped:max_total_exposure", {"loud": True}
        if volume > remaining:
            volume = math.floor(remaining / step) * step
            volume = round(volume, step_decimals(step))
            vmin = spec.get("volume_min") or 0
            if volume < vmin:
                return "skipped:max_total_exposure", {"loud": True}

    # 1c. tick freshness (port from repo gateway): never base a trade
    # decision on a stale quote. The EA re-validates spread at send time,
    # but the decision itself must rest on a live tick.
    if tick_age_s is not None and tick_age_s > TICK_MAX_AGE_S:
        return "skipped:stale_tick", {"loud": True}

    # 2. dry_run -> intended, write nothing
    if dry_run:
        return "intended", {"volume": volume, "risk_amount": risk_amount}

    # 3. market closed -> no NEW positions (existing positions are held)
    if not market_open:
        return "skipped:market_closed", {}

    # 4. server check (mode-aware). DEMO/legacy: refuse anything that is
    # not a demo server. LIVE: refuse demo servers outright -- a live
    # mode record trades on its bound live broker, never on demo
    # (NO_LIVE_TO_DEMO_FALLBACK); the M0 identity gate additionally binds
    # the exact server string to the mode record.
    server = ((specs.get("account") or {}).get("server")
              or specs.get("server") or "")
    if enforce_mode_checks and mode == "LIVE":
        if is_demo_server(server):
            return "skipped:live_on_demo_server", {"loud": True}
    elif not is_demo_server(server):
        return "skipped:non_demo_server", {"loud": True}

    # 5. max concurrent trades: confirmed open positions PLUS commands
    # already emitted but not yet filled (in-flight). A fill only becomes
    # visible after the EA's command poll + broker execution + the
    # trades-file tail, so a burst of signals inside that window used to
    # all pass the gate and overshoot the cap (2026-10-16 halt: 12/10
    # broker positions against a cap of 10).
    in_flight = in_flight_count or 0
    if len(open_positions) + in_flight >= max_concurrent:
        return "skipped:max_concurrent", {"in_flight": in_flight}

    # 6. symbol already has an open position (opportunity filter --
    #    disabled per owner order 2026-09-22)
    if enforce_single_position_per_symbol:
        for info in open_positions.values():
            if info.get("symbol") == symbol:
                return "skipped:symbol_already_open", {}

    # 6b. correlated positions: direction-aware net currency exposure.
    # A BUY is long base / short quote, a SELL the reverse. Two positions
    # on opposite sides of a shared currency (CHFJPY BUY is short-JPY,
    # NZDJPY SELL is long-JPY) NET OUT instead of stacking, so they must
    # not block each other. Block only when the new signal would raise
    # net absolute exposure in a shared currency beyond the limit.
    # Unknown position direction fails closed (assumed to stack).
    # Opportunity filter -- disabled per owner order 2026-09-22.
    if (enforce_correlation_gate and max_correlated_positions
            and max_correlated_positions > 0):
        cand = signed_currency_exposure(symbol, sig.get("direction"))
        if not cand:
            # Unknown candidate direction: conservative count-based proxy.
            codes = currency_codes(symbol)
            hits = sum(1 for info in open_positions.values()
                       if codes & currency_codes(info.get("symbol")))
            if hits >= max_correlated_positions:
                return "skipped:correlated_position", {"loud": True}
        else:
            net = {}
            for info in open_positions.values():
                expos = signed_currency_exposure(info.get("symbol"),
                                                 info.get("direction"))
                if not expos:
                    shared = (currency_codes(info.get("symbol"))
                              & set(cand))
                    for cur in shared:
                        expos[cur] = 1 if cand[cur] > 0 else -1
                for cur, e in expos.items():
                    net[cur] = net.get(cur, 0) + e
            for cur, cexp in cand.items():
                cur_net = net.get(cur, 0)
                new_net = cur_net + cexp
                if (abs(new_net) > max_correlated_positions
                        and abs(new_net) > abs(cur_net)):
                    return "skipped:correlated_position", {"loud": True}

    # 6c. consecutive losses: after max_consecutive_losses straight
    # losing closes, no new entries until a winner resets the count.
    # Opportunity filter -- disabled per owner order 2026-09-22.
    if (enforce_consecutive_losses_gate and max_consecutive_losses
            and max_consecutive_losses > 0):
        if (consecutive_losses or 0) >= max_consecutive_losses:
            return "skipped:consecutive_losses", {"loud": True}

    # 7. spread too wide: fixed cap per the user's approved risk config
    # (max_spread_points, default 50). Opportunity filter -- disabled per
    # owner order 2026-09-22 ("nothing should be blocking you from
    # trading"). NOTE: the NovaTrader EA enforces the same cap at send
    # time via InMaxSpreadPoints; that input was raised to effectively
    # unlimited in the same change so the EA does not re-block.
    spread = spec.get("spread_points")
    if enforce_spread_gate and spread is not None and spread > max_spread_points:
        return "skipped:spread_too_wide", {}

    # 8. daily loss limit (off the capital basis when one is set)
    daily_loss = 0.0 - todays_profit
    if daily_loss >= (max_daily_loss_pct / 100.0) * sizing_equity:
        return "skipped:daily_loss_limit", {"loud": True, "trip": True}

    # 9. floating drawdown: total open loss (bot + manual positions) at or
    # beyond FLOATING_DRAWDOWN_PCT of balance -> no NEW entries. Existing
    # positions keep working under their own hard SLs; the gate releases
    # automatically when floating recovers above the line.
    if balance:
        floating_loss = balance - equity  # > 0 means the account is red
        if floating_loss >= (FLOATING_DRAWDOWN_PCT / 100.0) * balance:
            return "skipped:floating_drawdown", {"loud": True}

    return "commanded", {"volume": volume, "risk_amount": risk_amount}

