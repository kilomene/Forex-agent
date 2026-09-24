"""Suite-wide import bootstrap.

Every test module must import bridge modules (trade_executor, trading_mode,
scripts/production_readiness, ...) without depending on the working directory
the suite was launched from. Previously a few modules relied on
``python -m pytest`` being run from the bridge dir (cwd lands on sys.path)
or on a sibling directory that only exists on the dev machine
(~/workspace/mt5/scripts). That made the suite green locally and red on a
fresh checkout -- exactly what killed CI on commit 23fad237.

This conftest puts the bridge root and its scripts/ dir on sys.path for the
whole tests/ package, so no module may rely on cwd or machine layout again.
"""
import os
import sys

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (BRIDGE, os.path.join(BRIDGE, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
