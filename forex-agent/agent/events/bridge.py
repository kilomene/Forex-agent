"""agent/events/bridge.py — persistent agent notification bridge.

The missing link between "Forex has an SSE endpoint" and "Forex can
proactively deliver real-time events to an autonomous Linux agent":

    Event Bus -> Event Journal -> SSE /events -> Bridge -> Host-Agent Sink

Someone must actually *subscribe* to the SSE stream. That someone is
this bridge: a small, persistent, vendor-neutral process that

  * subscribes to the localhost SSE event stream (GET /events),
  * keeps a DURABLE cursor (last delivered event_id) in
    ``$FOREX_AGENT_HOME/run/agent-event.cursor``,
  * reconnects automatically with backoff (API restart, connection
    reset, daemon restart, ...),
  * resumes with ``resume_from=<cursor>`` so missed events replay in
    order after a restart,
  * anchors a cursor-less ("live") subscription via GET /events/latest
    before connecting, so an event journaled between bridge start and
    the stream snapshot replays instead of being silently missed,
  * delivers each event AT-LEAST-ONCE to a host-provided sink,
    deduplicating on ``event_id``.

Sink contract (the host agent / environment provides the implementation;
Forex provides the bridge and this contract — it does NOT invent a fake
universal host API such as POST /agent/notify):

    class AgentNotificationSink(ABC):
        def deliver(self, event: dict) -> None: ...  # raise on failure
        def health(self) -> dict: ...                # secret-free status

The generic local sink shipped here is ``SubprocessSink``: an
argv-based subprocess (NEVER shell=True) configured via
``FOREX_AGENT_NOTIFICATION_COMMAND`` that receives one NDJSON envelope
per line on stdin::

    {"event_id": "evt_...", "event": "signal.detected",
     "severity": "NOTICE", "timestamp": "...", "payload": {...}}

Delivery semantics: at-least-once. The cursor is persisted AFTER a
successful sink delivery; a crash between delivery and cursor write
causes redelivery on restart, which the sink dedups on ``event_id``.
Exactly-once is NOT claimed.

Guarantee distinction (see docs/AGENT_INTEGRATION.md): Forex guarantees
event generated -> journaled -> streamable -> bridge running. It can NOT
guarantee "the host model woke up" — that depends on the host-provided
sink integration.

Security:
  * No shell=True anywhere; the sink command is parsed with shlex and
    executed as argv. The executable path is validated (absolute path
    must exist and be executable; bare names resolve via PATH).
  * Event payloads never carry secrets (subsystem invariant); the sink
    child inherits the bridge's environment, so do NOT put secrets in
    FOREX_AGENT_NOTIFICATION_COMMAND itself.
  * The bridge only ever talks to 127.0.0.1.

Run as a managed service: ``python3 -m agent.events.bridge``
(pidfile ``$FOREX_AGENT_HOME/run/agent_bridge.pid`` via
daemon.common.acquire_pidfile — a second bridge always loses and exits
quietly). ``scripts/forex-daemons`` and the ``forex-agent-bridge``
systemd unit manage it.
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from abc import ABC, abstractmethod
from collections import deque
from typing import Deque, Dict, List, Optional

logger = logging.getLogger("forex_agent.events.bridge")

DEFAULT_PORT = int(os.environ.get("FOREX_API_PORT", "8765"))
DEFAULT_API_BASE = "http://127.0.0.1:%d" % DEFAULT_PORT
CURSOR_FILENAME = "agent-event.cursor"
SINK_COMMAND_ENV = "FOREX_AGENT_NOTIFICATION_COMMAND"

# Reconnect backoff: 1, 2, 4, 8, 15, 30, 60, 60, ... seconds.
_BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0)
# Socket read timeout inside the SSE loop: the loop wakes up this often
# to check the stop flag even when the stream is idle.
_SSE_READ_TIMEOUT_S = 5.0
# In-memory dedup window for redelivered frames (at-least-once overlap).
_DEDUP_WINDOW = 2000
# Delivery retry: a transiently failing sink must not tear down the
# stream on the first rejection — retry in place a few times first.
# (Tearing down immediately would lose the event: with no cursor yet,
# the reconnect anchor would be the failed event itself and
# resume_from is strictly-after, so it could never replay.)
_DELIVERY_RETRIES = 10
_DELIVERY_RETRY_S = 1.0


class SinkError(Exception):
    """The host-agent sink failed to accept an event."""


# ---------------------------------------------------------------------------
# Sink contract
# ---------------------------------------------------------------------------

class AgentNotificationSink(ABC):
    """Host-provided event sink. Forex owns the bridge and this contract;
    the HOST owns the implementation.

    ``deliver`` must raise (SinkError or any Exception) when the event
    was NOT accepted — the bridge then retries instead of advancing the
    cursor. ``health`` returns a small secret-free status dict.
    """

    @abstractmethod
    def deliver(self, event: dict) -> None:
        """Deliver one event envelope. Raise when not accepted."""

    @abstractmethod
    def health(self) -> Dict[str, object]:
        """Secret-free status for operators."""


def _validate_executable(argv0: str) -> str:
    """Validate a sink executable path; return the resolved path.

    Bare names resolve via PATH; paths must be absolute, exist, be a
    file, and be executable. Raises ValueError otherwise — a sink that
    cannot be validated is never spawned.
    """
    if not argv0 or not isinstance(argv0, str):
        raise ValueError("sink command must start with an executable")
    if os.path.sep in argv0 or (os.path.altsep and os.path.altsep in argv0):
        if not os.path.isabs(argv0):
            raise ValueError(
                "sink executable path must be absolute, got %r" % argv0)
        if not os.path.isfile(argv0):
            raise ValueError(
                "sink executable does not exist: %r" % argv0)
        if not os.access(argv0, os.X_OK):
            raise ValueError(
                "sink executable is not executable: %r" % argv0)
        return argv0
    resolved = shutil.which(argv0)
    if not resolved:
        raise ValueError(
            "sink executable %r not found on PATH" % argv0)
    return resolved


def parse_sink_command(spec: str) -> List[str]:
    """Parse FOREX_AGENT_NOTIFICATION_COMMAND into argv (no shell).

    ``shlex.split`` only tokenizes — nothing is executed here. The
    executable (argv[0]) is validated; the rest are passed through
    verbatim as arguments.
    """
    if not spec or not spec.strip():
        raise ValueError("empty sink command")
    argv = shlex.split(spec.strip())
    if not argv:
        raise ValueError("empty sink command")
    argv[0] = _validate_executable(argv[0])
    return argv


class SubprocessSink(AgentNotificationSink):
    """Generic local sink: NDJSON event envelopes on a child's stdin.

    The child is spawned argv-based (no shell). If it exits, it is
    restarted on the next delivery; repeated fast crashes surface as
    SinkError so the bridge retries instead of advancing the cursor.

    Envelope per line: {event_id, event, severity, timestamp, payload}.
    """

    name = "subprocess"

    def __init__(self, argv: List[str]):
        if not argv:
            raise ValueError("argv must be non-empty")
        self.argv = [str(a) for a in argv]
        self._lock = threading.Lock()
        self._child: Optional[subprocess.Popen] = None
        self._crash_count = 0
        self._last_crash = 0.0

    @classmethod
    def from_command(cls, spec: str) -> "SubprocessSink":
        return cls(parse_sink_command(spec))

    @classmethod
    def from_env(cls) -> Optional["SubprocessSink"]:
        spec = os.environ.get(SINK_COMMAND_ENV, "").strip()
        if not spec:
            return None
        return cls.from_command(spec)

    def _spawn(self) -> None:
        # No shell=True: argv is executed directly. stdin=PIPE carries
        # the NDJSON envelopes; stdout/stderr go to DEVNULL (the child
        # is a sink, not a conversational peer).
        self._child = subprocess.Popen(
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        logger.info("notification sink: spawned %s (pid %d)",
                    self.argv[0], self._child.pid)

    def _ensure_child(self) -> None:
        child = self._child
        if child is not None and child.poll() is None:
            return
        if child is not None:
            # Child died: count fast crashes so a broken command
            # surfaces instead of respawning forever.
            now = time.monotonic()
            if now - self._last_crash < 10.0:
                self._crash_count += 1
            else:
                self._crash_count = 1
            self._last_crash = now
            logger.warning("notification sink child exited (rc=%s); "
                           "restarting", child.poll())
            if self._crash_count > 5:
                raise SinkError(
                    "sink command %r keeps crashing; not retrying blindly"
                    % (self.argv[0],))
        try:
            self._spawn()
        except (OSError, ValueError) as exc:
            raise SinkError("cannot spawn sink %r: %s"
                            % (self.argv[0], exc)) from exc

    def deliver(self, event: dict) -> None:
        # Normalize to the documented envelope shape
        # {event_id, event, severity, timestamp, payload}: the SSE data
        # frames already carry exactly this, so this is normally a
        # pass-through that just guards the keys.
        envelope = {
            "event_id": event.get("event_id"),
            "event": event.get("event"),
            "severity": event.get("severity", "INFO"),
            "timestamp": event.get("timestamp") or event.get("ts"),
            "payload": event.get("payload", event),
        }
        line = (json.dumps(envelope, default=str) + "\n").encode("utf-8")
        with self._lock:
            self._ensure_child()
            assert self._child is not None and self._child.stdin is not None
            try:
                self._child.stdin.write(line)
                self._child.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                # Child died mid-write: restart once and retry the line.
                logger.warning("sink write failed (%s); restarting child",
                               exc)
                try:
                    self._child.kill()
                except OSError:
                    pass
                self._child = None
                self._ensure_child()
                assert self._child is not None and self._child.stdin is not None
                try:
                    self._child.stdin.write(line)
                    self._child.stdin.flush()
                except (BrokenPipeError, OSError) as exc2:
                    raise SinkError(
                        "sink write failed after restart: %s" % exc2) from exc2

    def health(self) -> Dict[str, object]:
        child = self._child
        return {
            "name": self.name,
            "configured": True,
            "argv0": self.argv[0],
            "child_running": bool(child is not None and child.poll() is None),
        }

    def close(self) -> None:
        with self._lock:
            child, self._child = self._child, None
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()


class NullSink(AgentNotificationSink):
    """Tracking-only sink: advances the cursor, delivers nowhere.

    Used when no host sink is configured. The bridge still tails the
    stream and maintains the cursor (so a later-configured sink starts
    from a sane position), but nothing is proactively delivered — the
    events remain available via SSE resume. ``health()["configured"]``
    is False so installers report honestly.
    """

    name = "null"

    def deliver(self, event: dict) -> None:
        return None

    def health(self) -> Dict[str, object]:
        return {"name": self.name, "configured": False,
                "note": "no %s configured; set it to enable proactive "
                        "host-agent delivery" % SINK_COMMAND_ENV}


# ---------------------------------------------------------------------------
# Durable cursor
# ---------------------------------------------------------------------------

def default_cursor_path(home: Optional[str] = None) -> str:
    base = home or os.environ.get("FOREX_AGENT_HOME",
                                  os.path.join(os.path.expanduser("~"),
                                               ".forex-agent"))
    return os.path.join(base, "run", CURSOR_FILENAME)


def read_cursor(path: str) -> Optional[str]:
    """Last delivered event_id, or None when unknown/corrupt/missing."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("cursor unreadable (%s); starting without resume", exc)
        return None
    if not value or not value.startswith("evt_"):
        if value:
            logger.warning("cursor holds a non-event id %r; ignoring", value)
        return None
    return value


