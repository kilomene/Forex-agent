#!/usr/bin/env python3
"""
Durable idempotency store for broker-bound trade commands.

Every command the executor emits toward the NovaTrader EA is claimed here
BEFORE a single byte is appended to nova_commands.jsonl. The claim key is a
sha256 over the order's identity material, so a retried signal, a restarted
daemon, or a duplicated worker can never produce a second broker order for
the same intent.

Store: <state_dir>/idempotency.json, atomic write (tmp file + rename).

Record fields:
  key, signal_id, idempotency_key, mode, account, symbol, direction,
  volume, sl, tp, command_id, cmd_type, ticket,
  broker_ticket, broker_deal,
  status: proposed | sent | acknowledged | filled | unknown | failed |
          superseded
  supersedes_key, generation,
  sent_at, created_at, updated_at, note

Lifecycle:
  proposed  -- claimed, not yet written to the commands file
  sent      -- written; awaiting a broker response
  acknowledged -- broker/EA response seen (reserved for future use)
  filled    -- broker evidence adopted (ticket/deal recorded)
  unknown   -- no broker response within the timeout, or a stale claim
               found at restart; MUST be reconciled against broker state
               before any resend
  failed    -- the EA explicitly rejected the command
  superseded -- replaced by a resend (see supersedes_key / generation)

Restart safety: on load, claims stuck in proposed/sent for longer than
STALE_CLAIM_S (10 min) with no broker evidence are flipped to "unknown".
The executor's timeout sweeper reconciles every "unknown" entry against
live broker state (positions + trades file) before it may resend -- at
most once per original order.
"""

import hashlib
import json
import os
from datetime import datetime, timezone

STATUSES = ("proposed", "sent", "acknowledged", "filled", "unknown",
            "failed", "superseded")
PENDING_STATUSES = ("proposed", "sent", "unknown")

# A claim that never reached the broker (or never got a response) and is
# this old is no longer trustworthy: flip to "unknown" and reconcile.
STALE_CLAIM_S = 600  # 10 minutes


def utcnow_iso():
    """Current UTC time as an ISO-8601 string (used for sent_at)."""
    return datetime.now(timezone.utc).isoformat()


def parse_iso(s):
    """Parse an ISO-8601 timestamp; None on failure. Never raises."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def make_idempotency_key(*parts):
    """sha256 over the order's identity material, joined with '|'.

    For opens the parts are:
      signal_id | mode | account_login | symbol | direction | volume | sl | tp
    Every part is stringified; None becomes "".
    """
    material = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class IdempotencyStore:
    """Persistent claim registry; atomic writes; restart-safe."""

    def __init__(self, path):
        self.path = path
        self._records = {}
        self._stale_notes = []
        self.load()
        self.prune()

    # -- persistence ----------------------------------------------------
    def load(self):
        """(Re)load from disk, then flip stale claims to 'unknown'.

        Returns the list of (key, record) flipped this load so the caller
        can journal them.
        """
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        recs = data.get("records") if isinstance(data, dict) else None
        self._records = recs if isinstance(recs, dict) else {}
        marked = self.sweep_stale_claims()
        self._stale_notes = marked
        return marked

    def _save(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"records": self._records}, f)
        os.replace(tmp, self.path)

    # -- core API --------------------------------------------------------
    def check_or_claim(self, key, record):
        """Atomically claim key. Returns "new" or "duplicate".

        On "new" the record is stored with status "proposed" (the caller
        upgrades it to "sent" once the command bytes are on disk).
        """
        if key in self._records:
            return "duplicate"
        rec = dict(record or {})
        rec["key"] = key
        rec.setdefault("status", "proposed")
        now = utcnow_iso()
        rec.setdefault("created_at", now)
        rec["updated_at"] = now
        self._records[key] = rec
        self._save()
        return "new"

    def get(self, key):
        return self._records.get(key)

    def update(self, key, **fields):
        """Patch fields on an existing record. False when unknown key."""
        rec = self._records.get(key)
        if rec is None:
            return False
        rec.update(fields)
        rec["updated_at"] = utcnow_iso()
        self._save()
        return True

    def find_by_command_id(self, command_id):
        """(key, record) for the claim that produced this command id."""
        for k, r in self._records.items():
            if r.get("command_id") == command_id:
                return k, r
        return None, None

    def find_by_ticket(self, ticket, cmd_type=None):
        """(key, record) for a claim on this broker ticket (close/modify)."""
        for k, r in self._records.items():
            if str(r.get("ticket")) != str(ticket):
                continue
            if cmd_type is not None and r.get("cmd_type") != cmd_type:
                continue
            return k, r
        return None, None

    def pending(self):
        """Claims still needing broker resolution."""
        return {k: r for k, r in self._records.items()
                if r.get("status") in PENDING_STATUSES}

    def drain_stale_notes(self):
        """Return and clear the (key, record) pairs flipped to 'unknown'
        by the load-time stale-claim sweep, so the caller can journal
        them exactly once."""
        notes, self._stale_notes = self._stale_notes, []
        return notes

    # -- restart safety ---------------------------------------------------
    def sweep_stale_claims(self, now=None):
        """Flip proposed/sent claims older than STALE_CLAIM_S with no
        broker evidence to 'unknown'. Returns [(key, record)] flipped."""
        now_dt = now or datetime.now(timezone.utc)
        marked = []
        for key, rec in self._records.items():
            if rec.get("status") not in ("proposed", "sent"):
                continue
            if rec.get("broker_ticket") or rec.get("broker_deal"):
                continue
            anchor = (parse_iso(rec.get("sent_at"))
                      or parse_iso(rec.get("created_at")))
            if anchor is None:
                continue
            if (now_dt - anchor).total_seconds() > STALE_CLAIM_S:
                rec["status"] = "unknown"
                rec["updated_at"] = utcnow_iso()
                rec["note"] = ("stale claim at (re)start: no broker "
                               "evidence within 10 min; reconcile before "
                               "any resend")
                marked.append((key, rec))
        if marked:
            self._save()
        return marked

    def prune(self, max_entries=2000, max_age_days=7):
        """Drop terminal records (filled/failed/superseded) older than
        max_age_days; hard-cap the store at max_entries. Never raises."""
        try:
            now = datetime.now(timezone.utc)
            terminal = [
                k for k, r in self._records.items()
                if r.get("status") in ("filled", "failed", "superseded")
                and (now - (parse_iso(r.get("updated_at")) or now))
                    .total_seconds() > max_age_days * 86400
            ]
            for k in terminal:
                del self._records[k]
            if len(self._records) > max_entries:
                ordered = sorted(
                    self._records.items(),
                    key=lambda kv: kv[1].get("updated_at") or "")
                for k, _ in ordered[:len(self._records) - max_entries]:
                    del self._records[k]
            if terminal or len(self._records) > max_entries:
                self._save()
        except OSError:
            pass
