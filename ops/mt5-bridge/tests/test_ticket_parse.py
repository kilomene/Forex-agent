"""Issues 1+2 regression tests: whitespace-tolerant ParseTicketEntry.

MQL5 cannot run here, so this file carries faithful Python mirrors of the
OLD (pre-fix) and NEW (fixed) EA parse logic. The mirrors prove:
  * the old exact-literal logic fails on whitespace variants and, via the
    old load loop, drops every later ticket (the verified mass-loss bug);
  * the new FindJsonValuePos-based logic parses the same inputs and the
    new loop skips malformed entries instead of breaking.

A source-parity test asserts the bridge copy of NovaTrader.mq5 is
byte-identical to the deployed copy (and the repo clone copy), so the
two sources can never silently diverge again.
"""
import hashlib
import os
import sys

import pytest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)
DEPLOYED_MQ5 = os.path.expanduser(
    "~/workspace/mt5/prefix/drive_c/Program Files/MetaTrader 5/"
    "MQL5/Experts/NovaTrader.mq5")
REPO_MQ5 = os.path.expanduser(
    "~/workspace/forex-migration/repo/ops/mt5-bridge/NovaTrader.mq5")
BRIDGE_MQ5 = os.path.join(BRIDGE, "NovaTrader.mq5")

DIGITS = "0123456789"


# ----------------------------------------------------------------------
# OLD logic mirror (pre-2026-09-23-fix EA): exact literal '"ticket":',
# load loop breaks on first parse failure.
# ----------------------------------------------------------------------
def old_parse(txt, entry_pos):
    ticket, symbol, typ, volume, magic = 0, "", "", 0.0, 0
    p = txt.find('"ticket":', entry_pos)
    if p < 0 or p > entry_pos + 600:
        return None
    p += 9
    n, q = len(txt), p
    while q < n and txt[q] in DIGITS:
        q += 1
    if q == p:
        return None
    ticket = int(txt[p:q])
    ps = txt.find('"symbol":"', p)
    if 0 <= ps < p + 600:
        ps += 10
        qs = txt.find('"', ps)
        if qs > ps:
            symbol = txt[ps:qs]
    pt = txt.find('"type":"', p)
    if 0 <= pt < p + 600:
        pt += 8
        qt = txt.find('"', pt)
        if qt > pt:
            typ = txt[pt:qt]
    pv = txt.find('"volume":', p)
    if 0 <= pv < p + 600:
        pv += 9
        qv = pv
        while qv < n and (txt[qv] in DIGITS or txt[qv] == "."):
            qv += 1
        if qv > pv:
            volume = float(txt[pv:qv])
    pm = txt.find('"magic":', p)
    if 0 <= pm < p + 600:
        pm += 8
        qm = pm
        while qm < n and txt[qm] in DIGITS:
            qm += 1
        if qm > pm:
            magic = int(txt[pm:qm])
    return {"ticket": ticket, "symbol": symbol, "type": typ,
            "volume": volume, "magic": magic}


def old_load(txt):
    out, pos = [], 0
    while True:
        p = txt.find('"ticket":', pos)
        if p < 0:
            break
        r = old_parse(txt, p)
        if r is None:
            break  # the mass-loss bug: one bad entry ends the load
        if r["ticket"] > 0 and r["ticket"] not in [e["ticket"] for e in out]:
            out.append(r)
        pos = p + 9
    return out


# ----------------------------------------------------------------------
# NEW logic mirror (fixed EA): FindJsonValuePos whitespace tolerance,
# load loop skips malformed entries and continues.
# ----------------------------------------------------------------------
def _find_value_pos(txt, key, start, max_span):
    quoted = '"%s"' % key
    p = txt.find(quoted, start)
    if p < 0:
        return -1
    if max_span >= 0 and p > start + max_span:
        return -1
    n, q = len(txt), p + len(quoted)
    while q < n and txt[q] in " \t\r\n":
        q += 1
    if q >= n or txt[q] != ":":
        return -1
    q += 1
    while q < n and txt[q] in " \t\r\n":
        q += 1
    if q >= n:
        return -1
    return q


def new_parse(txt, entry_pos):
    ticket, symbol, typ, volume, magic = 0, "", "", 0.0, 0
    n = len(txt)
    p = _find_value_pos(txt, "ticket", entry_pos, 600)
    if p < 0:
        return None
    q = p
    while q < n and txt[q] in DIGITS:
        q += 1
    if q == p:
        return None
    ticket = int(txt[p:q])

    def quoted_field(key):
        vpos = _find_value_pos(txt, key, p, 600)
        if vpos >= 0 and vpos < n and txt[vpos] == '"':
            end = txt.find('"', vpos + 1)
            if end > vpos + 1 and end < p + 600:
                return txt[vpos + 1:end]
        return ""

    def num_field(key, is_float):
        vpos = _find_value_pos(txt, key, p, 600)
        if vpos < 0:
            return 0.0 if is_float else 0
        q2 = vpos
        while q2 < n and (txt[q2] in DIGITS or (is_float and txt[q2] == ".")):
            q2 += 1
        if q2 > vpos and q2 < p + 600:
            return float(txt[vpos:q2]) if is_float else int(txt[vpos:q2])
        return 0.0 if is_float else 0

    symbol = quoted_field("symbol")
    typ = quoted_field("type")
    volume = num_field("volume", True)
    magic = num_field("magic", False)
    return {"ticket": ticket, "symbol": symbol, "type": typ,
            "volume": volume, "magic": magic}


