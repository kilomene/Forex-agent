#!/usr/bin/env python3
"""
Protection-plugin framework for the MT5 trade executor's risk gates.

Phase 2 of the borrow-list item "Freqtrade's protection-plugin framework"
(research_notes/github-forex-trading-bot-research-20261018-0810/borrow-list.md
idea #1): trade_executor.check_gates()' long hardcoded gate chain becomes
named, composable, individually-toggleable filter objects.

HARD RULES (owner standing orders):
  * This module changes NO trading behavior. Every legacy filter preserves
    its gate's exact skip reason, flags, order, and semantics; parity is
    proven by tests/test_protection_parity.py against the frozen reference
    in tests/_legacy_gates.py.
  * The new plugins (CooldownPeriod, StoplossGuard, SessionFilter) are
    DISABLED BY DEFAULT and are NOT wired into the engine's legacy
    pipeline. Enablement is AUTO tier: it happens only through the
    pipeline's plugin-observe / plugin-enforce flow (calm observe soak
    inside preregistered bounds, engine wiring present, post-flip
    hard-rule assertions) -- never by hand-editing the config.
  * Trading stays HALTED: nothing here touches run/trading_enabled,
    blocked_symbols, risk config, or self-tune.

Contract
--------
Each filter exposes:
  name      stable identifier (the legacy gate name)
  enabled   bool toggle; the orchestrator skips disabled filters
  evaluate(sig, ctx) -> verdict

Verdicts:
  ALLOW ("allow")
      Gate passes; keep evaluating the pipeline.
  ("block", reason, flags, unlock_until)
      FIRST block wins. `reason` is the exact legacy "skipped:<...>" string
      so journaled trade.decision reasons are unchanged. `flags` carries the
      legacy side-effect flags (loud / trip / in_flight / sl_reason / ...).
      `unlock_until` is a tz-aware datetime (or None) for time-boxed locks;
      legacy gates always use None.
      NOTE: the tuple carries `flags` as a deliberate extension of the
      ("block", reason, unlock_until) sketch in the design note -- parity
      requires the flags.
  ("decide", decision, flags)
      Terminal pass decision. Only the dry-run filter uses this ("intended").

GateContext is a mutable namespace built from check_gates()' keyword
arguments. Filters read the inputs they need and publish derived values
(sizing_equity, spec, volume, risk_amount) for downstream filters, in the
same order the legacy chain computed them.

Pure helpers (validate_stop_loss, compute_volume, ...) stay in
trade_executor and are imported lazily inside evaluate(): trade_executor
imports THIS module at top level, so a top-level import here would be
circular. By the time any evaluate() runs, trade_executor is fully loaded.
"""

import fnmatch
import math
import re
from datetime import datetime, timedelta, timezone

import market_hours

# ---------------------------------------------------------------- verdicts

ALLOW = "allow"


def _te():
    """trade_executor module, imported lazily (see module docstring)."""
    import trade_executor
    return trade_executor


# ---------------------------------------------------------------- context


class GateContext:
    """Mutable evaluation context shared by the filter pipeline.

    Built from check_gates()' keyword arguments; derived slots
    (sizing_equity, spec, volume, risk_amount) are filled by filters in
    legacy order.
    """

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        # Derived values, published by filters in legacy order:
        self.sizing_equity = None  # RiskBasisFilter
        self.spec = None           # RiskBasisFilter (symbol spec lookup)
        self.volume = 0.0          # SizingFilter (clamped by TotalExposureFilter)
        self.risk_amount = 0.0     # SizingFilter


# ---------------------------------------------------------------- base


class ProtectionFilter:
    """Base class for all protection filters."""

    name = "base"
    enabled = True

    def __init__(self, enabled=True):
        self.enabled = enabled

    def evaluate(self, sig, ctx):
        """Return ALLOW, ("block", reason, flags, unlock_until), or
        ("decide", decision, flags)."""
        raise NotImplementedError


# ---------------------------------------------------------------- engine


