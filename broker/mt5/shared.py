"""
Single source of truth for identifying this bot's own positions/deals.
Moved here from the original bridge's mt5_shared.py — the canonical
definitions live in broker/__init__ so Linux core can use them without
importing the mt5 subpackage; this module re-exports them for the MT5
adapter's internal use.
"""

from broker import (
    MAGIC_NUMBER,
    extract_signal_id_from_comment,
    is_own_position,
)

__all__ = ["MAGIC_NUMBER", "extract_signal_id_from_comment", "is_own_position"]
