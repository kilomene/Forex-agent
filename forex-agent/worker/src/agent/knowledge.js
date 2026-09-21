/**
 * Static forex domain knowledge. This is real market structure, not
 * generic trivia — but it's intentionally static reference data, not
 * live-computed. Session times are fixed UTC windows (don't shift for
 * DST edge cases perfectly); correlations are commonly-cited long-run
 * approximations, not a rolling calculation off your own price feed.
 *
 * If you want live-calculated correlation later, that would be computed
 * from actual candle data in the bridge (it already pulls candles for all
 * four pairs every scan) rather than hardcoded here — flagging that as a
 * real upgrade path, not pretending this static table is equivalent.
 */

export const SESSIONS_UTC = {
  tokyo: { start: 0, end: 9 },
  london: { start: 8, end: 17 },
  new_york: { start: 13, end: 22 },
};

export function currentSessionInfo(date = new Date()) {
  const hour = date.getUTCHours();
  const active = [];
  for (const [name, { start, end }] of Object.entries(SESSIONS_UTC)) {
    if (hour >= start && hour < end) active.push(name);
  }

  const overlaps = [];
  if (active.includes('tokyo') && active.includes('london')) overlaps.push('tokyo/london');
  if (active.includes('london') && active.includes('new_york')) overlaps.push('london/new_york');

  return {
    utc_hour: hour,
    active_sessions: active,
    overlaps,
    liquidity_note:
      overlaps.length > 0
        ? `${overlaps.join(', ')} overlap — typically the highest liquidity and volatility window`
        : active.length === 0
        ? 'Outside major session hours — typically thin liquidity, wider spreads, less reliable moves'
        : `Only ${active.join(', ')} session active — moderate liquidity`,
  };
}

// Commonly-cited long-run correlation direction between majors.
// "positive" = pairs tend to move the same direction, "negative" = opposite.
// This is a reference heuristic, not a computed statistic.
const CORRELATION_REFERENCE = {
  EURUSD: { GBPUSD: 'positive', USDJPY: 'negative', AUDUSD: 'positive' },
  GBPUSD: { EURUSD: 'positive', USDJPY: 'negative', AUDUSD: 'positive' },
  USDJPY: { EURUSD: 'negative', GBPUSD: 'negative', AUDUSD: 'negative' },
  AUDUSD: { EURUSD: 'positive', GBPUSD: 'positive', USDJPY: 'negative' },

  // Indices: broadly move together (risk-on/risk-off), commonly-cited
  // long-run relationships, same static-reference caveat as the forex
  // table above — not a computed statistic.
  US30: { US100: 'positive', US500: 'positive' },
  US100: { US30: 'positive', US500: 'positive', AAPL: 'positive' },
  US500: { US30: 'positive', US100: 'positive' },

  // A stock correlates with an index it's a major component of — AAPL is
  // one of the largest Nasdaq-100 weights, so US100 exposure and AAPL
  // exposure aren't as independent as they might look at a glance.
  AAPL: { US100: 'positive' },

  // Gold commonly moves inversely to USD strength — a very well-known
  // relationship, though far from a precise or constant one.
  XAUUSD: { EURUSD: 'positive', USDJPY: 'negative' },
};

export function correlatedPairs(symbol) {
  return CORRELATION_REFERENCE[symbol] || {};
}

/**
 * Given a new signal and a list of currently open/approved positions,
 * flags correlated exposure — e.g. going long EURUSD while already long
 * GBPUSD is effectively doubling down on the same USD-weakness bet.
 */
export function checkCorrelatedExposure(newSignal, openPositions) {
  const correlations = correlatedPairs(newSignal.symbol);
  const flags = [];

  for (const pos of openPositions) {
    if (pos.symbol === newSignal.symbol) continue;
    const relation = correlations[pos.symbol];
    if (!relation) continue;

    const sameDirection = pos.direction === newSignal.direction;
    const effectivelyStacked =
      (relation === 'positive' && sameDirection) || (relation === 'negative' && !sameDirection);

    if (effectivelyStacked) {
      flags.push(
        `Correlated exposure: open ${pos.direction} ${pos.symbol} position is ${relation}ly ` +
          `correlated with this ${newSignal.direction} ${newSignal.symbol} signal — this may be ` +
          `effectively doubling the same directional bet rather than diversifying.`
      );
    }
  }

  return flags;
}
