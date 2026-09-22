"""Tests for state_store.py: durable program state in SQLite."""

import json
import os
import sys

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

from state_store import StateStore  # noqa: E402


@pytest.fixture()
def store(tmp_path):
    s = StateStore(db_path=str(tmp_path / "state.db"))
    yield s
    s.close()


def test_kv_roundtrip(store):
    store.set_text("trading_enabled", "1")
    assert store.get_text("trading_enabled") == "1"
    assert store.get_int("bridge:offset", 0) == 0
    store.set_int("bridge:offset", 34310)
    assert store.get_int("bridge:offset") == 34310


def test_kill_switch_defaults_off_and_toggles(store, tmp_path):
    assert store.kill_switch_on(state_dir=str(tmp_path)) is False
    store.set_kill_switch(True)
    assert store.kill_switch_on(state_dir=str(tmp_path)) is True
    store.set_kill_switch(False)
    assert store.kill_switch_on(state_dir=str(tmp_path)) is False


def test_kill_switch_imports_legacy_file_once(store, tmp_path):
    legacy = tmp_path / "trading_enabled"
    legacy.write_text("1")
    assert store.kill_switch_on(state_dir=str(tmp_path)) is True
    # legacy file must be retired so it cannot silently come back
    assert not legacy.exists()
    assert (tmp_path / "trading_enabled.migrated").exists()


def test_seen_dedup(store):
    assert store.seen_add("bridge:seen", "abc") is True
    assert store.seen_add("bridge:seen", "abc") is False
    assert store.seen_items("bridge:seen") == ["abc"]


def test_seen_prune_keeps_newest(store):
    for i in range(510):
        store.seen_add("x", f"item-{i:04d}")
    store.seen_prune("x", keep=500)
    items = store.seen_items("x")
    assert len(items) == 500
    assert items[0] == "item-0010"
    assert items[-1] == "item-0509"


def test_docs_roundtrip(store):
    store.set_doc("exec:cmd_index", {"a": {"x": 1}})
    assert store.get_doc("exec:cmd_index") == {"a": {"x": 1}}
    assert store.get_doc("missing") is None


def test_component_save_load_roundtrip(store, tmp_path):
    state = {"offset": 12, "seen": ["s1", "s2"],
             "trades_offset": 34, "trades_seen": ["t1"],
             "cmd_index": {"c": {}}, "open_map": {"o": 1},
             "config_sig": "sig", "config_applied": {"a": 1}}
    store.save_component("executor", state)
    loaded = store.load_component("executor", state_dir=str(tmp_path))
    assert loaded["offset"] == 12
    assert loaded["seen"] == ["s1", "s2"]
    assert loaded["trades_offset"] == 34
    assert loaded["trades_seen"] == ["t1"]
    assert loaded["cmd_index"] == {"c": {}}
    assert loaded["open_map"] == {"o": 1}
    assert loaded["config_sig"] == "sig"
    assert loaded["config_applied"] == {"a": 1}


def test_legacy_migration_imports_and_retires(store, tmp_path):
    legacy = tmp_path / "trade_executor.state.json"
    legacy.write_text(json.dumps({
        "offset": 34310,
        "seen": ["AUDJPY_M15_BUY_1", ["legacy", "tuple", "key"]],
        "trades_offset": 9024,
        "trades_seen": [],
        "cmd_index": {"cmd-1": {"symbol": "EURUSD"}},
        "open_map": {},
        "config_sig": "sig",
        "config_applied": {"trading_enabled": True},
    }))
    loaded = store.load_component("executor", state_dir=str(tmp_path))
    assert loaded["offset"] == 34310
    # legacy tuple-form keys are normalized to strings, not crashed on
    assert "legacy|tuple|key" in loaded["seen"]
    assert "AUDJPY_M15_BUY_1" in loaded["seen"]
    assert loaded["trades_offset"] == 9024
    assert loaded["cmd_index"] == {"cmd-1": {"symbol": "EURUSD"}}
    assert not legacy.exists()
    assert (tmp_path / "trade_executor.state.json.migrated").exists()
    # second load must not re-import (idempotent)
    loaded2 = store.load_component("executor", state_dir=str(tmp_path))
    assert loaded2["offset"] == 34310


def test_siggen_doc_component(store, tmp_path):
    store.save_component("siggen", {"EURUSD_last_ts": 123})
    assert store.load_component("siggen", state_dir=str(tmp_path)) == {
        "EURUSD_last_ts": 123}


def test_dry_run_never_persists(tmp_path):
    s = StateStore(db_path=str(tmp_path / "state.db"), dry_run=True)
    try:
        s.set_text("k", "v")
        s.set_int("n", 5)
        s.seen_add("set", "item")
        s.set_doc("d", {"a": 1})
        s.save_component("chat_notify", {"offset": 9, "seen": ["z"]})
        s.set_kill_switch(True)
    finally:
        s.close()
    check = StateStore(db_path=str(tmp_path / "state.db"))
    try:
        assert check.get_text("k") is None
        assert check.get_int("n", 0) == 0
        assert check.seen_items("set") == []
        assert check.get_doc("d") is None
        assert check.load_component(
            "chat_notify", state_dir=str(tmp_path))["offset"] == 0
        assert check.kill_switch_on(state_dir=str(tmp_path)) is False
    finally:
        check.close()


def test_missing_db_dir_is_created(tmp_path):
    deep = tmp_path / "a" / "b" / "state.db"
    s = StateStore(db_path=str(deep))
    try:
        s.set_text("k", "v")
        assert s.get_text("k") == "v"
    finally:
        s.close()
