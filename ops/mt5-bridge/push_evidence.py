#!/usr/bin/env python3
"""
Nova experiment evidence pusher.

Pushes trader code, the charter, and weekly journal slices to the public
evidence repo kilomene/forex-30day-trading-experiment using the GitHub
REST API with the stored custom.github credential (surrogate helpers --
the raw token is never read, printed, or logged here).

Usage:
  push_evidence.py --init     create repo (if needed) + push code/charter/README
  push_evidence.py --weekly   push this week's journal slice + review/reflection files
  push_evidence.py --final    push the journal + FINAL_REPORT.md (day 30)

Env:
  TRADER_STATE_DIR  state dir (default: <bridge dir>/run)
"""

import base64
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
from dynamic_credentials import (  # noqa: E402
    add_surrogate_to_request, read_json_response)

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("TRADER_STATE_DIR", os.path.join(BASE, "run"))
JOURNAL_PATH = os.path.join(STATE_DIR, "nova_journal.jsonl")
WATERMARK_PATH = os.path.join(STATE_DIR, "evidence_push.state.json")

OWNER = "kilomene"
REPO = "forex-30day-trading-experiment"
ALLOWED = ["api.github.com"]
CRED = "custom.github"


def gh_api(method, path, payload=None):
    url = f"https://api.github.com{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"User-Agent": "NovaWorks/1.0",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    add_surrogate_to_request(req, CRED, allowed_hosts=ALLOWED)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, read_json_response(resp)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()[:500]
        except Exception:
            body = ""
        return e.code, {"http_error": e.code, "body": body}


def ensure_repo():
    status, _ = gh_api("GET", f"/repos/{OWNER}/{REPO}")
    if status == 200:
        return True
    if status == 404:
        status, data = gh_api("POST", "/user/repos", {
            "name": REPO, "private": False,
            "description": "30-day autonomous demo trading experiment "
                           "(2026-09-21 to 2026-10-21): every signal, every "
                           "decision, every trade, wins and losses. "
                           "MetaQuotes-Demo, no real money.",
            "auto_init": False,
        })
        if status in (200, 201):
            print(f"created repo {OWNER}/{REPO}")
            return True
        print(f"repo creation failed: {status} {data}", file=sys.stderr)
        return False
    print(f"repo check failed: {status}", file=sys.stderr)
    return False


def put_file(repo_path, content_bytes, message):
    content_b64 = base64.b64encode(content_bytes).decode()
    status, existing = gh_api(
        "GET", f"/repos/{OWNER}/{REPO}/contents/{repo_path}")
    sha = existing.get("sha") if status == 200 else None
    payload = {"message": message, "content": content_b64}
    if sha:
        payload["sha"] = sha
    status, data = gh_api(
        "PUT", f"/repos/{OWNER}/{REPO}/contents/{repo_path}", payload)
    if status in (200, 201):
        print(f"pushed {repo_path}")
        return True
    print(f"push failed for {repo_path}: {status} {data}",
          file=sys.stderr)
    return False


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
    "tests/test_trader.py", "tests/test_market_hours.py",
    "tests/test_experiment.py",
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


def cmd_init():
    if not ensure_repo():
        return 1
    ok = True
    ok &= put_file("README.md", README_TEXT.encode(),
                   "experiment: README")
    with open(os.path.join(BASE, "EXPERIMENT_CHARTER.md"), "rb") as f:
        ok &= put_file("EXPERIMENT_CHARTER.md", f.read(),
                       "experiment: charter")
    # Never stage state/secret/binary artifacts: refuse .db/.sqlite/.so/.env
    # in the git-bound file list (defense in depth on top of .gitignore).
    from backup_trading import refuse_git_paths
    refuse_git_paths(TRADER_FILES, where="push_evidence TRADER_FILES")
    for name in TRADER_FILES:
        local = os.path.join(BASE, name)
        if not os.path.exists(local):
            print(f"skip missing {name}")
            continue
        with open(local, "rb") as f:
            ok &= put_file(f"trader/{name}", f.read(),
                           f"experiment: trader/{name}")
    return 0 if ok else 1


def journal_slice(since_line):
    try:
        with open(JOURNAL_PATH, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return [], 0
    return lines[since_line:], len(lines)


def cmd_weekly():
    if not ensure_repo():
        return 1
    wm = load_watermark()
    new_lines, total = journal_slice(wm.get("journal_lines", 0))
    ok = True
    week = datetime.now().date().isoformat()
    if new_lines:
        ok &= put_file(f"evidence/journal_{week}.jsonl",
                       "".join(new_lines).encode(),
                       f"experiment: journal slice {week} "
                       f"({len(new_lines)} events)")
        wm["journal_lines"] = total
        save_watermark(wm)
    else:
        print("no new journal lines to push")
    # review proposals + reflections
    for name in sorted(os.listdir(STATE_DIR)):
        if ((name.startswith("review_proposal_") or
             name.startswith("reflection_")) and name.endswith(".md")):
            with open(os.path.join(STATE_DIR, name), "rb") as f:
                ok &= put_file(f"evidence/{name}", f.read(),
                               f"experiment: {name}")
    return 0 if ok else 1


def cmd_final():
    if not ensure_repo():
        return 1
    ok = True
    try:
        with open(JOURNAL_PATH, "rb") as f:
            ok &= put_file("evidence/nova_journal_full.jsonl", f.read(),
                           "experiment day 30: full journal")
    except OSError as e:
        print(f"no journal to push: {e}", file=sys.stderr)
        ok = False
    report = os.path.join(STATE_DIR, "FINAL_REPORT.md")
    if os.path.exists(report):
        with open(report, "rb") as f:
            ok &= put_file("evidence/FINAL_REPORT.md", f.read(),
                           "experiment day 30: final report")
    else:
        print("FINAL_REPORT.md not found; run final_report.py first",
              file=sys.stderr)
        ok = False
    return 0 if ok else 1


def main(argv):
    if "--init" in argv:
        return cmd_init()
    if "--weekly" in argv:
        return cmd_weekly()
    if "--final" in argv:
        return cmd_final()
    print("usage: push_evidence.py --init | --weekly | --final",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