def write_cursor(path: str, event_id: str) -> None:
    """Atomically persist the cursor (tmp file + os.replace + fsync)."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(event_id + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class Bridge:
    """Subscribe -> deliver -> persist cursor, with reconnect + backoff.

    Runs ``run_forever()`` in the calling thread until ``stop()`` is
    called; ``start()`` runs it in a background thread for embedding.
    """

    def __init__(self, api_base: str = DEFAULT_API_BASE,
                 cursor_path: Optional[str] = None,
                 sink: Optional[AgentNotificationSink] = None,
                 connect_timeout: float = 10.0):
        parsed = urllib.parse.urlparse(api_base)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
            raise ValueError("bridge only talks to http://127.0.0.1 "
                             "(loopback); got %r" % api_base)
        self.api_base = api_base.rstrip("/")
        self.port = parsed.port or 80
        self.cursor_path = cursor_path or default_cursor_path()
        self.sink = sink if sink is not None else NullSink()
        self.connect_timeout = connect_timeout
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._recent: Deque[str] = deque(maxlen=_DEDUP_WINDOW)
        self._cursor: Optional[str] = None
        # Set once the bridge has established its first event-stream
        # subscription with the server (anchor + HTTP 200). Tests wait
        # on this before publishing: an event can only be guaranteed
        # delivered once the server has snapshotted the stream.
        self.subscribed = threading.Event()

    # -- lifecycle ------------------------------------------------------

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.run_forever,
                                        name="forex-agent-bridge",
                                        daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    @property
    def cursor(self) -> Optional[str]:
        return self._cursor

    # -- main loop ------------------------------------------------------

    def run_forever(self) -> None:
        self._cursor = read_cursor(self.cursor_path)
        if self._cursor:
            logger.info("bridge: resuming from cursor %s", self._cursor)
        else:
            logger.info("bridge: no cursor; streaming live events only")
        backoff_idx = 0
        while not self._stop.is_set():
            try:
                self._stream_once()
                backoff_idx = 0  # clean disconnect only happens on stop
            except _StreamEnd:
                return  # stop() was called
            except Exception as exc:  # reconnectable failure; back off
                wait = _BACKOFF_S[min(backoff_idx, len(_BACKOFF_S) - 1)]
                backoff_idx += 1
                logger.warning("bridge stream failed (%s); reconnecting "
                               "in %.0fs", exc, wait)
                if self._stop.wait(wait):
                    return

    def _fetch_latest_id(self) -> Optional[str]:
        """Ask the server for its newest journaled event_id.

        Used to anchor a cursor-less ("live") subscription: resume
        strictly after this id so events journaled between bridge start
        and the stream snapshot replay instead of being silently
        missed. Raises on failure — the caller backs off and retries.
        """
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=self.connect_timeout)
        try:
            conn.request("GET", "/events/latest?limit=1")
            resp = conn.getresponse()
            if resp.status != 200:
                raise ConnectionError(
                    "GET /events/latest -> HTTP %d" % resp.status)
            body = json.loads(resp.read().decode("utf-8"))
            last = body.get("last_event_id")
            return last if isinstance(last, str) and last else None
        finally:
            conn.close()

    def _stream_once(self) -> None:
        """One SSE connection: replay missed, then stream live."""
        cursor = self._cursor
        if cursor is None:
            # No durable cursor: anchor "live" at first contact instead
            # of letting the server snapshot whenever it gets around to
            # it — otherwise an event journaled after we start but
            # before the snapshot is silently missed.
            cursor = self._fetch_latest_id()
        query = ("/events?resume_from=" + urllib.parse.quote(cursor)
                 if cursor else "/events")
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=self.connect_timeout)
        try:
            conn.putrequest("GET", query)
            conn.putheader("Accept", "text/event-stream")
            conn.endheaders()
            resp = conn.getresponse()
            if resp.status == 400 and cursor:
                # Journal rotated/recreated since the cursor was written:
                # the id is unknown. Drop the stale cursor and restart
                # live rather than failing forever. (Documented; the
                # events remain in the old journal files if any.)
                logger.warning("bridge: cursor %s unknown to journal "
                               "(HTTP 400); restarting live", cursor)
                self._cursor = None
                try:
                    write_cursor(self.cursor_path, "")
                except OSError:
                    pass
                return
            if resp.status != 200:
                body = resp.read(512).decode("utf-8", "replace")
                raise ConnectionError(
                    "GET %s -> HTTP %d: %s" % (query, resp.status, body))
            ctype = resp.getheader("Content-Type", "")
            if "text/event-stream" not in ctype:
                raise ConnectionError(
                    "expected text/event-stream, got %r" % ctype)
            self.subscribed.set()
            self._pump(resp, conn)
        finally:
            conn.close()

    def _pump(self, resp: http.client.HTTPResponse,
              conn: http.client.HTTPConnection) -> None:
        """Read SSE frames until stop or stream failure."""
        # Wake up regularly so stop() takes effect even on an idle stream.
        try:
            conn.sock.settimeout(_SSE_READ_TIMEOUT_S)
        except (OSError, AttributeError):
            pass
        fp = resp.fp
        fid: Optional[str] = None
        fdata: Optional[str] = None
        while not self._stop.is_set():
            try:
                line = fp.readline(1 << 16)
            except (socket.timeout, TimeoutError):
                continue
            if not line:
                raise ConnectionError("SSE stream closed by server")
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if text == "":
                if fdata is not None and fid:
                    self._on_event(fid, fdata)
                fid, fdata = None, None
                continue
            if text.startswith(":"):
                continue  # hello / keep-alive comment
            if text.startswith("id:"):
                fid = text[3:].strip()
            elif text.startswith("data:"):
                piece = text[5:].strip()
                fdata = piece if fdata is None else fdata + "\n" + piece
        raise _StreamEnd()

    def _on_event(self, event_id: str, data: str) -> None:
        try:
            event = json.loads(data)
        except ValueError:
            logger.warning("bridge: skipping unparsable frame id=%s", event_id)
            return
        if not isinstance(event, dict):
            return
        if event_id in self._recent:
            # At-least-once overlap (replay after reconnect): the sink
            # already saw this id — advance the cursor, don't redeliver.
            self._advance(event_id)
            return
        self._deliver_with_retry(event_id, event)
        self._recent.append(event_id)
        self._advance(event_id)

    def _deliver_with_retry(self, event_id: str, event: dict) -> None:
        """Deliver, retrying a rejecting sink in place before giving up.

        A transient sink failure must not tear down the stream: with no
        cursor yet, the reconnect anchor would be the failed event
        itself and resume_from is strictly-after, so the event could
        never replay. Raises _DeliveryFailed after _DELIVERY_RETRIES
        attempts (the stream then reconnects and continues with newer
        events; the rejection is logged). Raises _StreamEnd when
        stop() is called mid-retry.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(1, _DELIVERY_RETRIES + 1):
            try:
                self.sink.deliver(event)
                return
            except Exception as exc:
                last_exc = exc
                logger.warning("bridge: sink rejected %s (%s); retry %d/%d",
                               event_id, exc, attempt, _DELIVERY_RETRIES)
                if self._stop.wait(_DELIVERY_RETRY_S):
                    raise _StreamEnd()
        raise _DeliveryFailed(
            "sink rejected %s %d times: %s"
            % (event_id, _DELIVERY_RETRIES, last_exc))

    def _advance(self, event_id: str) -> None:
        self._cursor = event_id
        try:
            write_cursor(self.cursor_path, event_id)
        except OSError as exc:
            # The cursor is a resume optimization, not the journal: a
            # failed write is logged, not fatal. Worst case the bridge
            # replays a few extra events after a restart (dedup covers it).
            logger.warning("bridge: cursor write failed (%s)", exc)


