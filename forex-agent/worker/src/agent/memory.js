/**
 * Real memory, backed by the same D1 table everything else already writes
 * to — not a separate vector store or anything exotic. The bridge already
 * reports back executed/rejected outcomes for every signal, so this table
 * IS the trade history. This module just queries it usefully.
 */

export async function getRecentHistory(db, symbol, limit = 10) {
  const { results } = await db
    .prepare(
      `SELECT direction, status, entry_price, stop_loss, take_profit, created_at
       FROM signals WHERE symbol = ? ORDER BY created_at DESC LIMIT ?`
    )
    .bind(symbol, limit)
    .all();
  return results;
}

export function summarizeHistory(history) {
  if (history.length === 0) {
    return 'No prior signal history for this symbol yet.';
  }

  const counts = history.reduce((acc, h) => {
    acc[h.status] = (acc[h.status] || 0) + 1;
    return acc;
  }, {});

  const parts = Object.entries(counts).map(([status, count]) => `${count} ${status}`);
  return `Last ${history.length} signals for this symbol: ${parts.join(', ')}.`;
}

/**
 * Currently "live" positions from this bot's own perspective — executed
 * and not yet known to be closed, plus anything approved and about to
 * execute. This is what the correlation check in knowledge.js runs against.
 *
 * Caveat: this Worker has no live feed of when MT5 closes a position, so
 * "executed" signals are treated as open until you build a close-reporting
 * route from the bridge — for now this is a reasonable approximation, not
 * a guarantee it reflects your real current exposure.
 */
export async function getActiveExposure(db) {
  const { results } = await db
    .prepare(
      `SELECT symbol, direction, status FROM signals
       WHERE status IN ('executed', 'approved') ORDER BY created_at DESC LIMIT 20`
    )
    .all();
  return results;
}