def run_pipeline(sig, ctx, filters):
    """Run enabled filters in order; first block (or terminal decide) wins.

    All filters allow -> ("commanded", {"volume", "risk_amount"}), exactly
    the legacy fall-through return.
    """
    for f in filters:
        if not f.enabled:
            continue
        verdict = f.evaluate(sig, ctx)
        if verdict == ALLOW:
            continue
        kind = verdict[0]
        if kind == "block":
            _, reason, flags, _unlock_until = verdict
            return reason, dict(flags or {})
        if kind == "decide":
            _, decision, flags = verdict
            return decision, dict(flags or {})
        raise ValueError(
            "unknown verdict %r from protection filter %s" % (verdict, f.name))
    return "commanded", {"volume": ctx.volume, "risk_amount": ctx.risk_amount}


def legacy_pipeline(ctx):
    """The exact legacy gate chain as composable filters, in legacy order.

    Each filter's `enabled` mirrors the legacy enforce_* toggle for that
    gate; the M3 stop-loss filter is always enabled (as in legacy -- the
    require_stop_loss flag can no longer disable it).
    """
    return [
        TradingEnabledFilter(),
        SignalFreshnessFilter(),
        BlockedSymbolsFilter(),
        RiskBasisFilter(),
        ModeIdentityFilter(enabled=ctx.enforce_mode_checks),
        MarginDataFilter(enabled=ctx.enforce_mode_checks),
        SpreadKnownFilter(enabled=ctx.enforce_mode_checks),
        StopLossFilter(),  # M3: ALWAYS enforced
        SpecsFreshFilter(),
        SizingFilter(),
        MaxRiskPerTradeFilter(enabled=ctx.enforce_mode_checks),
        ModeDailyLossFilter(enabled=ctx.enforce_mode_checks),
        EmergencyDrawdownFilter(enabled=ctx.enforce_mode_checks),
        TotalExposureFilter(),
        TickFreshnessFilter(),
        DryRunIntendedFilter(),
        MarketOpenFilter(),
        ServerModeFilter(),
        MaxConcurrentFilter(),
        SinglePositionPerSymbolFilter(
            enabled=ctx.enforce_single_position_per_symbol),
        CorrelatedPositionFilter(enabled=ctx.enforce_correlation_gate),
        ConsecutiveLossesFilter(enabled=ctx.enforce_consecutive_losses_gate),
        SpreadCapFilter(enabled=ctx.enforce_spread_gate),
        DailyLossLimitFilter(),
        FloatingDrawdownFilter(),
    ]


# ---------------------------------------------------------------- legacy
# filters (gates 1..25, in legacy order). Each class docstring names the
# legacy gate it ports; evaluate() transplants the legacy code verbatim.


class TradingEnabledFilter(ProtectionFilter):
    """Gate 1: not enabled (config or kill switch)."""

    name = "trading_disabled"

    def evaluate(self, sig, ctx):
        if not ctx.enabled:
            return ("block", "skipped:trading_disabled", {}, None)
        return ALLOW


class SignalFreshnessFilter(ProtectionFilter):
    """Gate 1a0 (2026-10-15): never execute a signal whose candle_time is
    older than max_signal_age_hours. Unparseable/missing candle_time fails
    closed."""

    name = "stale_signal"

    def evaluate(self, sig, ctx):
        limit = ctx.max_signal_age_hours
        if limit and limit > 0:
            candle_time = sig.get("candle_time")
            try:
                ct = datetime.strptime(
                    str(candle_time), "%Y.%m.%d %H:%M:%S").replace(
                        tzinfo=timezone.utc)
            except (ValueError, TypeError, AttributeError):
                return ("block", "skipped:stale_signal", {"loud": True}, None)
            age_h = (datetime.now(timezone.utc) - ct).total_seconds() / 3600.0
            if age_h > limit:
                return ("block", "skipped:stale_signal", {"loud": True}, None)
        return ALLOW


class BlockedSymbolsFilter(ProtectionFilter):
    """Gate 1a (Racheal engagement #3, 2026-10-09): blocked symbols fail
    closed -- symbols with unverifiable broker tick_values cannot be sized
    safely."""

    name = "symbol_blocked"

    def evaluate(self, sig, ctx):
        blocked = ctx.blocked_symbols or []
        if sig.get("symbol") in blocked:
            return ("block", "skipped:symbol_blocked", {"loud": True}, None)
        return ALLOW


