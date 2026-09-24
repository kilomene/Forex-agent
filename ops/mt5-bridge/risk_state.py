#!/usr/bin/env python3
"""
Per-mode persistent risk state for the Nova MT5 trade executor.

Worker C (2026-09-23): part of the Forex trading system upgrade.

Two independent state files live under the run dir and are NEVER shared or
mixed between modes:

    run/risk_state_DEMO.json
    run/risk_state_LIVE.json

Each holds, for its own mode only:

    mode, day (YYYY-MM-DD, server day), start_of_day_equity,
    realized_pl, floating_pl, commission, swap,
    peak_equity, current_drawdown (fraction, e.g. 0.012 = 1.2%),
    daily_loss_tripped (bool), kill_switch_tripped (bool), updated_at.

Durability rules
  * Atomic writes: write to a temp file in the same directory, flush +
    fsync, then os.replace(). A crash mid-write can never leave a torn
    JSON file.
  * Survives restarts: everything needed for the daily-loss and
    drawdown gates is reloaded from disk; in-memory state is never
    trusted across a restart.
  * Mode isolation: the file stores its own mode; loading a DEMO file as
    LIVE (or vice versa) raises ValueError instead of silently mixing
    P&L between the demo experiment and a live account.

Day roll: when the server day changes, start_of_day_equity is re-anchored
to the current broker equity and the daily counters reset. The kill-switch
flag is NOT auto-cleared by a day roll -- only an explicit operator action
(clear_kill_switch) clears it.
"""

import json
import math
import os
import tempfile
from datetime import datetime, timezone

MODES = ("DEMO", "LIVE")

FIELDS = (
    "mode", "day", "start_of_day_equity",
    "realized_pl", "floating_pl", "commission", "swap",
    "peak_equity", "current_drawdown",
    "daily_loss_tripped", "kill_switch_tripped", "updated_at",
)


def _now_str():
    # Repair round 2 (2026-09-24): UTC by construction (see
    # trade_executor.now_str).
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def state_path(run_dir, mode):
    """Filesystem path of the risk-state file for *mode*.

    Raises ValueError for anything but "DEMO"/"LIVE" -- there is no third
    mode and no shared file.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    return os.path.join(run_dir, f"risk_state_{mode}.json")


def _atomic_write_json(path, obj):
    """Write JSON atomically: temp file + fsync + os.replace."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".risk_state_", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(d, os.O_DIRECTORY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


