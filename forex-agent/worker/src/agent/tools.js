/**
 * Real tools the agent can call via Anthropic's tool-use API. Each tool
 * has an Anthropic-format schema (for the API call) and an `execute`
 * function (what actually runs when the model requests that tool).
 *
 * Deliberately absent: any tool that places, modifies, or closes a trade.
 * The agent gathers evidence and writes reasoning — it never touches
 * execution. That stays in the bridge's risk.py, outside the LLM's reach.
 */

import { getRecentHistory, summarizeHistory, getActiveExposure } from './memory.js';
import { checkCorrelatedExposure, currentSessionInfo } from './knowledge.js';
import { getLatestPerformanceSnapshot, getRecentReflections } from '../db.js';

// --- Tool: get_recent_ohlc ---
// Real candle data forwarded by the bridge with the signal itself (see
// signal_engine.py's `recent_candles`). Not fetched live — the Worker has
// no direct MT5 connection — but it IS the actual price series MT5 returned.

const getRecentOhlcTool = {
  name: 'get_recent_ohlc',
  description:
    "Returns the most recent OHLC candles for this signal's symbol/timeframe, oldest first. Use this to see the actual price path leading up to the signal, not just the final indicator values.",
  input_schema: {
    type: 'object',
    properties: {
      limit: { type: 'integer', description: 'Max candles to return (default 30, capped at what was provided)' },
    },
  },
};

function executeGetRecentOhlc(input, { signal }) {
  const candles = signal.recent_candles || [];
  if (candles.length === 0) {
    return { available: false, note: 'No OHLC series was provided with this signal.' };
  }
  const limit = Math.min(input.limit || 30, candles.length);
  return { available: true, count: limit, candles: candles.slice(-limit) };
}

// --- Tool: get_indicator_snapshot ---
// The exact EMA/RSI/ATR values the deterministic signal engine computed —
// same numbers already in the signal, exposed as an explicit tool call so
// the agent's reasoning is traceable to a specific lookup rather than just
// numbers dumped in the prompt.

const getIndicatorSnapshotTool = {
  name: 'get_indicator_snapshot',
  description:
    'Returns the EMA(fast), EMA(slow), RSI, and ATR values computed for this signal, plus which crossover triggered it.',
  input_schema: { type: 'object', properties: {} },
};

function executeGetIndicatorSnapshot(_input, { signal }) {
  return {
    ema_fast: signal.ema_fast,
    ema_slow: signal.ema_slow,
    rsi: signal.rsi_value,
    atr: signal.atr_value,
    trigger: signal.trigger,
    timeframe: signal.timeframe,
  };
}

// --- Tool: get_trade_history ---
// Real D1 query — this symbol's actual recent signal/outcome record.

const getTradeHistoryTool = {
  name: 'get_trade_history',
  description:
    "Returns this symbol's recent signal history from our own database — what fired before, and what happened to it (executed, rejected, pending). Use this to check whether recent similar signals worked out.",
  input_schema: {
    type: 'object',
    properties: {
      limit: { type: 'integer', description: 'Max records to return (default 10)' },
    },
  },
};

async function executeGetTradeHistory(input, { db, signal }) {
  const limit = input.limit || 10;
  const history = await getRecentHistory(db, signal.symbol, limit);
  return { count: history.length, summary: summarizeHistory(history), records: history };
}

// --- Tool: get_correlation_exposure ---
// Real check against currently open/approved positions from D1, using the
// static correlation reference table. Ground truth, not the model's guess.

const getCorrelationExposureTool = {
  name: 'get_correlation_exposure',
  description:
    "Checks this signal's symbol against currently open or approved positions for correlated exposure — e.g. flags if you're about to effectively double up on the same USD bet via two different pairs.",
  input_schema: { type: 'object', properties: {} },
};

async function executeGetCorrelationExposure(_input, { db, signal }) {
  const activePositions = await getActiveExposure(db);
  const flags = checkCorrelatedExposure(signal, activePositions);
  return {
    active_position_count: activePositions.length,
    active_positions: activePositions,
    correlation_flags: flags,
  };
}

