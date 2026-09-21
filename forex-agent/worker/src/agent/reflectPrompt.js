/**
 * Builds the reflection prompt: the five self-review questions, grounded
 * in the ORIGINAL reasoning (what the agent actually said before the
 * trade) against the ACTUAL outcome (what really happened). Without both
 * halves this would just be generic trading advice, not a real reflection
 * on a specific decision.
 */

export function buildReflectionPrompt({ signal, outcome, reflectionHistory }) {
  const pnlDirection = outcome === 'win' ? 'profitable' : outcome === 'loss' ? 'a loss' : 'breakeven';

  const historyLines =
    reflectionHistory && reflectionHistory.length > 0
      ? `\nPAST REFLECTIONS ON ${signal.symbol} (most recent first):\n${reflectionHistory
          .map((r, i) => `${i + 1}. [${r.outcome}] ${r.reflection_text}`)
          .join('\n')}`
      : '';

  return `You are Zenas, ALPHA's trading agent. A trade you reasoned about has now closed. Review it honestly.

ORIGINAL SIGNAL
Symbol: ${signal.symbol}
Direction: ${signal.direction}
Entry: ${signal.entry_price}
Stop loss: ${signal.stop_loss}
Take profit: ${signal.take_profit}

ORIGINAL REASONING (written before the outcome was known):
${signal.ai_reasoning || 'No reasoning was recorded for this signal.'}
Stated confidence at the time: ${signal.ai_confidence != null ? signal.ai_confidence + '%' : 'not recorded'}

ACTUAL OUTCOME
Result: ${pnlDirection} (closed at ${signal.closed_price}, entered at ${signal.entry_price})
${historyLines}

Answer honestly and specifically — this is for future reference, not a performance review to feel good about:
1. Why was this trade taken — what was the actual justification?
2. Was that reasoning correct, given what actually happened?
3. What signals (if any) were misleading, or what did the original reasoning miss?
4. What could have been done differently — a different entry, a tighter/wider stop, or skipping it entirely?
5. Does this match a pattern from past reflections above — is this a mistake that's happened before?

Respond with ONLY a JSON object, no other text:
{"reflection": "3-5 sentences answering the questions above, specific to this trade, not generic trading advice"}`;
}
