"""Tests for the continuous-evolution loop (Phase 4).

Covers: proposal dedupe/idempotency, manifest read/write, rollback file
restore (sandboxed), gate-order enforcement, post-install assertions,
hard-block scan, and the fixed gate order. Everything runs against fake
bridge roots in tmp_path -- the live tree is never touched.
"""
import json
import os
import subprocess
import sys
import time

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BRIDGE not in sys.path:
    sys.path.insert(0, BRIDGE)

from pipeline import lib, manifest  # noqa: E402
from pipeline import install as install_mod  # noqa: E402
from pipeline import run_pipeline, GATE_ORDER  # noqa: E402
import evolve  # noqa: E402


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def fake_bridge(tmp_path):
    root = tmp_path / "bridge"
    root.mkdir()
    (root / "dummy_mod.py").write_text("VALUE = 1\n")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n")
    (root / "versions").mkdir()
    return str(root)


@pytest.fixture()
def fake_state(tmp_path):
    sdir = tmp_path / "state"
    sdir.mkdir()
    (sdir / "trading_enabled").write_text("0")
    (sdir / "risk_config.json").write_text(json.dumps({
        "risk_per_trade_pct": 0.05,
        "max_concurrent_trades": 10,
        "blocked_symbols": ["GBPZAR", "XAUCHF"],
        "trading_enabled": False,
        "dry_run": True,
    }))
    (sdir / "nova_journal.jsonl").write_text("")
    return str(sdir)


