"""Optional XGBoost trainer on backtest-labeled CSVs.

Ported from forex-bot-bridge/train_model.py. This module is OPTIONAL: the
subsystem works fully with ML disabled, and ml serving stays PARKED (see
predict.py). Training is a deliberate offline experiment, never part of
any live flow.

Heavy deps (numpy/pandas/xgboost/scikit-learn) are guarded: this module
imports cleanly without them, and training raises a clear error telling
you what to install. Run: python -m intelligence.ml.train --input
backtest_dataset.csv --output model.json

Design notes kept from the original:
- TIME-BASED train/test split (no shuffle): training on post-test rows
  would be lookahead bias by another name.
- "timeout" rows are dropped (ambiguous outcome — neither win nor loss).
- Warns below MIN_VIABLE_ROWS and when AUC ≈ 0.5 (noise, not signal).
"""

import argparse
import json
import logging

from intelligence.ml.features import FEATURE_COLUMNS

logger = logging.getLogger("intelligence.ml.train")

MIN_VIABLE_ROWS = 200  # below this, a trained model is more likely noise than signal

try:
    import numpy as np
    import pandas as pd
    import xgboost as xgb
    from sklearn.metrics import (
        accuracy_score,
        precision_score,
        recall_score,
        roc_auc_score,
    )

    _HEAVY_DEPS_AVAILABLE = True
    _HEAVY_DEPS_ERROR = None
except ImportError as exc:  # optional deps — module must still import
    _HEAVY_DEPS_AVAILABLE = False
    _HEAVY_DEPS_ERROR = exc


class MLDependencyError(RuntimeError):
    """Raised when training is attempted without the optional ML stack."""


def _require_deps():
    if not _HEAVY_DEPS_AVAILABLE:
        raise MLDependencyError(
            "ML training requires the optional stack: "
            "pip install xgboost scikit-learn pandas numpy "
            f"(import failed: {_HEAVY_DEPS_ERROR})"
        )


def load_dataset(csv_path: str):
    """Load and label a backtest CSV. Raises ValueError on an empty frame."""
    _require_deps()
    df = pd.read_csv(csv_path)
    if df.empty:
        raise ValueError(f"Dataset is empty: {csv_path} — nothing to train on.")
    missing = [c for c in FEATURE_COLUMNS + ["outcome", "candle_time"] if c not in df.columns]
    if missing:
        raise ValueError(f"Dataset {csv_path} is missing columns: {missing}")
    df = df.sort_values("candle_time")  # chronological order, required for the split

    # Timeouts are ambiguous by construction (neither SL nor TP resolved
    # within the lookahead window) — training on them as either class
    # would teach the model a fake pattern. Dropping them is the honest
    # choice, not a data-cleaning nicety.
    before = len(df)
    df = df[df["outcome"] != "timeout"].copy()
    logger.info(
        "Dropped %d timeout rows (ambiguous outcome), %d remain",
        before - len(df), len(df),
    )

    df["label"] = (df["outcome"] == "win").astype(int)
    return df


def time_based_split(df, test_fraction: float = 0.2):
    split_index = int(len(df) * (1 - test_fraction))
    return df.iloc[:split_index], df.iloc[split_index:]


def train(csv_path: str, output_model_path: str, test_fraction: float = 0.2) -> dict:
    _require_deps()
    df = load_dataset(csv_path)

    if len(df) < MIN_VIABLE_ROWS:
        logger.warning(
            "Only %d labeled rows (win/loss, timeouts excluded) — below the %d-row "
            "guideline for a stable model. Training will proceed, but treat the "
            "result as exploratory, not production-ready.",
            len(df), MIN_VIABLE_ROWS,
        )

    train_df, test_df = time_based_split(df, test_fraction)
    if test_df.empty or train_df.empty:
        raise ValueError(
            "Time-based split produced an empty train or test set "
            f"(train={len(train_df)}, test={len(test_df)} rows)."
        )
    logger.info(
        "Train: %d rows (%s to %s). Test: %d rows (%s to %s).",
        len(train_df), train_df["candle_time"].min(), train_df["candle_time"].max(),
        len(test_df), test_df["candle_time"].min(), test_df["candle_time"].max(),
    )

    X_train, y_train = train_df[FEATURE_COLUMNS], train_df["label"]
    X_test, y_test = test_df[FEATURE_COLUMNS], test_df["label"]

    model = xgb.XGBClassifier(
        n_estimators=100,
        max_depth=4,
        learning_rate=0.1,
        eval_metric="logloss",
    )
    model.fit(X_train, y_train)

    predictions = model.predict(X_test)
    probabilities = model.predict_proba(X_test)[:, 1]

    metrics = {
        "test_rows": len(test_df),
        "accuracy": round(accuracy_score(y_test, predictions), 4),
        "precision": round(precision_score(y_test, predictions, zero_division=0), 4),
        "recall": round(recall_score(y_test, predictions, zero_division=0), 4),
        "auc": round(roc_auc_score(y_test, probabilities), 4) if len(set(y_test)) > 1 else None,
        "baseline_win_rate": round(float(y_test.mean()), 4),  # "always predict win" score
    }

    logger.info("Test metrics: %s", json.dumps(metrics, indent=2))
    if metrics["auc"] is not None and metrics["auc"] < 0.55:
        logger.warning(
            "AUC of %.2f is close to random (0.50) — this model likely isn't finding "
            "real signal yet. More data or better features are the honest next step, "
            "not shipping this as-is.",
            metrics["auc"],
        )

    model.save_model(output_model_path)
    logger.info("Saved model to %s", output_model_path)

    metrics_path = output_model_path.replace(".json", "_metrics.json")
    with open(metrics_path, "w") as f:
        json.dump(metrics, f, indent=2)

    return metrics


def main():
    parser = argparse.ArgumentParser(description="Train an XGBoost model on a backtest dataset.")
    parser.add_argument("--input", default="backtest_dataset.csv")
    parser.add_argument("--output", default="model.json")
    parser.add_argument("--test-fraction", type=float, default=0.2)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    train(args.input, args.output, args.test_fraction)


if __name__ == "__main__":
    main()