class _StreamEnd(Exception):
    """Internal: stop() was requested; unwind the pump cleanly."""


class _DeliveryFailed(Exception):
    """Internal: sink rejected an event; reconnect and retry it."""


# ---------------------------------------------------------------------------
# Managed-service entry point
# ---------------------------------------------------------------------------

def build_sink_from_env() -> AgentNotificationSink:
    """Host sink from FOREX_AGENT_NOTIFICATION_COMMAND, else NullSink."""
    try:
        sink = SubprocessSink.from_env()
    except ValueError as exc:
        logger.warning("invalid %s (%s); bridge runs without a sink",
                       SINK_COMMAND_ENV, exc)
        return NullSink()
    return sink if sink is not None else NullSink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="forex-agent notification bridge: SSE -> host-agent sink")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE,
                        help="local API base URL (default %(default)s)")
    parser.add_argument("--cursor", default=None,
                        help="cursor file (default $FOREX_AGENT_HOME/run/%s)"
                             % CURSOR_FILENAME)
    parser.add_argument("--sink-command", default=None,
                        help="override %s" % SINK_COMMAND_ENV)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    # Single instance: a second bridge always loses and exits quietly.
    from daemon.common import acquire_pidfile  # noqa: PLC0415
    if not acquire_pidfile("agent_bridge"):
        logger.warning("another agent_bridge is already running; exiting")
        return 0

    if args.sink_command:
        try:
            sink: AgentNotificationSink = SubprocessSink.from_command(
                args.sink_command)
        except ValueError as exc:
            logger.warning("invalid --sink-command (%s); no sink", exc)
            sink = NullSink()
    else:
        sink = build_sink_from_env()

    bridge = Bridge(api_base=args.api_base,
                    cursor_path=args.cursor,
                    sink=sink)
    logger.warning("forex-agent notification bridge starting (sink=%s)",
                   sink.health().get("name"))
    try:
        bridge.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if isinstance(sink, SubprocessSink):
            sink.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
