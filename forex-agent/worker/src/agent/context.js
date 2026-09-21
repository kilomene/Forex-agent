/**
 * This is the "agentic" step: before asking any model for reasoning text,
 * the Worker itself gathers real context — session timing, this symbol's
 * history, and correlated exposure against currently open positions — and
 * hands that bundle to the reasoning layer. The model never has to "know"
 * any of this on its own; it's handed the facts and asked to reason over
 * them, same as you'd brief a junior analyst before asking for an opinion.
 */

import { currentSessionInfo, checkCorrelatedExposure } from './knowledge.js';
import { getRecentHistory, summarizeHistory, getActiveExposure } from './memory.js';
import { getLatestPerformanceSnapshot, getRecentReflections } from '../db.js';

export async function buildContext(db, signal) {
  const session = currentSessionInfo();
  const history = await getRecentHistory(db, signal.symbol, 10);
  const historySummary = summarizeHistory(history);
  const activePositions = await getActiveExposure(db);
  const correlationFlags = checkCorrelatedExposure(signal, activePositions);
  const performanceSnapshot = await getLatestPerformanceSnapshot(db);
  const reflections = await getRecentReflections(db, { symbol: signal.symbol, limit: 5 });

  return {
    session,
    historySummary,
    correlationFlags,
    activePositionCount: activePositions.length,
    smcSummary: signal.smc_summary && signal.smc_summary.available !== false ? signal.smc_summary : null,
    performanceSnapshot: performanceSnapshot || null,
    reflections,
  };
}
