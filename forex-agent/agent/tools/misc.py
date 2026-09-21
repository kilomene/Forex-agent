"""Session, calendar, experience, and health capabilities.

- forex.get_session: port of the Worker get_session_info tool, served by
  intelligence.correlation (the real knowledge.js port) — pure.
- forex.get_calendar: port of get_economic_calendar - delegates to
  intelligence.economic_calendar; honest available:false until a
  CalendarProvider is configured (never invents events).
- forex.get_experience: port of get_past_reflections - local journal via
  intelligence.experience (kind="reflection" entries).
- forex.get_health: structured subsystem health for agents and scripts.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import urllib.request
from datetime import datetime
from typing import Any, Dict

from . import backend

logger = logging.getLogger("forex_agent.tools.misc")

# Contract for an external calendar source (matches the original
# get_economic_calendar tool contract):
#   GET {NEWS_CALENDAR_URL}?symbol=<symbol>&hours_ahead=<n>
#   Headers: Authorization: Bearer <NEWS_CALENDAR_API_KEY>  (if set)
#   Response: {"events": [{"time": "...", "currency": "...",
#                          "impact": "high"|"medium"|"low", "title": "..."}]}


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if dataclasses.is_dataclass(value):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def forex_get_session() -> dict:
    """Which forex sessions are active now and the liquidity implication."""
    try:
        info = backend.correlation().current_session_info()
    except RuntimeError as exc:
        return backend.dep_unavailable("intelligence.correlation", exc)
    if not isinstance(info, dict):
        return backend.err("DEPENDENCY_UNAVAILABLE",
                           "session info returned an unexpected shape")
    return {"ok": True, **info}


def _calendar_provider():
    """Optional CalendarProvider injection (tests / future wiring).
    None (default) -> get_calendar() honestly reports available:false."""
    return backend._overrides.get("calendar_provider")


def forex_get_calendar(symbol: str | None = None, hours_ahead: int = 24) -> dict:
    """Upcoming high-impact economic events for a symbol's currencies.

    Delegates to intelligence.economic_calendar (the real port of the
    Worker get_economic_calendar tool). With no CalendarProvider
    configured it returns available:false honestly — never invents events.
    """
    try:
        hours_ahead = max(1, min(int(hours_ahead), 24 * 14))
    except (TypeError, ValueError):
        return backend.err("INVALID_ARGUMENT", "hours_ahead must be an integer")
    try:
        from intelligence.economic_calendar.provider import (  # noqa: PLC0415
            get_calendar,
        )
    except ImportError as exc:
        return backend.dep_unavailable("intelligence.economic_calendar", exc)
    try:
        result = get_calendar(symbol=symbol, hours_ahead=hours_ahead,
                              provider=_calendar_provider())
    except Exception as exc:
        return backend.err("DEPENDENCY_UNAVAILABLE",
                           "Calendar lookup failed: %s" % exc)
    if not isinstance(result, dict):
        return backend.err("DEPENDENCY_UNAVAILABLE",
                           "calendar provider returned an unexpected shape")
    out = {"ok": True, "available": bool(result.get("available"))}
    out.update({k: v for k, v in result.items() if k != "available"})
    if not out["available"] and "note" not in out and "reason" not in out:
        out["note"] = ("No economic calendar provider configured - events are "
                       "unavailable, not invented.")
    return out


def forex_get_experience(symbol: str | None = None, limit: int = 5) -> dict:
    """Past self-reflections on a symbol: what was reasoned beforehand vs
    what actually happened, including documented mistakes.

    Port of the Worker get_past_reflections tool. Reads kind="reflection"
    entries from the local journal via intelligence.experience; the Worker
    cloud reflections table remains the long-term archive.
    """
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        return backend.err("INVALID_ARGUMENT", "limit must be an integer")
    try:
        exp = backend.experience()
    except RuntimeError as exc:
        return backend.dep_unavailable("intelligence.experience", exc)
    try:
        entries = exp.query(kind="reflection", symbol=symbol, limit=limit)
    except Exception as exc:
        return backend.err("DEPENDENCY_UNAVAILABLE",
                           "Could not read reflections: %s" % exc)
    reflections = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        reflections.append({
            "symbol": entry.get("symbol"),
            "ts": entry.get("_ts") or entry.get("ts"),
            "body": entry.get("detail", {}),
        })
    return {"ok": True, "available": True, "count": len(reflections),
            "reflections": reflections}


def _home_dir() -> str:
    return os.environ.get("FOREX_AGENT_HOME", "")


def forex_get_health() -> dict:
    """Structured health of every subsystem component.

    Never raises: each component reports pass/degraded/fail independently.
    Worker reachability is best-effort and NEVER affects local safety.
    """
    components: Dict[str, Dict[str, Any]] = {}

    # Broker ------------------------------------------------------------
    try:
        adapter = backend.broker_adapter()
        try:
            health = adapter.health()
            h = _jsonable(health)
            connected = bool(h.get("connected")) if isinstance(h, dict) else False
            components["broker"] = {
                "status": "pass" if connected else "fail",
                "detail": h, "adapter": type(adapter).__name__,
            }
        except Exception as exc:
            components["broker"] = {"status": "fail",
                                    "detail": backend.broker_error_result(
                                        exc, "broker health check")["message"],
                                    "error_code": getattr(exc, "code", "BROKER_UNAVAILABLE")}
    except RuntimeError as exc:
        components["broker"] = {"status": "fail", "detail": str(exc),
                                "error_code": "DEPENDENCY_UNAVAILABLE"}

    # Kill switch latch (local storage, authoritative) --------------------
    try:
        ks = backend.store().get_kill_switch()
        engaged = bool(ks.get("engaged")) if isinstance(ks, dict) else False
        components["kill_switch"] = {
            "status": "degraded" if engaged else "pass",
            "detail": {"engaged": engaged, "source": ks.get("source")},
        }
    except Exception as exc:
        components["kill_switch"] = {"status": "fail", "detail": str(exc),
                                     "error_code": "DEPENDENCY_UNAVAILABLE"}

    # Event bus / storage --------------------------------------------------
    try:
        backend.store()
        components["event_bus"] = {"status": "pass", "detail": "storage reachable"}
    except RuntimeError as exc:
        components["event_bus"] = {"status": "fail", "detail": str(exc),
                                   "error_code": "DEPENDENCY_UNAVAILABLE"}

    # Worker reachability (best-effort; NEVER gates local safety) ----------
    worker_url, api_key, worker_enabled = "", "", False
    try:
        cfg = backend.app_config()
        worker_url = getattr(getattr(cfg, "worker", None), "base_url", "") or ""
        api_key = getattr(getattr(cfg, "worker", None), "api_key", "") or ""
        worker_enabled = bool(getattr(getattr(cfg, "worker", None), "enabled", False))
    except RuntimeError:
        pass
    worker_url = os.environ.get("WORKER_URL", "") or worker_url
    api_key = os.environ.get("WORKER_API_KEY", "") or api_key
    if worker_url and worker_enabled:
        try:
            req = urllib.request.Request(worker_url.rstrip("/") + "/kill-switch")
            if api_key:
                req.add_header("Authorization", "Bearer %s" % api_key)
            with urllib.request.urlopen(req, timeout=5) as resp:
                components["worker"] = {
                    "status": "pass" if resp.status < 300 else "degraded",
                    "detail": {"url": worker_url, "http_status": resp.status},
                }
        except Exception as exc:
            components["worker"] = {"status": "degraded",
                                    "detail": "unreachable: %s" % exc,
                                    "error_code": "WORKER_UNAVAILABLE",
                                    "note": "Local safety (risk, kill switch, gateway) "
                                            "is unaffected by Worker outage."}
    else:
        components["worker"] = {"status": "degraded",
                                "detail": "Worker sync disabled or unconfigured - "
                                          "cloud sync off, local safety unaffected"}

    # Daemons (PID files) --------------------------------------------------
    daemons = {}
    home = _home_dir()
    run_dir = os.path.join(home, "run") if home else ""
    for name in ("market_monitor", "signal_monitor", "position_monitor", "health_monitor"):
        pid_file = os.path.join(run_dir, name + ".pid") if run_dir else ""
        pid, alive = None, False
        if pid_file and os.path.exists(pid_file):
            try:
                pid = int(open(pid_file).read().strip())
                os.kill(pid, 0)
                alive = True
            except Exception:
                alive = False
        daemons[name] = {"running": alive, "pid": pid}
    components["daemons"] = {
        "status": "pass" if all(d["running"] for d in daemons.values()) else "degraded",
        "detail": daemons,
    }

    overall = "healthy"
    if any(c["status"] == "fail" for c in components.values()):
        overall = "unhealthy"
    elif any(c["status"] == "degraded" for c in components.values()):
        overall = "degraded"
    return {"ok": True, "status": overall, "components": components}
