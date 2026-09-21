"""Reflections: deterministic post-trade outcome math + reflection records.

`reflect` is imported explicitly (not here) to avoid a package import cycle
with intelligence.experience.
"""

from .outcome import compute_outcome

__all__ = ["compute_outcome"]
