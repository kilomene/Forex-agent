#!/usr/bin/env python3
"""evolve.py --weekly : the continuous-evolution engineering pass.

Studies the journal (last 7 days), the latest weekend reflection, and
the borrow-list; writes a findings digest; maintains the proposal
registry (proposals/applied.json) with content-hash dedupe; instantiates
new proposals from complete idea-queue specs.

This script NEVER applies anything to the live tree and NEVER edits
trading state. It only writes files under proposals/. Applying a
proposal is the pipeline's job (pipeline.py prepare -> run), and only
for tier=auto: tier=zenas is hard-blocked (kill-switch resume or
blocked_symbols change -- Zenas's own actions) and refused by the
pipeline.

STDLIB ONLY. Idempotent: re-running changes nothing when nothing
changed (dedupe by content hash).
"""

import argparse
import glob
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timedelta

BRIDGE = os.path.dirname(os.path.abspath(__file__))
PROPOSALS = os.path.join(BRIDGE, "proposals")
APPLIED = os.path.join(PROPOSALS, "applied.json")
QUEUE = os.path.join(PROPOSALS, "idea_queue.json")
BORROW_LIST = os.path.expanduser(
    "~/workspace/research_notes/github-forex-trading-bot-research-"
    "20261018-0810/borrow-list.md")
FINDINGS_DAYS = 7


def utc_stamp():
    return datetime.now().strftime("%Y%m%d")


# ---------------------------------------------------------------- registry

def load_applied():
    try:
        with open(APPLIED) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_applied(d):
    tmp = APPLIED + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, APPLIED)


def content_hash(pdir):
    """sha256 over proposal.md + patch.diff + tests/*.py (sorted)."""
    h = hashlib.sha256()
    parts = []
    for fn in ("proposal.md", "patch.diff"):
        p = os.path.join(pdir, fn)
        if os.path.exists(p):
            parts.append(p)
    tdir = os.path.join(pdir, "tests")
    if os.path.isdir(tdir):
        for fn in sorted(os.listdir(tdir)):
            if fn.startswith("test_") and fn.endswith(".py"):
                parts.append(os.path.join(tdir, fn))
    for p in parts:
        h.update(os.path.basename(p).encode() + b"\x00")
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    return h.hexdigest()


def read_header(pdir):
    header = {}
    try:
        with open(os.path.join(pdir, "proposal.md")) as f:
            lines = f.read().splitlines()
    except OSError:
        return header
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if ":" in line:
                k, v = line.split(":", 1)
                header[k.strip()] = v.strip()
    return header


# ---------------------------------------------------------------- findings

def _parse_ts(s):
    if not s:
        return None
    s = str(s).strip().replace(".", "-")
    try:
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def analyze_journal(journal_path, days=FINDINGS_DAYS):
    cutoff = datetime.now() - timedelta(days=days)
    decisions, skips = {}, {}
    n_signals = n_closes = n_errors = 0
    total_pnl = 0.0
    try:
        f = open(journal_path, errors="replace")
    except OSError:
        return {"journal_missing": True}
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            ts = _parse_ts(ev.get("time"))
            if ts and ts < cutoff:
                continue
            t = ev.get("type")
            if t == "signal.received":
                n_signals += 1
            elif t == "trade.decision":
                d = ev.get("decision", "?")
                decisions[d] = decisions.get(d, 0) + 1
                if d.startswith("skipped:"):
                    skips[d] = skips.get(d, 0) + 1
            elif t == "trade.closed":
                n_closes += 1
                try:
                    total_pnl += float(ev.get("net_profit",
                                             ev.get("profit") or 0) or 0)
                except (TypeError, ValueError):
                    pass
            elif t in ("filter.error", "canary.aborted",
                       "pipeline.rejected"):
                n_errors += 1
    top_skips = sorted(skips.items(), key=lambda kv: -kv[1])[:10]
    return {
        "window_days": days,
        "n_signals": n_signals,
        "n_decisions": sum(decisions.values()),
        "n_closes_raw": n_closes,
        "raw_close_pnl": round(total_pnl, 2),
        "top_skip_reasons": [{"reason": k, "count": v}
                             for k, v in top_skips],
        "n_pipeline_errors": n_errors,
    }


def latest_reflection():
    paths = sorted(glob.glob(os.path.join(
        BRIDGE, "run", "reflection_*.md")))
    if not paths:
        return None, ""
    p = paths[-1]
    try:
        with open(p, errors="replace") as f:
            return p, f.read()[:4000]
    except OSError:
        return p, ""


def borrow_ideas():
    ideas = []
    try:
        with open(BORROW_LIST, errors="replace") as f:
            text = f.read()
    except OSError:
        return ideas
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("### ") and not s.startswith("### 3b"):
            ideas.append(s[4:].strip()[:100])
    return ideas[:12]


# ---------------------------------------------------------------- queue