@pytest.fixture()
def stub_daemon(tmp_path, monkeypatch):
    """A stub process whose argv[1] IS a trade_executor.py path."""
    d = tmp_path / "stubbin"
    d.mkdir()
    script = d / "trade_executor.py"
    script.write_text("import time\ntime.sleep(300)\n")
    p = subprocess.Popen([sys.executable, str(script)],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    monkeypatch.setenv("EVOLUTION_DAEMON_SCRIPT", str(script))
    yield str(script)
    p.terminate()
    p.wait()


@pytest.fixture()
def stub_supervise(tmp_path, monkeypatch):
    s = tmp_path / "supervise_stub.py"
    s.write_text(
        "print('daemon trade_executor.py: pid 4242')\n")
    monkeypatch.setenv("EVOLUTION_SUPERVISE_SCRIPT", str(s))
    return str(s)


# ---------------------------------------------------------------- manifest

def test_manifest_roundtrip(tmp_path):
    b = str(tmp_path)
    man = manifest.new_manifest(
        "v1", parent=None, change_summary="s", proposal_id="P-1",
        tier="auto", change_class="refactor", files=["a.py"])
    manifest.write_version(man, b)
    back = manifest.read_version("v1", b)
    assert back["id"] == "v1"
    assert back["author"] == "evolution-loop"
    assert back["status"] == "prepared"
    manifest.write_installed("v1", None, b)
    inst = manifest.read_installed(b)
    assert inst == {"current": "v1", "previous": None,
                    "updated_utc": inst["updated_utc"]}


def test_gate_order_is_fixed():
    assert GATE_ORDER == ["scan", "a", "g", "b", "c", "e", "h"]


def test_note_of_handles_noteless_dicts():
    from pipeline import _note_of
    assert _note_of({"details": [1, 2]}) == ""
    assert _note_of({"note": "hello"}) == "hello"
    assert _note_of("plain") == "plain"
    assert _note_of(None) == "None"
    assert _note_of({"note": "x" * 500}, 300) == "x" * 300


# ---------------------------------------------------------------- evolve idempotency

def _seed_proposal(proposals_dir, pid="P-X"):
    pdir = os.path.join(proposals_dir, pid)
    os.makedirs(os.path.join(pdir, "tests"), exist_ok=True)
    with open(os.path.join(pdir, "proposal.md"), "w") as f:
        f.write("---\nid: %s\ntier: auto\nchange_class: refactor\n---\n# t\n"
                % pid)
    with open(os.path.join(pdir, "patch.diff"), "w") as f:
        f.write("diff --git a/x.py b/x.py\n")
    with open(os.path.join(pdir, "tests", "test_x.py"), "w") as f:
        f.write("def test_x():\n    assert True\n")
    return pdir


def _patch_evolve_paths(monkeypatch, bridge_root):
    monkeypatch.setattr(evolve, "BRIDGE", bridge_root)
    monkeypatch.setattr(evolve, "PROPOSALS",
                        os.path.join(bridge_root, "proposals"))
    monkeypatch.setattr(evolve, "APPLIED",
                        os.path.join(bridge_root, "proposals",
                                     "applied.json"))
    monkeypatch.setattr(evolve, "QUEUE",
                        os.path.join(bridge_root, "proposals",
                                     "idea_queue.json"))


def test_evolve_registers_and_is_idempotent(tmp_path, monkeypatch):
    broot = str(tmp_path / "b")
    os.makedirs(os.path.join(broot, "proposals"))
    os.makedirs(os.path.join(broot, "run"))
    _seed_proposal(os.path.join(broot, "proposals"))
    _patch_evolve_paths(monkeypatch, broot)
    monkeypatch.setattr(evolve, "BORROW_LIST", "/nonexistent")

    assert evolve.weekly_pass() == 0
    applied1 = evolve.load_applied()
    assert list(applied1) == ["P-X"]
    h1 = applied1["P-X"]["content_hash"]

    assert evolve.weekly_pass() == 0  # second run: no changes
    applied2 = evolve.load_applied()
    assert applied2["P-X"]["content_hash"] == h1
    assert "revised_utc" not in applied2["P-X"]


def test_evolve_detects_revision_but_never_duplicates(tmp_path, monkeypatch):
    broot = str(tmp_path / "b")
    os.makedirs(os.path.join(broot, "proposals"))
    os.makedirs(os.path.join(broot, "run"))
    pdir = _seed_proposal(os.path.join(broot, "proposals"))
    _patch_evolve_paths(monkeypatch, broot)
    monkeypatch.setattr(evolve, "BORROW_LIST", "/nonexistent")
    evolve.weekly_pass()
    h1 = evolve.load_applied()["P-X"]["content_hash"]

    with open(os.path.join(pdir, "proposal.md"), "a") as f:
        f.write("\nrevised\n")
    evolve.weekly_pass()
    applied = evolve.load_applied()
    assert list(applied) == ["P-X"]  # still one entry
    assert applied["P-X"]["content_hash"] != h1
    assert "revised_utc" in applied["P-X"]


def test_evolve_skips_incomplete_queue_specs(tmp_path, monkeypatch):
    broot = str(tmp_path / "b")
    os.makedirs(os.path.join(broot, "proposals"))
    os.makedirs(os.path.join(broot, "run"))
    _patch_evolve_paths(monkeypatch, broot)
    monkeypatch.setattr(evolve, "BORROW_LIST", "/nonexistent")
    with open(os.path.join(broot, "proposals", "idea_queue.json"),
              "w") as f:
        json.dump({"ideas": {
            "Q-9": {"title": "half-baked", "spec_status": "incomplete"}}},
            f)
    evolve.weekly_pass()
    applied = evolve.load_applied()
    assert applied == {}  # nothing half-built
    assert not os.path.exists(os.path.join(broot, "proposals", "Q-9"))


# ---------------------------------------------------------------- rollback (sandboxed)

def test_rollback_restores_files_sandbox(fake_bridge, fake_state,
                                         stub_daemon, monkeypatch, tmp_path):
    b, s = fake_bridge, fake_state
    # installed: current=v9999-broken, previous=v0001-test.
    # The tree live before v9999-broken installed is stored as
    # versions/v9999-broken/previous_snapshot (taken at install time).
    snap = os.path.join(b, "versions", "v9999-broken", "previous_snapshot")
    os.makedirs(snap)
    with open(os.path.join(snap, "dummy_mod.py"), "w") as f:
        f.write("VALUE = 1  # restored\n")
    manifest.write_version(manifest.new_manifest(
        "v0001-test", parent=None, change_summary="t", proposal_id=None,
        tier="auto", change_class="refactor", files=["dummy_mod.py"]), b)
    manifest.write_installed("v9999-broken", "v0001-test", b)
    with open(os.path.join(b, "dummy_mod.py"), "w") as f:
        f.write("BROKEN BROKEN")

    relauncher = tmp_path / "relaunch_stub.py"
    relauncher.write_text("import sys; print('stub relaunch'); sys.exit(0)\n")
    monkeypatch.setenv("EVOLUTION_RELAUNCH_SCRIPT", str(relauncher))
    rc, out, err = lib.run(
        [sys.executable, os.path.join(BRIDGE, "rollback.py"),
         "--previous", "--bridge-root", b, "--state-dir", s,
         "--no-restart"], timeout=60)
    assert rc == 0, out + err
    assert open(os.path.join(b, "dummy_mod.py")).read() == \
        "VALUE = 1  # restored\n"
    inst = manifest.read_installed(b)
    assert inst["current"] == "v0001-test"  # rolled back to previous
    assert inst["previous"] == "v9999-broken"
    # journal got the rollback event in the SANDBOX journal
    journal = open(os.path.join(s, "nova_journal.jsonl")).read()
    assert "pipeline.rollback" in journal


def test_rollback_removes_files_the_bad_version_added(
        fake_bridge, fake_state, stub_daemon, monkeypatch, tmp_path):
    b, s = fake_bridge, fake_state
    snap = os.path.join(b, "versions", "v9999-broken", "previous_snapshot")
    os.makedirs(snap)
    with open(os.path.join(snap, "dummy_mod.py"), "w") as f:
        f.write("VALUE = 1\n")
    manifest.write_version(manifest.new_manifest(
        "v9999-broken", parent=None, change_summary="t", proposal_id=None,
        tier="auto", change_class="refactor",
        files=["dummy_mod.py", "tests/test_added_by_bad.py"]), b)
    manifest.write_installed("v9999-broken", "v0001-test", b)
    # the bad version added a new file (not in the snapshot)
    added = os.path.join(b, "tests", "test_added_by_bad.py")
    with open(added, "w") as f:
        f.write("def test_x():\n    assert False\n")

    relauncher = tmp_path / "relaunch_stub.py"
    relauncher.write_text("import sys; sys.exit(0)\n")
    monkeypatch.setenv("EVOLUTION_RELAUNCH_SCRIPT", str(relauncher))
    rc, out, err = lib.run(
        [sys.executable, os.path.join(BRIDGE, "rollback.py"),
         "--previous", "--bridge-root", b, "--state-dir", s,
         "--no-restart"], timeout=60)
    assert rc == 0, out + err
    assert not os.path.exists(added), \
        "rollback must remove files the bad version added"
    assert "removed 1 file(s)" in out


def test_rollback_imports_nothing_from_bridge():
    import ast
    tree = ast.parse(open(os.path.join(BRIDGE, "rollback.py")).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert "trade_executor" not in a.name, a.name
                assert a.name != "pipeline", a.name
                assert not a.name.startswith("pipeline."), a.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert "trade_executor" not in mod, mod
            assert mod != "pipeline", mod
            assert not mod.startswith("pipeline."), mod


# ---------------------------------------------------------------- gate order

def _make_version_with_failing_test(b, vid):
    vdir = os.path.join(b, "versions", vid)
    fdir = os.path.join(vdir, "files", "tests")
    os.makedirs(fdir)
    with open(os.path.join(fdir, "test_broken.py"), "w") as f:
        f.write("def test_broken():\n    assert False, 'deliberate'\n")
    manifest.write_version(manifest.new_manifest(
        vid, parent=None, change_summary="deliberate failure",
        proposal_id=None, tier="auto", change_class="refactor",
        files=["tests/test_broken.py"]), b)


def test_failing_gate_a_never_reaches_gate_b(fake_bridge, fake_state,
                                              monkeypatch):
    b, s = fake_bridge, fake_state
    vid = "v0002-fail-a"
    _make_version_with_failing_test(b, vid)
    monkeypatch.setenv("EVOLUTION_BLOCKED_HASH", "none")

    ok, man = run_pipeline(vid, b, state_override=s,
                           soak_signals=1, soak_seconds=5,
                           skip_supervisor=True, no_restart=True)
    assert ok is False
    assert man["status"] == "rejected"
    assert man["gates"]["a"]["ok"] is False
    assert "b" not in man["gates"], \
        "gate (b) must never run when gate (a) fails"
    assert not os.path.exists(
        os.path.join(b, "versions", vid, "staging_report.json"))
    # rejection journaled to the sandbox journal
    assert "pipeline.rejected" in open(
        os.path.join(s, "nova_journal.jsonl")).read()


# ---------------------------------------------------------------- post-install asserts

def test_post_install_asserts_happy(fake_bridge, fake_state, stub_daemon,
                                    stub_supervise, monkeypatch):
    monkeypatch.setenv("EVOLUTION_BLOCKED_HASH",
                       install_mod._blocked_hash_now(fake_state))
    results = install_mod.post_install_asserts(
        fake_bridge, state_override=fake_state, skip_supervisor=False)
    assert all(ok for _n, ok, _d in results)
    names = [n for n, _o, _d in results]
    assert any("trading_enabled" in n for n in names)
    assert any("supervise_stack" in n for n in names)


def test_post_install_asserts_reject_tampered_risk(
        fake_bridge, fake_state, stub_daemon, stub_supervise, monkeypatch):
    cfg = json.load(open(os.path.join(fake_state, "risk_config.json")))
    cfg["risk_per_trade_pct"] = 0.99
    json.dump(cfg, open(os.path.join(fake_state, "risk_config.json"), "w"))
    monkeypatch.setenv("EVOLUTION_BLOCKED_HASH",
                       install_mod._blocked_hash_now(fake_state))
    with pytest.raises(lib.PipelineError):
        install_mod.post_install_asserts(
            fake_bridge, state_override=fake_state, skip_supervisor=True)


def test_post_install_asserts_reject_live_kill_switch(
        fake_bridge, fake_state, stub_daemon, stub_supervise, monkeypatch):
    open(os.path.join(fake_state, "trading_enabled"), "w").write("1")
    monkeypatch.setenv("EVOLUTION_BLOCKED_HASH",
                       install_mod._blocked_hash_now(fake_state))
    with pytest.raises(lib.PipelineError) as e:
        install_mod.post_install_asserts(
            fake_bridge, state_override=fake_state, skip_supervisor=True)
    assert "trading_enabled" in str(e.value)


def test_post_install_asserts_reject_blocked_symbols_change(
        fake_bridge, fake_state, stub_daemon, stub_supervise, monkeypatch):
    monkeypatch.setenv("EVOLUTION_BLOCKED_HASH", "deadbeef")
    with pytest.raises(lib.PipelineError) as e:
        install_mod.post_install_asserts(
            fake_bridge, state_override=fake_state, skip_supervisor=True)
    assert "blocked_symbols" in str(e.value)


def test_post_install_asserts_reject_dead_daemon(
        fake_bridge, fake_state, stub_supervise, monkeypatch):
    # no stub_daemon fixture: nothing alive
    monkeypatch.setenv("EVOLUTION_BLOCKED_HASH",
                       install_mod._blocked_hash_now(fake_state))
    with pytest.raises(lib.PipelineError) as e:
        install_mod.post_install_asserts(
            fake_bridge, state_override=fake_state, skip_supervisor=True)
    assert "daemon" in str(e.value).lower()


# ---------------------------------------------------------------- hard-block scan

def test_hard_block_scan_trips_on_kill_switch_write(tmp_path):
    d = str(tmp_path)
    with open(os.path.join(d, "evil.py"), "w") as f:
        f.write('open("/x/run/trading_enabled", "w").write("1")\n')
    from pipeline import hard_block_scan
    assert hard_block_scan(d)


def test_hard_block_scan_clean(tmp_path):
    d = str(tmp_path)
    with open(os.path.join(d, "fine.py"), "w") as f:
        f.write("X = 1\n")
    from pipeline import hard_block_scan
    assert hard_block_scan(d) == []


def test_zenas_tier_is_hard_blocked(fake_bridge, fake_state):
    # Tier zenas = the two hard-blocked classes: (a) kill-switch resume
    # and (b) blocked_symbols changes. The pipeline refuses these
    # outright -- there is no approval file anymore.
    b, s = fake_bridge, fake_state
    vid = "v0003-zenas"
    vdir = os.path.join(b, "versions", vid)
    os.makedirs(os.path.join(vdir, "files"))
    manifest.write_version(manifest.new_manifest(
        vid, parent=None, change_summary="t", proposal_id="P-ZZZ",
        tier="zenas", change_class="behavior", files=[]), b)
    with pytest.raises(lib.PipelineError) as e:
        run_pipeline(vid, b, state_override=s)
    assert "hard-block" in str(e.value)


def test_zenas_tier_hard_blocked_even_with_approval_file(
        fake_bridge, fake_state):
    # The one-tap approval flow is gone: a zenas_approve.json does NOT
    # unblock a tier-zenas version. These classes are Zenas's own
    # actions, never installed by the pipeline.
    b, s = fake_bridge, fake_state
    vid = "v0003b-zenas"
    vdir = os.path.join(b, "versions", vid)
    os.makedirs(os.path.join(vdir, "files"))
    pdir = os.path.join(b, "proposals", "P-ZZZ")
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, "zenas_approve.json"), "w") as f:
        f.write('{"tap": true}\n')
    manifest.write_version(manifest.new_manifest(
        vid, parent=None, change_summary="t", proposal_id="P-ZZZ",
        tier="zenas", change_class="behavior", files=[]), b)
    with pytest.raises(lib.PipelineError) as e:
        run_pipeline(vid, b, state_override=s)
    assert "hard-block" in str(e.value)


