/**
 * All D1 access for the signals table in one place.
 */

function newId() {
  return crypto.randomUUID();
}

function now() {
  return new Date().toISOString();
}

export async function insertSignal(db, signal, agentResult = {}) {
  const id = newId();
  const ts = now();
  const {
    reasoning = null,
    confidence = null,
    risk_notes = null,
    tool_calls = null,
    decision = null,
  } = agentResult;

  await db
    .prepare(
      `INSERT INTO signals (
        id, symbol, timeframe, direction, entry_price, stop_loss, take_profit,
        ema_fast, ema_slow, rsi_value, atr_value, candle_time, trigger_text,
        ai_reasoning, ai_confidence, ai_risk_notes, ai_tool_calls, ai_decision,
        recent_candles, smc_summary, symbol_specs, status, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)`
    )
    .bind(
      id,
      signal.symbol,
      signal.timeframe,
      signal.direction,
      signal.entry_price,
      signal.stop_loss,
      signal.take_profit,
      signal.ema_fast ?? null,
      signal.ema_slow ?? null,
      signal.rsi_value ?? null,
      signal.atr_value ?? null,
      signal.candle_time,
      signal.trigger ?? null,
      reasoning,
      confidence,
      risk_notes,
      tool_calls ? JSON.stringify(tool_calls) : null,
      decision,
      signal.recent_candles ? JSON.stringify(signal.recent_candles) : null,
      signal.smc_summary ? JSON.stringify(signal.smc_summary) : null,
      signal.symbol_specs ? JSON.stringify(signal.symbol_specs) : null,
      ts,
      ts
    )
    .run();

  return id;
}

export async function getSignal(db, id) {
  return db.prepare(`SELECT * FROM signals WHERE id = ?`).bind(id).first();
}

export async function listSignals(db, { status, limit = 50 } = {}) {
  if (status) {
    const { results } = await db
      .prepare(`SELECT * FROM signals WHERE status = ? ORDER BY created_at DESC LIMIT ?`)
      .bind(status, limit)
      .all();
    return results;
  }
  const { results } = await db
    .prepare(`SELECT * FROM signals ORDER BY created_at DESC LIMIT ?`)
    .bind(limit)
    .all();
  return results;
}

export async function getApprovedPending(db) {
  // "Approved but not yet executed" = status is exactly 'approved'.
  const { results } = await db
    .prepare(`SELECT * FROM signals WHERE status = 'approved' ORDER BY created_at ASC`)
    .all();
  return results;
}

export async function setStatus(db, id, status, extra = {}) {
  const ts = now();
  const fields = ['status = ?', 'updated_at = ?'];
  const values = [status, ts];

  if ('mt5_result' in extra) {
    fields.push('mt5_result = ?');
    values.push(JSON.stringify(extra.mt5_result));
  }
  if ('rejection_reason' in extra) {
    fields.push('rejection_reason = ?');
    values.push(extra.rejection_reason);
  }
  // Set by POST /signals/:id/failed — the bridge's MT5 order_send failure
  // detail. Requires migration 0002 (adds failure_reason to signals).
  if ('failure_reason' in extra) {
    fields.push('failure_reason = ?');
    values.push(extra.failure_reason);
  }

  values.push(id);
  await db.prepare(`UPDATE signals SET ${fields.join(', ')} WHERE id = ?`).bind(...values).run();
}

export async function upsertDeviceToken(db, token) {
  const ts = now();
  await db
    .prepare(
      `INSERT INTO device_tokens (token, registered_at, last_seen_at) VALUES (?, ?, ?)
       ON CONFLICT(token) DO UPDATE SET last_seen_at = excluded.last_seen_at`
    )
    .bind(token, ts, ts)
    .run();
}

export async function getAllDeviceTokens(db) {
  const { results } = await db.prepare(`SELECT token FROM device_tokens`).all();
  return results.map((r) => r.token);
}

// --- Settings ---

export async function getSetting(db, key, defaultValue = null) {
  const row = await db.prepare(`SELECT value FROM settings WHERE key = ?`).bind(key).first();
  return row ? row.value : defaultValue;
}

