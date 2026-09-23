#!/usr/bin/env python3
"""Two-mode trading registry for the Nova MT5 system.

Exactly two modes exist: DEMO and LIVE. Nothing else. This module owns the
durable record of which mode the system is in, which account identity each
mode is bound to, and the single audited path for switching between them.

Key rules enforced here:
  * The mode record is durable (run/trading_mode.json) and every change is
    appended to an append-only log (run/trading_mode.log.jsonl) with the
    previous version number. The switch path is the ONLY way the mode can
    change, and it always logs who changed it, when, and why.
  * DEMO and LIVE credentials live in separate env namespaces (DEMO_MT5_* /
    LIVE_MT5_*) and this module can only ever read the namespace of the mode
    it was asked for. Passwords are never logged; redact() is provided for
    safe display.
  * A switch to LIVE requires a live authorization on file that has not
    expired, plus an explicit operator confirmation the first time the
    system ever goes LIVE.
  * A switch is refused while positions are open unless explicitly allowed,
    because positions are never carried across modes.
  * All broker validation goes through an injected broker_verifier callable,
    so tests can use fakes while production passes the real terminal query.

Stdlib only. No network calls.
"""

import json
import os
import tokenize
from dataclasses import dataclass, field
from datetime import datetime, timezone

MODES = ("DEMO", "LIVE")

# Absolute rule: the system must never silently move a live setup back to
# demo. The only downgrade path is switch_mode(), which always logs.
NO_LIVE_TO_DEMO_FALLBACK = True

# Canonical DEMO identity for the 30-day demo experiment.
DEMO_LOGIN = 112975129
DEMO_SERVER = "MetaQuotes-Demo"

_RUN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run")


def set_run_dir(path):
    """Override the run directory (used by tests for isolation)."""
    global _RUN_DIR
    _RUN_DIR = str(path)


def _run_dir():
    return _RUN_DIR


def _mode_path():
    return os.path.join(_run_dir(), "trading_mode.json")


def _log_path():
    return os.path.join(_run_dir(), "trading_mode.log.jsonl")


def _auth_path():
    return os.path.join(_run_dir(), "live_authorization.json")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class ModeError(Exception):
    """Base error for all trading-mode failures."""


class AccountIdentityMismatch(ModeError):
    """The broker-reported account identity does not match the mode record."""


class ModeUnavailable(ModeError):
    """A mode cannot be entered right now. Carries .status.

    status is "DEMO_UNAVAILABLE" or "LIVE_UNAVAILABLE".
    """

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


@dataclass
class ModeRecord:
    mode: str
    account_login: int
    broker_server: str
    account_type: str  # "DEMO" | "LIVE"
    changed_at: str  # ISO UTC
    changed_by: str
    reason: str
    version: int  # increments on every change

    def to_dict(self):
        return {
            "mode": self.mode,
            "account_login": int(self.account_login),
            "broker_server": self.broker_server,
            "account_type": self.account_type,
            "changed_at": self.changed_at,
            "changed_by": self.changed_by,
            "reason": self.reason,
            "version": int(self.version),
        }


def _record_from_dict(data):
    if not isinstance(data, dict):
        raise ModeError("trading mode record is not a JSON object")
    mode = data.get("mode")
    if mode not in MODES:
        raise ModeError("corrupt mode record: mode %r not in %r" % (mode, MODES))
    acct_type = str(data.get("account_type", "")).upper()
    if acct_type not in MODES:
        raise ModeError("corrupt mode record: account_type %r invalid" % (data.get("account_type"),))
    try:
        login = int(data.get("account_login"))
        version = int(data.get("version"))
    except (TypeError, ValueError):
        raise ModeError("corrupt mode record: account_login/version not integers")
    if version < 1:
        raise ModeError("corrupt mode record: version must be >= 1")
    return ModeRecord(
        mode=mode,
        account_login=login,
        broker_server=str(data.get("broker_server", "")).strip(),
        account_type=acct_type,
        changed_at=str(data.get("changed_at", "")),
        changed_by=str(data.get("changed_by", "")),
        reason=str(data.get("reason", "")),
        version=version,
    )


def _commit(record, *, prev_version, operator_confirmed=False):
    """Durably write the new record and append the audit entry.

    The record file is written atomically (tmp + os.replace); it is the
    source of truth. The log entry is appended right after.
    """
    os.makedirs(_run_dir(), exist_ok=True)
    tmp = _mode_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(record.to_dict(), f, indent=2)
    os.replace(tmp, _mode_path())
    entry = record.to_dict()
    entry["prev_version"] = prev_version
    entry["operator_confirmed"] = bool(operator_confirmed)
    with open(_log_path(), "a") as f:
        f.write(json.dumps(entry) + "\n")
    return record