class RiskBasisFilter(ProtectionFilter):
    """Gate 1b (2026-09-22): owner-allocated capital basis, fail closed.
    Publishes ctx.sizing_equity and ctx.spec (symbol spec lookup) for
    downstream filters, exactly where the legacy chain computed them."""

    name = "risk_basis_unavailable"

    def evaluate(self, sig, ctx):
        try:
            rb = float(ctx.risk_basis)
        except (TypeError, ValueError):
            rb = float("nan")
        if not math.isfinite(rb) or rb <= 0:
            return ("block", "skipped:risk_basis_unavailable",
                    {"loud": True}, None)
        ctx.sizing_equity = rb
        specs = ctx.specs
        ctx.spec = ((specs.get("symbols") or {}).get(sig.get("symbol"))
                    if specs else None)
        return ALLOW


class ModeIdentityFilter(ProtectionFilter):
    """Gate M0 (Worker C, 2026-09-23): mode valid ("DEMO"/"LIVE"), account
    identity matches the mode record, LIVE authorization present when
    mode == LIVE. Runs only when enforce_mode_checks is True."""

    name = "mode_identity"

    def evaluate(self, sig, ctx):
        if ctx.mode not in ("DEMO", "LIVE"):
            return ("block", "skipped:invalid_mode", {"loud": True}, None)
        if ctx.mode_identity_ok is not True:
            return ("block", "skipped:mode_identity_mismatch",
                    {"loud": True}, None)
        if ctx.mode == "LIVE" and not ctx.live_authorized:
            return ("block", "skipped:live_not_authorized",
                    {"loud": True}, None)
        return ALLOW


class MarginDataFilter(ProtectionFilter):
    """Gate M1: equity / balance / free margin / margin level present and
    sane. Fail closed."""

    name = "margin_data_unavailable"

    def evaluate(self, sig, ctx):
        te = _te()
        specs = ctx.specs
        acct = (specs.get("account") or {}) if specs else {}
        fm = (acct.get("free_margin") if ctx.free_margin is None
              else ctx.free_margin)
        ml = (acct.get("margin_level") if ctx.margin_level is None
              else ctx.margin_level)
        if not (te._finite_pos_number(ctx.equity)
                and te._finite_pos_number(ctx.balance)
                and te._finite_pos_number(fm)
                and te._finite_pos_number(ml)):
            return ("block", "skipped:margin_data_unavailable",
                    {"loud": True}, None)
        return ALLOW


class SpreadKnownFilter(ProtectionFilter):
    """Gate M2: spread must come from live broker data (fail closed)."""

    name = "spread_unknown"

    def evaluate(self, sig, ctx):
        if ctx.spec is None or ctx.spec.get("spread_points") is None:
            return ("block", "skipped:spread_unknown", {"loud": True}, None)
        return ALLOW


class StopLossFilter(ProtectionFilter):
    """Gate M3: mandatory valid broker-side SL -- ALWAYS enforced, even
    when enforce_mode_checks is False (the no-SL audit found the old
    require_stop_loss config flag could disable the SL gate entirely)."""

    name = "stop_loss"

    def evaluate(self, sig, ctx):
        te = _te()
        if not sig.get("take_profit"):
            return ("block", "skipped:missing_sl_tp", {"loud": True}, None)
        sl_ok, sl_reason = te.validate_stop_loss(
            sig, ctx.spec, price_reference=ctx.price_reference)
        if not sl_ok:
            if sl_reason == "missing_sl":
                return ("block", "skipped:missing_sl_tp", {"loud": True}, None)
            return ("block", "skipped:invalid_sl",
                    {"loud": True, "sl_reason": sl_reason}, None)
        return ALLOW


class SpecsFreshFilter(ProtectionFilter):
    """Gate "sizing needs specs": stale/missing specs journal their skip
    reason (even in dry_run, which still wants full computed details)."""

    name = "stale_specs"

    def evaluate(self, sig, ctx):
        if not ctx.specs_fresh or ctx.spec is None:
            return ("block", "skipped:stale_specs", {}, None)
        return ALLOW