export async function setSetting(db, key, value) {
  const ts = now();
  await db
    .prepare(
      `INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
       ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`
    )
    .bind(key, value, ts)
    .run();
}

/**
 * 'manual' (default, safe) or 'autonomous'. Defaults to manual so a fresh
 * deploy — or a DB row that was never set — never silently trades without
 * approval.
 */
export async function getTradingMode(db) {
  const mode = await getSetting(db, 'trading_mode', 'manual');
  return mode === 'autonomous' ? 'autonomous' : 'manual';
}

// --- Kill switch ---
// Two independent mechanisms:
//   1. `kill_switch_engaged` — a persistent toggle. While true, NO new
//      entry can happen, autonomous or manual — checked at every gate
//      (autonomous auto-approval, manual /approve, and the bridge's own
//      risk.py as a third, independent check — defense in depth, the
//      same principle the whole risk-engine architecture is built on).
//   2. `kill_switch_close_all_requested_at` — a one-shot timestamp
//      marker, not a toggle. Setting it asks the bridge to immediately
//      force-close every open position on its next (fast) poll cycle,
//      bypassing all normal exit logic. Cleared once the bridge
//      acknowledges completion.

export async function isKillSwitchEngaged(db) {
  const value = await getSetting(db, 'kill_switch_engaged', 'false');
  return value === 'true';
}

export async function setKillSwitchEngaged(db, engaged) {
  await setSetting(db, 'kill_switch_engaged', engaged ? 'true' : 'false');
}

export async function requestCloseAll(db) {
  await setSetting(db, 'kill_switch_close_all_requested_at', new Date().toISOString());
}

export async function getCloseAllRequestedAt(db) {
  const value = await getSetting(db, 'kill_switch_close_all_requested_at', '');
  return value || null;
}

export async function clearCloseAllRequest(db) {
  await setSetting(db, 'kill_switch_close_all_requested_at', '');
}

// --- API clients (per-client credentials) ---
// Provisioned records created by scripts/provision-client.mjs. Secrets are
// stored as SHA-256 hex digests only — never raw secrets. Revoked clients
// are rejected by auth.js without distinguishing the failure reason.

export async function getApiClient(db, clientId) {
  return db
    .prepare(`SELECT client_id, secret_hash, revoked, last_used_at FROM api_clients WHERE client_id = ?`)
    .bind(clientId)
    .first();
}

export async function upsertApiClient(db, clientId, secretHash, label = null) {
  const ts = now();
  await db
    .prepare(
      `INSERT INTO api_clients (id, client_id, secret_hash, label, revoked, created_at)
       VALUES (?, ?, ?, ?, 0, ?)
       ON CONFLICT(client_id) DO UPDATE SET
         secret_hash = excluded.secret_hash, label = excluded.label, revoked = 0`
    )
    .bind(newId(), clientId, secretHash, label, ts)
    .run();
}

export async function revokeApiClient(db, clientId) {
  await db.prepare(`UPDATE api_clients SET revoked = 1 WHERE client_id = ?`).bind(clientId).run();
}

/** Best-effort usage stamp; callers must never fail a request on this. */
export async function touchApiClient(db, clientId) {
  await db
    .prepare(`UPDATE api_clients SET last_used_at = ? WHERE client_id = ?`)
    .bind(now(), clientId)
    .run();
}

// --- Position close reporting (from exit_manager.py) ---

export async function markClosed(db, id, { reason, price, commission, swap }) {
  const ts = now();
  await db
    .prepare(
      `UPDATE signals SET status = 'closed', closed_reason = ?, closed_price = ?, closed_at = ?, updated_at = ?, commission = ?, swap = ?
       WHERE id = ?`
    )
    .bind(reason, price, ts, ts, commission ?? null, swap ?? null, id)
    .run();
}

export async function setExecutionDetails(db, id, { spreadAtEntry, lotSize }) {
  const ts = now();
  await db
    .prepare(`UPDATE signals SET spread_at_entry = ?, lot_size = ?, updated_at = ? WHERE id = ?`)
    .bind(spreadAtEntry ?? null, lotSize ?? null, ts, id)
    .run();
}

