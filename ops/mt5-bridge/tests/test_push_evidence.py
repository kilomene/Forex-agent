"""Adversarial tests for push_evidence.py (Racheal engagement 007).

The day-30 evidence push must land a ~47 MB journal in the public evidence
repo. The previous Contents-API implementation could not do that, the
username-pathed repo check flapped, and the weekly watermark advanced on
failure. These tests prove the rebuilt Git-Data-API push path:

- watermark advances ONLY on a verified successful push
- oversize blobs are gzipped and stored as <name>.gz
- repo-ID resolution recovers from the username-path 404 flap
- transient network failures are retried, then fail loudly
- the commit is built correctly and the ref is verified after update
- credential-like content can never reach the public repo
"""
import gzip
import io
import json
import os
import sys
import urllib.error

import pytest

import push_evidence as pe


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "run"
    d.mkdir()
    monkeypatch.setattr(pe, "STATE_DIR", str(d))
    monkeypatch.setattr(pe, "JOURNAL_PATH", str(d / "nova_journal.jsonl"))
    monkeypatch.setattr(pe, "WATERMARK_PATH",
                        str(d / "evidence_push.state.json"))
    monkeypatch.setattr(pe, "_repo_id", 12345678)
    return d


@pytest.fixture
def journal(state_dir):
    p = state_dir / "nova_journal.jsonl"
    lines = ['{"type": "trade.closed", "ticket": %d}\n' % i for i in range(10)]
    p.write_text("".join(lines))
    return p


def _write_watermark(state_dir, n):
    (state_dir / "evidence_push.state.json").write_text(
        json.dumps({"journal_lines": n}))


def _read_watermark(state_dir):
    return json.loads(
        (state_dir / "evidence_push.state.json").read_text())


# --- BUG-3: watermark must advance only on verified success -------------

def test_watermark_not_advanced_on_push_failure(state_dir, journal,
                                                monkeypatch):
    _write_watermark(state_dir, 4)
    monkeypatch.setattr(pe, "push_commit", lambda files, msg: (False, None))
    assert pe.cmd_weekly() == 1
    assert _read_watermark(state_dir)["journal_lines"] == 4


def test_watermark_advanced_on_push_success(state_dir, journal, monkeypatch):
    _write_watermark(state_dir, 4)
    seen = {}

    def fake_push(files, msg):
        seen.update(files)
        return True, "deadbeef" * 5

    monkeypatch.setattr(pe, "push_commit", fake_push)
    assert pe.cmd_weekly() == 0
    wm = _read_watermark(state_dir)
    assert wm["journal_lines"] == 10
    assert wm["commit"] == "deadbeef"
    assert any(k.startswith("evidence/journal_") for k in seen)


def test_weekly_no_new_content_pushes_nothing(state_dir, journal,
                                              monkeypatch):
    _write_watermark(state_dir, 10)  # journal fully consumed
    called = []
    monkeypatch.setattr(pe, "push_commit",
                        lambda files, msg: called.append(files) or (True, "x"))
    assert pe.cmd_weekly() == 0
    assert called == []


def test_weekly_docs_only_keeps_journal_watermark(state_dir, journal,
                                                  monkeypatch):
    _write_watermark(state_dir, 10)
    (state_dir / "reflection_2026-10-18.md").write_text("# r\n")
    monkeypatch.setattr(pe, "push_commit", lambda files, msg: (True, "abc"))
    assert pe.cmd_weekly() == 0
    wm = _read_watermark(state_dir)
    assert wm["journal_lines"] == 10  # docs pushed, journal cursor untouched


# --- BUG-1: oversize blobs gzipped ---------------------------------------