class SizingFilter(ProtectionFilter):
    """Position sizing via compute_volume. Sizing failures journal their
    skip reason ("skipped:bad_specs" / "skipped:volume_below_min").
    Publishes ctx.volume / ctx.risk_amount for downstream filters."""

    name = "sizing"

    def evaluate(self, sig, ctx):
        te = _te()
        specs = ctx.specs
        acct_ccy = ((specs.get("account") or {}).get("currency") or "USD") \
            if specs else "USD"
        volume, risk_amount, err = te.compute_volume(
            ctx.sizing_equity, ctx.risk_pct,
            sig.get("entry_price"), sig.get("stop_loss"), ctx.spec,
            tick_value_overrides=ctx.tick_value_overrides,
            symbol=sig.get("symbol"),
            account_currency=acct_ccy)
        if err:
            return ("block", err, {}, None)
        ctx.volume = volume
        ctx.risk_amount = risk_amount
        return ALLOW


class MaxRiskPerTradeFilter(ProtectionFilter):
    """Gate M4: per-trade planned risk hard cap ($100/trade on the $200,000
    capital basis)."""

    name = "max_risk_per_trade"

    def evaluate(self, sig, ctx):
        te = _te()
        try:
            cap = float(ctx.max_risk_per_trade_usd)
        except (TypeError, ValueError):
            cap = te.MAX_RISK_PER_TRADE_USD
        if ctx.risk_amount > cap + 1e-9:
            return ("block", "skipped:max_risk_per_trade",
                    {"loud": True}, None)
        return ALLOW


class ModeDailyLossFilter(ProtectionFilter):
    """Gate M5: per-mode daily loss < 3% of the mode's start-of-day equity.
    Blocked entries journal risk.daily_loss_block via the trip_mode_daily
    flag (handled by the engine, which owns I/O)."""

    name = "mode_daily_loss"

    def evaluate(self, sig, ctx):
        te = _te()
        sod = te._mrs_get(ctx.mode_risk_state, "start_of_day_equity")
        if not te._finite_pos_number(sod):
            return ("block", "skipped:mode_state_unavailable",
                    {"loud": True}, None)
        if te._mrs_get(ctx.mode_risk_state, "daily_loss_tripped", False):
            return ("block", "skipped:mode_daily_loss",
                    {"loud": True, "trip_mode_daily": True}, None)
        realized = te._mrs_get(ctx.mode_risk_state, "realized_pl", 0.0) or 0.0
        floating = te._mrs_get(ctx.mode_risk_state, "floating_pl", 0.0) or 0.0
        if -(realized + floating) >= 0.03 * sod:
            return ("block", "skipped:mode_daily_loss",
                    {"loud": True, "trip_mode_daily": True}, None)
        return ALLOW


class EmergencyDrawdownFilter(ProtectionFilter):
    """Gate M6: per-mode emergency drawdown trips the kill switch (trip +
    trip_mode_kill flags; the engine applies them)."""

    name = "emergency_drawdown"

    def evaluate(self, sig, ctx):
        te = _te()
        try:
            edd = float(ctx.emergency_drawdown_pct)
        except (TypeError, ValueError):
            edd = te.EMERGENCY_DRAWDOWN_PCT
        dd = te._mrs_get(ctx.mode_risk_state, "current_drawdown", 0.0) or 0.0
        if (te._mrs_get(ctx.mode_risk_state, "kill_switch_tripped", False)
                or dd >= edd / 100.0):
            return ("block", "skipped:emergency_drawdown",
                    {"loud": True, "trip": True, "trip_mode_kill": True},
                    None)
        return ALLOW


class TotalExposureFilter(ProtectionFilter):
    """Gate 1b (exposure): clamp the new trade so bot-wide open volume never
    exceeds max_total_exposure_lots. Clamp (not reject) keeps trading alive
    at small size; a trade that cannot fit at all is skipped. Mutates
    ctx.volume exactly as the legacy clamp did."""

    name = "max_total_exposure"

    def evaluate(self, sig, ctx):
        te = _te()
        cap = ctx.max_total_exposure_lots
        if cap and cap > 0:
            remaining = cap - (ctx.open_lots or 0.0)
            step = ctx.spec.get("volume_step") or 0.01
            if remaining <= 0:
                return ("block", "skipped:max_total_exposure",
                        {"loud": True}, None)
            if ctx.volume > remaining:
                volume = math.floor(remaining / step) * step
                volume = round(volume, te.step_decimals(step))
                vmin = ctx.spec.get("volume_min") or 0
                if volume < vmin:
                    return ("block", "skipped:max_total_exposure",
                            {"loud": True}, None)
                ctx.volume = volume
        return ALLOW


