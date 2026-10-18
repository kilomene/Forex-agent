# Continuous Evolution Loop — design doc

**Status:** live 2026-10-18. **Owner:** Nova (CEO-agent), for Zenas.
**Authority:** Zenas's standing orders of 2026-10-18 ("you're the builder",
"everything would be done by you tho auto update and upgrading", every
version end-to-end tested before install).

## Standing principles (Zenas — verbatim, baked into every gate)

1. **"The demo would still feel or be like real money to you."**
   Every demo dollar treated as real. No "it's only demo" leniency in any
   gate, threshold, or variant. Canary/rollback/staging criteria set as if
   P&L were real.
2. **FULL FUNCTIONALITY, NEVER PATCHES.** Complete implementations; if
   the right fix is a rebuild, rebuild.
3. **WIRED AND TESTED BEFORE INSTALL.** Integration end-to-end against
   real wiring is mandatory; unit tests alone never qualify a version.

## What this is

A weekly engineering pass (`evolve.py --weekly`, cron
`project-evolution-weekly`) studies the journal, the latest reflection,
and the borrow-list, and maintains concrete, tested improvement
proposals under `proposals/<id>/` (each: `proposal.md` + `patch.diff` +
`tests/`). A gated install pipeline (`pipeline.py` / the `pipeline/`
package) carries a candidate version through fixed gates in order, then
installs it or rejects it with evidence. `rollback.py --previous`
restores the last good version with one command and works even when the
installed version is broken (stdlib only, zero bridge imports).

## Approval tier

Per Zenas's governing 2026-10-18 order ("everything would be done by
you tho auto update and upgrading", every version end-to-end tested
before install), proposals carry a tier:

- **`auto`** — everything else: execution refactors, protection-plugin
  changes (still disabled-by-default at build; enablement only after
  the calm canary via `plugin-observe` / `plugin-enforce`), report
  upgrades, journaling, tooling, measurement, docs, EA updates (with
  the MQL5 recompile gate). Flows through the full fixed gate chain
  with no per-change approval. Risk config (`risk_per_trade_pct`
  0.05%, `max_concurrent_trades` 10) is NOT an approval item: it is a
  pipeline-asserted invariant, verified post-install, rollback on
  drift.
- **`zenas` (hard-blocked)** — ONLY:
  - **(a)** anything that would set `run/trading_enabled` to `"1"` /
    resume trading, and
  - **(b)** any change to `blocked_symbols`.
  The pipeline REFUSES tier-`zenas` versions: no approval file, no
  install. These two classes are Zenas's own actions. (a) is tripped
  early by the pre-install static scan; (b) is asserted unchanged by
  the post-install asserts (gate h), which roll back on drift.

Hard blocks the pipeline never crosses, no exceptions:

- **(a) Kill-switch resume.** No version may flip `run/trading_enabled`
  to `"1"`. A pre-install static scan rejects tripwires, install refuses
  while the switch is live, and the post-install asserts verify it is
  still `"0"`. Auto-updates NEVER resume trading.
- **(b) GBPZAR / blocked_symbols.** `blocked_symbols` is hashed before
  install and asserted unchanged after. The GBPZAR exclusion is
  Zenas-only, full stop.

Note (correction 2026-10-18): an earlier build of this doc implemented
Zenas's 01:41 PDT framing -- all execution/risk/EA changes need
Zenas's one-tap `zenas_approve.json`. That framing was SUPERSEDED by
Zenas's 01:44 PDT order, which narrowed the approval tier to exactly
the (a)/(b) hard block above. The stale "approval tier" version has
been removed; the pipeline refuses tier-`zenas` versions outright
instead of waiting for an approval file.

- **No credential / network / external-service changes**, ever.
- **Self-tune stays advisory.** The pipeline does not change
  `self_tune.py`'s authority. P-002 only annotates proposals with
  walk-forward evidence.

## Pipeline stages (fixed order, no skipping)

