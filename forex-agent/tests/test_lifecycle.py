"""Lifecycle hardening tests (Phase 7).

Covers the supervisor script and the restart safety sequence:

  * start twice -> a single daemon set (no duplicate daemons), pids stable
  * stop -> every daemon stopped, pidfiles gone
  * stale pidfile -> reported by status, reclaimed by start
  * restart -> stop + start + safety sequence OK, daemons running again
  * restart safety sequence -> kill-switch latch, idempotency records and
    event-journal state survive a restart (fresh Store/gateway objects on
    the same DB file); broker honestly reported
  * pre-restart request_id recognized post-restart: the original decision
    is returned and the broker is never touched twice
  * kill-switch engaged persists across restart
  * health_monitor emits a schema-valid broker.disconnected that reaches
    the event journal
  * event-journal pruning bounds growth by age and by count; newest kept
  * acquire_pidfile race across processes -> exactly one owner

The script-level tests run ``scripts/forex-daemons`` against a fake
``python3`` shim that mimics the daemon pidfile discipline and then
sleeps, so no real daemon loops are spawned. ``daemon.supervisor``
invocations are delegated to the real interpreter.
"""

import multiprocessing
import os
import subprocess
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = str(REPO_ROOT / "scripts" / "forex-daemons")
DAEMONS = ("market_monitor", "signal_monitor", "position_monitor",
           "health_monitor")

SHIM = """#!/usr/bin/env bash
# Test shim for $PYTHON: mimics `python3 -m daemon.<name>` pidfile
# discipline, then sleeps. daemon.supervisor runs on the real interpreter.
mod=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-m" ]; then mod="$a"; fi
  prev="$a"
done
case "$mod" in
  daemon.supervisor) exec /usr/bin/python3 "$@" ;;
  daemon.*)
    name="${mod#daemon.}"
    echo $$ > "$FOREX_AGENT_HOME/run/$name.pid"
    exec sleep 300 ;;
  *) exec /usr/bin/python3 "$@" ;;
esac
"""


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def fx_home(tmp_path, monkeypatch):
    """Isolated FOREX_AGENT_HOME + storage DB + fake-python shim."""
    home = tmp_path / "fxhome"
    home.mkdir()
    db = home / "storage" / "local.db"
    monkeypatch.setenv("FOREX_AGENT_HOME", str(home))
    monkeypatch.setenv("FOREX_AGENT_STORAGE", str(db))
    import storage.store as store_mod
    monkeypatch.setattr(store_mod, "DEFAULT_DB_PATH", db)
    from agent.events import bus as event_bus
    event_bus.reset_for_tests()
    shim = tmp_path / "shim" / "fake-python3"
    shim.parent.mkdir()
    shim.write_text(SHIM)
    shim.chmod(0o755)
    monkeypatch.setenv("PYTHON", str(shim))
    from agent.tools import backend
    saved = dict(backend._overrides)
    backend._overrides.clear()
    yield home
    backend._overrides.clear()
    backend._overrides.update(saved)
    event_bus.reset_for_tests()


def _script(*args, timeout=120):
    return subprocess.run([SCRIPT, *args], capture_output=True, text=True,
                          timeout=timeout)


def _pids(home):
    out = {}
    for name in DAEMONS:
        pf = home / "run" / (name + ".pid")
        out[name] = int(pf.read_text().strip()) if pf.exists() else None
    return out


def _alive(pid):
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pids, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if not any(_alive(p) for p in pids if p):
            return True
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# supervisor script: start/stop/restart/status
# ---------------------------------------------------------------------------
def test_start_twice_yields_single_daemon_set(fx_home):
    r = _script("start")
    assert r.returncode == 0, r.stderr
    first = _pids(fx_home)
    assert all(first[n] for n in DAEMONS)

    r2 = _script("start")
    assert r2.returncode == 0, r2.stderr
    assert "already running" in r2.stdout
    second = _pids(fx_home)
    assert first == second, "second start must not launch duplicates"
    assert all(_alive(p) for p in second.values())

    _script("stop")


def test_stop_stops_all_daemons(fx_home):
    _script("start")
    before = _pids(fx_home)
    assert all(before.values())
    r = _script("stop")
    assert r.returncode == 0, r.stderr
    assert all("stopped" in line for line in r.stdout.splitlines()
               if line.split(":")[0] in DAEMONS)
    # pidfiles removed, processes actually dead
    assert not list((fx_home / "run").glob("*.pid"))
    assert _wait_dead(list(before.values())), "daemon processes must exit"
    st = _script("status")
    assert all("stopped" in line for line in st.stdout.splitlines()
               if line.split(":")[0] in DAEMONS)


