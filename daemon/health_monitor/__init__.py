"""health_monitor — the local safety watchdog.

Runs every ``kill_switch.check_interval_seconds`` (default 10s) and:

  1. KILL-SWITCH POLL (fail-closed): read the latch. If the read itself
     fails, ENGAGE the kill switch — an unreadable latch is treated as
     engaged. This is the single most important thing this daemon does.
  2. Cloud mirror check (best-effort): compare with the Worker's
     /kill-switch; a mismatch is logged as a warning — local wins,
     always. Worker outage never affects local safety.
  3. Broker health: adapter.broker_status() -> broker.connected /
     broker.disconnected event on change (schema-valid payloads).
  4. Sibling daemons: market/signal/position monitor PIDs alive?
  5. Disk space: warn below 500 MB free in FOREX_AGENT_HOME.
  6. EVENT-QUEUE DRAIN: dequeue_events() -> worker_client.sync_events().
     Persistent Worker failure (WORKER_UNAVAILABLE after retries) lands
     the batch in the local journal as kind="worker_dead_letter" —
     nothing is silently dropped.
  7. PERFORMANCE SYNC every ``performance_review.interval_hours``
     (default 6h): compute_performance() -> journal kind=
     "performance_snapshot" + POST /performance/snapshot (best-effort).

Emits ``health.check`` on state change plus a 5-minute heartbeat.
"""

from __future__ import annotations

import logging
import shutil
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from daemon.common import (Daemon, get_adapter, get_daemon_state, get_store,
                           load_config, main, read_pid, save_daemon_state)
from daemon.worker_client import WorkerClient, WorkerUnavailable, from_config
from agent.events import bus as event_bus

logger = logging.getLogger("forex_agent.daemon.health_monitor")

SIBLING_DAEMONS = ("market_monitor", "signal_monitor", "position_monitor")
HEARTBEAT_SECONDS = 300
DISK_WARN_BYTES = 500 * 1024 * 1024


