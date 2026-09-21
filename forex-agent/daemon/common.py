"""Shared daemon plumbing: PID files, lifecycle, config/store/adapter access.

Every daemon is a separate OS process running ``python3 -m daemon.<name>``.
PID files live in ``$FOREX_AGENT_HOME/run/<name>.pid`` — the same files
``forex.get_health`` and ``scripts/forex-daemons`` read.

A daemon never imports another daemon. All cross-area access goes through
the helpers here (which reuse agent/tools/backend seams where they exist).
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("forex_agent.daemon")


def get_home() -> str:
    home = os.environ.get("FOREX_AGENT_HOME", "")
    if home:
        return home
    home = os.path.join(os.path.expanduser("~"), ".forex-agent")
    os.makedirs(home, exist_ok=True)
    return home


def run_dir() -> str:
    path = os.path.join(get_home(), "run")
    os.makedirs(path, exist_ok=True)
    return path


def pid_file(name: str) -> str:
    return os.path.join(run_dir(), name + ".pid")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid(name: str) -> Optional[int]:
    """PID recorded for a daemon, or None if no live process."""
    path = pid_file(name)
    try:
        with open(path) as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return None
    return pid if _pid_alive(pid) else None


def acquire_pidfile(name: str) -> bool:
    """Idempotent PID acquisition. Returns True if THIS process now owns
    the pidfile; False if another live process already does (caller
    should exit quietly). Stale pidfiles are reclaimed."""
    path = pid_file(name)
    existing = read_pid(name)
    if existing is not None:
        return False
    with open(path, "w") as fh:
        fh.write(str(os.getpid()))
    # Lost a race? Whoever wrote last wins; re-read to be sure it's us.
    try:
        with open(path) as fh:
            return int(fh.read().strip()) == os.getpid()
    except (OSError, ValueError):
        return False


def release_pidfile(name: str) -> None:
    path = pid_file(name)
    try:
        with open(path) as fh:
            if int(fh.read().strip()) != os.getpid():
                return  # not ours; leave it alone
    except (OSError, ValueError):
        return
    try:
        os.remove(path)
    except OSError:
        pass


def load_config():
    from config.config import load_config  # noqa: PLC0415
    return load_config()


def get_store():
    from storage import Store  # noqa: PLC0415
    return Store()


def get_adapter(config=None):
    """The configured broker adapter (same construction as agent tools)."""
    from agent.tools import backend  # noqa: PLC0415
    return backend.broker_adapter()


def get_daemon_state(store, name: str) -> dict:
    """Last persisted state blob for a daemon (survives restarts)."""
    try:
        rows = store.journal_query(kind="daemon_state", symbol=name, limit=1)
    except Exception:
        return {}
    if not rows:
        return {}
    detail = rows[0].get("detail")
    return dict(detail) if isinstance(detail, dict) else {}


def save_daemon_state(store, name: str, state: dict) -> None:
    try:
        store.journal_add({"kind": "daemon_state", "symbol": name,
                           "detail": dict(state)})
    except Exception:
        logger.exception("daemon %s: could not persist state", name)


class Daemon:
    """Base class: acquire pidfile, loop run_once() every interval,
    exit cleanly on SIGTERM/SIGINT. Subclasses implement run_once()."""

    name = "daemon"
    interval = 60.0

    def __init__(self, interval: Optional[float] = None):
        self._stop = False
        if interval is not None:
            self.interval = interval

    def run_once(self) -> None:
        raise NotImplementedError

    def _handle_signal(self, signum, frame):
        logger.warning("daemon %s: received signal %s, stopping", self.name, signum)
        self._stop = True

    def run_forever(self) -> int:
        logging.basicConfig(
            stream=sys.stderr, level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s")
        if not acquire_pidfile(self.name):
            logger.warning("daemon %s: already running (pid %s), exiting",
                           self.name, read_pid(self.name))
            return 0
        logger.warning("daemon %s: started (pid %d)", self.name, os.getpid())
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        try:
            from agent.events import bus as event_bus  # noqa: PLC0415
            try:
                event_bus.configure(store=get_store())
                event_bus.publish({"event": "daemon.started", "daemon": self.name,
                                   "pid": os.getpid()})
            except Exception:
                logger.exception("daemon %s: could not publish daemon.started",
                                 self.name)
            while not self._stop:
                started = time.monotonic()
                try:
                    self.run_once()
                except Exception:
                    logger.exception("daemon %s: run_once crashed (continuing)",
                                     self.name)
                elapsed = time.monotonic() - started
                wait = self.interval - elapsed
                end = time.monotonic() + max(0.0, wait)
                while not self._stop and time.monotonic() < end:
                    time.sleep(min(1.0, end - time.monotonic()))
        finally:
            release_pidfile(self.name)
            try:
                from agent.events import bus as event_bus  # noqa: PLC0415
                event_bus.configure(store=get_store())
                event_bus.publish({"event": "daemon.stopped", "daemon": self.name,
                                   "reason": "shutdown"})
            except Exception:
                pass
            logger.warning("daemon %s: stopped", self.name)
        return 0


def main(daemon_factory: Callable[[], Daemon], argv=None) -> int:
    """Entry point for ``python3 -m daemon.<name>``. ``--once`` runs a
    single cycle (used by tests and the installer smoke test)."""
    daemon = daemon_factory()
    if argv and "--once" in argv:
        logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")
        daemon.run_once()
        return 0
    return daemon.run_forever()
