"""Notification dispatcher: delivery journal -> channels.

Consumes the durable delivery journal via the event bus's PUBLIC read
API only (``latest_events`` / ``replay_after`` / ``last_event_id``) —
the bus module is owned by another builder and is never modified here —
and routes each event to the channels selected for its severity.

Failure semantics
-----------------
* One channel failing never blocks the others and never breaks the event
  journal: every ``send()`` is wrapped, failures are logged, dispatch
  continues.
* Retries are bounded (default: 1 retry) and apply to NOTIFICATION
  delivery only. This package never touches trade execution — the
  execution gateway lives below the agent layer and is not reachable
  from here — so there is no financial-execution path to retry.
* Each event is processed at most once per dispatcher lifetime: the
  cursor advances on ``event_id`` and a bounded seen-set dedups replays.
  Optional sinks are at-least-once across restarts (the agent channel is
  idempotent by journal presence).

Launch
------
* Foreground loop: ``python3 -m agent.notifications.dispatcher``
* CLI: ``scripts/forex notify --run`` (loop), ``--once`` (one pass),
  ``--status`` (channel health). See docs/NOTIFICATIONS.md for the
  systemd unit a supervisor would install.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

from agent.events import bus  # noqa: E402 (documented read-only dep)
from agent.notifications.channels import (  # noqa: E402
    AgentChannel,
    FCMChannel,
    NotificationChannel,
    TelegramChannel,
    WebhookChannel,
    WorkerChannel,
)

logger = logging.getLogger("forex_agent.notifications")

# Severity -> channel names. CRITICAL reaches every configured channel;
# INFO stays on the agent channel only. Overridable via
# ``notifications.routing`` in config/defaults.yaml.
DEFAULT_ROUTING: Dict[str, Sequence[str]] = {
    "CRITICAL": ("agent", "worker", "fcm", "webhook", "telegram"),
    "WARNING": ("agent", "worker", "telegram"),
    "NOTICE": ("agent", "worker", "telegram"),
    "INFO": ("agent",),
}

_SEEN_CAP = 20000  # bound the dedup set; journal cursor is the real state
_BATCH_LIMIT = 500


def _state_dir() -> str:
    """Directory for the dispatcher's cursor file."""
    home = os.environ.get("FOREX_AGENT_HOME", "")
    if not home:
        home = os.path.join(os.path.expanduser("~"), ".forex-agent")
    path = os.path.join(home, "run")
    os.makedirs(path, exist_ok=True)
    return path


