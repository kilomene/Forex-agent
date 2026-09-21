"""Canonical ML feature schema — the SINGLE source of truth.

Ported from forex-bot-bridge/ml_features.py (the byte-duplicate under
ml_serving/ was intentionally dropped; see MIGRATION.intel.md).

This 8-feature schema is the training-time/inference-time contract: the
backtest writes these columns, train.py trains on them, and any future
serving endpoint must compute exactly these features. If one side changes,
all sides must change together — keep this list stable.
"""

FEATURE_COLUMNS = [
    "ema_fast", "ema_slow", "rsi_value", "atr_value",
    "ema_spread_pct",  # (ema_fast - ema_slow) / entry_price — normalizes across pairs
    "smc_trend_uptrend", "smc_trend_downtrend", "smc_trend_ranging",  # one-hot
]
