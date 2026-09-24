# Kill Switch Source of Truth — 2026-09-23 trip

## Canonical record

The journal event `kill_switch.tripped` in `run/nova_journal.jsonl` is the
**single authoritative record** of the kill-switch trip. Everything else
(file mtimes, seat logs, notes) is supporting evidence.

```json
{"type": "kill_switch.tripped", "time": "2026-09-23 21:05:18",
 "tripped_by": "parent/main-agent (observed by T3)",
 "note": "trading_enabled set to 0 ~21:03 PDT; NO daily-loss breach by T3
  measures (broker-day 2026.09.23 realized -1419.44 = 23.7% of $6000);
  both positions closed overnight 2026.09.24 00:00:35 broker:
  USDTRY -114.66, USDHKD -137.98; 0/10 positions;
  balance=equity=$1255620.08"}
```

## Exact sequence (all times UTC)

| Time (UTC)          | Event |
|---------------------|-------|
| 2026-09-23 21:00:39 | Last two positions closed broker-side: USDHKD ticket 10649170114 (-137.98), USDTRY ticket 10629297282 (-114.66). Executor log `journal linked trade.closed` lines. |
| 2026-09-23 21:03:37 | T3 sweep observes 0 open positions, kill switch live. |
| 2026-09-23 21:03:38 | `run/trading_enabled` written with `"0"` (file mtime 21:03:38.919 UTC — the physical ground truth of the halt). |
| 2026-09-23 21:05:18 | `kill_switch.tripped` journaled by parent/main-agent (observed by T3), 1m40s after the file write. |
| 2026-09-23 21:09:04 | T3 sweep log records `status=kill_switch_tripped_by_parent`. |

(PDT equivalents: 14:00:39, 14:03:37, 14:03:38, 14:05:18, 14:09:04.)

## Why other records differ

- **Journal note says "~21:03 PDT"** while the file mtime is 21:03:38 **UTC**.
  The journal `time` fields are UTC (a 21:05 PDT event would be in the
  future at the time of writing — the trip was ~1.5h before the 15:36 PDT
  audit). The note's "PDT" label is a labeling error; the correct reading
  is ~21:03 UTC (14:03 PDT). The file mtime (21:03:38 UTC) is ground truth.
- **T3 sweep log line 101 says "journaled at 21:05:18 PDT"** — repeats the
  same mislabel; the journal timestamp is 21:05:18 UTC.
- **Executor log has no kill entry after 21:00:39** — the executor never
  journals the trip itself; it only sees the `trading_disabled` gate on
  the next signal (verified: USDHKD `skipped:trading_disabled` at 21:15:07).
- **This was NOT a daily-loss breach trip.** T3 measured broker-day
  realized −1419.44 = 23.7% of the $6,000 daily-loss limit. The trip was a
  manual halt by parent/main-agent during the emergency repair order.

## Current state

`run/trading_enabled` = `"0"` (halted). Trading stays halted until Zenas
explicitly authorizes re-enable. The executor daemon is still running and
continues to journal decisions as `skipped:trading_disabled`.

## Regression check

`tests/test_kill_switch_truth.py` asserts these anchors still parse:
the journal event exists with time `2026-09-23 21:05:18` and
`tripped_by` containing `parent/main-agent`, and `run/trading_enabled`
reads `"0"` with an mtime dated 2026-09-23.
