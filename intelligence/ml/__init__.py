"""Machine learning: OPTIONAL. Feature schema (canonical), offline trainer,
honest prediction stub. The subsystem works fully with ML disabled."""

from .features import FEATURE_COLUMNS
from .predict import get_ml_prediction

__all__ = ["FEATURE_COLUMNS", "get_ml_prediction"]