class HealthMonitor(Daemon):
    name = "health_monitor"
    interval = 10.0

    def __init__(self, interval: Optional[float] = None,
                 worker: Optional[WorkerClient] = None):
        super().__init__(interval)
        self._worker_override = worker

    # -- helpers -----------------------------------------------------------
    def _kill_switch(self):
        from agent.tools import backend  # noqa: PLC0415
        gateway = backend.gateway()
        return getattr(gateway, "kill_switch", None)

    def _worker(self, config) -> WorkerClient:
        if self._worker_override is not None:
            return self._worker_override
        return from_config(config)

    # -- one full cycle -----------------------------------------------------
    def run_once(self) -> None:
        config = load_config()
        store = get_store()
        event_bus.configure(store=store)
        state = get_daemon_state(store, self.name)
        now = time.time()
        status: dict = {"ok": True, "checks": {}}

        self._check_kill_switch(config, store, state, status)
        self._check_broker(config, state, status)
        self._check_siblings(state, status)
        self._check_disk(state, status)
        self._drain_events(config, store, state, status)
        self._maybe_sync_performance(config, store, state, status, now)

        status_changed = status != state.get("last_status")
        heartbeat_due = now - float(state.get("last_heartbeat", 0)) >= HEARTBEAT_SECONDS
        if status_changed or heartbeat_due:
            event_bus.publish({"event": "health.check", "ok": status["ok"],
                  "checks": status["checks"], "daemon": self.name})
            state["last_heartbeat"] = now
        state["last_status"] = status
        save_daemon_state(store, self.name, state)

    # -- individual checks ---------------------------------------------------
    def _check_kill_switch(self, config, store, state, status) -> None:
        checks = status["checks"]
        try:
            ks = self._kill_switch()
            if ks is None:
                raise RuntimeError("gateway exposes no kill_switch controller")
            engaged = bool(ks.is_engaged())
            checks["kill_switch"] = {"engaged": engaged, "read_ok": True}
        except Exception as exc:
            # FAIL-CLOSED: an unreadable latch is treated as engaged.
            logger.critical("kill-switch latch unreadable (%s) — engaging fail-closed", exc)
            try:
                ks = self._kill_switch()
                if ks is not None:
                    ks.engage(source="health_monitor:fail-closed")
            except Exception:
                logger.exception("fail-closed engage also failed")
            checks["kill_switch"] = {"engaged": True, "read_ok": False,
                                     "error": str(exc)[:200]}
            status["ok"] = False

        # Cloud mirror is informational only; a Worker outage or a
        # mismatch never changes the local latch.
        try:
            worker = self._worker(config)
            if worker.enabled:
                cloud = worker.get_kill_switch()
                cloud_engaged = bool(cloud.get("engaged"))
                checks["kill_switch_cloud"] = {"engaged": cloud_engaged}
                if cloud_engaged != checks["kill_switch"]["engaged"]:
                    logger.warning("kill-switch mismatch: local=%s cloud=%s "
                                   "(local is authoritative)",
                                   checks["kill_switch"]["engaged"], cloud_engaged)
        except WorkerUnavailable as exc:
            logger.warning("WORKER_UNAVAILABLE: kill-switch mirror check skipped: %s",
                           exc.reason)
            checks["kill_switch_cloud"] = {"unavailable": True}

    def _check_broker(self, config, state, status) -> None:
        checks = status["checks"]
        # The structured broker_status() API is the honest health source:
        # {"broker": {"provider", "configured", "reachable", "connected",
        #             "account_available", ..., "detail"}}. It never raises.
        try:
            from agent.tools import backend  # noqa: PLC0415
            adapter = backend.broker_adapter()
            blob = adapter.broker_status()
            broker = blob.get("broker") if isinstance(blob, dict) else None
            broker = broker if isinstance(broker, dict) else {}
            connected = bool(broker.get("connected"))
            adapter_name = str(broker.get("provider")
                               or getattr(adapter, "adapter_name", "unknown"))
            detail = broker.get("detail")
            detail = detail if isinstance(detail, dict) else {}
        except Exception as exc:
            connected, adapter_name = False, "unknown"
            detail = {"reason": str(exc)[:200]}
        checks["broker"] = {"connected": connected, "adapter": adapter_name}
        prev = state.get("broker_connected")
        if prev is not None and bool(prev) != connected:
            # Bus schema: both events REQUIRE "adapter"; "broker.connected"
            # takes no extra fields beyond account/server, while
            # "broker.disconnected" accepts an optional "error".
            if connected:
                event_bus.publish({"event": "broker.connected",
                                   "adapter": adapter_name})
            else:
                event_bus.publish({
                    "event": "broker.disconnected",
                    "adapter": adapter_name,
                    "error": (detail.get("reason") or detail.get("probe_error")
                              or "broker unreachable"),
                })
            logger.warning("broker connection changed: %s -> %s", prev, connected)
        state["broker_connected"] = connected
        if not connected:
            status["ok"] = False

    def _check_siblings(self, state, status) -> None:
        checks = status["checks"]
        missing = [name for name in SIBLING_DAEMONS if read_pid(name) is None]
        checks["daemons"] = {"running": [n for n in SIBLING_DAEMONS if n not in missing],
                             "missing": missing}
        if missing:
            logger.warning("sibling daemons not running: %s", missing)
            status["ok"] = False

    def _check_disk(self, state, status) -> None:
        from daemon.common import get_home  # noqa: PLC0415
        checks = status["checks"]
        try:
            free = shutil.disk_usage(get_home()).free
        except OSError:
            free = -1
        checks["disk"] = {"free_bytes": free}
        if 0 <= free < DISK_WARN_BYTES:
            logger.warning("low disk space in agent home: %.1f MB free",
                           free / 1024 / 1024)
            status["ok"] = False

    # -- worker sync ----------------------------------------------------------
    def _drain_events(self, config, store, state, status) -> None:
        checks = status["checks"]
        worker = self._worker(config)
        if not worker.enabled:
            checks["worker_sync"] = {"enabled": False}
            return
        try:
            batch = store.dequeue_events(limit=200)
        except Exception as exc:
            logger.exception("event dequeue failed")
            checks["worker_sync"] = {"error": str(exc)[:200]}
            return
        if not batch:
            checks["worker_sync"] = {"drained": 0}
            return
        try:
            result = worker.sync_events(batch)
            checks["worker_sync"] = {"drained": len(batch), **result}
            logger.info("synced %d events to Worker (%d skipped: no endpoint)",
                        result["sent"], result["skipped"])
        except WorkerUnavailable as exc:
            # Dead-letter locally instead of dropping the batch.
            try:
                store.journal_add({"kind": "worker_dead_letter",
                                   "detail": {"events": batch, "error": exc.reason,
                                              "ts": datetime.now(timezone.utc).isoformat()}})
            except Exception:
                logger.exception("could not dead-letter %d events", len(batch))
            logger.warning("WORKER_UNAVAILABLE: dead-lettered %d events: %s",
                           len(batch), exc.reason)
            checks["worker_sync"] = {"drained": len(batch), "dead_lettered": True}

    def _maybe_sync_performance(self, config, store, state, status, now) -> None:
        checks = status["checks"]
        review = getattr(config, "performance_review", None)
        interval_h = float(getattr(review, "interval_hours", 6.0) or 6.0)
        lookback_d = int(getattr(review, "lookback_days", 30) or 30)
        last = float(state.get("last_perf_sync", 0))
        if now - last < interval_h * 3600:
            return
        try:
            from core.performance import compute_performance, snapshot_to_dict  # noqa: PLC0415
            from agent.tools import backend  # noqa: PLC0415
            adapter = backend.broker_adapter()
            perf = compute_performance(adapter, lookback_days=lookback_d)
            snapshot = snapshot_to_dict(perf)
            store.journal_add({"kind": "performance_snapshot",
                               "detail": snapshot})
            state["last_perf_sync"] = now  # journal is the durable record;
            save_daemon_state(store, self.name, state)  # persist BEFORE notify
            worker = self._worker(config)
            if worker.enabled:
                try:
                    worker.post_performance_snapshot(snapshot)
                except WorkerUnavailable as exc:
                    logger.warning("WORKER_UNAVAILABLE: perf snapshot kept local: %s",
                                   exc.reason)
            try:
                event_bus.publish({"event": "performance.snapshot",
                                   "period_days": lookback_d,
                                   "snapshot": snapshot, "daemon": self.name})
            except Exception:
                logger.exception("performance.snapshot event publish failed")
            logger.warning("performance snapshot synced (local journal + Worker)")
            checks["performance_sync"] = {"ok": True}
        except Exception as exc:
            logger.exception("performance sync failed")
            checks["performance_sync"] = {"ok": False, "error": str(exc)[:200]}


def main_entry(argv=None):
    return main(lambda: HealthMonitor(), argv)


if __name__ == "__main__":
    sys.exit(main_entry(sys.argv[1:]))
