#!/usr/bin/env python3
"""
Nova experiment evidence pusher.

Pushes trader code, the charter, weekly journal slices, and the day-30
full journal + FINAL_REPORT.md to the public evidence repo
kilomene/forex-30day-trading-experiment via the GitHub **Git Data API**,
using the stored custom.github credential (surrogate helpers -- the raw
token is never read, printed, or logged here).

Rebuilt 2026-10-18 (Racheal engagement 007): the previous Contents-API
implementation could not work -- a single PUT cannot carry payloads above
~1 MB in this environment, the username-pathed repo check 404s
intermittently for this account (which then triggered a doomed repo
re-creation attempt), and the weekly watermark advanced even when the push
failed, silently skipping those journal lines forever. Every push here is
now a single verified commit: blobs (gzipped past 30 MB) -> tree ->
commit -> ref update -> ref re-verified. The watermark advances only on a
verified successful push.

Usage:
  push_evidence.py --init     create repo (if needed) + push code/charter/README
  push_evidence.py --weekly   push this week's journal slice + review/reflection files
  push_evidence.py --final    push the journal + FINAL_REPORT.md (day 30)

Env:
  TRADER_STATE_DIR  state dir (default: <bridge dir>/run)
"""

import base64
import gzip
import json
import os
import re
import socket
import sys
import time
import urllib.request
import urllib.error
import http.client
from datetime import datetime, timezone

def _credentials():
    """Lazy import of the host credential helper.

    dynamic_credentials only exists on the Nova host (connector surrogate
    auth); importing it at module level broke `import push_evidence` (and
    therefore the test-suite collection) on any other machine, including
    CI. It is only needed on the single code path that actually talks to
    api.github.com, so it is imported here, on demand.
    """
    # dynamic_credentials moved out of the old skill-creator path; probe the
    # live locations first, legacy path last.
    for _p in ("/opt/muse/skills/custom-connector-setup/bin",
               "/opt/hatch/workflow-skills/custom-connector-setup/bin",
               "/opt/hatch/skills/skill-creator/bin"):
        if os.path.isdir(_p):
            sys.path.insert(0, _p)
            break
    from dynamic_credentials import (  # noqa: E402
        add_surrogate_to_request, read_json_response)
    return add_surrogate_to_request, read_json_response

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("TRADER_STATE_DIR", os.path.join(BASE, "run"))
JOURNAL_PATH = os.path.join(STATE_DIR, "nova_journal.jsonl")
WATERMARK_PATH = os.path.join(STATE_DIR, "evidence_push.state.json")

OWNER = "kilomene"
REPO = "forex-30day-trading-experiment"
BRANCH = "main"
ALLOWED = ["api.github.com"]
API = "https://api.github.com"
CRED = "custom.github"
UA = "NovaWorks/1.0"

# GitHub's git-blobs endpoint rejects payloads above ~40-44 MB with 422
# ("input was too large to process"). Files bigger than this are gzipped
# before upload and stored as <name>.gz in the tree (JSONL compresses
# ~10:1, so this buys a large margin). Same convention as the daily
# evidence pusher (gh-push-evidence-daily.py).
BLOB_GZIP_THRESHOLD = 30 * 1024 * 1024

# Network retries: api.github.com intermittently drops connections behind
# the egress proxy. HTTP error codes are NOT retried blindly here -- 429
# and 5xx are (with backoff), other 4xx are returned to the caller.
MAX_ATTEMPTS = 5
TRANSIENT_EXC = (
    http.client.RemoteDisconnected, ConnectionResetError,
    ConnectionAbortedError, BrokenPipeError, TimeoutError,
    socket.timeout, urllib.error.URLError,
)

_repo_id = None  # resolved once per process via resolve_repo_id()


class PushError(RuntimeError):
    """A push step failed loudly; nothing was advanced."""


class SecretsLeakBlocked(PushError):
    """Credential-like content was about to be pushed to the public repo."""