def load_queue():
    try:
        with open(QUEUE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def instantiate_from_queue(applied):
    """Instantiate proposals whose queue spec is complete.

    A spec is complete only when proposals/queue/<qid>/ holds
    proposal.md + patch.diff (+ tests/). Incomplete specs are reported,
    never half-built: no stubs, ever.
    """
    q = load_queue()
    made = []
    for qid, entry in sorted(q.get("ideas", {}).items()):
        if not isinstance(entry, dict):
            continue
        if entry.get("proposal_id"):
            continue  # already instantiated
        if entry.get("spec_status") != "complete":
            continue
        src = os.path.join(PROPOSALS, "queue", qid)
        needed = [os.path.join(src, "proposal.md"),
                  os.path.join(src, "patch.diff")]
        if not all(os.path.exists(p) for p in needed):
            continue
        pid = entry.get("suggested_id") or qid.upper()
        dest = os.path.join(PROPOSALS, pid)
        if os.path.exists(dest):
            continue
        shutil.copytree(src, dest)
        header = read_header(dest)
        applied[pid] = {
            "content_hash": content_hash(dest),
            "status": "proposed",
            "tier": header.get("tier", "auto"),
            "change_class": header.get("change_class", "refactor"),
            "from_queue": qid,
            "created_utc": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        entry["proposal_id"] = pid
        made.append(pid)
    if made:
        tmp = QUEUE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(q, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, QUEUE)
    return made


# ---------------------------------------------------------------- main

def weekly_pass():
    os.makedirs(PROPOSALS, exist_ok=True)
    applied = load_applied()
    journal = os.path.join(BRIDGE, "run", "nova_journal.jsonl")
    stats = analyze_journal(journal)
    refl_path, refl_text = latest_reflection()
    ideas = borrow_ideas()

    # 1. register / re-hash every proposal dir (dedupe by content hash)
    changed = []
    for name in sorted(os.listdir(PROPOSALS)):
        pdir = os.path.join(PROPOSALS, name)
        if not os.path.isdir(pdir) or name == "queue":
            continue
        if not os.path.exists(os.path.join(pdir, "proposal.md")):
            continue
        h = content_hash(pdir)
        header = read_header(pdir)
        rec = applied.get(name)
        if rec is None:
            applied[name] = {
                "content_hash": h,
                "status": "proposed",
                "tier": header.get("tier", "auto"),
                "change_class": header.get("change_class", "refactor"),
                "created_utc": datetime.now().strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
            }
            changed.append(f"registered {name}")
        elif rec.get("content_hash") != h and \
                rec.get("status") not in ("installed",):
            rec["content_hash"] = h
            rec["revised_utc"] = datetime.now().strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            changed.append(f"revised {name} (hash changed)")

    # 2. sync installed statuses from versions/
    vdir = os.path.join(BRIDGE, "versions")
    if os.path.isdir(vdir):
        for v in os.listdir(vdir):
            vp = os.path.join(vdir, v, "version.json")
            try:
                with open(vp) as f:
                    man = json.load(f)
            except (OSError, ValueError):
                continue
            pid = man.get("proposal_id")
            if pid and pid in applied and \
                    man.get("status") == "installed":
                if applied[pid].get("status") != "installed":
                    applied[pid]["status"] = "installed"
                    applied[pid]["version_id"] = v
                    changed.append(f"{pid} installed as {v}")

    # 3. instantiate complete queue specs
    made = instantiate_from_queue(applied)
    changed.extend(f"instantiated {p}" for p in made)

    save_applied(applied)

    # 4. findings digest
    q = load_queue()
    pending_queue = [
        f"{qid}: {e.get('title', '')} "
        f"(spec: {e.get('spec_status', 'incomplete')})"
        for qid, e in sorted(q.get("ideas", {}).items())
        if isinstance(e, dict) and not e.get("proposal_id")]
    digest = {
        "date": utc_stamp(),
        "journal_stats": stats,
        "latest_reflection": (os.path.basename(refl_path)
                              if refl_path else None),
        "borrow_list_ideas": ideas,
        "proposals": {pid: {"status": r.get("status"),
                            "tier": r.get("tier"),
                            "hash": r.get("content_hash", "")[:12]}
                      for pid, r in sorted(applied.items())},
        "queue_pending": pending_queue,
        "registry_changes": changed,
    }
    dp = os.path.join(PROPOSALS, f"findings_{utc_stamp()}.md")
    with open(dp, "w") as f:
        f.write(f"# Evolution findings {digest['date']}\n\n")
        f.write("```json\n" + json.dumps(digest, indent=2) + "\n```\n\n")
        if refl_text:
            f.write("## Latest reflection (excerpt)\n\n")
            f.write(refl_text + "\n")
        if pending_queue:
            f.write("\n## Idea queue: awaiting engineer spec\n\n")
            for item in pending_queue:
                f.write(f"- {item}\n")
            f.write("\nThese are NOT proposals yet. evolve.py never "
                    "half-builds a proposal: a queue idea becomes a "
                    "proposal only when its spec (proposal.md + patch.diff "
                    "+ tests) is complete.\n")

    # 5. run log
    logp = os.path.join(BRIDGE, "run", "evolution.log")
    with open(logp, "a") as f:
        f.write(json.dumps({
            "utc": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": "evolve_weekly",
            "proposals": len(applied),
            "registry_changes": changed,
            "digest": os.path.basename(dp),
        }) + "\n")

    print(f"evolve --weekly: {len(applied)} proposals tracked, "
          f"{len(changed)} registry change(s), digest {dp}")
    for c in changed:
        print(f"  - {c}")
    return 0


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--weekly", action="store_true",
                    help="run the weekly engineering pass")
    args = ap.parse_args(argv)
    if not args.weekly:
        ap.print_help()
        return 2
    return weekly_pass()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