// --- Tool: get_session_info ---
const getSessionInfoTool = {
  name: 'get_session_info',
  description:
    'Returns which forex trading sessions (Tokyo/London/New York) are currently active and the liquidity implication — thin sessions produce less reliable moves.',
  input_schema: { type: 'object', properties: {} },
};

function executeGetSessionInfo() {
  return currentSessionInfo();
}

// --- Tool: calculate_position_risk ---
// Real pip-value math, not a placeholder. Standard lot = 100,000 units.
// For XXX/USD pairs (USD is the quote currency), pip value per standard
// lot is fixed at ~$10/pip (pip size 0.0001 * 100,000). For USD/JPY, USD
// is the base currency, so pip value depends on the current price.

const calculatePositionRiskTool = {
  name: 'calculate_position_risk',
  description:
    'Given a lot size and stop-loss distance, calculates the real dollar risk on this trade using correct forex pip-value math (accounts for JPY pairs having a different pip value formula than XXX/USD pairs).',
  input_schema: {
    type: 'object',
    properties: {
      lot_size: { type: 'number', description: 'Position size in standard lots, e.g. 0.01' },
    },
    required: ['lot_size'],
  },
};

function executeCalculatePositionRisk(input, { signal }) {
  const { symbol, entry_price, stop_loss, symbol_specs } = signal;
  const lotSize = input.lot_size;
  const stopDistance = Math.abs(entry_price - stop_loss);

  // Prefer REAL broker-specified contract values, forwarded by the bridge
  // with the signal (mt5_client.get_symbol_trade_specs) — this is what
  // makes this calculation correct for metals/indices/stocks, not just
  // forex, and it's exact rather than an assumption. Falls back to the
  // old JPY-vs-not forex heuristic only if the bridge's specs fetch
  // failed for some reason (e.g. a transient MT5 error) — better to give
  // an approximate answer than none, but the fallback path is honestly
  // labeled as an estimate in the response either way.
  if (symbol_specs && symbol_specs.tick_value > 0 && symbol_specs.tick_size > 0) {
    const ticksAtRisk = stopDistance / symbol_specs.tick_size;
    const dollarRisk = ticksAtRisk * symbol_specs.tick_value * lotSize;
    return {
      symbol,
      lot_size: lotSize,
      stop_distance_ticks: Number(ticksAtRisk.toFixed(1)),
      estimated_dollar_risk: Number(dollarRisk.toFixed(2)),
      note: 'Calculated from real broker-specified contract values (tick_value/tick_size), not an estimate.',
    };
  }

  const isJpyPair = symbol.includes('JPY');
  const pipSize = isJpyPair ? 0.01 : 0.0001;
  const pipsAtRisk = stopDistance / pipSize;
  const pipValuePerStandardLot = isJpyPair ? 1000 / entry_price : 10;
  const dollarRisk = pipsAtRisk * pipValuePerStandardLot * lotSize;

  return {
    symbol,
    lot_size: lotSize,
    stop_distance_pips: Number(pipsAtRisk.toFixed(1)),
    estimated_dollar_risk: Number(dollarRisk.toFixed(2)),
    note: 'Broker trade specs were not available for this signal, so this is a forex-only estimate (assumes a USD-denominated account) — may not be accurate for non-forex instruments.',
  };
}

// --- Tool: get_smc_patterns ---
// Real structure analysis forwarded by the bridge's smc_analysis.py —
// swing points, market structure/BOS, trend lines, S/R zones, liquidity
// zones, fair value gaps, and order blocks. Computed deterministically
// against real candles, not the model's interpretation of a chart image.

const getSmcPatternsTool = {
  name: 'get_smc_patterns',
  description:
    "Returns Smart Money Concepts structure analysis for this signal's symbol: market structure/trend, recent structure breaks (BOS), support/resistance zones, liquidity zones (equal highs/lows), fair value gaps, and order blocks. Use this to see whether the signal aligns with or fights the broader structure.",
  input_schema: { type: 'object', properties: {} },
};

function executeGetSmcPatterns(_input, { signal }) {
  if (!signal.smc_summary || signal.smc_summary.available === false) {
    return { available: false, note: 'No SMC structure analysis was provided with this signal.' };
  }
  return signal.smc_summary;
}