def gh_api(method, path, payload=None, timeout=60):
    """(status, data) with retry on transient failures.

    HTTP errors are returned as (code, {"http_error": code, "body": ...})
    so callers implement explicit status logic. Raises PushError only when
    the network stays down through all attempts.
    """
    add_surrogate_to_request, read_json_response = _credentials()
    url = f"{API}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    last_exc = None
    for attempt in range(MAX_ATTEMPTS):
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"User-Agent": UA,
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json"})
        add_surrogate_to_request(req, CRED, allowed_hosts=ALLOWED)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, read_json_response(resp)
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode()[:500]
            except Exception:
                body = ""
            if e.code == 429 or 500 <= e.code < 600:
                last_exc = e
                time.sleep(2 ** attempt)
                continue
            return e.code, {"http_error": e.code, "body": body}
        except TRANSIENT_EXC as e:
            last_exc = e
            time.sleep(2 ** attempt)
    raise PushError(
        f"{method} {path} -> still failing after {MAX_ATTEMPTS} attempts: "
        f"{last_exc}")


def resolve_repo_id():
    """Numeric repo id, via username-pathed GET with flap recovery.

    Username-pathed routes 404 intermittently for this account (est.
    2026-10-11). A 404 therefore means "maybe missing, maybe flap": try to
    create, and on 422 (name already taken) treat it as a flap and
    re-resolve. All later calls use the repo-ID-pathed routes, which do
    not flap.
    """
    global _repo_id
    if _repo_id is not None:
        return _repo_id
    for _ in range(3):
        status, data = gh_api("GET", f"/repos/{OWNER}/{REPO}")
        if status == 200 and isinstance(data, dict) and "id" in data:
            _repo_id = data["id"]
            return _repo_id
        if status == 404:
            status_c, data_c = gh_api("POST", "/user/repos", {
                "name": REPO, "private": False,
                "description": "30-day autonomous demo trading experiment "
                               "(2026-09-21 to 2026-10-21): every signal, "
                               "every decision, every trade, wins and "
                               "losses. MetaQuotes-Demo, no real money.",
                "auto_init": False,
            })
            if status_c in (200, 201) and isinstance(data_c, dict):
                _repo_id = data_c.get("id")
                if _repo_id:
                    return _repo_id
            # 422 "name already exists" (or any create failure after a 404)
            # is the known flap signature: the repo exists, the GET lied.
            time.sleep(2)
            continue
        raise PushError(f"repo resolve failed: GET /repos/{OWNER}/{REPO} "
                        f"-> {status} {data}")
    raise PushError("repo resolve failed: persistent 404 flap")


def rpath(path):
    """Repo-ID-pathed route (immune to the username-path 404 flap)."""
    return f"/repositories/{resolve_repo_id()}{path}"


# Credential-like patterns must never reach the public evidence repo.
# The journal is safe today (audited 2026-10-18, zero matches); this is
# defense in depth so a future journaled field can never leak silently.
_SECRET_RES = [
    re.compile(r"(?i)(api[_-]?key|secret|passwd|password|bearer|"
               r"private[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+]{8,}"),
    re.compile(r"-----BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY-----"),
]


def secrets_guard(content, where):
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    for rx in _SECRET_RES:
        m = rx.search(text)
        if m:
            raise SecretsLeakBlocked(
                f"refusing to push {where}: credential-like pattern "
                f"matched ({m.group(0)[:24]}...)")


def prepare_blob(repo_path, content):
    """(stored_path, blob_payload, gzipped). Gzips past the threshold."""
    stored_path = repo_path
    if len(content) > BLOB_GZIP_THRESHOLD:
        content = gzip.compress(content)
        stored_path = repo_path + ".gz"
    try:
        payload = {"content": content.decode("utf-8"), "encoding": "utf-8"}
    except UnicodeDecodeError:
        payload = {"content": base64.b64encode(content).decode(),
                   "encoding": "base64"}
    return stored_path, payload, stored_path != repo_path


