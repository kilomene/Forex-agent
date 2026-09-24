"""Repair round 2 (2026-09-24): the seen-file watchdog finally has a
production caller.

audit_seen_file() used to have zero callers ("not wired"); supervise.py
now calls it on its 5-minute tick via audit_seen_tickets() -- read-only,
findings logged LOUD. These tests pin the wiring without ever launching
daemons (supervise.main() is never called here).
"""
import inspect
import json
import os
import sys

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import supervise  # noqa: E402


def _write(path, text):
    with open(path, "w") as f:
        f.write(text)
    return path


def _sandbox(tmp_path, seen_text='{"tickets": []}'):
    files = tmp_path / "files"
    run = tmp_path / "run"
    files.mkdir()
    run.mkdir()
    _write(str(files / "nova_positions_seen.json"), seen_text)
    _write(str(files / "nova_trades.jsonl"), "")
    _write(str(run / "nova_journal.jsonl"), "")
    return str(files), str(run)


def test_main_tick_calls_seen_audit():
    # Static pin: the supervisor tick must actually invoke the audit.
    src = inspect.getsource(supervise.main)
    assert "audit_seen_tickets()" in src


def test_healthy_seen_file_logs_ok(tmp_path, monkeypatch):
    files, run = _sandbox(tmp_path)
    lines = []
    monkeypatch.setattr(supervise, "log", lines.append)
    supervise.audit_seen_tickets(files_dir=files, run_dir=run)
    assert any("seen-audit: OK" in l for l in lines), lines
    assert not any("FINDING" in l for l in lines), lines


def test_drifted_schema_logs_finding(tmp_path, monkeypatch):
    files, run = _sandbox(tmp_path, seen_text=json.dumps(
        {"seen": [{"ticket": 111}]}))
    lines = []
    monkeypatch.setattr(supervise, "log", lines.append)
    supervise.audit_seen_tickets(files_dir=files, run_dir=run)
    assert any("seen-audit: !!! FINDING" in l for l in lines), lines


def test_missing_seen_file_logs_finding_not_crash(tmp_path, monkeypatch):
    files, run = _sandbox(tmp_path)
    os.remove(os.path.join(files, "nova_positions_seen.json"))
    lines = []
    monkeypatch.setattr(supervise, "log", lines.append)
    supervise.audit_seen_tickets(files_dir=files, run_dir=run)  # no raise
    assert any("seen-audit:" in l for l in lines), lines