// --- Tool: get_performance_stats ---
// Real D1 query against the latest performance snapshot, which itself
// came from MT5's actual deal history (performance_review.py) — this is
// the bot reviewing its own real track record, not a self-assessment
// based on vibes.

const getPerformanceStatsTool = {
  name: 'get_performance_stats',
  description:
    "Returns this bot's real recent trading performance (win rate, profit factor, total P/L) computed from actual MT5 account history. Use this to check whether you've been performing well before weighing a new signal too heavily.",
  input_schema: { type: 'object', properties: {} },
};

async function executeGetPerformanceStats(_input, { db }) {
  const snapshot = await getLatestPerformanceSnapshot(db);
  if (!snapshot) {
    return { available: false, note: 'No performance snapshot has been reported yet.' };
  }
  return { available: true, ...snapshot, by_symbol: JSON.parse(snapshot.by_symbol || '{}') };
}

// --- Tool: get_past_reflections ---
// Real D1 query — actual self-review text generated after past trades
// closed, comparing the original reasoning to what really happened. This
// is the retrieval half of "reflection" — the agent can check whether
// it's about to repeat a documented mistake.

const getPastReflectionsTool = {
  name: 'get_past_reflections',
  description:
    "Returns past self-reflections on this symbol — honest post-trade reviews comparing what was reasoned beforehand to what actually happened, including what went wrong. Use this to check whether the current setup resembles a past mistake.",
  input_schema: {
    type: 'object',
    properties: {
      limit: { type: 'integer', description: 'Max reflections to return (default 5)' },
    },
  },
};

async function executeGetPastReflections(input, { db, signal }) {
  const limit = input.limit || 5;
  const reflections = await getRecentReflections(db, { symbol: signal.symbol, limit });
  return { count: reflections.length, reflections };
}

// --- Tool: get_economic_calendar ---
// Contract-only by design. I don't have verified, current details of a
// working free forex economic-calendar API in training data, and shipping
// code against guessed endpoint specifics would likely just hand you a
// broken integration. This calls YOUR configured source instead — a paid
// calendar API you've wired up, or something you self-host — using the
// same honest "not configured" pattern as the custom AI provider.
//
// Contract:
//   GET env.NEWS_CALENDAR_URL?symbol=<symbol>&hours_ahead=<n>
//   Headers: Authorization: Bearer <NEWS_CALENDAR_API_KEY>  (if set)
//   Expected response: { "events": [{ "time": "...", "currency": "...",
//     "impact": "high"|"medium"|"low", "title": "..." }] }

const getEconomicCalendarTool = {
  name: 'get_economic_calendar',
  description:
    "Returns upcoming high-impact economic events for this symbol's currencies, if a calendar source has been configured. Use this to check whether a major news release is imminent — trading into one is materially different risk than normal conditions.",
  input_schema: {
    type: 'object',
    properties: {
      hours_ahead: { type: 'integer', description: 'How many hours ahead to check (default 24)' },
    },
  },
};

async function executeGetEconomicCalendar(input, { signal, env }) {
  if (!env.NEWS_CALENDAR_URL) {
    return {
      available: false,
      note: 'No economic calendar source configured. Set NEWS_CALENDAR_URL to a real calendar API or a self-hosted source implementing this tool\'s contract.',
    };
  }

  const hoursAhead = input.hours_ahead || 24;
  const headers = {};
  if (env.NEWS_CALENDAR_API_KEY) headers.Authorization = `Bearer ${env.NEWS_CALENDAR_API_KEY}`;

  const url = `${env.NEWS_CALENDAR_URL}?symbol=${encodeURIComponent(signal.symbol)}&hours_ahead=${hoursAhead}`;
  const response = await fetch(url, { headers });
  if (!response.ok) {
    return { available: false, note: `Calendar source returned ${response.status}` };
  }
  const data = await response.json();
  return { available: true, events: data.events || [] };
}

