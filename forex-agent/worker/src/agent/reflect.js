/**
 * Runs when a signal closes: generates a structured reflection comparing
 * the original reasoning to the actual outcome, and stores it so future
 * decisions (via the get_past_reflections tool) can reference it.
 *
 * Honest framing: this is retrieval-augmented experience, not learning in
 * any deep sense. The underlying model's weights never change — what
 * changes is that future prompts can include text like "past reflection:
 * you took a similar EURUSD long against a bearish order block and it
 * lost, reasoning cited RSI momentum but ignored structure." That's a real
 * mechanism with real (if bounded) value: a model that's handed its own
 * past mistake is more likely to weigh it than one reasoning from zero
 * context every time — but it's probabilistic, not guaranteed, and it's
 * exactly as fallible as any other piece of context it might ignore.
 */

import { askProvider } from '../ai/index.js';
import { buildReflectionPrompt } from './reflectPrompt.js';
import { insertReflection, getRecentReflections } from '../db.js';
import { tryParseStructured } from './parse.js';
import { applyDynamicAiSettings } from './dynamicSettings.js';

/**
 * signal: the full D1 row for the closed signal (includes ai_reasoning,
 * ai_confidence, entry_price, closed_price, direction, symbol).
 * Returns the reflection text (or null if generation failed) — storing
 * still happens with a fallback note even on failure, so there's a
 * record that a close happened even without a generated reflection.
 */
export async function runReflection(db, signal, outcome, env) {
  env = await applyDynamicAiSettings(db, env);

  const reflectionHistory = await getRecentReflections(db, { symbol: signal.symbol, limit: 5 });

  const prompt = buildReflectionPrompt({ signal, outcome, reflectionHistory });
  const raw = await askProvider(prompt, env);
  const parsed = tryParseStructured(raw);

  const reflectionText =
    parsed && typeof parsed.reflection === 'string'
      ? parsed.reflection
      : raw || 'Reflection generation failed — no text was produced.';

  await insertReflection(db, {
    signal_id: signal.id,
    symbol: signal.symbol,
    direction: signal.direction,
    outcome,
    entry_price: signal.entry_price,
    closed_price: signal.closed_price,
    original_reasoning: signal.ai_reasoning,
    original_confidence: signal.ai_confidence,
    reflection_text: reflectionText,
  });

  return reflectionText;
}

/** win/loss/breakeven from entry vs close price and direction — plain math, not opinion. */
export function computeOutcome(direction, entryPrice, closedPrice) {
  const diff = direction === 'BUY' ? closedPrice - entryPrice : entryPrice - closedPrice;
  if (diff > 0) return 'win';
  if (diff < 0) return 'loss';
  return 'breakeven';
}
