"""Defensive Cloudflare Worker client (cloud/sync layer only).

The Worker is NEVER on the local safety path: every method here raises
WorkerUnavailable on any transport/contract failure, callers log it as
WORKER_UNAVAILABLE and continue with local state. Nothing in this module
may raise anything else out of a public method.

Worker contract (from forex-signal-worker/src/index.js + src/auth.js):
  auth:  Authorization: Bearer <WORKER_API_KEY>
  GET  /kill-switch            -> {"engaged": bool, "close_all_requested_at": ...}
  POST /kill-switch            {engaged: bool} -> 2xx JSON
  POST /signals                {symbol,timeframe,direction,entry_price,
                                stop_loss,take_profit,candle_time,...}
                               -> 2xx JSON (missing field -> 400)
  POST /performance/snapshot  {...} -> {"ok": true, "id": ...} (201)
  GET  /performance/latest     -> {"available": bool, "snapshot"?: ...}

Retry policy: up to 3 attempts with exponential backoff (1s, 2s) per
call; a persistent failure surfaces as WorkerUnavailable and the caller
(e.g. health_monitor) dead-letters the batch to the local journal instead
of losing it.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

logger = logging.getLogger("forex_agent.daemon.worker_client")

WORKER_UNAVAILABLE = "WORKER_UNAVAILABLE"


class WorkerUnavailable(Exception):
    """The Worker could not be reached OR its response failed contract
    validation. Local safety is unaffected — log and continue."""

    def __init__(self, operation: str, reason: str):
        super().__init__("%s: %s" % (operation, reason))
        self.operation = operation
        self.reason = reason
        self.code = WORKER_UNAVAILABLE


def _require_shape(data: Any, shape: Dict[str, type], operation: str) -> dict:
    """Validate a Worker response body; raise WorkerUnavailable on mismatch."""
    if not isinstance(data, dict):
        raise WorkerUnavailable(operation, "contract: expected JSON object, got %s"
                                % type(data).__name__)
    for field, ftype in shape.items():
        if field not in data:
            raise WorkerUnavailable(operation, "contract: missing field %r" % field)
        if not isinstance(data[field], ftype):
            raise WorkerUnavailable(
                operation, "contract: field %r expected %s, got %s"
                % (field, ftype.__name__, type(data[field]).__name__))
    return data


class WorkerClient:
    """Thin defensive wrapper around the Worker's HTTP API."""

    def __init__(self, base_url: str, api_key: str = "",
                 timeout: float = 10.0, max_attempts: int = 3):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout
        self.max_attempts = max(1, int(max_attempts))

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    # -- low level ---------------------------------------------------------
    def _request(self, method: str, path: str,
                 payload: Optional[dict] = None) -> Any:
        """One attempt. Raises WorkerUnavailable (never raw URLErrors)."""
        operation = "%s %s" % (method, path)
        if not self.base_url:
            raise WorkerUnavailable(operation, "no base_url configured")
        url = self.base_url + path
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer %s" % self.api_key
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            raise WorkerUnavailable(operation, "http %s" % exc.code) from exc
        except Exception as exc:  # timeout, refused, DNS, ...
            raise WorkerUnavailable(operation, "%s: %s"
                                    % (type(exc).__name__, exc)) from exc
        if not 200 <= status < 300:
            raise WorkerUnavailable(operation, "http %s" % status)
        if not raw.strip():
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise WorkerUnavailable(operation, "non-JSON response") from exc

    def _with_retries(self, method: str, path: str,
                      payload: Optional[dict] = None) -> Any:
        last: Optional[WorkerUnavailable] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._request(method, path, payload)
            except WorkerUnavailable as exc:
                last = exc
                logger.warning("WORKER_UNAVAILABLE (%s attempt %d/%d): %s",
                               path, attempt, self.max_attempts, exc.reason)
                if attempt < self.max_attempts:
                    time.sleep(2 ** (attempt - 1))
        assert last is not None
        raise last

    # -- Worker API ----------------------------------------------------------
    def get_kill_switch(self) -> dict:
        """Cloud kill-switch mirror. Validates the {"engaged": bool} contract."""
        data = self._with_retries("GET", "/kill-switch")
        return _require_shape(data, {"engaged": bool}, "GET /kill-switch")

    def set_kill_switch(self, engaged: bool) -> dict:
        return self._with_retries("POST", "/kill-switch", {"engaged": bool(engaged)})

    def post_signal(self, signal: dict) -> dict:
        """Report a detected signal to the cloud ledger. The Worker
        requires symbol/timeframe/direction/entry_price/stop_loss/
        take_profit/candle_time; signals missing those are skipped
        (returned, not raised) — the local event log stays authoritative."""
        required = ("symbol", "timeframe", "direction", "entry_price",
                    "stop_loss", "take_profit", "candle_time")
        missing = [f for f in required if signal.get(f) is None]
        if missing:
            logger.info("not syncing signal to Worker: missing %s", missing)
            return {"ok": False, "skipped": True, "missing": missing}
        return self._with_retries("POST", "/signals", signal)

    def post_performance_snapshot(self, snapshot: dict) -> dict:
        data = self._with_retries("POST", "/performance/snapshot", snapshot)
        return _require_shape(data, {"ok": bool}, "POST /performance/snapshot")

    def get_latest_performance(self) -> dict:
        data = self._with_retries("GET", "/performance/latest")
        return _require_shape(data, {"available": bool}, "GET /performance/latest")

    # -- event-queue forwarding ----------------------------------------------
    def sync_events(self, events: List[dict]) -> dict:
        """Forward a drained batch to the cloud. Only event types with a
        real Worker endpoint are sent; the rest are counted, never failed.

        Returns {"sent": n, "skipped": m}. Raises WorkerUnavailable only
        when a sendable event could not be delivered after retries —
        the caller must then dead-letter the batch locally.
        """
        sent, skipped = 0, 0
        for event in events:
            name = event.get("event", "")
            try:
                if name == "signal.detected":
                    result = self.post_signal(event)
                    if result.get("ok", True):
                        sent += 1
                    else:
                        skipped += 1
                elif name == "performance.snapshot":
                    self.post_performance_snapshot(event.get("snapshot") or {})
                    sent += 1
                elif name == "kill_switch.activated":
                    self.set_kill_switch(True)
                    sent += 1
                elif name == "kill_switch.cleared":
                    self.set_kill_switch(False)
                    sent += 1
                else:
                    skipped += 1  # no cloud endpoint for this event type
            except WorkerUnavailable:
                raise  # caller dead-letters the whole batch
            except Exception as exc:  # noqa: BLE001 — defensive: never leak
                logger.exception("unexpected sync error for %s", name)
                raise WorkerUnavailable("sync_events",
                                        "unexpected: %s" % exc) from exc
        return {"sent": sent, "skipped": skipped}


def from_config(config=None) -> WorkerClient:
    """Build from AppConfig (.worker.base_url/.api_key/.enabled)."""
    if config is None:
        from config.config import load_config  # noqa: PLC0415
        config = load_config()
    worker = getattr(config, "worker", None)
    base_url = getattr(worker, "base_url", "") or ""
    api_key = getattr(worker, "api_key", "") or ""
    enabled = bool(getattr(worker, "enabled", False))
    return WorkerClient(base_url if enabled else "", api_key)