def load_mode():
    """Load the current mode record, creating the DEMO default if missing."""
    path = _mode_path()
    if not os.path.exists(path):
        record = ModeRecord(
            mode="DEMO",
            account_login=DEMO_LOGIN,
            broker_server=DEMO_SERVER,
            account_type="DEMO",
            changed_at=_now_iso(),
            changed_by="system-init",
            reason="initial default: 30-day demo experiment",
            version=1,
        )
        return _commit(record, prev_version=0)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ModeError("cannot read trading mode record: %s" % e)
    return _record_from_dict(data)


def verify_identity(record, login, server, acct_type):
    """Raise AccountIdentityMismatch on ANY mismatch of the three bindings.

    Normalizes: login -> int, server -> stripped str, type -> uppercased str.
    """
    try:
        login_n = int(str(login).strip())
    except (TypeError, ValueError):
        raise AccountIdentityMismatch("login %r is not a valid integer" % (login,))
    server_n = str(server or "").strip()
    type_n = str(acct_type or "").strip().upper()
    bad = []
    if login_n != int(record.account_login):
        bad.append("account_login")
    if server_n != str(record.broker_server).strip():
        bad.append("broker_server")
    if type_n != str(record.account_type).upper():
        bad.append("account_type")
    if bad:
        raise AccountIdentityMismatch(
            "identity mismatch on %s (record binds login=%s server=%r type=%s)"
            % (", ".join(bad), record.account_login, record.broker_server, record.account_type)
        )
    return True


def get_profile(mode):
    """Return the credential profile for exactly one mode's env namespace.

    Reads ONLY <MODE>_MT5_LOGIN / <MODE>_MT5_SERVER / <MODE>_MT5_PASSWORD,
    where the prefix is built from the mode argument -- cross-reading the
    other mode's namespace is structurally impossible. Raises ModeError on
    unknown mode. For LIVE, missing vars raise
    ModeError("live credentials not configured").

    The returned dict carries the raw password; never log it -- use redact()
    for display.
    """
    if mode not in MODES:
        raise ModeError("unknown mode %r; only %r exist" % (mode, MODES))
    prefix = mode + "_MT5_"
    login_raw = os.environ.get(prefix + "LOGIN")
    server = os.environ.get(prefix + "SERVER")
    password = os.environ.get(prefix + "PASSWORD")
    if mode == "LIVE" and not (login_raw and server and password):
        raise ModeError("live credentials not configured")
    login = None
    if login_raw is not None:
        try:
            login = int(str(login_raw).strip())
        except ValueError:
            raise ModeError("%sLOGIN is not a valid integer" % prefix)
    return {
        "mode": mode,
        "login": login,
        "server": server.strip() if server else None,
        "password": password,
    }


def redact(profile):
    """Safe display form of a credential profile (password never included)."""
    return {
        "mode": profile.get("mode"),
        "login": profile.get("login"),
        "server": profile.get("server"),
        "password": "set" if profile.get("password") else "missing",
    }


_AUTH_FIELDS = ("authorized_by", "live_login", "live_server", "authorized_at", "expires_at", "note")


def authorize_live(record):
    """Persist a live authorization record (dict with the _AUTH_FIELDS keys)."""
    if not isinstance(record, dict):
        raise ModeError("authorization record must be a dict")
    missing = [k for k in _AUTH_FIELDS if k not in record]
    if missing:
        raise ModeError("authorization record missing fields: %s" % ", ".join(missing))
    try:
        datetime.fromisoformat(str(record["expires_at"]))
    except ValueError:
        raise ModeError("authorization expires_at is not valid ISO time")
    os.makedirs(_run_dir(), exist_ok=True)
    tmp = _auth_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump({k: record[k] for k in _AUTH_FIELDS}, f, indent=2)
    os.replace(tmp, _auth_path())
    return True


def is_live_authorized():
    """True only if a live authorization exists on disk and has not expired."""
    try:
        with open(_auth_path()) as f:
            auth = json.load(f)
        exp = datetime.fromisoformat(str(auth.get("expires_at")))
    except (OSError, ValueError, TypeError, KeyError):
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > datetime.now(timezone.utc)


def _has_ever_been_live():
    """True if the change log shows the system has ever entered LIVE."""
    try:
        with open(_log_path()) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    if json.loads(line).get("mode") == "LIVE":
                        return True
                except ValueError:
                    continue
    except OSError:
        return False
    return False


def _target_record(target, auth=None):
    if target == "DEMO":
        return {
            "mode": "DEMO",
            "account_login": DEMO_LOGIN,
            "broker_server": DEMO_SERVER,
            "account_type": "DEMO",
        }
    # LIVE target identity comes from the authorization, never from a default.
    return {
        "mode": "LIVE",
        "account_login": int(auth["live_login"]),
        "broker_server": str(auth["live_server"]).strip(),
        "account_type": "LIVE",
    }