class TickFreshnessFilter(ProtectionFilter):
    """Gate 1c (port from repo gateway): never base a trade decision on a
    stale quote."""

    name = "stale_tick"

    def evaluate(self, sig, ctx):
        te = _te()
        if ctx.tick_age_s is not None and ctx.tick_age_s > te.TICK_MAX_AGE_S:
            return ("block", "skipped:stale_tick", {"loud": True}, None)
        return ALLOW


class DryRunIntendedFilter(ProtectionFilter):
    """Gate 2: dry_run -> "intended" with the computed volume/risk_amount;
    write nothing. A terminal pass decision (not a block)."""

    name = "dry_run_intended"

    def evaluate(self, sig, ctx):
        if ctx.dry_run:
            return ("decide", "intended",
                    {"volume": ctx.volume, "risk_amount": ctx.risk_amount})
        return ALLOW


class MarketOpenFilter(ProtectionFilter):
    """Gate 3: market closed -> no NEW positions (existing positions are
    held)."""

    name = "market_closed"

    def evaluate(self, sig, ctx):
        if not ctx.market_open:
            return ("block", "skipped:market_closed", {}, None)
        return ALLOW


class ServerModeFilter(ProtectionFilter):
    """Gate 4: server check (mode-aware). DEMO/legacy: refuse anything that
    is not a demo server. LIVE: refuse demo servers outright (a live mode
    record trades on its bound live broker, never on demo)."""

    name = "server_mode"

    def evaluate(self, sig, ctx):
        te = _te()
        specs = ctx.specs
        server = ((specs.get("account") or {}).get("server")
                  or specs.get("server") or "")
        if ctx.enforce_mode_checks and ctx.mode == "LIVE":
            if te.is_demo_server(server):
                return ("block", "skipped:live_on_demo_server",
                        {"loud": True}, None)
        elif not te.is_demo_server(server):
            return ("block", "skipped:non_demo_server", {"loud": True}, None)
        return ALLOW


class MaxConcurrentFilter(ProtectionFilter):
    """Gate 5: confirmed open positions PLUS in-flight commands (trade.open
    emitted but not yet broker-confirmed) against max_concurrent."""

    name = "max_concurrent"

    def evaluate(self, sig, ctx):
        in_flight = ctx.in_flight_count or 0
        if len(ctx.open_positions) + in_flight >= ctx.max_concurrent:
            return ("block", "skipped:max_concurrent",
                    {"in_flight": in_flight}, None)
        return ALLOW


class SinglePositionPerSymbolFilter(ProtectionFilter):
    """Gate 6: symbol already has an open position. Opportunity filter --
    disabled per owner order 2026-09-22."""

    name = "symbol_already_open"

    def evaluate(self, sig, ctx):
        symbol = sig.get("symbol")
        for info in ctx.open_positions.values():
            if info.get("symbol") == symbol:
                return ("block", "skipped:symbol_already_open", {}, None)
        return ALLOW


class CorrelatedPositionFilter(ProtectionFilter):
    """Gate 6b: direction-aware net currency exposure. Block only when the
    new signal would raise net absolute exposure in a shared currency
    beyond the limit; opposite-side positions net out instead of
    stacking. Unknown position direction fails closed (assumed to stack).
    Opportunity filter -- disabled per owner order 2026-09-22."""

    name = "correlated_position"

    def evaluate(self, sig, ctx):
        te = _te()
        max_corr = ctx.max_correlated_positions
        if max_corr and max_corr > 0:
            symbol = sig.get("symbol")
            cand = te.signed_currency_exposure(symbol, sig.get("direction"))
            if not cand:
                # Unknown candidate direction: conservative count-based
                # proxy.
                codes = te.currency_codes(symbol)
                hits = sum(1 for info in ctx.open_positions.values()
                           if codes & te.currency_codes(info.get("symbol")))
                if hits >= max_corr:
                    return ("block", "skipped:correlated_position",
                            {"loud": True}, None)
            else:
                net = {}
                for info in ctx.open_positions.values():
                    expos = te.signed_currency_exposure(info.get("symbol"),
                                                        info.get("direction"))
                    if not expos:
                        shared = (te.currency_codes(info.get("symbol"))
                                  & set(cand))
                        for cur in shared:
                            expos[cur] = 1 if cand[cur] > 0 else -1
                    for cur, e in expos.items():
                        net[cur] = net.get(cur, 0) + e
                for cur, cexp in cand.items():
                    cur_net = net.get(cur, 0)
                    new_net = cur_net + cexp
                    if (abs(new_net) > max_corr
                            and abs(new_net) > abs(cur_net)):
                        return ("block", "skipped:correlated_position",
                                {"loud": True}, None)
        return ALLOW


