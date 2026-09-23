# Fallback-path audit — ~/workspace/mt5/bridge/*.py

Date: 2026-09-23. Auditor: Worker A (trading-mode upgrade).
Method: case-insensitive grep for `demo|fake|synthetic|simulate|simulation|paper|fallback|reconnect|retry|cached|cache` plus manual review of credential loading (`os.environ`) in every bridge script. `trade_executor.py` was audited read-only (owned by Worker B) — findings there are recommendations, not changes.

Verdicts: **acceptable** (keep as-is) · **gate** (must be gated before LIVE exists) · **remove** (delete). I changed exactly one line of production code (see §4); everything else is documented for the coordinator / Worker B. No trading behavior of the running demo experiment was altered.

---

## Findings

### 1. trade_executor.py:432-433, 726-730 — demo-only server gate
`is_demo_server()` returns true only if the server string contains "demo"; `check_gates` step 4 refuses with `skipped:non_demo_server` and a loud flag otherwise.
**Verdict: acceptable.** Fail-closed, loud, no silent substitution. Under the two-mode model Worker B should re-express this as "server identity must match the active mode record" (trading_mode.verify_identity), but the current gate is safe.

### 2. trade_executor.py:65, 399-401, 718-720 — dry_run default true
Executor defaults `dry_run: True`, `trading_enabled: false`; in dry-run zero command bytes are written and decisions are journaled as `intended`.
**Verdict: gate (Worker B).** Not a mode fallback today — it is the experiment's deliberate safety posture — but `dry_run` is a third quasi-state alongside DEMO/LIVE. Recommendation: Worker B maps dry_run onto the mode record (e.g. dry_run only meaningful in DEMO; LIVE requires dry_run=false + authorization), rather than keeping an independent flag that could mask a live intent. **Not changed by me** (file owned by Worker B; behavior load-bearing).

### 3. signal_generator.py:191-194 — synthetic `--test` signal lands in the live signals file
`--test` writes a synthetic EURUSD BUY signal into `nova_signals.jsonl` with only `{"test": True}` in meta. **Nothing in the pipeline filters test signals** (verified: no `test`-flag check anywhere in trade_executor.py). Today dry_run absorbs it; if trading were ever enabled, a synthetic price would be treated as a real signal.
**Verdict: gate (Worker B).** Recommendation: executor adds a `skipped:test_signal` gate for signals carrying `test:true`, journaled. The `--test` flag itself stays — it is load-bearing for Telegram bridge testing. **Not changed by me** (executor owned by Worker B; changing the signal_generator destination would break the bridge test).

### 4. mt5_tunnel.py:30 — hardcoded demo broker host [CHANGED]
`TARGET = "demo.metaquotes.net:443"` was the only place a DEMO-namespace broker address was baked in. A future LIVE terminal using this tunnel would have silently CONNECTed to the demo host.
**Verdict: gate — fixed.** Changed to `TARGET = os.environ.get("MT5_TUNNEL_TARGET", "demo.metaquotes.net:443")`. Default is byte-identical, so the running demo experiment is unaffected; LIVE operation must set `MT5_TUNNEL_TARGET` to its own broker host. This is the only production code change in this audit.

### 5. watch_mt5.py:194-281 — watchdog relaunch after reboot/feed loss
On dead terminal or stale feed it reinstalls Wine if needed and relaunches the **same** terminal via `scripts/run-mt5-tunnel.sh`, which re-authorizes as the same stored demo account (112975129).
**Verdict: acceptable.** Recovery of the identical account, not a reconnect to a different one. No mode/account substitution anywhere in this path.

### 6. signal_bridge.py:183-190 — Telegram send retry
On failed broadcast the file offset is held at the failed line and retried next pass.
**Verdict: acceptable.** Delivery retry to the same destination; no message substitution, no fallback channel.

### 7. tg_commands.py:55-56, 209-210 — credentials from environment only
`BOT_TOKEN`/`CHAT_ID` read from `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`; launch refuses without them. No literals, no defaults, no demo substitution.
**Verdict: acceptable.**

### 8. NovaTrader.mq5:592-599 — EA OnInit DEMO-only hard refusal
The EA refuses to run unless the server name contains "demo".
**Verdict: gate (coordinator).** Load-bearing for the demo experiment — do NOT edit in place while attached to the live demo chart. LIVE mode needs a separately-built EA variant (or a parameterized guard) deployed to its own terminal/profile. Flagged for the coordinator's LIVE build plan.

### 9. trade_report.py:565 — `random.Random(ticket)` in chart rendering
Deterministic per-ticket jitter for schematic chart candles only; output pages are visibly labeled schematic and unresolved trades claim no profit.
**Verdict: acceptable.** Not a price source for trading decisions.

### 10. signal_generator.py:53-71 — candle fetch has no synthetic fallback
`fetch_candles` pulls real Yahoo candles; on failure it prints `fetch failed` and skips the symbol. No cached/placeholder prices are ever substituted.
**Verdict: acceptable.**

---

## Credential-namespace sweep

- All broker/Telegram credentials are env-loaded; **no passwords, tokens, or API keys are hardcoded** in any bridge `.py` file.
- No `DEMO_*`/`LIVE_*` credential variables exist yet; the new `trading_mode.get_profile()` establishes the `DEMO_MT5_*` / `LIVE_MT5_*` namespaces with structural cross-read protection (prefix built from the mode argument).
- `mt5_tunnel.py` change (§4) keeps the demo default; the LIVE host travels via `MT5_TUNNEL_TARGET`, never via code edit.

## Summary for the coordinator

- **Changed:** 1 line (mt5_tunnel.py TARGET env override, default unchanged).
- **Acceptable as-is:** 7 findings (demo gate, watchdog relaunch, bridge retry, env creds, schematic charts, real-price fetch, loud non-demo refusal).
- **Gated for Worker B:** dry_run→mode mapping (§2), `skipped:test_signal` gate (§3).
- **Gated for coordinator:** LIVE EA variant for the OnInit demo-only guard (§8).
- **Removed:** nothing (no genuine silent-fallback path was found; dry_run and the demo gate are deliberate, loud safety postures).
