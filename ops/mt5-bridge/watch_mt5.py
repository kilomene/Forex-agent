#!/usr/bin/env python3
"""
MT5 watchdog (runs from cron every 5 minutes).

Checks:
  1. terminal64.exe process is alive
  2. nova_feed.json is fresh (< 180 s old) and shows connected:true

On failure: relaunch via scripts/run-mt5-tunnel.sh, then best-effort GUI
re-attach of the NovaSignals EA (Xvfb :99 + xdotool). Logs everything to
run/watch_mt5.log. Exits 0 on healthy, 1 when it had to intervene (so the
cron result surfaces the incident), 2 on hard failure.

Self-match guard: the pgrep pattern uses the [.] bracket trick so this
script's own command line never matches.
"""

import codecs
import glob
import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
BRIDGE = os.path.join(HOME, "workspace", "mt5", "bridge")
RUN_DIR = os.path.join(BRIDGE, "run")
LOG_PATH = os.path.join(RUN_DIR, "watch_mt5.log")
FEED_PATH = os.path.join(
    HOME, "workspace", "mt5", "prefix", "drive_c", "Program Files",
    "MetaTrader 5", "MQL5", "Files", "nova_feed.json")
TERM_LOG = os.path.join(
    HOME, "workspace", "mt5", "prefix", "drive_c", "Program Files",
    "MetaTrader 5", "logs", "20260921.log")
LAUNCHER = os.path.join(HOME, "workspace", "mt5", "scripts",
                        "run-mt5-tunnel.sh")
