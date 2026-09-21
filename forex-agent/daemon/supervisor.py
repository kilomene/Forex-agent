"""supervisor — lifecycle library behind ``scripts/forex-daemons``.

The shell script owns process management (PID files, locks, start/stop);
this module owns *state*:

  * ``daemon_state(name)`` — per-daemon running/stopped/stale report.
  * ``collect_health()`` — aggregate: daemon liveness + kill-switch latch
    + broker_status + event-journal tail + disk. Backs
    ``forex-daemons health``.
  * ``restart_safety_sequence()`` — the ordered post-restart sequence:
    load local state -> broker probe (honest) -> reconcile positions ->
    verify event state (journal intact, SSE resume works) ->
    verify monitoring/notifications/MCP config -> prune event journal ->
    report health. Backs ``forex-daemons safety`` and runs automatically
    at the end of ``forex-daemons restart``.
  * ``prune_event_journal()`` — bounded, config-driven event-journal
    pruning. Backs ``forex-daemons prune`` and the systemd prune timer.

Safety invariants enforced here:

  * Never repeats a trade: the sequence never submits, modifies, or
    closes anything. Reconciliation only *reports* externally-closed
    positions (``core.reconciliation`` never trades).
  * Kill-switch state is read, never written, by the safety sequence.
  * Every probe is honest: an unavailable broker is reported as
    ``connected: false``, never faked.
  * Daemons are independent of the agent process: they run detached
    (setsid/nohup via the supervisor script, or systemd), so an agent
    restart is NOT a forex restart. This sequence is for forex restarts
    only (host reboot, ``forex-daemons restart``, manual stop/start).

CLI: ``python3 -m daemon.supervisor {health|safety|prune} [--json]``
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from daemon.common import get_daemon_state, get_home, load_config, read_pid, run_dir

logger = logging.getLogger("forex_agent.daemon.supervisor")

DAEMONS = ("market_monitor", "signal_monitor", "position_monitor", "health_monitor")

_EVENT_LOG_SCAN_LIMIT = 5000


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_store():
    from storage import Store  # noqa: PLC0415
    return Store()


# ---------------------------------------------------------------------------
# Per-daemon state
# ---------------------------------------------------------------------------
def daemon_state(name: str) -> Dict[str, Any]:
    """Running pid / stopped / stale-pidfile report for one daemon."""
    pid = read_pid(name)
    pidfile = os.path.join(run_dir(), name + ".pid")
    if pid is not None:
        return {"name": name, "state": "running", "pid": pid,
                "stale_pidfile": False}
    return {"name": name, "state": "stopped", "pid": None,
            "stale_pidfile": os.path.exists(pidfile)}


def all_daemon_states() -> List[Dict[str, Any]]:
    return [daemon_state(n) for n in DAEMONS]


# ---------------------------------------------------------------------------
# Broker probe (honest; never raises)
# ---------------------------------------------------------------------------
def probe_broker() -> Dict[str, Any]:
    """Structured broker status via adapter.broker_status().

    Never raises: failures are reported as connected=false with the error
    in ``detail``. Never fakes a connection.
    """
    try:
        from agent.tools import backend  # noqa: PLC0415
        adapter = backend.broker_adapter()
        blob = adapter.broker_status()
        broker = blob.get("broker") if isinstance(blob, dict) else None
        broker = broker if isinstance(broker, dict) else {}
        return {
            "connected": bool(broker.get("connected")),
            "provider": str(broker.get("provider")
                            or getattr(adapter, "adapter_name", "unknown")),
            "configured": bool(broker.get("configured")),
            "reachable": bool(broker.get("reachable")),
            "account_available": bool(broker.get("account_available")),
            "market_data_available": bool(broker.get("market_data_available")),
            "trading_available": bool(broker.get("trading_available")),
            "detail": broker.get("detail") if isinstance(
                broker.get("detail"), dict) else {},
        }
    except Exception as exc:  # defensive: status must never raise
        logger.exception("broker probe failed")
        return {"connected": False, "provider": "unknown",
                "configured": False, "reachable": False,
                "account_available": False, "market_data_available": False,
                "trading_available": False,
                "detail": {"probe_error": str(exc)[:200]}}


# ---------------------------------------------------------------------------
# Aggregate health
# ---------------------------------------------------------------------------
def collect_health(config=None, store=None) -> Dict[str, Any]:
    """Aggregate health: daemons + kill-switch latch + broker + journal tail.

    ``ok`` is True only when every daemon is running, the latch is
    readable, and the broker reports connected. A disconnected broker is
    reported honestly as degraded, not as healthy.
    """
    if config is None:
        config = load_config()
    if store is None:
        store = _get_store()

    daemons = all_daemon_states()
    daemons_ok = all(d["state"] == "running" for d in daemons)

    try:
        latch = dict(store.get_kill_switch())
        latch_ok = True
    except Exception as exc:
        latch = {"engaged": True, "source": None, "ts": None,
                 "read_error": str(exc)[:200]}
        latch_ok = False  # fail-closed: unreadable latch = engaged

    broker = probe_broker()

    try:
        tail = store.event_journal_latest(5)
        journal = {"ok": True, "last_event_id": store.event_journal_last_id(),
                   "recent": [{"event_id": e.get("event_id"),
                               "event": e.get("event"),
                               "severity": e.get("severity"),
                               "ts": e.get("ts")} for e in tail]}
    except Exception as exc:
        journal = {"ok": False, "error": str(exc)[:200]}

    try:
        disk_free = shutil.disk_usage(get_home()).free
    except OSError:
        disk_free = -1

    ok = daemons_ok and latch_ok and bool(broker.get("connected"))
    return {
        "ts": _utcnow(),
        "ok": ok,
        "degraded": (daemons_ok and latch_ok and not broker.get("connected")),
        "daemons": daemons,
        "kill_switch": latch,
        "kill_switch_readable": latch_ok,
        "broker": broker,
        "event_journal": journal,
        "disk_free_bytes": disk_free,
    }


# ---------------------------------------------------------------------------
# Event-journal pruning (bounded, config-driven)
# ---------------------------------------------------------------------------
def prune_event_journal(store=None, config=None) -> Dict[str, Any]:
    """Prune the event delivery journal per config.

    Defaults: keep 30 days / 100k events (events.journal_retention_days,
    events.journal_max_events; env EVENTS_JOURNAL_RETENTION_DAYS /
    EVENTS_JOURNAL_MAX_EVENTS). Non-positive disables that bound.
    """
    if config is None:
        config = load_config()
    if store is None:
        store = _get_store()
    events_cfg = getattr(config, "events", None)
    retention = int(getattr(events_cfg, "journal_retention_days", 30) or 0)
    max_events = int(getattr(events_cfg, "journal_max_events", 100000) or 0)
    result = store.event_journal_prune(retention_days=retention,
                                       max_events=max_events)
    logger.warning("event journal pruned: %s (retention_days=%s max_events=%s)",
                   result, retention, max_events)
    return {"ts": _utcnow(), "retention_days": retention,
            "max_events": max_events, **result}


# ---------------------------------------------------------------------------
# Restart safety sequence
# ---------------------------------------------------------------------------
def _believed_open_signal_ids(store) -> Set[str]:
    """Signal IDs the bot believes are still open.

    Derived from the pollable event log: trade.executed signal_ids minus
    signals closed via position.closed (ticket -> signal from the
    trade.executed ticket map) or position.external_close.
    """
    ticket_to_signal: Dict[Any, str] = {}
    executed: Set[str] = set()
    closed: Set[str] = set()
    try:
        entries = store.journal_query(kind="event",
                                      limit=_EVENT_LOG_SCAN_LIMIT)
    except Exception:
        logger.exception("could not scan event log for open signals")
        return set()
    for entry in entries:
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        etype = payload.get("event")
        if etype == "trade.executed":
            sid = payload.get("signal_id")
            ticket = payload.get("ticket")
            if sid:
                executed.add(str(sid))
                if ticket is not None:
                    ticket_to_signal[ticket] = str(sid)
        elif etype == "position.closed":
            sid = ticket_to_signal.get(payload.get("ticket"))
            if sid:
                closed.add(sid)
        elif etype == "position.external_close":
            note = str(payload.get("note") or "")
            if note.startswith("reconciled_external_close:"):
                closed.add(note.split(":", 1)[1])
    return executed - closed


def restart_safety_sequence(config=None, store=None) -> Dict[str, Any]:
    """Run the ordered post-restart safety sequence and report.

    Steps:
      1. load_local_state — config, kill-switch latch, persisted daemon
         states, idempotency-store readability. Never repeats a trade:
         the idempotency keys are only *read* here.
      2. broker — broker_status() probe; honest ``connected`` flag.
      3. reconcile — core.reconciliation over believed-open signals, but
         only when the broker is connected; skipped honestly otherwise.
         Reconciliation never trades — it only reports externally-closed
         positions.
      4. event_state — journal intact; SSE ``resume_from`` works across
         the restart (event_journal_after(last_id) is empty, last_id is
         stable and readable).
      5. monitoring — persisted daemon states + notifications/Worker
         config intact (daemons re-read config every cycle; nothing to
         push).
      6. prune — bounded event-journal pruning.
      7. health — aggregate health report.

    ``ok`` is True when every step that *must* succeed did. A
    disconnected broker is reported honestly in the broker step and
    degrades (not fails) the sequence — the local safety posture
    (kill switch, idempotency, journal) is what must be intact.
    """
    if config is None:
        config = load_config()
    if store is None:
        store = _get_store()
    report: Dict[str, Any] = {"ts": _utcnow(), "steps": [], "ok": True}

    def step(name: str, ok: bool, detail: Optional[dict] = None,
             critical: bool = True) -> None:
        report["steps"].append({"step": name, "ok": bool(ok),
                                "critical": critical,
                                "detail": detail or {}})
        if critical and not ok:
            report["ok"] = False

    # -- 1. local state -----------------------------------------------------
    try:
        latch = dict(store.get_kill_switch())
        latch_ok, latch_err = True, None
    except Exception as exc:
        latch = {"engaged": True, "source": None, "ts": None}
        latch_ok, latch_err = False, str(exc)[:200]
    try:
        # Idempotency store readable (read-only probe; keys are never
        # rewritten by this sequence).
        store.idem_get("__supervisor_probe__")
        idem_ok, idem_err = True, None
    except Exception as exc:
        idem_ok, idem_err = False, str(exc)[:200]
    daemon_saved = {n: get_daemon_state(store, n) for n in DAEMONS}
    step("load_local_state", latch_ok and idem_ok, {
        "kill_switch": latch, "kill_switch_readable": latch_ok,
        "kill_switch_error": latch_err,
        "idempotency_store_readable": idem_ok,
        "idempotency_error": idem_err,
        "daemon_saved_states": {n: bool(s) for n, s in
                                daemon_saved.items()},
    })

    # -- 2. broker (honest) --------------------------------------------------
    broker = probe_broker()
    step("broker", True, {"status": broker,
                          "connected": broker["connected"]},
         critical=False)  # honest report; disconnected degrades, not fails

    # -- 3. reconcile positions ----------------------------------------------
    if broker["connected"]:
        try:
            from core.reconciliation import reconcile  # noqa: PLC0415
            from agent.tools import backend  # noqa: PLC0415
            adapter = backend.broker_adapter()
            believed = _believed_open_signal_ids(store)
            reconciled_notes: List[dict] = []

            def on_close(signal_id: str, reason: str, price: float,
                         costs) -> None:
                reconciled_notes.append({"signal_id": signal_id,
                                         "reason": reason,
                                         "price": price})

            count = reconcile(adapter, believed, on_close, lookback_days=14)
            step("reconcile", True, {
                "believed_open": sorted(believed),
                "reconciled": count,
                "notes": reconciled_notes,
            })
        except Exception as exc:
            logger.exception("reconciliation failed")
            step("reconcile", False, {"error": str(exc)[:200]})
    else:
        step("reconcile", True, {
            "skipped": True,
            "reason": "broker not connected — nothing to reconcile against",
        }, critical=False)

    # -- 4. event state -------------------------------------------------------
    try:
        last_id = store.event_journal_last_id()
        # SSE resume works iff the anchor is readable and the "after"
        # query is stable (nothing newer than the tip).
        after = store.event_journal_after(last_id, 1) if last_id else []
        tail = store.event_journal_latest(3)
        step("event_state", True, {
            "journal_intact": True,
            "last_event_id": last_id,
            "resume_from_tip_stable": after == [],
            "tail_events": len(tail),
        })
    except Exception as exc:
        logger.exception("event journal check failed")
        step("event_state", False, {"error": str(exc)[:200]})

    # -- 5. monitoring / notifications / MCP -----------------------------------
    notifications = getattr(config, "notifications", None)
    worker = getattr(config, "worker", None)
    try:
        from agent.events import bus as event_bus  # noqa: PLC0415
        event_bus.configure(store=store)
        bus_ok = True
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("event bus configure failed")
        bus_ok = False
    step("monitoring", bus_ok, {
        "notifications_enabled": bool(getattr(notifications, "enabled",
                                              False)),
        "worker_enabled": bool(getattr(worker, "enabled", False)),
        "event_bus_configured": bus_ok,
        "note": "daemons re-read config/state every cycle; no push restore needed",
    })

    # -- 6. prune --------------------------------------------------------------
    try:
        pruned = prune_event_journal(store=store, config=config)
        step("prune", True, pruned, critical=False)
    except Exception as exc:
        logger.exception("event journal pruning failed")
        step("prune", False, {"error": str(exc)[:200]}, critical=False)

    # -- 7. health ---------------------------------------------------------------
    try:
        health = collect_health(config=config, store=store)
        step("health", True, {
            "ok": health["ok"],
            "degraded": health["degraded"],
            "daemons": [{k: d[k] for k in ("name", "state", "pid")}
                        for d in health["daemons"]],
            "broker_connected": health["broker"]["connected"],
        }, critical=False)
        report["health"] = health
    except Exception as exc:
        logger.exception("health collection failed")
        step("health", False, {"error": str(exc)[:200]}, critical=False)

    logger.warning("restart safety sequence complete: ok=%s", report["ok"])
    return report


# ---------------------------------------------------------------------------
# CLI: python3 -m daemon.supervisor {health|safety|prune} [--json]
# ---------------------------------------------------------------------------
def _print(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2, default=str))
        return
    if "steps" in report:  # safety report
        print("restart safety sequence: %s"
              % ("OK" if report["ok"] else "FAILED"))
        for s in report["steps"]:
            mark = "ok" if s["ok"] else ("SKIP" if not s["critical"] else "FAIL")
            print("  [%s] %s" % (mark, s["step"]))
            detail = s.get("detail") or {}
            for key in ("kill_switch", "connected", "reconciled",
                        "last_event_id", "pruned", "remaining",
                        "broker_connected", "skipped", "error"):
                if key in detail:
                    print("        %s: %s" % (key, detail[key]))
    elif "daemons" in report:  # health report
        print("health: %s" % ("OK" if report["ok"]
                              else ("DEGRADED" if report.get("degraded")
                                    else "NOT OK")))
        for d in report["daemons"]:
            print("  %s: %s%s" % (d["name"], d["state"],
                                  (" (pid %s)" % d["pid"])
                                  if d["pid"] else ""))
        ks = report.get("kill_switch", {})
        print("  kill_switch: engaged=%s readable=%s"
              % (ks.get("engaged"), report.get("kill_switch_readable")))
        print("  broker: connected=%s provider=%s"
              % (report["broker"]["connected"],
                 report["broker"]["provider"]))
        print("  event_journal: %s" % (report["event_journal"],))
    else:  # prune report
        print("pruned %(pruned)s events, %(remaining)s remain "
              "(retention %(retention_days)sd / max %(max_events)s)"
              % report)


def main_entry(argv=None) -> int:
    argv = list(argv or [])
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    cmd = argv[0] if argv else "health"
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        if cmd == "health":
            _print(collect_health(), as_json)
            return 0
        if cmd == "safety":
            report = restart_safety_sequence()
            _print(report, as_json)
            return 0 if report["ok"] else 1
        if cmd == "prune":
            _print(prune_event_journal(), as_json)
            return 0
        print("usage: python3 -m daemon.supervisor {health|safety|prune} [--json]",
              file=sys.stderr)
        return 2
    except Exception as exc:
        logger.exception("supervisor %s failed", cmd)
        print("supervisor %s failed: %s" % (cmd, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main_entry(sys.argv[1:]))