def test_prepare_blob_gzips_over_threshold(monkeypatch):
    monkeypatch.setattr(pe, "BLOB_GZIP_THRESHOLD", 100)
    content = b'{"a": 1}\n' * 50  # 450 bytes of compressible JSONL
    stored, payload, was_gz = pe.prepare_blob("evidence/x.jsonl", content)
    assert was_gz
    assert stored == "evidence/x.jsonl.gz"
    raw = (payload["content"].encode()
           if payload["encoding"] == "utf-8"
           else __import__("base64").b64decode(payload["content"]))
    assert gzip.decompress(raw) == content


def test_prepare_blob_small_stays_plain(monkeypatch):
    monkeypatch.setattr(pe, "BLOB_GZIP_THRESHOLD", 100)
    stored, payload, was_gz = pe.prepare_blob("evidence/x.jsonl", b"small")
    assert not was_gz
    assert stored == "evidence/x.jsonl"
    assert payload["encoding"] == "utf-8"


# --- BUG-2: repo-ID resolution survives the 404 flap ----------------------

def test_resolve_repo_id_recovers_from_flap(monkeypatch):
    calls = []

    def fake_gh(method, path, payload=None, timeout=60):
        calls.append((method, path))
        if path.startswith("/repos/"):
            # first GET flaps 404, second succeeds after the 422
            if sum(1 for c in calls if c[1].startswith("/repos/")) == 1:
                return 404, {"http_error": 404, "body": "flap"}
            return 200, {"id": 999}
        if path == "/user/repos":
            return 422, {"http_error": 422, "body": "name already exists"}
        raise AssertionError(path)

    monkeypatch.setattr(pe, "_repo_id", None)
    monkeypatch.setattr(pe, "gh_api", fake_gh)
    assert pe.resolve_repo_id() == 999
    assert pe.rpath("/git/refs/heads/main") == \
        "/repositories/999/git/refs/heads/main"


def test_resolve_repo_id_creates_when_truly_missing(monkeypatch):
    def fake_gh(method, path, payload=None, timeout=60):
        if path.startswith("/repos/"):
            return 404, {"http_error": 404, "body": "not found"}
        if path == "/user/repos":
            return 201, {"id": 1001}
        raise AssertionError(path)

    monkeypatch.setattr(pe, "_repo_id", None)
    monkeypatch.setattr(pe, "gh_api", fake_gh)
    assert pe.resolve_repo_id() == 1001


# --- transient retry -----------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self.status = 200
        self._payload = json.dumps(payload).encode()
        self._served = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, size=-1):
        if self._served:
            return b""
        self._served = True
        return self._payload if size in (-1, None) else self._payload[:size]


def test_gh_api_retries_transient_then_succeeds(monkeypatch):
    attempts = []

    def fake_urlopen(req, timeout=60):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionResetError("proxy flap")
        return _FakeResp({"ok": True})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(pe, "time", pe.time)  # keep real backoff module
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    status, data = pe.gh_api("GET", "/repositories/1/git/refs/heads/main")
    assert status == 200 and data == {"ok": True}
    assert len(attempts) == 3


def test_gh_api_raises_loudly_when_network_stays_down(monkeypatch):
    def fake_urlopen(req, timeout=60):
        raise ConnectionResetError("dead")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    with pytest.raises(pe.PushError):
        pe.gh_api("GET", "/x")


# --- commit construction + ref verification -------------------------------

def _fake_github(monkeypatch, ref_after="C" * 40, fail_at=None):
    """Full fake Git Data API. fail_at: step name to force-fail."""
    calls = []
    TIP = "A" * 40
    COMMIT = "C" * 40

    def fake_gh(method, path, payload=None, timeout=60):
        calls.append((method, path))
        if fail_at == "ref_read":
            return 500, {"http_error": 500, "body": "x"}
        if method == "GET" and path.endswith("/git/refs/heads/main"):
            sha = ref_after if sum(
                1 for c in calls if c[0] == "GET") > 1 else TIP
            return 200, {"object": {"sha": sha}}
        if method == "GET" and "/git/commits/" in path:
            return 200, {"tree": {"sha": "T" * 40}}
        if method == "POST" and path.endswith("/git/blobs"):
            if fail_at == "blob":
                return 422, {"http_error": 422, "body": "too large"}
            return 201, {"sha": "B" * 40}
        if method == "POST" and path.endswith("/git/trees"):
            return 201, {"sha": "T2" * 20}
        if method == "POST" and path.endswith("/git/commits"):
            return 201, {"sha": COMMIT}
        if method == "PATCH":
            return 200, {"object": {"sha": COMMIT}}
        raise AssertionError((method, path))

    monkeypatch.setattr(pe, "gh_api", fake_gh)
    monkeypatch.setattr(pe, "_repo_id", 424242)
    return calls, TIP, COMMIT


