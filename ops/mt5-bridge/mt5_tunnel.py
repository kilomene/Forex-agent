#!/usr/bin/env python3
"""
MT5 CONNECT-tunnel forwarder.

Listens on 127.0.0.1:9443. Every inbound TCP connection is relayed through
an HTTP CONNECT tunnel via the egress proxy to demo.metaquotes.net:443,
so the MT5 terminal's proprietary binary protocol reaches the real
MetaQuotes demo server (direct TCP is swallowed by the transparent
middlebox in this environment).

Pair with an iptables rule like:
  iptables -t nat -A OUTPUT -p tcp -d <demo-ip> --dport 443 \
      -j REDIRECT --to-ports 9443
so the terminal's "direct" connection is transparently forwarded.

Logs to run/mt5_tunnel.log. Daemonize with --daemon.
"""

import os
import select
import socket
import sys
import threading
import time

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("TUNNEL_PORT", "9443"))
PROXY_HOST = "hatch-egress-proxy"
PROXY_PORT = 3128
TARGET = "demo.metaquotes.net:443"

BASE = os.path.dirname(os.path.abspath(__file__))
RUN_DIR = os.path.join(BASE, "run")
os.makedirs(RUN_DIR, exist_ok=True)
LOG_PATH = os.path.join(RUN_DIR, "mt5_tunnel.log")


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def pipe(a, b, label):
    up_bytes = down_bytes = 0
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 60)
            if not r:
                log(f"{label} idle timeout (up={up_bytes} down={down_bytes})")
                break
            data = r[0].recv(65536)
            if not data:
                log(f"{label} EOF from {'client' if r[0] is a else 'server'} (up={up_bytes} down={down_bytes})")
                break
            if r[0] is a:
                up_bytes += len(data)
                b.sendall(data)
            else:
                down_bytes += len(data)
                a.sendall(data)
    except OSError as e:
        log(f"{label} socket error: {e} (up={up_bytes} down={down_bytes})")
    finally:
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass


def handle(client):
    peer = client.getpeername()
    try:
        # First line from the shim: "TARGET <host>:<port>\n" with the real
        # destination (the shim redirected a connect() to 127.0.0.1:9443).
        client.settimeout(10)
        hdr = b""
        while b"\n" not in hdr:
            chunk = client.recv(64)
            if not chunk:
                raise OSError("client closed before TARGET header")
            hdr += chunk
        line, rest = hdr.split(b"\n", 1)
        if not line.startswith(b"TARGET "):
            raise OSError(f"bad header: {line!r}")
        target = line[7:].decode().strip()
        client.settimeout(None)
        log(f"{peer} target={target}")

        up = socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=15)
        up.sendall(
            f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode()
        )
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = up.recv(1024)
            if not chunk:
                raise OSError("proxy closed during CONNECT")
            resp += chunk
        status = resp.split(b"\r\n", 1)[0]
        if b" 200 " not in status:
            log(f"{peer} proxy refused: {status!r}")
            client.close()
            up.close()
            return
        log(f"{peer} tunnel established -> {target}")
        # forward any bytes that arrived with the header, then first chunk
        if rest:
            log(f"{peer} header-trailing {len(rest)} bytes: {rest.hex()}")
            up.sendall(rest)
        client.settimeout(10)
        try:
            first = client.recv(256)
            if first:
                log(f"{peer} first {len(first)} client bytes: {first.hex()}")
                up.sendall(first)
        except socket.timeout:
            log(f"{peer} no client data within 10s")
        client.settimeout(None)
        pipe(client, up, f"{peer}")
        log(f"{peer} tunnel closed")
    except Exception as e:
        log(f"{peer} error: {e}")
        try:
            client.close()
        except OSError:
            pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(16)
    log(f"listening on {LISTEN_HOST}:{LISTEN_PORT} -> CONNECT {TARGET} via {PROXY_HOST}:{PROXY_PORT}")
    while True:
        client, _ = srv.accept()
        threading.Thread(target=handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        import subprocess
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__)],
            stdout=open(os.path.join(RUN_DIR, "mt5_tunnel.out"), "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        print("tunnel daemon started")
    else:
        main()
