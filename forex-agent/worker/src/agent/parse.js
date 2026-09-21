/**
 * Parses a model's final text into {reasoning, confidence, risk_notes}.
 * Shared between the tool-calling agent loop (agentLoop.js) and the
 * single-shot fallback path (used for openrouter/custom providers).
 *
 * Deliberately defensive — see reasoning.js history for why: local models
 * don't reliably return clean JSON, and even Claude sometimes wraps it in
 * a sentence. This tries progressively looser extraction before giving up.
 */

export function tryParseStructured(raw) {
  if (!raw) return null;

  try {
    return JSON.parse(raw);
  } catch {
    // fall through
  }

  const fenced = raw.match(/```(?:json)?\s*([\s\S]*?)\s*```/i);
  if (fenced) {
    try {
      return JSON.parse(fenced[1]);
    } catch {
      // fall through
    }
  }

  const braceMatch = raw.match(/\{[\s\S]*\}/);
  if (braceMatch) {
    try {
      return JSON.parse(braceMatch[0]);
    } catch {
      // fall through
    }
  }

  return null;
}

export function clampConfidence(value) {
  const n = Number(value);
  if (Number.isNaN(n)) return null;
  return Math.max(0, Math.min(100, Math.round(n)));
}

/** Turns a parsed-or-raw model response into the final result shape. */
export function toResult(raw, parsed) {
  if (parsed && typeof parsed.reasoning === 'string') {
    return {
      reasoning: parsed.reasoning,
      confidence: clampConfidence(parsed.confidence),
      risk_notes: typeof parsed.risk_notes === 'string' ? parsed.risk_notes : null,
      decision: parsed.decision === 'enter' || parsed.decision === 'skip' ? parsed.decision : null,
    };
  }
  if (raw) {
    return { reasoning: raw, confidence: null, risk_notes: null, decision: null };
  }
  return { reasoning: null, confidence: null, risk_notes: null, decision: null };
}