class NotificationDispatcher:
    """Route journaled events to channels by severity."""

    def __init__(self,
                 channels: Optional[Sequence[NotificationChannel]] = None,
                 routing: Optional[Dict[str, Sequence[str]]] = None,
                 poll_interval_s: float = 5.0,
                 max_attempts: int = 2,
                 backoff_s: float = 1.0,
                 start_at_head: bool = True,
                 state_dir: Optional[str] = None):
        self.channels: Dict[str, NotificationChannel] = {
            c.name: c for c in (channels or [AgentChannel()])}
        self.routing: Dict[str, Sequence[str]] = dict(DEFAULT_ROUTING)
        if routing:
            for sev, names in routing.items():
                self.routing[str(sev).upper()] = tuple(names)
        self.poll_interval_s = max(0.5, float(poll_interval_s))
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_s = max(0.0, float(backoff_s))
        self._start_at_head = bool(start_at_head)
        self._state_dir = state_dir or _state_dir()
        self._cursor_file = os.path.join(self._state_dir, "notify.cursor")
        self._cursor: Optional[str] = None
        self._initialized = False
        self._seen: set = set()
        self._stop = False
        self._load_cursor()

    # -- cursor persistence ------------------------------------------------

    def _load_cursor(self) -> None:
        try:
            with open(self._cursor_file, "r", encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                self._cursor = value
                self._initialized = True
                logger.info("notify dispatcher: resumed from cursor %s",
                            value)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("notify dispatcher: cannot read cursor file: %s",
                           exc)

    def _save_cursor(self) -> None:
        if self._cursor is None:
            return
        tmp = self._cursor_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(self._cursor)
            os.replace(tmp, self._cursor_file)
        except OSError as exc:
            logger.warning("notify dispatcher: cannot save cursor: %s", exc)

    @property
    def cursor(self) -> Optional[str]:
        return self._cursor

    def seek(self, event_id: str) -> None:
        """Move the cursor (used by ``forex notify --once --since-id``)."""
        if not event_id:
            raise ValueError("event_id must be a non-empty string")
        self._cursor = event_id
        self._initialized = True

    # -- routing -----------------------------------------------------------

    def channels_for(self, severity: Optional[str]) -> List[NotificationChannel]:
        names = self.routing.get((severity or "INFO").upper(), ("agent",))
        return [self.channels[n] for n in names if n in self.channels]

    def dispatch_event(self, event: dict) -> Dict[str, str]:
        """Route one event to its severity's channels.

        Returns ``{channel_name: outcome}`` where outcome is ``"sent"``,
        ``"skipped: not configured"``, or ``"failed: <reason>"``. A
        failing channel never blocks the others.
        """
        results: Dict[str, str] = {}
        for chl in self.channels_for(event.get("severity")):
            if not chl.is_configured():
                results[chl.name] = "skipped: not configured"
                logger.debug("notify: channel %s not configured; skipping %s",
                             chl.name, event.get("event_id"))
                continue
            results[chl.name] = self._send_with_retry(chl, event)
        return results

    def _send_with_retry(self, chl: NotificationChannel,
                         event: dict) -> str:
        """Bounded retries for NOTIFICATION delivery only.

        The dispatcher never touches trade execution, so no retry here
        can ever re-fire a financial action.
        """
        last_error = "unknown"
        for attempt in range(1, self.max_attempts + 1):
            try:
                chl.send(event)
                return "sent"
            except Exception as exc:  # noqa: BLE001 — isolated by design
                last_error = str(exc)
                logger.warning(
                    "notify: channel %s attempt %d/%d failed for %s: %s",
                    chl.name, attempt, self.max_attempts,
                    event.get("event_id"), exc)
                if attempt < self.max_attempts:
                    time.sleep(self.backoff_s * attempt)
        return "failed: %s" % last_error

    # -- journal consumption -------------------------------------------------

    def _remember(self, event_id: Optional[str]) -> None:
        if event_id:
            self._seen.add(event_id)
            if len(self._seen) > _SEEN_CAP:
                # Drop the arbitrary half; the cursor is the durable state.
                self._seen = set(list(self._seen)[_SEEN_CAP // 2:])

    def run_once(self) -> int:
        """Process every journaled event newer than the cursor.

        Returns the number of events dispatched. Never raises: journal
        read failures are logged and retried on the next pass — the
        journal itself is untouched.
        """
        try:
            events = self._next_batch()
        except (RuntimeError, ValueError) as exc:
            # RuntimeError: journal backend unavailable. ValueError: the
            # cursor is unknown to the journal (rotated) — resume at head
            # rather than silently missing events.
            logger.warning("notify: journal read failed (%s); resuming at head",
                           exc)
            self._cursor = bus.last_event_id()
            self._initialized = True
            self._save_cursor()
            return 0
        except Exception:  # noqa: BLE001 — never break the loop on reads
            logger.exception("notify: unexpected journal read failure")
            return 0

        count = 0
        for event in events:
            event_id = event.get("event_id") if isinstance(event, dict) else None
            if event_id and event_id in self._seen:
                self._cursor = event_id  # still advance past replays
                continue
            try:
                self.dispatch_event(event)
            except Exception:  # noqa: BLE001 — dispatch_event isolates already
                logger.exception("notify: dispatch failed for %s", event_id)
            self._remember(event_id)
            if event_id:
                self._cursor = event_id
            count += 1
        if count:
            self._save_cursor()
        return count

    def _next_batch(self) -> List[dict]:
        if not self._initialized:
            self._initialized = True
            if self._start_at_head:
                # A fresh dispatcher does not replay history: the SSE
                # stream already delivered it. Backfill explicitly with
                # --since-id.
                self._cursor = bus.last_event_id()
                self._save_cursor()
                return []
        if self._cursor is None:
            # Journal was empty at last check; poll from the beginning.
            return bus.latest_events(limit=_BATCH_LIMIT)
        return bus.replay_after(self._cursor, limit=_BATCH_LIMIT)

    def run_forever(self) -> None:
        """Poll the journal until stopped (SIGTERM/SIGINT/Ctrl-C)."""
        while not self._stop:
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — the loop must not die
                logger.exception("notify: loop iteration failed; continuing")
            time.sleep(self.poll_interval_s)

    def stop(self) -> None:
        self._stop = True


# ---------------------------------------------------------------------------
# Wiring: config -> channels -> dispatcher -> entry point
# ---------------------------------------------------------------------------

def build_dispatcher_from_config(cfg=None) -> NotificationDispatcher:
    """Assemble the dispatcher from AppConfig (env + defaults.yaml)."""
    if cfg is None:
        from config.config import load_config  # noqa: PLC0415
        cfg = load_config()
    ncfg = cfg.notifications
    wcfg = cfg.worker
    channels: List[NotificationChannel] = [
        AgentChannel(enabled=ncfg.channel_agent),
        WorkerChannel(enabled=ncfg.channel_worker,
                      base_url=wcfg.base_url, api_key=wcfg.api_key,
                      timeout=ncfg.timeout_seconds),
        FCMChannel(enabled=ncfg.channel_fcm,
                   project_id=ncfg.fcm_project_id,
                   worker_base_url=wcfg.base_url, api_key=wcfg.api_key,
                   push_path=ncfg.fcm_push_path,
                   timeout=ncfg.timeout_seconds),
        WebhookChannel(enabled=ncfg.channel_webhook,
                       url=ncfg.webhook_url,
                       timeout=ncfg.timeout_seconds),
        TelegramChannel(enabled=ncfg.channel_telegram,
                        bot_token=ncfg.telegram_bot_token,
                        chat_id=ncfg.telegram_chat_id,
                        timeout=ncfg.timeout_seconds),
    ]
    return NotificationDispatcher(
        channels=channels,
        routing=ncfg.routing,
        poll_interval_s=ncfg.poll_interval_seconds,
        max_attempts=ncfg.max_attempts,
        backoff_s=ncfg.backoff_seconds,
    )


def run_dispatcher(dispatcher: Optional[NotificationDispatcher] = None) -> int:
    """Foreground dispatcher loop.

    Launched by ``python3 -m agent.notifications.dispatcher`` or
    ``scripts/forex notify --run`` — the entry point a supervisor /
    systemd unit execs. Returns 0 on clean stop.
    """
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    disp = dispatcher or build_dispatcher_from_config()

    def _handle_stop(signum, _frame):
        logger.info("notify dispatcher: received signal %s; stopping", signum)
        disp.stop()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    summary = ", ".join(
        "%s=%s" % (name, "on" if ch.is_configured() else "off")
        for name, ch in disp.channels.items())
    logger.info("notify dispatcher: starting (channels: %s)", summary)
    try:
        disp.run_forever()
    finally:
        disp._save_cursor()
    logger.info("notify dispatcher: stopped")
    return 0


if __name__ == "__main__":
    sys.exit(run_dispatcher())
