"""pytest path bootstrap: runtime sets PYTHONPATH to forex-agent/;
tests do the same here so they never rely on the working directory."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