def switch_mode(target, *, changed_by, reason, broker_verifier,
                operator_confirm=False, positions_provider=None,
                allow_with_positions=False, reconcile_hook=None,
                risk_confirm_hook=None):
    """Atomically switch the trading mode. The ONLY switch path.

    broker_verifier(profile) validates the broker connection for the target
    mode and returns (login, server, acct_type, capabilities). In production
    it queries the real terminal; tests inject a fake.

    Any exception raised before the commit leaves the previous record
    untouched. Positions are never carried across modes: the switch is
    refused while positions_provider() reports open tickets unless
    allow_with_positions=True.
    """
    if target not in MODES:
        raise ModeError("unknown mode %r; only %r exist" % (target, MODES))
    if not changed_by or not str(changed_by).strip():
        raise ModeError("changed_by is required (explicit operator identity)")
    if not reason or not str(reason).strip():
        raise ModeError("reason is required (no silent mode changes)")

    current = load_mode()
    if current.mode == target:
        return current  # no-op: same mode, no version bump, no log entry

    auth = None
    first_live = False
    if target == "LIVE":
        if not is_live_authorized():
            raise ModeUnavailable("LIVE_UNAVAILABLE",
                                  "LIVE switch refused: no valid live authorization on file")
        with open(_auth_path()) as f:
            auth = json.load(f)
        first_live = not _has_ever_been_live()
        if first_live and not operator_confirm:
            raise ModeError("first LIVE transition requires operator_confirm=True")

    if positions_provider is not None and not allow_with_positions:
        tickets = positions_provider() or []
        if tickets:
            raise ModeError(
                "refusing mode switch with %d open position(s); "
                "positions are never carried across modes" % len(tickets))

    profile = get_profile(target)
    verified = broker_verifier(profile)
    try:
        login, server, acct_type, capabilities = verified
    except (TypeError, ValueError):
        raise ModeError("broker_verifier must return (login, server, acct_type, capabilities)")

    target_fields = _target_record(target, auth)
    target_record = ModeRecord(
        mode=target_fields["mode"],
        account_login=target_fields["account_login"],
        broker_server=target_fields["broker_server"],
        account_type=target_fields["account_type"],
        changed_at=_now_iso(),
        changed_by=str(changed_by),
        reason=str(reason),
        version=current.version + 1,
    )
    # The broker must report exactly the identity the target record binds.
    verify_identity(target_record, login, server, acct_type)

    if reconcile_hook is not None:
        reconcile_hook(target_record, capabilities or {})
    if risk_confirm_hook is not None:
        risk_confirm_hook(target_record, capabilities or {})

    return _commit(target_record, prev_version=current.version,
                   operator_confirmed=bool(operator_confirm and target == "LIVE"))


def history_scope(mode):
    """Per-mode history scope for reports. DEMO and LIVE histories are separate."""
    if mode not in MODES:
        raise ModeError("unknown mode %r; only %r exist" % (mode, MODES))
    return {"journal": "nova_journal.jsonl", "trades": "nova_trades.jsonl", "scope": mode}


def ensure_single_scope(scopes):
    """Raise if records from more than one mode scope would be combined.

    Report functions must call this before aggregating: DEMO and LIVE
    history are never mixed in one report.
    """
    distinct = {s for s in scopes if s is not None}
    if len(distinct) > 1:
        raise ModeError(
            "refusing to combine history scopes %s in one report" % sorted(distinct))
    return True


# --- module-level legacy-mode guard ---------------------------------------

_FORBIDDEN_TOKENS = ("AUTO", "PAPER", "SIMULATION", "FALLBACK", "SAFE_DEMO", "DRY_LIVE")
_ALLOWED_IDENTIFIER = "NO_LIVE_TO_DEMO_FALLBACK"


def _assert_no_legacy_modes():
    """Scan this module's own source for forbidden mode tokens.

    Only real code and string literals are scanned (comments are excluded
    via tokenize). The guard's own constant definitions are skipped by line,
    as is the single allowed no-fallback identifier. Anything else matching
    a forbidden token raises at import.
    """
    import re
    path = os.path.abspath(__file__)
    with open(path) as f:
        lines = f.readlines()
    # Lines that define the guard's own constants are not violations.
    skip_lines = set()
    for i, line in enumerate(lines, 1):
        if re.match(r"^\s*(_FORBIDDEN_TOKENS|_ALLOWED_IDENTIFIER)\s*=", line):
            skip_lines.add(i)

    def _norm(text):
        if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
            return text[1:-1]
        return text

    with tokenize.open(path) as f:
        for tok in tokenize.generate_tokens(f.readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                            tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER):
                continue
            if tok.start[0] in skip_lines:
                continue
            text = _norm(tok.string)
            if text == _ALLOWED_IDENTIFIER:
                continue
            if tok.type == tokenize.NAME:
                hit = next((p for p in text.split("_") if p in _FORBIDDEN_TOKENS), None)
            else:
                hit = next((b for b in _FORBIDDEN_TOKENS if b in text), None)
            if hit:
                raise ModeError(
                    "legacy mode token %r found in trading_mode.py; only DEMO/LIVE exist" % hit)


_assert_no_legacy_modes()