```
scan -> (a) -> (g) -> (b) -> (c) -> (e) -> (h)
```

- **scan** — static tripwire: rejects any candidate that writes the
  kill-switch file or sets `trading_enabled` to `"1"`. (The post-install
  asserts are the authoritative enforcement; this is the early tripwire.)
- **(a) Full test suite green.** `python3 -m pytest tests/ -q` inside the
  candidate work tree (an isolated copy, so the candidate's own code is
  what gets tested). Any failure rejects the version — except the two
  known Sunday market-closed environmental failures (below), and only
  while the market is actually closed per `market_hours`.
  `PYTHONPATH` includes `~/workspace/mt5` so
  `tests/test_supervise_stack.py` (which imports `supervise_stack` from
  the bridge's grandparent dir) collects inside work trees.
- **(g) EA recompile gate** (only when the version touches `*.mq5`).
  The candidate `.mq5` is compiled with the real `MetaEditor64.exe`
  under `wine64` in a `/tmp` sandbox (Z: mapping); the live MT5 prefix
  is untouched. Any compile error rejects the version. The built `.ex5`
  becomes the install artifact.
- **(b) Staging, wired end-to-end.** The candidate tree and a pristine
  baseline tree each run the REAL `check_gates()` in isolated
  subprocesses over REAL journaled signals (last 14 days, up to 2000),
  with the live risk config and a copy of the live symbol specs.
  - `refactor`: the two decision sequences must be byte-identical
    (decision + flags). Any divergence rejects.
  - `behavior`: decisions are scored by attributable outcomes
    (signal -> `trade.opened` ticket -> deduped `trade.closed` P&L via
    `performance.dedupe_closes`). Real-money standard: candidate total
    P&L >= current total P&L ("doesn't lose much" fails), no regression
    beyond 2% on Sharpe / profit factor / max drawdown, strict
    improvement on at least one of them, >= 30 attributable trades per
    side and >= 50% attribution coverage — else "insufficient evidence"
    rejects.
  - Documented replay simplifications (identical for both trees):
    `max_signal_age_hours=0` (staleness is unit-tested; old signals
    would all read stale), `specs_fresh=True` (unit-tested),
    `open_positions=[]` / `todays_profit=0.0` (position-state replay is
    out of scope; divergences are reported for human review),
    `enforce_mode_checks=False`, `mode="DEMO"` (halted demo legacy path).
- **(c) Shadow canary.** While trading is halted (the normal case), the
  candidate evaluates each live `signal.received` in dry-run shadow
  mode; decisions are logged next to the live engine's journaled
  decisions in `versions/<id>/canary/`. Soak: 48h or 200 signals,
  whichever first. Guard trips abort immediately: candidate crash,
  error-rate spike (>1%), any write to `nova_commands.jsonl` in the
  isolated canary state, any kill-switch write attempt.
  If trading ever resumes, canary instead installs with tightened
  guards (max 1 concurrent position via a temporary, reverted config
  overlay) — implemented, never exercised while halted.
- **(d) Auto-rollback.** Any guard trip during canary invokes
  `rollback.py --previous` automatically, verifies health, and journals
  the abort with evidence. (Pre-install there is nothing to restore;
  rollback is idempotent and the health check + journal still run.)
- **(e) Install.** Snapshot the live code tree to
  `versions/<id>/previous_snapshot/`, overlay the candidate files,
  copy `.ex5` artifacts into the MT5 prefix for EA versions
  (both `MQL5/Experts/` and `MQL5/Experts/Advisors/`; GUI re-attach
  stays manual and is logged), restart the executor via
  `scripts/relaunch_executor.py`.
- **(h) Post-install assertions** (any failure -> auto-rollback):
  1. `run/trading_enabled` still `"0"`;
  2. `risk_config.json`: `risk_per_trade_pct == 0.05`,
     `max_concurrent_trades == 10`;
  3. `blocked_symbols` hash unchanged;
  4. executor daemon alive — `/proc/<pid>/cmdline` argv[1] IS
     `trade_executor.py` (name-mention matching is not accepted);
  5. `supervise_stack.py --health` exits 0 AND reports the executor pid.
- **(f) Plugin two-stage enablement** (separate pipeline mode):
  1. `pipeline.py plugin-observe --plugin <name> --proposal <pid>` runs
     the filter over recent signals in OBSERVE mode; evaluations are
     journaled to `run/protections_observe.jsonl` (not the live trader
     journal), nothing enforced. Requires zero evaluation errors.
  2. `pipeline.py plugin-enforce` flips the config to enforce ONLY when:
     the observe soak was calm (zero errors, block rate inside the
     proposal's preregistered expected bounds), and `trade_executor.py`
     actually reads the `protections` config section — otherwise the
     flip would be a silent no-op, so the pipeline REFUSES instead.

## Version manifest

`versions/<version-id>/`:
`version.json` (id, UTC timestamp, parent version, change summary,
proposal id, tier, change class, author `"evolution-loop"`, status,
gate results), `files/` (the candidate's changed files), `proposal.md`,
`previous_snapshot/` (the tree live before install — the rollback
source), `pytest_output.txt`, `staging_report.json`,
`canary/canary_report.json`. `versions/installed.json` tracks
`{current, previous}`. The previous version is always retained.

## Rollback procedure

```
python3 ~/workspace/mt5/bridge/rollback.py --previous
# or: python3 rollback.py --version <id>
```

Restores `previous_snapshot` over the bridge, restarts the executor via
`relaunch_executor.py`, verifies (daemon alive via `/proc` cmdline;
specs fresh when the market is open — freshness is skipped on closed
weekends by design), journals `pipeline.rollback`, updates
`installed.json`. Works even if the installed version is broken: stdlib
only, no bridge imports. Overrides for sandbox/testing:
`--bridge-root`, `--state-dir`, `--no-restart`,
`EVOLUTION_RELAUNCH_SCRIPT`, `EVOLUTION_DAEMON_SCRIPT`.

## Known environmental test failures

`tests/test_trader.py::test_daily_loss_limit_trips_kill_switch_live` and
`tests/test_trader.py::test_live_mode_writes_command_and_journals_commanded`
fail when the FX market is closed (they expect live `commanded`
decisions but get `skipped:market_closed`). Gate (a) excuses EXACTLY
these two, and only when `market_hours.is_market_open()` is False. Any
other failure, or these two while the market is open, rejects the
version.

## Real-money standard

No "it's only demo" leniency anywhere: staging requires the candidate
to not lose more in total and to strictly improve a risk-adjusted
metric; canary guard trips roll back automatically; post-install asserts
treat the kill switch, risk config, and blocked symbols as inviolable.
A version that "doesn't lose much" fails.

## Files

- `evolve.py` — weekly pass (cron `project-evolution-weekly`).
- `pipeline/` + `pipeline.py` — the install pipeline.
- `rollback.py` — one-command rollback.
- `proposals/<id>/` — proposal.md + patch.diff + tests/ (tier is
  `auto` for everything the pipeline installs; tier `zenas` is
  hard-blocked and refused); `applied.json` registry
  (content-hash dedupe); `idea_queue.json` (incomplete specs wait here;
  evolve.py never half-builds a proposal); `findings_YYYYMMDD.md`.
- `versions/` — manifests, snapshots, reports; `installed.json`.
- `run/evolution.log` — every pipeline/evolve run appends
  `{utc, version/event, gates, decision}`.
- `run/protections_observe.jsonl` — plugin observe evaluations.

## What the pipeline does NOT do

- Never resumes trading; never touches the kill switch except to verify
  it is `"0"`.
- Never touches `blocked_symbols` (GBPZAR is Zenas-only).
- Never changes risk config keys `risk_per_trade_pct` / 
...[truncated 453 chars]