FEED_MAX_AGE = 180


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def terminal_alive():
    # bracket trick: pattern text in our own cmdline is "terminal64[.]exe"
    # which the regex "terminal64[.]exe" does NOT match
    r = subprocess.run(["pgrep", "-f", "terminal64[.]exe"],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


def feed_fresh():
    try:
        age = time.time() - os.path.getmtime(FEED_PATH)
        if age > FEED_MAX_AGE:
            return False, "feed age %.0fs > %ds" % (age, FEED_MAX_AGE)
        with open(FEED_PATH) as f:
            feed = json.load(f)
        if not feed.get("connected"):
            return False, "feed connected != true"
        return True, "feed age %.0fs, %d symbols" % (
            age, len(feed.get("symbols", {})))
    except (OSError, ValueError) as e:
        return False, "feed unreadable: %s" % e


APT_CACHE = "/var/cache/apt/archives"
WINE_LIB = "/usr/lib/wine/wine64"
WINE_LINK = "/usr/local/bin/wine64"
# Proven 2026-09-22: after a host reboot the system layer loses wine64;
# reinstall from the persistent apt archive cache with dpkg (no network,
# no apt index needed), then recreate the /usr/local/bin symlink.
WINE_DEBS = ["wine64_9.0~repack-4build3_amd64.deb",
             "wine_9.0~repack-4build3_all.deb",
             "libwine_9.0~repack-4build3_amd64.deb",
             "fonts-wine_9.0~repack-4build3_all.deb"]
WINE_DEP_GLOBS = ["libcapi20-3t64_*.deb", "libgphoto2-6t64_*.deb",
                  "libgphoto2-port12t64_*.deb",
                  "libgstreamer-plugins-base1.0-0_*.deb",
                  "libgstreamer1.0-0_*.deb", "libpcap0.8t64_*.deb",
                  "libpcsclite1_*.deb", "libxkbregistry0_*.deb",
                  "libz-mingw-w64_*.deb", "iso-codes_*.deb",
                  "libdw1t64_*.deb", "libexif12_*.deb", "libgd3_*.deb",
                  "libibverbs1_*.deb", "liborc-0.4-0t64_*.deb",
                  "libnl-3-200_*.deb", "libnl-route-3-200_*.deb"]


def wine_ok():
    if not (os.path.isfile(WINE_LIB) and os.access(WINE_LIB, os.X_OK)):
        return False
    r = subprocess.run([WINE_LIB, "--version"], capture_output=True,
                       text=True, timeout=30)
    return r.returncode == 0 and "wine-" in r.stdout.lower()


def dpkg_status(pkg):
    r = subprocess.run(["dpkg-query", "-W", "-f", "${db:Status-Status}",
                        pkg], capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


def ensure_wine():
    """Reinstall Wine from the local apt cache if a reboot wiped it.

    Returns True when a working wine64 is available afterwards.
    Never raises.
    """
    if wine_ok():
        return True
    log("wine missing after reboot; reinstalling from apt cache")
    try:
        debs = []
        for name in WINE_DEBS:
            p = os.path.join(APT_CACHE, name)
            if os.path.isfile(p):
                debs.append(p)
            else:
                log("ensure_wine: cached deb not found: %s" % name)
        for pattern in WINE_DEP_GLOBS:
            hits = sorted(glob.glob(os.path.join(APT_CACHE, pattern)))
            if hits:
                debs.append(hits[0])
        if not debs:
            log("ensure_wine: no cached debs available")
            return False
        for round_no in range(3):
            r = subprocess.run(
                ["dpkg", "-i"] + debs, capture_output=True, text=True,
                timeout=180)
            r2 = subprocess.run(["dpkg", "--configure", "-a"],
                                capture_output=True, text=True, timeout=180)
            if dpkg_status("wine64") == "installed":
                break
            log("ensure_wine round %d: wine64 status=%s" %
                (round_no, dpkg_status("wine64")))
        try:
            if os.path.islink(WINE_LINK) or not os.path.exists(WINE_LINK):
                if os.path.islink(WINE_LINK):
                    os.unlink(WINE_LINK)
                os.symlink(WINE_LIB, WINE_LINK)
        except OSError as e:
            log("ensure_wine symlink failed: %s" % e)
        ok = wine_ok()
        log("ensure_wine %s" % ("OK" if ok else "FAILED"))
        return ok
    except Exception as e:  # noqa: BLE001 - best effort by design
        log("ensure_wine exception: %s" % e)
        return False


def relaunch():
    log("relaunching MT5 via run-mt5-tunnel.sh")
    r = subprocess.run(["bash", LAUNCHER], capture_output=True, text=True,
                       timeout=120)
    log("launcher exit=%d tail=%s" % (r.returncode, r.stdout[-300:]))
    return r.returncode == 0


def gui_reattach():
    """Best-effort: double-click NovaSignals in Navigator, press Enter.

    Returns True if the terminal log shows a fresh 'loaded successfully'
    line within 90 s. Never raises.
    """
    try:
        env = dict(os.environ, DISPLAY=":99")
        before = _load_marker()
        subprocess.run(
            ["xdotool", "mousemove", "62", "405", "click", "--repeat", "2",
             "1"],
            env=env, timeout=30, capture_output=True)
        time.sleep(4)
        # find the EA properties dialog (window name starts with NovaSignals)
        r = subprocess.run(["xdotool", "search", "--name",
                            "^NovaSignals 1"],
                           env=env, capture_output=True, text=True,
                           timeout=30)
        dlg = (r.stdout.strip().split() or [None])[0]
        if dlg:
            subprocess.run(["xdotool", "key", "--window", dlg, "Return"],
                           env=env, timeout=30, capture_output=True)
        else:
            # no dialog found; try a bare Enter in case focus is right
            subprocess.run(["xdotool", "key", "Return"], env=env,
                           timeout=30, capture_output=True)
        time.sleep(5)
        ok = _wait_for_load(before, 90)
        log("gui_reattach %s" % ("OK" if ok else "FAILED"))
        return ok
    except Exception as e:  # noqa: BLE001 - best effort by design
        log("gui_reattach exception: %s" % e)
        return False


def _load_marker():
    try:
        with codecs.open(TERM_LOG, encoding="utf-16le",
                         errors="replace") as f:
            lines = f.read().splitlines()
        hits = [l for l in lines
                if "NovaSignals" in l and "loaded successfully" in l]
        return hits[-1] if hits else ""
    except OSError:
        return ""


def _wait_for_load(before, timeout_s):
    end = time.time() + timeout_s
    while time.time() < end:
        if _load_marker() != before and _load_marker():
            return True
        time.sleep(5)
    return False


def main():
    alive = terminal_alive()
    fresh, detail = feed_fresh()
    if alive and fresh:
        log("healthy: terminal up; %s" % detail)
        return 0
    log("UNHEALTHY: terminal_alive=%s feed: %s" % (alive, detail))
    if not alive and not ensure_wine():
        log("HARD FAILURE: wine reinstall failed")
        return 2
    if not relaunch():
        log("HARD FAILURE: launcher failed")
        return 2
    time.sleep(60)  # let the terminal start + authorize
    if not terminal_alive():
        log("HARD FAILURE: terminal not alive after relaunch")
        return 2
    fresh2, detail2 = feed_fresh()
    if fresh2:
        log("recovered: feed live after relaunch (%s)" % detail2)
        return 1
    # feed still stale -> EA probably not attached; try GUI re-attach
    log("feed still stale after relaunch; attempting GUI re-attach")
    gui_reattach()
    time.sleep(30)
    fresh3, detail3 = feed_fresh()
    log("post-reattach feed: %s (%s)" % (fresh3, detail3))
    return 1


if __name__ == "__main__":
    sys.exit(main())