def new_load(txt):
    out, skipped, pos = [], 0, 0
    while True:
        key_at = txt.find('"ticket"', pos)
        if key_at < 0:
            break
        vpos = _find_value_pos(txt, "ticket", key_at, 0)
        if vpos < 0:
            pos = key_at + 8
            continue
        r = new_parse(txt, key_at)
        if r is None:
            skipped += 1
            pos = key_at + 8
            continue
        if r["ticket"] > 0 and r["ticket"] not in [e["ticket"] for e in out]:
            out.append(r)
        pos = vpos + 1
    return out, skipped


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
COMPACT = ('{"tickets":[{"ticket":10641277984,"symbol":"GBPNZD",'
           '"type":"BUY","volume":1.04000000,"tracked_at":1790000000,'
           '"missing_since":0},'
           '{"ticket":10640513739,"symbol":"CHFJPY","type":"BUY",'
           '"volume":1.13000000,"tracked_at":1790000001,"missing_since":0}]}')

WS_BEFORE_COLON_1 = '{"ticket" :10641277984,"symbol":"GBPNZD"}'
WS_AFTER_COLON_1 = '{"ticket":  10641277984,"symbol":"GBPNZD"}'
WS_FIELDS = ('{"tickets":[{\n\t"ticket"\t:\t10641277984,\n'
             '\t"symbol" : "GBPNZD",\n\t"type":"BUY",\n'
             '\t"volume" : 1.04\n}]}')
MALFORMED_MID = ('{"tickets":[{"ticket":111,"symbol":"AAA","type":"BUY",'
                 '"volume":1.0},'
                 '{"ticket":ABC,"symbol":"BAD","type":"BUY","volume":1.0},'
                 '{"ticket":222,"symbol":"BBB","type":"SELL","volume":2.0}]}')


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------
def test_compact_parses_on_both():
    assert old_parse(COMPACT, 0)["ticket"] == 10641277984
    assert new_parse(COMPACT, 0)["ticket"] == 10641277984
    assert new_parse(COMPACT, 0)["symbol"] == "GBPNZD"
    assert new_parse(COMPACT, 0)["volume"] == 1.04


def test_space_before_colon_old_fails_new_parses():
    assert old_parse(WS_BEFORE_COLON_1, 0) is None
    r = new_parse(WS_BEFORE_COLON_1, 0)
    assert r is not None and r["ticket"] == 10641277984


def test_space_before_colon_old_silently_skips_to_next_entry():
    # The old loop searched for the exact literal '"ticket":', so a
    # whitespace-variant entry was silently skipped on load; the next
    # wholesale save then deleted it from the file permanently.
    ws_multi = COMPACT.replace('"ticket":10641277984', '"ticket" :10641277984')
    assert old_parse(ws_multi, 0)["ticket"] == 10640513739
    assert new_parse(ws_multi, 0)["ticket"] == 10641277984


def test_space_after_colon_old_fails_new_parses():
    assert old_parse(WS_AFTER_COLON_1, 0) is None
    r = new_parse(WS_AFTER_COLON_1, 0)
    assert r is not None and r["ticket"] == 10641277984


def test_tabs_newlines_new_parses():
    r = new_parse(WS_FIELDS, 0)
    assert r is not None
    assert r["ticket"] == 10641277984
    assert r["symbol"] == "GBPNZD"
    assert r["volume"] == 1.04
    assert old_parse(WS_FIELDS, 0) is None


def test_malformed_entry_old_drops_later_tickets():
    # The verified mass-loss mechanism: old loop breaks at the bad entry.
    assert [e["ticket"] for e in old_load(MALFORMED_MID)] == [111]


def test_malformed_entry_new_skips_and_continues():
    entries, skipped = new_load(MALFORMED_MID)
    assert [e["ticket"] for e in entries] == [111, 222]
    assert skipped == 1


def test_missing_optional_fields_new_ok():
    r = new_parse('{"ticket":777}', 0)
    assert r is not None and r["ticket"] == 777
    assert r["symbol"] == "" and r["volume"] == 0.0 and r["magic"] == 0


def test_no_ticket_key_returns_none():
    assert new_parse('{"tickets":[]}', 0) is None
    assert old_parse('{"tickets":[]}', 0) is None


def test_span_guard_both():
    # The span guard limits how far the KEY may sit past entryPos; the
    # gap between key and colon is intentionally tolerated by the fix.
    txt = '{"pad":"' + "x" * 700 + '","ticket":123}'
    assert old_parse(txt, 0) is None
    assert new_parse(txt, 0) is None
    txt2 = '{"ticket" :  123}'  # key at 0: gap to colon is fine
    assert new_parse(txt2, 0)["ticket"] == 123


def _md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def test_bridge_source_matches_deployed_source():
    """Bridge copy must never silently diverge from the deployed EA source.

    The deployed MT5 prefix only exists on the trading machine, so on a
    fresh checkout (CI) this local-deployment invariant is skipped rather
    than failed.
    """
    if not os.path.exists(DEPLOYED_MQ5):
        pytest.skip("deployed NovaTrader.mq5 not present on this machine")
    assert os.path.exists(BRIDGE_MQ5), "bridge NovaTrader.mq5 missing"
    assert _md5(BRIDGE_MQ5) == _md5(DEPLOYED_MQ5), (
        "bridge/NovaTrader.mq5 diverged from the deployed EA source")


def test_repo_source_matches_deployed_source():
    if not os.path.exists(DEPLOYED_MQ5):
        pytest.skip("deployed NovaTrader.mq5 not present on this machine")
    if not os.path.exists(REPO_MQ5):
        pytest.skip("local repo clone not present on this machine")
    assert _md5(REPO_MQ5) == _md5(DEPLOYED_MQ5), (
        "repo ops/mt5-bridge/NovaTrader.mq5 diverged from deployed source")
