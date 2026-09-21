"""Tests for intelligence.ml: canonical schema, parked serving, guarded trainer."""

import os
import tempfile

from intelligence.ml import FEATURE_COLUMNS, get_ml_prediction
from intelligence.ml import train as train_mod


def test_feature_schema_has_expected_8_fields():
    assert FEATURE_COLUMNS == [
        "ema_fast", "ema_slow", "rsi_value", "atr_value",
        "ema_spread_pct",
        "smc_trend_uptrend", "smc_trend_downtrend", "smc_trend_ranging",
    ]
    assert len(FEATURE_COLUMNS) == 8


def test_prediction_honestly_unavailable():
    result = get_ml_prediction(features={c: 0.0 for c in FEATURE_COLUMNS})
    assert result == {"available": False, "reason": "no trained model deployed"}


def test_trainer_imports_without_heavy_deps():
    # The module must import on a bare Linux box (no pandas/xgboost).
    assert hasattr(train_mod, "train")
    assert hasattr(train_mod, "load_dataset")
    assert train_mod.MIN_VIABLE_ROWS == 200


def test_trainer_guards_missing_deps_or_empty_frame():
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    try:
        if not train_mod._HEAVY_DEPS_AVAILABLE:
            try:
                train_mod.load_dataset(path)
            except train_mod.MLDependencyError:
                pass
            else:
                raise AssertionError("expected MLDependencyError without pandas")
        else:
            # Empty CSV -> the empty-frame edge case must raise, not train on nothing.
            with open(path, "w") as f:
                f.write("candle_time," + ",".join(FEATURE_COLUMNS) + ",outcome\n")
            try:
                train_mod.load_dataset(path)
            except ValueError as exc:
                assert "empty" in str(exc).lower()
            else:
                raise AssertionError("expected ValueError on empty dataset")
    finally:
        os.unlink(path)
