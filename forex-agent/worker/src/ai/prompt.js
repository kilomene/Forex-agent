/**
 * Shared prompt construction. Every provider gets the exact same prompt —
 * the only thing that differs between providers is the HTTP call shape.
 *
 * The prompt now includes real gathered context (session timing, this
 * symbol's history, correlated-exposure flags) rather than just the raw
 * signal numbers — this is what lets a small local model reason usefully
 * instead of just describing the indicators back at you.
 */

export function buildPrompt(signal, context = {}) {
  const { session, historySummary, correlationFlags = [], smcSummary, performanceSnapshot, reflections = [] } = context;

  const sessionLine = session
    ? `Current session: ${session.active_sessions.join(', ') || 'none major'} (UTC hour ${session.utc_hour}). ${session.liquidity_note}.`
    : '';

  const correlationLines =
    correlationFlags.length > 0
      ? `\nCorrelation warnings:\n${correlationFlags.map((f) => `- ${f}`).join('\n')}`
      : '\nNo correlated-exposure conflicts detected against currently open/approved positions.';

  const smcLines = smcSummary
    ? `\nMARKET STRUCTURE\nTrend: ${smcSummary.market_structure?.trend || 'unknown'}\nRecent structure breaks: ${JSON.stringify(smcSummary.recent_structure_breaks || [])}\nNearby support/resistance: ${JSON.stringify(smcSummary.support_resistance_near_price || [])}\nLiquidity zones: ${JSON.stringify(smcSummary.liquidity_zones || {})}\nRecent fair value gaps: ${JSON.stringify(smcSummary.fair_value_gaps_recent || [])}\nRecent order blocks: ${JSON.stringify(smcSummary.order_blocks_recent || [])}`
    : '\nMARKET STRUCTURE\nNo structure analysis available for this signal.';

  const performanceLines = performanceSnapshot
    ? `\nBOT PERFORMANCE (last ${performanceSnapshot.total_closed_trades} closed trades): win rate ${performanceSnapshot.win_rate_pct ?? 'n/a'}%, gross P/L ${performanceSnapshot.total_profit}, net P/L after real commission+swap ${performanceSnapshot.net_profit ?? 'n/a'}, profit factor ${performanceSnapshot.profit_factor ?? 'n/a'}`
    : '\nBOT PERFORMANCE\nNo performance history reported yet.';

  const reflectionLines =
    reflections.length > 0
      ? `\nPAST REFLECTIONS ON THIS SYMBOL (weigh these seriously — this is the system's own honest record of what's gone wrong before):\n${reflections
          .map((r, i) => `${i + 1}. [${r.outcome}] ${r.reflection_text}`)
          .join('\n')}`
      : '\nPAST REFLECTIONS\nNone recorded yet for this symbol.';

  return `You are Zenas, the trading agent for an automated forex system called ALPHA — you are the decision-maker, not just a commentator. You are given real gathered context below — use it, don't guess beyond it. Do not invent information not present. A deterministic risk engine still runs after your decision and can block or resize the trade, but it never originates a trade — that only happens if you decide to enter.

SIGNAL
Symbol: ${signal.symbol}
Timeframe: ${signal.timeframe}
Direction: ${signal.direction}
Entry price: ${signal.entry_price}
Stop loss: ${signal.stop_loss}
Take profit: ${signal.take_profit}
EMA fast: ${signal.ema_fast}
EMA slow: ${signal.ema_slow}
RSI: ${signal.rsi_value}
ATR: ${signal.atr_value}
Trigger: ${signal.trigger}

MARKET CONTEXT
${sessionLine}
${correlationLines}
${smcLines}
${performanceLines}
${reflectionLines}

HISTORY
${historySummary || 'No history available.'}

Respond with ONLY a JSON object, no other text, in this exact shape:
{"decision": "enter" | "skip", "reasoning": "2-4 sentences explaining the decision grounded in the numbers and context above", "confidence": <integer 0-100 reflecting how well the evidence supports entering, not a prediction of profit>, "risk_notes": "any genuine concerns from the correlation warnings, session liquidity, market structure, performance history, past reflections, or general history above — empty string if none"}

"decision" is your actual call — "skip" is a completely valid answer when the evidence is weak or matches a past mistake.`;
}