// --- Performance snapshots ---

export async function insertPerformanceSnapshot(db, snapshot) {
  const id = newId();
  const ts = now();
  await db
    .prepare(
      `INSERT INTO performance_snapshots (
        id, period_start, period_end, total_closed_trades, wins, losses, breakeven,
        win_rate_pct, total_profit, average_win, average_loss, profit_factor,
        largest_win, largest_loss, by_symbol, total_commission, total_swap, net_profit, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
    )
    .bind(
      id,
      snapshot.period_start,
      snapshot.period_end,
      snapshot.total_closed_trades,
      snapshot.wins,
      snapshot.losses,
      snapshot.breakeven,
      snapshot.win_rate_pct ?? null,
      snapshot.total_profit,
      snapshot.average_win ?? null,
      snapshot.average_loss ?? null,
      snapshot.profit_factor ?? null,
      snapshot.largest_win ?? null,
      snapshot.largest_loss ?? null,
      JSON.stringify(snapshot.by_symbol || {}),
      snapshot.total_commission ?? null,
      snapshot.total_swap ?? null,
      snapshot.net_profit ?? null,
      ts
    )
    .run();
  return id;
}

export async function getLatestPerformanceSnapshot(db) {
  return db
    .prepare(`SELECT * FROM performance_snapshots ORDER BY created_at DESC LIMIT 1`)
    .first();
}

// --- Reflections ---

export async function insertReflection(db, reflection) {
  const id = newId();
  const ts = now();
  await db
    .prepare(
      `INSERT INTO reflections (
        id, signal_id, symbol, direction, outcome, entry_price, closed_price,
        original_reasoning, original_confidence, reflection_text, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
    )
    .bind(
      id,
      reflection.signal_id,
      reflection.symbol,
      reflection.direction,
      reflection.outcome,
      reflection.entry_price,
      reflection.closed_price,
      reflection.original_reasoning ?? null,
      reflection.original_confidence ?? null,
      reflection.reflection_text,
      ts
    )
    .run();
  return id;
}

export async function getRecentReflections(db, { symbol, limit = 5 } = {}) {
  if (symbol) {
    const { results } = await db
      .prepare(`SELECT * FROM reflections WHERE symbol = ? ORDER BY created_at DESC LIMIT ?`)
      .bind(symbol, limit)
      .all();
    return results;
  }
  const { results } = await db
    .prepare(`SELECT * FROM reflections ORDER BY created_at DESC LIMIT ?`)
    .bind(limit)
    .all();
  return results;
}

// --- Chart data (app's Charts tab, independent of the signal pipeline) ---

export async function upsertChartData(db, { symbol, timeframe, candles, ema_fast, ema_slow, ema_fast_period, ema_slow_period }) {
  const ts = new Date().toISOString();
  await db
    .prepare(
      `INSERT INTO chart_data (symbol, timeframe, candles, ema_fast, ema_slow, ema_fast_period, ema_slow_period, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(symbol, timeframe) DO UPDATE SET
         candles = excluded.candles, ema_fast = excluded.ema_fast, ema_slow = excluded.ema_slow,
         ema_fast_period = excluded.ema_fast_period, ema_slow_period = excluded.ema_slow_period,
         updated_at = excluded.updated_at`
    )
    .bind(
      symbol,
      timeframe,
      JSON.stringify(candles || []),
      ema_fast ? JSON.stringify(ema_fast) : null,
      ema_slow ? JSON.stringify(ema_slow) : null,
      ema_fast_period ?? null,
      ema_slow_period ?? null,
      ts
    )
    .run();
}

export async function getChartData(db, symbol, timeframe) {
  return db
    .prepare(`SELECT * FROM chart_data WHERE symbol = ? AND timeframe = ?`)
    .bind(symbol, timeframe)
    .first();
}

/** Every symbol/timeframe combination currently being reported — lets the
 * app's pickers only offer what's actually available, not guess at it. */
export async function listAvailableCharts(db) {
  const { results } = await db
    .prepare(`SELECT symbol, timeframe, updated_at FROM chart_data ORDER BY symbol, timeframe`)
    .all();
  return results;
}