def test_push_commit_builds_verified_commit(monkeypatch):
    calls, TIP, COMMIT = _fake_github(monkeypatch)
    ok, sha = pe.push_commit({"evidence/a.jsonl": b'{"x":1}\n'}, "msg")
    assert ok and sha == COMMIT
    methods = [c[0] for c in calls]
    assert methods.count("POST") >= 3  # blob, tree, commit
    assert ("PATCH", "/repositories/424242/git/refs/heads/main") in calls
    # every route is repo-ID-pathed
    assert all(c[1].startswith("/repositories/424242/") for c in calls)


def test_push_commit_blob_failure_aborts_cleanly(monkeypatch):
    calls, _, _ = _fake_github(monkeypatch, fail_at="blob")
    ok, sha = pe.push_commit({"evidence/a.jsonl": b"data"}, "msg")
    assert (ok, sha) == (False, None)
    assert not any(c[0] == "PATCH" for c in calls)


def test_push_commit_ref_mismatch_rebuilds_then_fails(monkeypatch):
    # ref never equals our commit: two rebuild attempts, then give up
    calls, _, _ = _fake_github(monkeypatch, ref_after="D" * 40)
    ok, sha = pe.push_commit({"evidence/a.jsonl": b"data"}, "msg")
    assert (ok, sha) == (False, None)
    patches = [c for c in calls if c[0] == "PATCH"]
    assert len(patches) == 2  # initial + one rebuild, then stop


# --- secrets guard --------------------------------------------------------

def test_secrets_guard_blocks_credential_like_content():
    with pytest.raises(pe.SecretsLeakBlocked):
        pe.secrets_guard(b'config api_key = "sk-live-abcdef123456"\n', "j")
    with pytest.raises(pe.SecretsLeakBlocked):
        pe.secrets_guard(b"-----BEGIN RSA PRIVATE KEY-----\n...", "j")


def test_secrets_guard_passes_clean_journal():
    pe.secrets_guard(
        b'{"type": "trade.closed", "ticket": 123, "profit": -120.5}\n', "j")


def test_push_commit_runs_secrets_guard(monkeypatch):
    calls, _, _ = _fake_github(monkeypatch)
    ok, _ = pe.push_commit(
        {"evidence/a.jsonl": b'password = "hunter2hunter2"\n'}, "msg")
    assert ok is False
    assert not any(c[0] == "PATCH" for c in calls)


# --- cmd_final ------------------------------------------------------------

def test_cmd_final_requires_final_report(state_dir, journal, monkeypatch):
    monkeypatch.setattr(pe, "push_commit",
                        lambda files, msg: (_ for _ in ()).throw(
                            AssertionError("must not push")))
    assert pe.cmd_final() == 1  # FINAL_REPORT.md absent


def test_cmd_final_pushes_journal_and_report(state_dir, journal,
                                             monkeypatch):
    (state_dir / "FINAL_REPORT.md").write_text("# final\n")
    seen = {}
    monkeypatch.setattr(
        pe, "push_commit",
        lambda files, msg: (seen.update(files), (True, "f" * 40))[1])
    assert pe.cmd_final() == 0
    assert "evidence/nova_journal_full.jsonl" in seen
    assert "evidence/FINAL_REPORT.md" in seen
