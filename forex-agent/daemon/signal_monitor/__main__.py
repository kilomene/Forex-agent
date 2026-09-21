import sys

from daemon.signal_monitor import main_entry

sys.exit(main_entry(sys.argv[1:]))