class ConsecutiveLossesFilter(ProtectionFilter):
    """Gate 6c: after max_consecutive_losses straight losing closes, no new
    entries until a winner resets the count. Opportunity filter --
    disabled per owner order 2026-09-22."""

    name = "consecutive_losses"

    def evaluate(self, sig, ctx):
        max_cl = ctx.max_consecutive_losses
        if max_cl and max_cl > 0:
            if (ctx.consecutive_losses or 0) >= max_cl:
                return ("block", "skipped:consecutive_losses",
                        {"loud": True}, None)
        return ALLOW


class SpreadCapFilter(ProtectionFilter):
    """Gate 7: spread too wide (fixed cap per the approved risk config).
    Opportunity filter -- disabled per owner order 2026-09-22."""

    name = "spread_too_wide"

    def evaluate(self, sig, ctx):
        spread = ctx.spec.get("spread_points")
        if spread is not None and spread > ctx.max_spread_points:
            return ("block", "skipped:spread_too_wide", {}, None)
        return ALLOW


class DailyLossLimitFilter(ProtectionFilter):
    """Gate 8: daily loss limit (off the capital basis when one is set).
    Trips the kill switch via the "trip" flag (the engine owns the I/O)."""

    name = "daily_loss_limit"

    def evaluate(self, sig, ctx):
        daily_loss = 0.0 - ctx.todays_profit
        if daily_loss >= (ctx.max_daily_loss_pct / 100.0) * ctx.sizing_equity:
            return ("block", "skipped:daily_loss_limit",
                    {"loud": True, "trip": True}, None)
        return ALLOW


class FloatingDrawdownFilter(ProtectionFilter):
    """Gate 9: total open loss (bot + manual positions) at or beyond
    FLOATING_DRAWDOWN_PCT of balance -> no NEW entries. Existing positions
    keep working under their own hard SLs; the gate releases automatically
    when floating recovers above the line."""

    name = "floating_drawdown"

    def evaluate(self, sig, ctx):
        te = _te()
        if ctx.balance:
            floating_loss = ctx.balance - ctx.equity  # > 0 means red
            if floating_loss >= (te.FLOATING_DRAWDOWN_PCT / 100.0) * ctx.balance:
                return ("block", "skipped:floating_drawdown",
                        {"loud": True}, None)
        return ALLOW


# ---------------------------------------------------------------- new
# plugins (Freqtrade/EA31337 borrows). ALL DISABLED BY DEFAULT and NOT
# wired into legacy_pipeline(). Enablement is AUTO tier: it happens only
# through the pipeline's plugin-observe / plugin-enforce flow (calm
# observe soak inside preregistered bounds, engine wiring present).
# Each takes its state inputs from the GateContext (see class
# docstrings for the honest state design); the engine does not feed those
# inputs today.


class CooldownPeriod(ProtectionFilter):
    """Freqtrade CooldownPeriod: no re-entry on a symbol for
    `cooldown_candles` candles after an exit.

    State input (honest design): the engine would feed
    `ctx.last_exit_times`, a dict mapping symbol -> tz-aware datetime of
    the most recent exit (built from trade.closed journal events). Absent
    -> no cooldown, allow. Candle length comes from the signal's timeframe
    ("M15" -> 15 min, "H1" -> 60 min, "D1" -> 1440 min); unparseable or
    missing timeframe falls back to `default_candle_minutes`.

    A block carries unlock_until = last exit + cooldown window, so a
    future engine can journal/respect the lock expiry.
    """

    name = "cooldown_period"

    def __init__(self, enabled=False, cooldown_candles=3,
                 default_candle_minutes=60):
        super().__init__(enabled=enabled)
        self.cooldown_candles = cooldown_candles
        self.default_candle_minutes = default_candle_minutes

    @staticmethod
    def _candle_minutes(sig, default):
        tf = (sig.get("timeframe") or "").upper().strip()
        m = re.match(r"^M(\d+)$", tf)
        if m:
            return int(m.group(1))
        m = re.match(r"^H(\d+)$", tf)
        if m:
            return int(m.group(1)) * 60
        m = re.match(r"^D(\d*)$", tf)
        if m:
            return int(m.group(1) or 1) * 24 * 60
        return default

    def evaluate(self, sig, ctx):
        last_exits = getattr(ctx, "last_exit_times", None) or {}
        last = last_exits.get(sig.get("symbol"))
        if last is None:
            return ALLOW
        minutes = self._candle_minutes(sig, self.default_candle_minutes)
        unlock_until = last + timedelta(
            minutes=self.cooldown_candles * minutes)
        if datetime.now(timezone.utc) < unlock_until:
            return ("block", "skipped:cooldown_period",
                    {"cooldown_candles": self.cooldown_candles},
                    unlock_until)
        return ALLOW