def test_prepare_refuses_zenas_tier_proposal(fake_bridge):
    # A class (a)/(b) proposal never becomes a version: prepare refuses
    # it before any gate runs.
    from pipeline import prepare
    b = fake_bridge
    pdir = os.path.join(b, "proposals", "P-Z")
    os.makedirs(pdir)
    with open(os.path.join(pdir, "proposal.md"), "w") as f:
        f.write("---\nid: P-Z\ntier: zenas\nchange_class: behavior\n"
                "---\n# t\n")
    with pytest.raises(lib.PipelineError) as e:
        prepare.prepare_from_proposal("P-Z", b)
    assert "hard-block" in str(e.value)


def test_kill_switch_resume_version_refused_at_scan(
        fake_bridge, fake_state):
    # Class (a): a version that would resume trading is refused by the
    # pipeline at the pre-install scan gate, before gate (a).
    b, s = fake_bridge, fake_state
    vid = "v0004-resume"
    fdir = os.path.join(b, "versions", vid, "files")
    os.makedirs(fdir)
    with open(os.path.join(fdir, "evil.py"), "w") as f:
        f.write('open("run/trading_enabled", "w").write("1")\n')
    manifest.write_version(manifest.new_manifest(
        vid, parent=None, change_summary="t", proposal_id="P-EVIL",
        tier="auto", change_class="behavior", files=["evil.py"]), b)
    with pytest.raises(lib.PipelineError) as e:
        run_pipeline(vid, b, state_override=s)
    assert "scan" in str(e.value).lower()
    man = manifest.read_version(vid, b)
    assert man["gates"]["scan"]["ok"] is False
    assert "a" not in man["gates"], \
        "gate (a) must never run when the scan gate refuses"
    assert man["status"] == "rejected"


