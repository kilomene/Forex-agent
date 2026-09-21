"""Notification channels: the sinks an event can be delivered to.

Channel contract
----------------
``NotificationChannel`` is the abstract base. Concrete channels implement:

* ``name`` — short identifier used in the severity routing table.
* ``is_configured()`` — True only when the channel can actually send.
  Unconfigured channels are *skipped* by the dispatcher, never called.
* ``send(event)`` — deliver one event dict. May raise
  ``NotificationError`` (or any Exception); the dispatcher isolates every
  failure so one bad channel never blocks the others or the event journal.
* ``health()`` — a small secret-free dict for ``forex notify --status``.

Channels
--------
* ``AgentChannel`` (``agent``) — PRIMARY. Delivery = presence in the
  durable delivery journal; the localhost SSE stream (``GET /events``)
  streams the journal to the agent. Always available.
* ``WorkerChannel`` (``worker``) — forwards events to the Cloudflare
  Worker cloud/sync layer when the Worker is configured. Best-effort:
  the published Worker contract (``worker/CONTRACT.md``) does not define
  an agent-event sink yet, so non-2xx/transport failures are logged and
  isolated.
* ``FCMChannel`` (``fcm``) — mobile push. OPTIONAL, legacy sink, never
  required for agent notification. Worker-mediated (the Worker owns the
  FCM token registry and the FCM send path); ``is_configured()`` is False
  until the credentials AND a Worker push path are present — see the
  class docstring for exactly what is missing.
* ``WebhookChannel`` (``webhook``) — POST the event JSON to a configured
  URL. Optional, timeout-bounded, failure-isolated.

Security: no secrets in code, logs, or health output. URLs are logged
by host only; event payloads are never logged.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

logger = logging.getLogger("forex_agent.notifications")


class NotificationError(Exception):
    """A notification channel failed to deliver. Isolated by the
    dispatcher — never propagates into the event journal or trading."""


class NotificationChannel(ABC):
    """Abstract notification sink. See module docstring for the contract."""

    name: str = "base"

    def __init__(self, enabled: bool = True):
        self.enabled = bool(enabled)

    @abstractmethod
    def is_configured(self) -> bool:
        """True only when this channel can actually send right now."""

    @abstractmethod
    def send(self, event: dict) -> None:
        """Deliver one event. May raise NotificationError."""

    def health(self) -> Dict[str, Any]:
        """Secret-free status for operators."""
        return {"name": self.name, "configured": self.is_configured()}


# ---------------------------------------------------------------------------
# AgentChannel — the PRIMARY path (always available)
# ---------------------------------------------------------------------------

class AgentChannel(NotificationChannel):
    """PRIMARY notification channel: the agent event channel.

    Delivery means *presence in the durable delivery journal*
    (``storage.event_journal``). The localhost SSE stream
    (``GET /events`` in scripts/local_api.py) streams the journal to the
    agent — including reconnect/resume replay — so a journaled event is
    a delivered event. No extra transport is needed or used.

    ``send()`` verifies the event is journaled (``replay_after`` raises
    ``ValueError`` for an unknown ``event_id``). If the event is somehow
    missing it is re-published: ``event_journal_add`` uses
    ``INSERT OR IGNORE`` on the unique ``event_id``, so a re-journal is a
    no-op, never a duplicate.
    """

    name = "agent"

    def is_configured(self) -> bool:
        # The journal is local storage — always available.
        return True

    def send(self, event: dict) -> None:
        from agent.events import bus  # noqa: PLC0415 (documented dep)

        event_id = event.get("event_id") if isinstance(event, dict) else None
        if not event_id:
            raise NotificationError("agent channel requires an event_id")
        try:
            bus.replay_after(event_id, limit=1)
        except ValueError:
            # Not journaled — (re)publish. INSERT OR IGNORE on the
            # unique event_id makes this idempotent, never a duplicate.
            logger.warning("agent channel: %s missing from journal; re-journaling",
                           event_id)
            bus.publish(dict(event))
        except RuntimeError as exc:
            raise NotificationError(
                "event journal unavailable: %s" % exc) from exc

    def health(self) -> Dict[str, Any]:
        from agent.events import bus  # noqa: PLC0415 (documented dep)

        status: Dict[str, Any] = {"name": self.name, "configured": True}
        try:
            status["journal_head"] = bus.last_event_id()
        except Exception as exc:  # journal unreadable — report, don't raise
            status["journal_head"] = None
            status["error"] = str(exc)
        return status


# ---------------------------------------------------------------------------
# WorkerChannel — Cloudflare Worker cloud/sync layer (optional)
# ---------------------------------------------------------------------------

class WorkerChannel(NotificationChannel):
    """Forward events to the Cloudflare Worker (cloud/sync layer).

    Active only when the Worker is configured (``worker.enabled`` plus a
    base URL). Posts the event JSON to
    ``POST {base_url}/agent/events`` with
    ``Authorization: Bearer <WORKER_API_KEY>`` when an API key is set.

    Honest limitation: the published Worker contract
    (``worker/CONTRACT.md``) does not currently define an agent-event
    sink endpoint, so this channel is best-effort and forward-compatible
    — non-2xx responses and transport failures are logged and isolated,
    never raised into the event path. Coordinate with the Worker owner
    to add ``POST /agent/events`` for full cloud sync of the event
    stream.
    """

    name = "worker"
    PATH = "/agent/events"

    def __init__(self, enabled: bool = True, base_url: str = "",
                 api_key: str = "", timeout: float = 5.0):
        super().__init__(enabled=enabled)
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = max(0.5, float(timeout))

    def is_configured(self) -> bool:
        return self.enabled and bool(self.base_url)

    def send(self, event: dict) -> None:
        url = self.base_url + self.PATH
        headers = {"Content-Type": "application/json",
                   "User-Agent": "forex-agent-notify/1.0"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        try:
            req = urllib.request.Request(
                url, data=json.dumps(event, default=str).encode("utf-8"),
                headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            raise NotificationError(
                "worker %s returned HTTP %s" % (self.PATH, exc.code)) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                OSError) as exc:
            raise NotificationError(
                "worker POST failed: %s" % exc) from exc
        if status >= 300:
            raise NotificationError(
                "worker %s returned HTTP %s" % (self.PATH, status))
        # Log routing facts only — never the payload.
        logger.info("notify worker: delivered %s severity=%s",
                    event.get("event_id"), event.get("severity"))

    def health(self) -> Dict[str, Any]:
        status = super().health()
        status["base_url"] = self.base_url or None
        status["note"] = ("no /agent/events endpoint in worker/CONTRACT.md; "
                          "best-effort until the Worker owner adds it")
        return status


# ---------------------------------------------------------------------------
# FCMChannel — mobile push (OPTIONAL, legacy sink)
# ---------------------------------------------------------------------------

class FCMChannel(NotificationChannel):
    """Mobile push — OPTIONAL, legacy sink. Never required for agent
    notification.

    Mobile push is Worker-mediated: the Worker owns the FCM device-token
    registry and the FCM send path (``worker/src/fcm.js``). This
    agent-side channel therefore needs ALL of:

    * ``notifications.channels.fcm: true``,
    * ``FCM_PROJECT_ID`` set (the credentials that authorize push),
    * the Worker configured (``worker.enabled`` + base URL),
    * ``notifications.fcm_push_path`` set to the Worker's push endpoint
      path (e.g. ``/notify``).

    What is missing today: the published Worker contract documents only
    ``POST /devices/test-push`` (a manual test hook), not a real
    per-event push endpoint — so ``fcm_push_path`` defaults to empty and
    ``is_configured()`` is honestly False until the Worker owner adds one.

    Direct FCM HTTP v1 sends from the agent are deliberately NOT
    implemented: that would require google-auth JWT signing (not
    installed) and a local copy of the device-token registry, which
    lives in the Worker. If a direct path is ever needed, implement it
    in ``_send_direct`` and extend ``is_configured()`` accordingly.
    """

    name = "fcm"

    def __init__(self, enabled: bool = False, project_id: str = "",
                 worker_base_url: str = "", api_key: str = "",
                 push_path: str = "", timeout: float = 5.0):
        super().__init__(enabled=enabled)
        self.project_id = project_id or ""
        self.worker_base_url = (worker_base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.push_path = (push_path or "").strip()
        if self.push_path and not self.push_path.startswith("/"):
            self.push_path = "/" + self.push_path
        self.timeout = max(0.5, float(timeout))

    def is_configured(self) -> bool:
        return (self.enabled and bool(self.project_id)
                and bool(self.worker_base_url) and bool(self.push_path))

    def send(self, event: dict) -> None:
        if not self.is_configured():
            raise NotificationError(
                "fcm not configured: needs fcm.enabled + FCM_PROJECT_ID + "
                "worker base_url + fcm_push_path (no Worker push endpoint "
                "exists yet — see worker/CONTRACT.md)")
        url = self.worker_base_url + self.push_path
        payload = {
            "project_id": self.project_id,
            "event_id": event.get("event_id"),
            "severity": event.get("severity"),
            "event": event.get("event"),
            "ts": event.get("ts"),
            "title": "[%s] %s" % (event.get("severity", "INFO"),
                                  event.get("event", "event")),
            "body": event.get("event", ""),
        }
        headers = {"Content-Type": "application/json",
                   "User-Agent": "forex-agent-notify/1.0"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            raise NotificationError(
                "fcm push path returned HTTP %s" % exc.code) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                OSError) as exc:
            raise NotificationError("fcm push POST failed: %s" % exc) from exc
        if status >= 300:
            raise NotificationError(
                "fcm push path returned HTTP %s" % status)

    def health(self) -> Dict[str, Any]:
        status = super().health()
        status["project_id"] = self.project_id or None
        status["push_path"] = self.push_path or None
        status["note"] = ("optional/legacy; worker-mediated; unconfigured "
                          "until fcm_push_path is set")
        return status


# ---------------------------------------------------------------------------
# WebhookChannel — POST event JSON to a URL (optional)
# ---------------------------------------------------------------------------

class WebhookChannel(NotificationChannel):
    """POST the event JSON to a configured URL. Optional.

    The URL comes from ``NOTIFY_WEBHOOK_URL`` (preferred — the URL may
    embed a token, and secrets never belong in yaml) or
    ``notifications.webhook_url``. Timeout-bounded; every failure is
    isolated by the dispatcher. Logs carry the URL host only, never the
    full URL.
    """

    name = "webhook"

    def __init__(self, enabled: bool = False, url: str = "",
                 timeout: float = 5.0):
        super().__init__(enabled=enabled)
        self.url = (url or "").strip()
        self.timeout = max(0.5, float(timeout))

    def is_configured(self) -> bool:
        return self.enabled and bool(self.url)

    @property
    def _host(self) -> str:
        try:
            return urllib.parse.urlparse(self.url).hostname or "?"
        except Exception:
            return "?"

    def send(self, event: dict) -> None:
        if not self.is_configured():
            raise NotificationError("webhook not configured: set "
                                    "NOTIFY_WEBHOOK_URL")
        try:
            req = urllib.request.Request(
                self.url,
                data=json.dumps(event, default=str).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "User-Agent": "forex-agent-notify/1.0"},
                method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            raise NotificationError(
                "webhook %s returned HTTP %s" % (self._host, exc.code)) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                OSError) as exc:
            # Includes connection timeouts/refused: isolated, never raised
            # past the dispatcher.
            raise NotificationError(
                "webhook %s POST failed: %s" % (self._host, exc)) from exc
        if status >= 300:
            raise NotificationError(
                "webhook %s returned HTTP %s" % (self._host, status))
        logger.info("notify webhook: delivered %s to %s",
                    event.get("event_id"), self._host)

    def health(self) -> Dict[str, Any]:
        status = super().health()
        status["host"] = self._host if self.url else None
        return status


def channel(name: str, **kwargs: Any) -> NotificationChannel:
    """Build a channel by name (``agent`` | ``worker`` | ``fcm`` | ``webhook``)."""
    registry = {"agent": AgentChannel, "worker": WorkerChannel,
                "fcm": FCMChannel, "webhook": WebhookChannel}
    try:
        cls = registry[name]
    except KeyError:
        raise NotificationError("unknown notification channel %r" % (name,))
    return cls(**kwargs)