// --- Tool: get_ml_prediction ---
// Contract-only, honestly reporting unavailable until a real trained model
// exists behind it. XGBoost/LightGBM cannot be trained without a labeled
// backtest dataset, which doesn't exist yet — this tool is the plumbing
// for when it does, not a placeholder pretending to work now.
//
// Contract:
//   POST env.ML_MODEL_URL  { "features": { ema_fast, ema_slow, rsi, atr,
//     smc_trend, ... } }
//   Expected response: { "win_probability": 0.0-1.0, "model_version": "..." }

const getMlPredictionTool = {
  name: 'get_ml_prediction',
  description:
    'Returns a trained model win-probability estimate for this setup, if an ML model endpoint has been configured. Currently unavailable in most setups — no labeled backtest dataset exists yet to train one. Do not treat the absence of this tool as a negative signal.',
  input_schema: { type: 'object', properties: {} },
};

async function executeGetMlPrediction(_input, { signal, env }) {
  if (!env.ML_MODEL_URL) {
    return {
      available: false,
      note: 'No ML model endpoint configured. A trained XGBoost/LightGBM model requires a backtest/labeling pipeline that has not been built yet — this is expected, not an error.',
    };
  }

  const headers = { 'Content-Type': 'application/json' };
  if (env.ML_MODEL_API_KEY) headers.Authorization = `Bearer ${env.ML_MODEL_API_KEY}`;

  // Feature schema MUST match forex-bot-bridge/ml_features.py's
  // FEATURE_COLUMNS exactly — this is the training-time/inference-time
  // contract. If one side changes, both must change together.
  const trend = signal.smc_summary?.market_structure?.trend ?? 'ranging';
  const emaSpreadPct =
    signal.ema_fast != null && signal.ema_slow != null && signal.entry_price
      ? (signal.ema_fast - signal.ema_slow) / signal.entry_price
      : 0;

  const features = {
    ema_fast: signal.ema_fast,
    ema_slow: signal.ema_slow,
    rsi_value: signal.rsi_value,
    atr_value: signal.atr_value,
    ema_spread_pct: emaSpreadPct,
    smc_trend_uptrend: trend === 'uptrend' ? 1 : 0,
    smc_trend_downtrend: trend === 'downtrend' ? 1 : 0,
    smc_trend_ranging: trend === 'ranging' ? 1 : 0,
  };

  const response = await fetch(env.ML_MODEL_URL, {
    method: 'POST',
    headers,
    body: JSON.stringify({ features }),
  });
  if (!response.ok) {
    return { available: false, note: `Model endpoint returned ${response.status}` };
  }
  const data = await response.json();
  return { available: true, win_probability: data.win_probability, model_version: data.model_version };
}

// --- Registry ---

export const TOOL_DEFINITIONS = [
  getRecentOhlcTool,
  getIndicatorSnapshotTool,
  getTradeHistoryTool,
  getCorrelationExposureTool,
  getSessionInfoTool,
  calculatePositionRiskTool,
  getSmcPatternsTool,
  getPerformanceStatsTool,
  getPastReflectionsTool,
  getEconomicCalendarTool,
  getMlPredictionTool,
];

const EXECUTORS = {
  get_recent_ohlc: executeGetRecentOhlc,
  get_indicator_snapshot: executeGetIndicatorSnapshot,
  get_trade_history: executeGetTradeHistory,
  get_correlation_exposure: executeGetCorrelationExposure,
  get_session_info: executeGetSessionInfo,
  calculate_position_risk: executeCalculatePositionRisk,
  get_smc_patterns: executeGetSmcPatterns,
  get_performance_stats: executeGetPerformanceStats,
  get_past_reflections: executeGetPastReflections,
  get_economic_calendar: executeGetEconomicCalendar,
  get_ml_prediction: executeGetMlPrediction,
};

/**
 * Executes one tool call. Never throws — a broken tool call becomes a
 * tool_result the model can see and reason about, rather than crashing
 * the whole agent loop.
 */
export async function executeTool(name, input, ctx) {
  const fn = EXECUTORS[name];
  if (!fn) {
    return { error: `Unknown tool: ${name}` };
  }
  try {
    return await fn(input, ctx);
  } catch (err) {
    return { error: String(err) };
  }
}