def test_stale_pidfile_reported_and_reclaimed(fx_home):
    (fx_home / "run").mkdir(parents=True, exist_ok=True)
    (fx_home / "run" / "market_monitor.pid").write_text("99999999")
    st = _script("status", "market_monitor")
    assert "stale pidfile" in st.stdout
    r = _script("start", "market_monitor")
    assert r.returncode == 0, r.stderr
    pid = int((fx_home / "run" / "market_monitor.pid").read_text().strip())
    assert pid != 99999999 and _alive(pid)
    _script("stop", "market_monitor")


def test_restart_runs_safety_sequence_and_restores(fx_home):
    _script("start")
    before = _pids(fx_home)
    r = _script("restart", timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "restart safety sequence: OK" in r.stdout + r.stderr
    after = _pids(fx_home)
    assert all(after[n] and _alive(after[n]) for n in DAEMONS)
    assert _wait_dead(list(before.values())), "old daemons must be gone"
    _script("stop")


def test_health_and_prune_commands(fx_home):
    _script("start")
    h = _script("health")
    assert h.returncode == 0, h.stderr
    assert "market_monitor: running" in h.stdout
    assert "kill_switch" in h.stdout
    p = _script("prune")
    assert p.returncode == 0, p.stderr
    assert "pruned" in p.stdout
    _script("stop")


# ---------------------------------------------------------------------------
# acquire_pidfile: cross-process race -> exactly one owner
# ---------------------------------------------------------------------------
def test_pidfile_race_single_owner(fx_home):
    import time as _time
    from daemon import common

    def worker(q):
        won = common.acquire_pidfile("race_probe")
        q.put(won)
        if won:
            # Hold the pidfile like a real daemon: losers polling for an
            # owner must observe a LIVE pid and back off.
            _time.sleep(2)

    ctx = multiprocessing.get_context("fork")
    q = ctx.Queue()
    procs = [ctx.Process(target=worker, args=(q,)) for _ in range(8)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(15)
    results = [q.get() for _ in procs]
    assert sum(1 for r in results if r) == 1, \
        "exactly one process may own the pidfile, got %s" % (results,)
    # cleanup: the winner was a child, so remove its pidfile directly
    try:
        os.remove(common.pid_file("race_probe"))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# restart safety sequence: state survives restart
# ---------------------------------------------------------------------------
def _engage_and_seed(store):
    from core.execution import KillSwitch
    KillSwitch(store=store).engage(source="lifecycle-test")
    from daemon.common import save_daemon_state
    save_daemon_state(store, "market_monitor", {"last_heartbeat": 123.0})
    store.idem_put("pre-restart-req", "request_trade",
                   {"approved": True, "ticket": 4242})
    store.event_journal_add(event_id="evt_prerestart_1", event="health.check",
                            severity="INFO",
                            ts=datetime.now(timezone.utc).isoformat(),
                            payload={"event": "health.check", "ok": True})


def test_restart_safety_sequence_restores_state(fx_home):
    from storage import Store
    from daemon.supervisor import restart_safety_sequence

    home = fx_home
    db = str(home / "storage" / "local.db")
    _engage_and_seed(Store(db))

    # "Restart": brand-new Store object on the same DB file.
    store2 = Store(db)
    report = restart_safety_sequence(store=store2)
    steps = {s["step"]: s for s in report["steps"]}
    assert report["ok"] is True

    load = steps["load_local_state"]
    assert load["ok"] is True
    assert load["detail"]["kill_switch"]["engaged"] is True
    assert load["detail"]["kill_switch"]["source"] == "lifecycle-test"
    assert load["detail"]["idempotency_store_readable"] is True

    # Broker is honestly reported (disconnected in this environment).
    assert steps["broker"]["detail"]["connected"] is False
    assert steps["reconcile"]["detail"].get("skipped") is True

    ev = steps["event_state"]
    assert ev["ok"] is True
    assert ev["detail"]["last_event_id"] == "evt_prerestart_1"
    assert ev["detail"]["resume_from_tip_stable"] is True

    # Kill switch still engaged after the restart; idempotency intact.
    from core.execution import KillSwitch
    assert KillSwitch(store=store2).is_engaged() is True
    rec = store2.idem_get("pre-restart-req")
    assert rec["decision"] == {"approved": True, "ticket": 4242}


def test_kill_switch_engaged_persists_across_restart(fx_home, tmp_path):
    from storage import Store
    from core.execution import KillSwitch

    db = str(tmp_path / "ks.db")
    ks1 = KillSwitch(store=Store(db))
    assert ks1.is_engaged() is False
    ks1.engage(source="lifecycle-test")
    del ks1

    ks2 = KillSwitch(store=Store(db))  # fresh objects, same DB file
    assert ks2.is_engaged() is True
    state = ks2.state()
    assert state["engaged"] is True
    assert state["source"] == "lifecycle-test"


# ---------------------------------------------------------------------------
# pre-restart request_id recognised post-restart: no double execution
# ---------------------------------------------------------------------------
def _gateway_on_db(monkeypatch, db_path):
    """Real ExecutionGateway + real SQLite Store on one DB file."""
    import core.events as events_mod
    monkeypatch.setattr(events_mod, "_forward", lambda event: False)
    events_mod.drain_queue()

    from broker import AccountInfo, OrderRequest, OrderResult, Position, \
        Quote, SymbolSpec
    from config import AppConfig, RiskConfig
    from core.execution import ExecutionGateway, KillSwitch, TradeRequest
    from core.risk import RiskManager
    from storage import Store

    class LifecycleBroker:
        adapter_name = "lifecycle-fake"

        def __init__(self):
            self.submits = []
            self.modify_calls = []
            self.close_calls = []

        def account_info(self):
            return AccountInfo(balance=10000.0, equity=10000.0,
                               currency="USD")

        def positions(self):
            return [Position(ticket=111, position_id=111, symbol="EURUSD",
                             direction="BUY", volume=0.10, price_open=1.1000,
                             price_current=1.1050, sl=1.0950, tp=1.1100,
                             profit=50.0, swap=0.0, magic=1, comment="t",
                             time_open=datetime.now(timezone.utc))]

        def symbols(self, names=None):
            return [SymbolSpec(name="EURUSD", volume_min=0.01,
                               volume_max=100.0, volume_step=0.01,
                               tick_value=1.0, tick_size=0.00001,
                               contract_size=100000.0, digits=5,
                               point=0.00001)]

        def quote(self, symbol):
            return Quote(symbol=symbol, bid=1.1000, ask=1.1002,
                         time=datetime.now(timezone.utc))

        def submit_order(self, req: OrderRequest):
            self.submits.append(req)
            return OrderResult(ticket=777, symbol=req.symbol,
                               direction=req.direction, volume=req.volume,
                               price=1.1000, retcode=0, message="done")

        def modify_order(self, ticket, sl, tp):
            self.modify_calls.append((ticket, sl, tp))

        def close_position(self, ticket):
            self.close_calls.append(ticket)
            return 1.1050

    monkeypatch.setenv("MT5_LOGIN", "1")
    monkeypatch.setenv("MT5_PASSWORD", "x")
    monkeypatch.setenv("MT5_SERVER", "y")
    cfg = replace(AppConfig(), mode="live")
    risk_cfg = RiskConfig(fixed_lot_size=0.10, use_percent_risk_sizing=False,
                          max_risk_per_trade=0.01, max_daily_loss=0.03,
                          max_open_positions=3, max_total_exposure_lots=10.0,
                          max_consecutive_losses=4,
                          max_correlated_positions=5,
                          require_stop_loss=True)
    store = Store(db_path)
    store.set_risk_state({"day": date.today().isoformat(),
                          "start_of_day_equity": 10000.0,
                          "recent_outcomes": []})
    broker = LifecycleBroker()
    gw = ExecutionGateway(cfg, broker, RiskManager(risk_cfg, store=store),
                          KillSwitch(store=store), store=store)

    def req(request_id):
        return TradeRequest(signal_id="sig-%s" % request_id, symbol="EURUSD",
                            direction="BUY", stop_loss=1.0950,
                            take_profit=1.1100, entry_price=1.1000,
                            idempotency_key="key-%s" % request_id,
                            request_id=request_id, source="agent")

    return gw, broker, req


def test_prerestart_request_id_recognized_no_double_execution(
        fx_home, monkeypatch, tmp_path):
    db = str(tmp_path / "restart-idem.db")
    gw1, broker1, req = _gateway_on_db(monkeypatch, db)

    d1 = gw1.request_trade(req("lifecycle-trade"))
    assert d1.approved is True
    m1 = gw1.modify_position(111, stop_loss=1.0980,
                             request_id="lifecycle-mod")
    assert m1["ok"] is True
    assert len(broker1.submits) == 1
    assert len(broker1.modify_calls) == 1

    # ---- process "restart": everything rebuilt on the same DB file ----
    gw2, broker2, req2 = _gateway_on_db(monkeypatch, db)
    d2 = gw2.request_trade(req2("lifecycle-trade"))
    assert d2.approved is True
    assert d2.ticket == d1.ticket == 777
    assert d2.state == "executed"
    m2 = gw2.modify_position(111, stop_loss=1.0999,
                             request_id="lifecycle-mod")
    assert m2["ok"] is True
    assert m2["stop_loss"] == 1.0980  # original result replayed
    # The fresh broker saw ZERO write calls: nothing executed twice.
    assert broker2.submits == []
    assert broker2.modify_calls == []
    assert broker2.close_calls == []


# ---------------------------------------------------------------------------
# health_monitor: broker.disconnected is schema-valid and journaled
# ---------------------------------------------------------------------------
def test_health_monitor_broker_disconnected_reaches_journal(
        fx_home, monkeypatch):
    from storage import Store
    from agent.events import bus as event_bus
    from agent.tools import backend
    from daemon.common import load_config
    from daemon.health_monitor import HealthMonitor

    store = Store()
    monkeypatch.setattr(event_bus, "_store_override", store)

    class OutageAdapter:
        adapter_name = "mt5"

        def broker_status(self):
            return {"broker": {
                "provider": "mt5", "configured": True, "reachable": False,
                "connected": False, "account_available": False,
                "market_data_available": False, "trading_available": False,
                "detail": {"reason": "simulated outage"}}}

    monkeypatch.setattr(backend, "broker_adapter", lambda: OutageAdapter())

    hm = HealthMonitor()
    state = {"broker_connected": True}  # transition: connected -> down
    status = {"ok": True, "checks": {}}
    hm._check_broker(load_config(), state, status)

    assert status["checks"]["broker"]["connected"] is False
    assert status["ok"] is False
    assert state["broker_connected"] is False

    found = [e for e in store.event_journal_latest(10)
             if e.get("event") == "broker.disconnected"]
    assert found, "broker.disconnected must reach the event journal"
    payload = found[-1]
    assert payload["adapter"] == "mt5"          # required field present
    assert "error" in payload                    # allowed optional field
    assert "note" not in payload                 # 'note' is schema-invalid
    assert payload["severity"] == "CRITICAL"


def test_health_monitor_broker_reconnect_emits_connected(fx_home, monkeypatch):
    from storage import Store
    from agent.events import bus as event_bus
    from agent.tools import backend
    from daemon.common import load_config
    from daemon.health_monitor import HealthMonitor

    store = Store()
    monkeypatch.setattr(event_bus, "_store_override", store)

    class UpAdapter:
        adapter_name = "mt5"

        def broker_status(self):
            return {"broker": {
                "provider": "mt5", "configured": True, "reachable": True,
                "connected": True, "account_available": True,
                "market_data_available": True, "trading_available": True,
                "detail": {}}}

    monkeypatch.setattr(backend, "broker_adapter", lambda: UpAdapter())

    hm = HealthMonitor()
    state = {"broker_connected": False}  # transition: down -> connected
    status = {"ok": True, "checks": {}}
    hm._check_broker(load_config(), state, status)

    assert status["checks"]["broker"]["connected"] is True
    assert status["ok"] is True
    found = [e for e in store.event_journal_latest(10)
             if e.get("event") == "broker.connected"]
    assert found, "broker.connected must reach the event journal"
    assert found[-1]["adapter"] == "mt5"


# ---------------------------------------------------------------------------
# event-journal pruning
# ---------------------------------------------------------------------------
def _seed_event(store, event_id, ts):
    store.event_journal_add(event_id=event_id, event="health.check",
                            severity="INFO", ts=ts,
                            payload={"event": "health.check", "ok": True,
                                     "event_id": event_id})


def test_journal_pruning_bounds_growth_by_age_and_count(tmp_path):
    from storage import Store

    store = Store(str(tmp_path / "prune.db"))
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=60)).isoformat()
    new_ts = now.isoformat()
    for i in range(150):
        _seed_event(store, "evt_old_%03d" % i, old_ts)
    for i in range(10):
        _seed_event(store, "evt_new_%02d" % i, new_ts)

    result = store.event_journal_prune(retention_days=30, max_events=100000)
    assert result["pruned"] == 150
    assert result["remaining"] == 10

    # Age bound disabled (0), count bound active: newest 20 kept.
    for i in range(50):
        _seed_event(store, "evt_cap_%02d" % i, new_ts)
    result = store.event_journal_prune(retention_days=0, max_events=20)
    assert result["remaining"] == 20
    latest = store.event_journal_latest(20)
    assert latest[-1]["event_id"] == "evt_cap_49", \
        "pruning must keep the newest events (SSE resume intact)"
    assert latest[0]["event_id"] == "evt_cap_30"


def test_prune_is_config_driven(fx_home, monkeypatch):
    from storage import Store
    from daemon import supervisor

    store = Store()
    now = datetime.now(timezone.utc).isoformat()
    for i in range(40):
        _seed_event(store, "evt_cfg_%02d" % i, now)

    monkeypatch.setenv("EVENTS_JOURNAL_MAX_EVENTS", "25")
    monkeypatch.setenv("EVENTS_JOURNAL_RETENTION_DAYS", "0")
    from config.config import load_config
    report = supervisor.prune_event_journal(store=store,
                                            config=load_config())
    assert report["max_events"] == 25
    assert report["retention_days"] == 0
    assert report["remaining"] == 25