def push_commit(files, message):
    """Push {repo_path: bytes} as ONE verified commit. Returns (ok, sha).

    Steps: read ref -> read base tree -> upload blobs -> create tree ->
    create commit -> update ref -> re-read ref and verify it equals the new
    commit. Any failure returns (False, None) and advances nothing. On a
    ref race (someone else moved main under us) the whole push is rebuilt
    once on the new tip.
    """
    if not files:
        return False, None
    for _ in range(2):  # one rebuild on a ref race
        status, ref = gh_api("GET", rpath(f"/git/refs/heads/{BRANCH}"))
        if status != 200:
            print(f"push aborted: cannot read ref: {ref}", file=sys.stderr)
            return False, None
        tip = ref["object"]["sha"]
        status, commit_obj = gh_api("GET", rpath(f"/git/commits/{tip}"))
        if status != 200 or "tree" not in commit_obj:
            print(f"push aborted: cannot read tip commit: {commit_obj}",
                  file=sys.stderr)
            return False, None
        base_tree = commit_obj["tree"]["sha"]

        tree = []
        gzipped = []
        try:
            for repo_path, content in files.items():
                secrets_guard(content, repo_path)
                stored_path, blob, was_gz = prepare_blob(repo_path, content)
                if was_gz:
                    gzipped.append(stored_path)
                status, b = gh_api("POST", rpath("/git/blobs"), blob)
                if status != 201 or "sha" not in b:
                    print(f"push aborted: blob upload failed for "
                          f"{repo_path}: {b}", file=sys.stderr)
                    return False, None
                tree.append({"path": stored_path, "mode": "100644",
                             "type": "blob", "sha": b["sha"]})
        except SecretsLeakBlocked as e:
            print(f"push aborted: {e}", file=sys.stderr)
            return False, None

        full_message = message
        if gzipped:
            full_message += ("\n\nLarge files gzipped over the "
                             f"{BLOB_GZIP_THRESHOLD}-byte blob threshold: "
                             + ", ".join(gzipped))
        status, tree_obj = gh_api(
            "POST", rpath("/git/trees"),
            {"base_tree": base_tree, "tree": tree})
        if status != 201 or "sha" not in tree_obj:
            print(f"push aborted: tree creation failed: {tree_obj}",
                  file=sys.stderr)
            return False, None
        status, commit = gh_api("POST", rpath("/git/commits"), {
            "message": full_message,
            "tree": tree_obj["sha"],
            "parents": [tip],
            "author": {"name": "Nova",
                      "email": "nova@forex-agent.local"},
        })
        if status != 201 or "sha" not in commit:
            print(f"push aborted: commit failed: {commit}", file=sys.stderr)
            return False, None
        new_sha = commit["sha"]
        status, _ = gh_api("PATCH", rpath(f"/git/refs/heads/{BRANCH}"),
                           {"sha": new_sha})
        if status != 200:
            print(f"push aborted: ref update failed", file=sys.stderr)
            return False, None
        # Verify the ref actually landed where we put it.
        status, ref2 = gh_api("GET", rpath(f"/git/refs/heads/{BRANCH}"))
        if status == 200 and ref2["object"]["sha"] == new_sha:
            print(f"pushed commit {new_sha[:8]} ({len(tree)} files)")
            return True, new_sha
        print("push aborted: ref verification mismatch (race); "
              "rebuilding on new tip", file=sys.stderr)
    return False, None


def load_watermark():
    try:
        with open(WATERMARK_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"journal_lines": 0}


def save_watermark(wm):
    tmp = WATERMARK_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(wm, f)
    os.replace(tmp, WATERMARK_PATH)


TRADER_FILES = [
    "trade_executor.py", "market_hours.py", "NovaTrader.mq5",
    "tg_commands.py", "review_trades.py", "daily_summary.py",
    "weekend_reflection.py", "final_report.py", "push_evidence.py",
    "performance.py", "protections.py", "evolve.py", "pipeline.py",
    "rollback.py", "prereg.py", "self_tune.py", "backup_trading.py",
    "signal_bridge.py", "tg_subscribers.py", "chat_notify.py",
    "tests/test_trader.py", "tests/test_market_hours.py",
    "tests/test_experiment.py", "tests/test_performance.py",
    "tests/test_protections.py", "tests/test_protection_parity.py",
    "tests/test_protection_wired.py", "tests/test_evolution.py",
    "tests/test_preregistration.py", "tests/test_close_dedup.py",
    "tests/test_in_flight_gate.py", "tests/test_push_evidence.py",
]

README_TEXT = """# 30-Day Autonomous Demo Trading Experiment

**2026-09-21 → 2026-10-21.** A MetaTrader 5 demo account (MetaQuotes-Demo,
no real money) trades signals from the NovaSignals EA
(EMA20/EMA50 cross + RSI, M15, 77 symbols) through a risk-gated autonomous
executor.

Everything is logged: `evidence/` holds weekly journal slices, review
proposals, reflections, and the day-30 `FINAL_REPORT.md` — wins **and**
losses, exactly as they happened.

- [EXPERIMENT_CHARTER.md](EXPERIMENT_CHARTER.md) — the rules (risk bounds,
  market hours, kill-switch discipline, learning loop)
- `trader/` — the executor, market-hours gate, NovaTrader EA source,
  review/reflection/report tooling, and the full test suite
- `evidence/` — the audit trail

The goal was for wins to outweigh losses. The final report shows what
actually happened — no cherry-picking, no fabrication.
"""


