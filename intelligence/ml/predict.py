"""ML prediction interface — honestly unavailable, serving PARKED.

This is the subsystem's replacement for the Worker's `get_ml_prediction`
tool. There is no labeled dataset, no trained model, and no serving
endpoint, so this returns::

    {"available": False, "reason": "no trained model deployed"}

SERVING STATUS: PARKED. The original `ml_serving/serve_model.py` (a working
FastAPI `/predict`) was deliberately not ported: nothing in the live flow
ever called it, and wiring a prediction endpoint before a real trained
model exists would be theater. The path to un-parking is explicit:

  1. backtesting/ produces a labeled CSV (real walk-forward, closed candles)
  2. intelligence.ml.train trains a model.json (offline, optional deps)
  3. a serving process is built that loads model.json and computes
     intelligence.ml.features.FEATURE_COLUMNS from live signals
  4. ONLY then does this module get a real backend

Until then the subsystem works fully with ML disabled — no strategy, risk
check, or daemon may treat a missing prediction as a negative signal.
"""


def get_ml_prediction(features=None, model=None) -> dict:
    """Win-probability estimate for a setup — honestly unavailable.

    `features`: dict of FEATURE_COLUMNS values (accepted for interface
    compatibility; unused while parked). Returns available=False until a
    trained model is deployed behind a real serving endpoint.
    """
    return {"available": False, "reason": "no trained model deployed"}
