import sys

from daemon.market_monitor import main_entry

sys.exit(main_entry(sys.argv[1:]))
