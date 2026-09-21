#!/usr/bin/env python3
"""local_api — localhost-only HTTP API for the Forex-agent subsystem.

Binds ONLY to 127.0.0.1 (never 0.0.0.0). No auth — the loopback bind IS
the access control; do not expose this port beyond the host.

Routes:
  GET  /health /status /signals /positions /account /performance /events
  POST /analyze /trade/request /position/close /position/modify

All responses are JSON. Query params (GET) or JSON body (POST) map onto
the registry capability arguments.

Run:  python3 scripts/local_api.py [--port 8765]
Env:  FOREX_API_PORT, FOREX_AGENT_HOME, FOREX_AGENT_STORAGE, ...
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.tools import registry  # noqa: E402

logger = logging.getLogger("forex_agent.local_api")

DEFAULT_PORT = int(os.environ.get("FOREX_API_PORT", "8765"))
MAX_BODY = 64 * 1024


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
    return {
        "ok": bool(health.get("ok")),
        "status": health.get("status"),
        "kill_switch_engaged": health.get("components", {}).get(
            "kill_switch", {}).get("detail", {}).get("engaged"),
        "equity": (account.get("account") or {}).get("equity"),
        "currency": (account.get("account") or {}).get("currency"),
        "open_positions": positions.get("count"),
        "components": {k: v.get("status")
                       for k, v in health.get("components", {}).items()},
    }


def _events_payload(query: dict) -> dict:
    from agent.events.bus import poll  # noqa: PLC0415
    limit = int(query.get("limit", ["50"])[0])
    since = query.get("since", [None])[0]
    try:
        events = poll(limit=limit, since=since)
    except ValueError as exc:
        return {"ok": False, "error_code": "INVALID_ARGUMENT",
                "message": str(exc)}
    return {"ok": True, "count": len(events), "events": events}


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
    server = run(args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