class StoplossGuard(ProtectionFilter):
    """Freqtrade StoplossGuard: lock a symbol (or all symbols) after
    `max_stopouts` stop-outs within the last `lookback_trades` closes.

    State input (honest design): the engine would feed
    `ctx.recent_closes`, a list of dicts with at least {"symbol",
    "reason"} built from trade.closed journal events (oldest first).
    A close counts as a stop-out when its reason (case-insensitive) is
    one of {"sl", "stop_loss", "stoploss", "stop"}.
    `only_symbol=True` (default) locks just the signal's symbol; False
    locks everything after N stop-outs anywhere.

    The lock is window-based (it releases as the lookback window slides
    past the stop-outs), so unlock_until is None.
    """

    name = "stoploss_guard"
    STOPOUT_REASONS = frozenset({"sl", "stop_loss", "stoploss", "stop"})

    def __init__(self, enabled=False, max_stopouts=3, lookback_trades=10,
                 only_symbol=True):
        super().__init__(enabled=enabled)
        self.max_stopouts = max_stopouts
        self.lookback_trades = lookback_trades
        self.only_symbol = only_symbol

    def evaluate(self, sig, ctx):
        closes = getattr(ctx, "recent_closes", None) or []
        window = (closes[-self.lookback_trades:]
                  if self.lookback_trades > 0 else [])
        symbol = sig.get("symbol")
        stopouts = 0
        for ev in window:
            if str(ev.get("reason") or "").lower() not in self.STOPOUT_REASONS:
                continue
            if self.only_symbol and ev.get("symbol") != symbol:
                continue
            stopouts += 1
        if stopouts >= self.max_stopouts:
            return ("block", "skipped:stoploss_guard",
                    {"stopouts": stopouts,
                     "lookback_trades": self.lookback_trades},
                    None)
        return ALLOW


class SessionFilter(ProtectionFilter):
    """Per-symbol session gating (EA31337 SignalOpenFilterTime pattern):
    `session_rules` maps a symbol pattern (fnmatch, case-insensitive,
    e.g. "GBP*") to the set of blocked session buckets
    (market_hours.session_bucket names: "asia", "london", "newyork",
    "late"). Empty rules -> allow everything.

    State input (honest design): `ctx.session` (bucket name, as computed
    by the engine's server_market_state) or `ctx.server_now` (server-tz
    datetime, from which the bucket is derived). Neither present -> allow
    (fail-open would be wrong for a gate, but with no session knowledge
    there is nothing to enforce; the filter is disabled by default
    anyway).
    """

    name = "session_filter"

    def __init__(self, enabled=False, session_rules=None):
        super().__init__(enabled=enabled)
        self.session_rules = dict(session_rules or {})

    def evaluate(self, sig, ctx):
        if not self.session_rules:
            return ALLOW
        session = getattr(ctx, "session", None)
        if session is None:
            server_now = getattr(ctx, "server_now", None)
            if server_now is None:
                return ALLOW
            session = market_hours.session_bucket(server_now.hour)
        symbol = (sig.get("symbol") or "").upper()
        for pattern, blocked in self.session_rules.items():
            if fnmatch.fnmatchcase(symbol, str(pattern).upper()):
                if session in set(blocked or ()):
                    return ("block", "skipped:session_blocked",
                            {"session": session, "rule": pattern}, None)
        return ALLOW