def _read_local(name):
    local = os.path.join(BASE, name)
    if not os.path.exists(local):
        print(f"skip missing {name}")
        return None
    with open(local, "rb") as f:
        return f.read()


def cmd_init():
    try:
        resolve_repo_id()
    except PushError as e:
        print(f"init failed: {e}", file=sys.stderr)
        return 1
    files = {"README.md": README_TEXT.encode()}
    charter = _read_local("EXPERIMENT_CHARTER.md")
    if charter is None:
        return 1
    files["EXPERIMENT_CHARTER.md"] = charter
    # Never stage state/secret/binary artifacts: refuse .db/.sqlite/.so/.env
    # in the git-bound file list (defense in depth on top of .gitignore).
    from backup_trading import refuse_git_paths
    refuse_git_paths(TRADER_FILES, where="push_evidence TRADER_FILES")
    for name in TRADER_FILES:
        content = _read_local(name)
        if content is not None:
            files[f"trader/{name}"] = content
    ok, _ = push_commit(files, "experiment: init code/charter/README")
    return 0 if ok else 1


def journal_slice(since_line):
    try:
        with open(JOURNAL_PATH, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return [], 0
    return lines[since_line:], len(lines)


def _doc_files():
    """Review proposals + reflections waiting in the state dir."""
    out = {}
    try:
        names = sorted(os.listdir(STATE_DIR))
    except OSError:
        return out
    for name in names:
        if ((name.startswith("review_proposal_") or
             name.startswith("reflection_")) and name.endswith(".md")):
            with open(os.path.join(STATE_DIR, name), "rb") as f:
                out[f"evidence/{name}"] = f.read()
    return out


def cmd_weekly():
    try:
        resolve_repo_id()
    except PushError as e:
        print(f"weekly push failed: {e}", file=sys.stderr)
        return 1
    wm = load_watermark()
    new_lines, total = journal_slice(wm.get("journal_lines", 0))
    files = {}
    week = datetime.now(timezone.utc).date().isoformat()
    if new_lines:
        files[f"evidence/journal_{week}.jsonl"] = "".join(
            new_lines).encode()
    files.update(_doc_files())
    if not files:
        print("no new journal lines or documents to push")
        return 0
    msg = (f"experiment: weekly evidence {week} "
           f"({len(new_lines)} journal events, "
           f"{len(files) - (1 if new_lines else 0)} documents)")
    ok, sha = push_commit(files, msg)
    if not ok:
        # Watermark deliberately NOT advanced: the lines stay queued for
        # the next run instead of being skipped forever.
        return 1
    if new_lines:
        wm["journal_lines"] = total
    wm["pushed_at"] = datetime.now(timezone.utc).isoformat()
    wm["commit"] = sha[:8]
    save_watermark(wm)
    return 0


def cmd_final():
    try:
        resolve_repo_id()
    except PushError as e:
        print(f"final push failed: {e}", file=sys.stderr)
        return 1
    try:
        with open(JOURNAL_PATH, "rb") as f:
            journal = f.read()
    except OSError as e:
        print(f"no journal to push: {e}", file=sys.stderr)
        return 1
    report = os.path.join(STATE_DIR, "FINAL_REPORT.md")
    if not os.path.exists(report):
        print("FINAL_REPORT.md not found; run final_report.py first",
              file=sys.stderr)
        return 1
    with open(report, "rb") as f:
        report_bytes = f.read()
    ok, sha = push_commit(
        {"evidence/nova_journal_full.jsonl": journal,
         "evidence/FINAL_REPORT.md": report_bytes},
        f"experiment day 30: full journal ({len(journal)} bytes) + "
        f"final report")
    if not ok:
        return 1
    print(f"day-30 evidence landed as {sha[:8]}")
    return 0


def main(argv):
    try:
        if "--init" in argv:
            return cmd_init()
        if "--weekly" in argv:
            return cmd_weekly()
        if "--final" in argv:
            return cmd_final()
    except PushError as e:
        print(f"push_evidence failed: {e}", file=sys.stderr)
        return 1
    print("usage: push_evidence.py --init | --weekly | --final",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
