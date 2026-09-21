#!/usr/bin/env python3
"""local_api — localhost-only HTTP API for the Forex-agent subsystem.

Binds ONLY to 127.0.0.1 (never 0.0.0.0). No auth — the loopback bind IS
the access control; do not expose this port beyond the host.

Routes:
  GET  /health /status /signals /positions /account /performance /events
       /events/latest
  POST /analyze /trade/request /position/close /position/modify

  GET /events is content-negotiated:
    * Accept: text/event-stream  -> live SSE push stream of agent events
      (query: resume_from=<event_id> replays missed events in order,
      ack=<event_id> acknowledges receipt). See docs/EVENTS.md.
    * otherwise                  -> JSON poll of the event log (unchanged).
  GET /events/latest?since=<event_id>&limit=N -> JSON poll fallback:
    events journaled after <event_id> (or the newest N), chronological.

All JSON responses are objects. Query params (GET) or JSON body (POST)
map onto the registry capability arguments.

Run:  python3 scripts/local_api.py [--port 8765]
Env:  FOREX_API_PORT, FOREX_AGENT_HOME, FOREX_AGENT_STORAGE, ...
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.tools import registry  # noqa: E402

logger = logging.getLogger("forex_agent.local_api")

DEFAULT_PORT = int(os.environ.get("FOREX_API_PORT", "8765"))
MAX_BODY = 64 * 1024

# SSE stream tuning (localhost agent channel; see docs/EVENTS.md).
_SSE_POLL_INTERVAL_S = 1.0    # journal tail interval for new events
_SSE_KEEPALIVE_S = 15.0       # idle comment interval to hold the connection
_SSE_REPLAY_LIMIT = 1000      # max events replayed on resume_from connect

# Cap on the JSON event-poll endpoints: a single localhost client must
# not be able to pull an unbounded journal slice into memory.
_EVENTS_LIMIT_MAX = 1000


def _clamp_limit(query: dict) -> int:
    return max(1, min(int(query.get("limit", ["50"])[0]), _EVENTS_LIMIT_MAX))


def _json_response(handler: BaseHTTPRequestHandler, data: dict,
                   status: int = 200) -> None:
    body = json.dumps(data, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _status_payload() -> dict:
    health = registry.call("forex.get_health", {})
    account = registry.call("forex.get_account", {})
    positions = registry.call("forex.get_positions", {})
    try:
        from agent.tools.backend import broker_adapter  # noqa: PLC0415
        broker_status = broker_adapter().broker_status().get("broker")
    except Exception as exc:  # status endpoint must never fail on this
        broker_status = {"provider": "unknown", "configured": False,
                         "detail": {"reason": str(exc)[:200]}}
    return {
        "ok": bool(health.get("ok")),
        "status": health.get("status"),
        "kill_switch_engaged": health.get("components", {}).get(
            "kill_switch", {}).get("detail", {}).get("engaged"),
        "broker_status": broker_status,
        "equity": (account.get("account") or {}).get("equity"),
        "currency": (account.get("account") or {}).get("currency"),
        "open_positions": positions.get("count"),
        "components": {k: v.get("status")
                       for k, v in health.get("components", {}).items()},
    }


def _events_payload(query: dict) -> dict:
    from agent.events.bus import poll  # noqa: PLC0415
    limit = _clamp_limit(query)
    since = query.get("since", [None])[0]
    try:
        events = poll(limit=limit, since=since)
    except ValueError as exc:
        return {"ok": False, "error_code": "INVALID_ARGUMENT",
                "message": str(exc)}
    return {"ok": True, "count": len(events), "events": events}


def _events_latest_payload(query: dict) -> dict:
    """JSON poll fallback for the push channel: events journaled after
    ``since``=<event_id> (chronological), or the newest ``limit`` events
    when ``since`` is absent. ValueError (unknown event_id / bad limit)
    is mapped to HTTP 400 by the GET dispatcher."""
    from agent.events import bus  # noqa: PLC0415
    limit = _clamp_limit(query)
    since = query.get("since", [None])[0]
    if since:
        events = bus.replay_after(since, limit=limit)
    else:
        events = bus.latest_events(limit=limit)
    return {"ok": True, "count": len(events), "events": events,
            "last_event_id": bus.last_event_id()}


def _sse_frame(entry: dict) -> bytes:
    """One SSE frame for a journaled event entry.

    id:    the event_id (native Last-Event-ID / dedup key)
    event: the dotted event type (native SSE event field)
    data:  JSON envelope {event_id, event, severity, timestamp, payload}
    """
    payload = {k: v for k, v in entry.items() if not k.startswith("_")}
    event_id = str(payload.get("event_id", ""))
    data = {
        "event_id": event_id,
        "event": payload.get("event"),
        "severity": payload.get("severity", "INFO"),
        "timestamp": payload.get("ts"),
        "payload": payload,
    }
    # json.dumps escapes embedded newlines, so data stays a single line.
    return ("id: %s\nevent: %s\ndata: %s\n\n"
            % (event_id, payload.get("event", "unknown"),
               json.dumps(data, default=str))).encode("utf-8")


def _handle_events_sse(handler: BaseHTTPRequestHandler, query: dict) -> None:
    """Serve GET /events as a Server-Sent Events stream.

    Query params:
      resume_from=<event_id>  replay every event journaled after it
                              (in order) before streaming live ones.
      ack=<event_id>          acknowledge receipt (cumulative: this event
                              and everything journaled at/before it).

    The stream tails the persistent delivery journal, so events published
    by other processes (daemons) are delivered too. Unknown resume_from /
    ack ids get HTTP 400 (JSON) — a client holding an id the journal
    never saw must notice, not silently miss events.
    """
    from agent.events import bus  # noqa: PLC0415
    resume_from = query.get("resume_from", [None])[0]
    ack_id = query.get("ack", [None])[0]
    try:
        if ack_id is not None and not bus.acknowledge(ack_id):
            raise ValueError("unknown event_id in ack: %r" % (ack_id,))
        missed = (bus.replay_after(resume_from, limit=_SSE_REPLAY_LIMIT)
                  if resume_from else [])
        cursor = (missed[-1]["event_id"] if missed else bus.last_event_id())
    except ValueError as exc:
        _json_response(handler, {"ok": False, "error_code": "INVALID_ARGUMENT",
                                 "message": str(exc)}, 400)
        return
    except RuntimeError as exc:
        logger.exception("SSE /events journal unavailable")
        _json_response(handler, {"ok": False, "error_code": "INTERNAL_ERROR",
                                 "message": str(exc)}, 500)
        return

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("X-Accel-Buffering", "no")  # no proxy buffering
    handler.end_headers()

    def emit(raw: bytes) -> None:
        handler.wfile.write(raw)
        handler.wfile.flush()

    try:
        emit(b": connected to forex-agent event stream\n\n")
        for entry in missed:
            emit(_sse_frame(entry))
            cursor = entry["event_id"]
        idle_since = time.monotonic()
        while True:
            time.sleep(_SSE_POLL_INTERVAL_S)
            try:
                fresh = (bus.replay_after(cursor, limit=200) if cursor
                         else bus.latest_events(limit=50))
            except ValueError:
                # Journal rotated/recreated mid-stream: re-anchor on latest.
                fresh = bus.latest_events(limit=50)
            for entry in fresh:
                emit(_sse_frame(entry))
                cursor = entry["event_id"]
            if fresh:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= _SSE_KEEPALIVE_S:
                emit(b": ping\n\n")  # keep-alive comment holds the connection
                idle_since = time.monotonic()
    except (BrokenPipeError, ConnectionResetError):
        pass  # subscriber went away; normal
    finally:
        logger.info("SSE /events subscriber disconnected")


_GET_ROUTES = {
    "/health": lambda q: registry.call("forex.get_health", {}),
    "/status": lambda q: _status_payload(),
    "/signals": lambda q: registry.call("forex.get_signals", {
        "limit": int(q.get("limit", ["20"])[0]),
        **({"status": q["status"][0]} if "status" in q else {}),
    }),
    "/positions": lambda q: registry.call("forex.get_positions", {}),
    "/account": lambda q: registry.call("forex.get_account", {}),
    "/performance": lambda q: registry.call("forex.get_performance", {
        "lookback_days": int(q.get("lookback_days", ["30"])[0])}),
    "/events": _events_payload,
    "/events/latest": _events_latest_payload,
}

_POST_ROUTES = {
    "/analyze": "forex.analyze",
    "/trade/request": "forex.request_trade",
    "/position/close": "forex.close_position",
    "/position/modify": "forex.modify_position",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "forex-agent-local-api/0.1"

    def log_message(self, fmt, *args):  # stderr, not stdout
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, data: dict, status: int = 200) -> None:
        _json_response(self, data, status)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        # /events is content-negotiated: SSE push stream when the client
        # asks for text/event-stream, JSON poll otherwise (unchanged).
        if (parsed.path == "/events"
                and "text/event-stream" in self.headers.get("Accept", "")):
            _handle_events_sse(self, parse_qs(parsed.query))
            return
        route = _GET_ROUTES.get(parsed.path)
        if route is None:
            self._send({"ok": False, "error_code": "NOT_FOUND",
                        "message": "unknown route %s" % parsed.path}, 404)
            return
        try:
            self._send(route(parse_qs(parsed.query)))
        except (ValueError, TypeError) as exc:
            self._send({"ok": False, "error_code": "INVALID_ARGUMENT",
                        "message": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("GET %s failed", parsed.path)
            self._send({"ok": False, "error_code": "INTERNAL_ERROR",
                        "message": str(exc)}, 500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        capability = _POST_ROUTES.get(parsed.path)
        if capability is None:
            self._send({"ok": False, "error_code": "NOT_FOUND",
                        "message": "unknown route %s" % parsed.path}, 404)
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_BODY:
            self._send({"ok": False, "error_code": "INVALID_ARGUMENT",
                        "message": "body too large"}, 413)
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (ValueError, UnicodeDecodeError):
            self._send({"ok": False, "error_code": "INVALID_ARGUMENT",
                        "message": "body must be JSON"}, 400)
            return
        if not isinstance(body, dict):
            self._send({"ok": False, "error_code": "INVALID_ARGUMENT",
                        "message": "body must be a JSON object"}, 400)
            return
        try:
            result = registry.call(capability, body)
        except Exception as exc:  # noqa: BLE001
            logger.exception("POST %s failed", parsed.path)
            self._send({"ok": False, "error_code": "INTERNAL_ERROR",
                        "message": str(exc)}, 500)
            return
        self._send(result, 200 if result.get("ok", True) else 422)


def run(port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    # 127.0.0.1 ONLY — never 0.0.0.0. This bind is the access control.
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    logger.warning("forex-agent local API on http://127.0.0.1:%d (loopback only)",
                   server.server_address[1])
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="forex-agent localhost API")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # Single instance: a second API server always loses and exits quietly,
    # so two processes can never fight over the port.
    from daemon.common import acquire_pidfile  # noqa: PLC0415
    if not acquire_pidfile("local_api"):
        logger.warning("another local_api is already running; exiting")
        return 0
    server = run(args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
