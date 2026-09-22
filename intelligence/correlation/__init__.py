"""Correlation intelligence: static sessions + correlation exposure (port of knowledge.js)."""

from .knowledge import (
    CORRELATION_REFERENCE,
    SESSIONS_UTC,
    check_correlated_exposure,
    correlated_pairs,
    current_session_info,
)

__all__ = [
    "SESSIONS_UTC",
    "CORRELATION_REFERENCE",
    "current_session_info",
    "correlated_pairs",
    "check_correlated_exposure",
]