class ModeRiskState:
    """Persistent per-mode risk ledger. DEMO and LIVE never share."""

    def __init__(self, mode, **kw):
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        self.mode = mode
        self.day = kw.get("day")
        self.start_of_day_equity = kw.get("start_of_day_equity")
        self.realized_pl = float(kw.get("realized_pl") or 0.0)
        self.floating_pl = float(kw.get("floating_pl") or 0.0)
        self.commission = float(kw.get("commission") or 0.0)
        self.swap = float(kw.get("swap") or 0.0)
        self.peak_equity = kw.get("peak_equity")
        self.current_drawdown = float(kw.get("current_drawdown") or 0.0)
        self.daily_loss_tripped = bool(kw.get("daily_loss_tripped"))
        self.kill_switch_tripped = bool(kw.get("kill_switch_tripped"))
        self.updated_at = kw.get("updated_at")

    # -- persistence ------------------------------------------------------
    @classmethod
    def load(cls, run_dir, mode):
        """Load the state for *mode*, creating a fresh one if missing.

        Raises ValueError if the file on disk claims a different mode --
        DEMO and LIVE data must never mix.
        """
        path = state_path(run_dir, mode)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return cls(mode)
        if not isinstance(data, dict) or data.get("mode") != mode:
            raise ValueError(
                f"risk-state mode mismatch at {path}: file claims "
                f"{(data.get('mode') if isinstance(data, dict) else '?')!r}, "
                f"asked for {mode!r}; refusing to mix DEMO/LIVE data")
        return cls(mode, **{k: data.get(k) for k in FIELDS if k != "mode"})

    def save(self, run_dir=None, path=None):
        """Persist atomically. Pass run_dir (mode-derived path) or path."""
        target = path or state_path(run_dir, self.mode)
        self.updated_at = _now_str()
        _atomic_write_json(target, self.to_dict())
        return target

    def to_dict(self):
        return {
            "mode": self.mode,
            "day": self.day,
            "start_of_day_equity": self.start_of_day_equity,
            "realized_pl": self.realized_pl,
            "floating_pl": self.floating_pl,
            "commission": self.commission,
            "swap": self.swap,
            "peak_equity": self.peak_equity,
            "current_drawdown": self.current_drawdown,
            "daily_loss_tripped": self.daily_loss_tripped,
            "kill_switch_tripped": self.kill_switch_tripped,
            "updated_at": self.updated_at,
        }

    # -- day roll ---------------------------------------------------------
    def roll_day_if_needed(self, day_str, current_equity):
        """Re-anchor to a new server day. Returns True when a roll happened.

        The kill-switch flag survives a day roll: only an explicit
        operator action clears it.
        """
        try:
            eq = float(current_equity)
        except (TypeError, ValueError):
            eq = None
        if not (eq is not None and math.isfinite(eq) and eq > 0):
            return False
        if self.day == day_str and self.start_of_day_equity:
            return False
        self.day = day_str
        self.start_of_day_equity = eq
        self.realized_pl = 0.0
        self.floating_pl = 0.0
        self.peak_equity = eq
        self.current_drawdown = 0.0
        self.daily_loss_tripped = False
        return True

    # -- P&L updates ------------------------------------------------------
    def _refresh_drawdown(self):
        sod = self.start_of_day_equity
        try:
            sod = float(sod)
        except (TypeError, ValueError):
            return
        if not (math.isfinite(sod) and sod > 0):
            return
        cur = sod + self.realized_pl + self.floating_pl
        try:
            peak = float(self.peak_equity)
        except (TypeError, ValueError):
            peak = None
        if peak is None or not math.isfinite(peak) or peak <= 0:
            peak = sod
        self.peak_equity = max(peak, cur)
        self.current_drawdown = max(
            0.0, (self.peak_equity - cur) / self.peak_equity)

    def record_closed_trade(self, profit, commission=0.0, swap=0.0):
        """Fold a closed trade's P&L into today's realized ledger."""
        for val, name in ((profit, "profit"), (commission, "commission"),
                          (swap, "swap")):
            try:
                v = float(val)
            except (TypeError, ValueError):
                v = 0.0
            if not math.isfinite(v):
                v = 0.0
            if name == "profit":
                self.realized_pl += v
            elif name == "commission":
                self.commission += v
            else:
                self.swap += v
        self._refresh_drawdown()

    def update_floating(self, floating_pl):
        """Refresh the mark-to-market floating P&L (broker positions feed)."""
        try:
            v = float(floating_pl)
        except (TypeError, ValueError):
            return
        if not math.isfinite(v):
            return
        self.floating_pl = v
        self._refresh_drawdown()

    # -- queries ----------------------------------------------------------
    def daily_pnl(self):
        """Today's realized + floating P&L for this mode."""
        return self.realized_pl + self.floating_pl

    def daily_loss_pct(self):
        """Today's loss as a positive % of start-of-day equity (0 if up)."""
        try:
            sod = float(self.start_of_day_equity)
        except (TypeError, ValueError):
            return 0.0
        if not (math.isfinite(sod) and sod > 0):
            return 0.0
        pnl = self.daily_pnl()
        return max(0.0, -pnl / sod * 100.0)

    def breached_daily_loss(self, limit_pct=3.0):
        """True when today's loss reached the limit or was already tripped."""
        if self.daily_loss_tripped:
            return True
        try:
            lim = float(limit_pct)
        except (TypeError, ValueError):
            return True
        return self.daily_loss_pct() >= lim

    # -- operator actions -------------------------------------------------
    def note_daily_loss_tripped(self):
        self.daily_loss_tripped = True

    def note_kill_switch_tripped(self):
        self.kill_switch_tripped = True

    def clear_daily_loss(self):
        """Explicit operator action: re-arm entries after a daily-loss halt."""
        self.daily_loss_tripped = False

    def clear_kill_switch(self):
        """Explicit operator action: clear the kill switch. Never automatic."""
        self.kill_switch_tripped = False