def test_auto_behavior_version_proceeds_past_tier_gate(
        fake_bridge, fake_state):
    # A behavior-change proposal in tier auto is NOT refused at the tier
    # gate: it runs the fixed gate order (scan ok, gate (a) green). In
    # this sandbox it then halts at gate (b) because the fake bridge has
    # no trade_executor for the replay runner -- an environmental limit,
    # not a tier refusal.
    b, s = fake_bridge, fake_state
    vid = "v0005-auto-behavior"
    fdir = os.path.join(b, "versions", vid, "files")
    os.makedirs(fdir)
    with open(os.path.join(fdir, "dummy_mod.py"), "w") as f:
        f.write("VALUE = 2  # harmless behavior-adjacent tweak\n")
    manifest.write_version(manifest.new_manifest(
        vid, parent=None, change_summary="t", proposal_id="P-OK",
        tier="auto", change_class="behavior", files=["dummy_mod.py"]), b)
    with pytest.raises(lib.PipelineHalt) as e:
        run_pipeline(vid, b, state_override=s,
                     soak_signals=1, soak_seconds=5,
                     skip_supervisor=True, no_restart=True)
    assert "hard-block" not in str(e.value)
    assert "replay runner" in str(e.value)
    man = manifest.read_version(vid, b)
    assert man["gates"]["scan"]["ok"] is True
    assert man["gates"]["a"]["ok"] is True, \
        "the full test suite gate must have run green"
