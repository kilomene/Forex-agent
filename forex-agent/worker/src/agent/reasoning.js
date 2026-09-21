/**
 * Entry point for the whole agent pipeline. Routes to one of two shapes:
 *
 *   - AI_PROVIDER=anthropic | openrouter -> real multi-turn tool-calling
 *     (agentLoop.js / openrouterAgentLoop.js respectively). The model
 *     decides which tools to call and in what order — this is genuine
 *     agentic behavior for both providers now, not just Anthropic.
 *
 *   - AI_PROVIDER=custom -> the pre-fetched-context path (context.js + a
 *     single-shot call). This is NOT a lesser version of the same thing —
 *     it's the honest ceiling for self-hosted models. Small local models
 *     (Qwen-3B, Phi-3-mini) mostly can't reliably do multi-turn tool use
 *     at all — they'll ignore the schema or hallucinate a call. Handing
 *     them everything up front in one prompt is more reliable than a
 *     tool loop they can't follow.
 *
 * IMPORTANT robustness note for openrouter specifically: not every model
 * on OpenRouter supports tool calling — it's a real per-model capability,
 * not a given. If the configured model doesn't support it, the API call
 * typically errors. Rather than let that fail the whole signal, the
 * openrouter tool-calling attempt is wrapped so a failure here falls back
 * to the plain single-shot path automatically — same principle as every
 * other AI call in this codebase never being allowed to hard-fail a signal.
 *
 * Both paths converge on the same output shape and the same non-negotiable
 * step: correlation flags from OUR OWN data get appended to risk_notes
 * regardless of what the model said, because that's ground truth, not
 * an opinion the model could have missed or gotten wrong.
 */

import { buildContext } from './context.js';
import { runAgentLoop } from './agentLoop.js';
import { runOpenRouterAgentLoop } from './openrouterAgentLoop.js';
import { generateReasoning as callProviderDirect } from '../ai/index.js';
import { tryParseStructured, toResult } from './parse.js';
import { applyDynamicAiSettings } from './dynamicSettings.js';

async function runAnthropicToolCallingPath(db, signal, env) {
  const { raw_text, tool_calls } = await runAgentLoop(signal, db, env);
  const parsed = tryParseStructured(raw_text);
  return { ...toResult(raw_text, parsed), tool_calls };
}

async function runOpenRouterToolCallingPath(db, signal, env) {
  const { raw_text, tool_calls } = await runOpenRouterAgentLoop(signal, db, env);
  const parsed = tryParseStructured(raw_text);
  return { ...toResult(raw_text, parsed), tool_calls };
}

async function runFallbackPath(db, signal, env) {
  const context = await buildContext(db, signal);
  const raw = await callProviderDirect(signal, env, context);
  const parsed = tryParseStructured(raw);
  return { ...toResult(raw, parsed), tool_calls: null, _context: context };
}

/**
 * Returns { reasoning, confidence, risk_notes, tool_calls, context_summary }.
 * Never throws — a total model/tool failure just means reasoning/confidence
 * are null; correlation flags (computed independently of the model) still
 * get surfaced in risk_notes either way.
 */
export async function runAgent(db, signal, env) {
  // App-configured self-hosted model settings (if any) override the
  // Worker's deploy-time env — this is what lets the provider be changed
  // from the mobile app without redeploying. See dynamicSettings.js for
  // the exact precedence rule.
  env = await applyDynamicAiSettings(db, env);

  const provider = (env.AI_PROVIDER || 'anthropic').toLowerCase();

  let result;
  let context;

  try {
    if (provider === 'anthropic') {
      result = await runAnthropicToolCallingPath(db, signal, env);
      context = await buildContext(db, signal); // for correlation ground-truth below
    } else if (provider === 'openrouter') {
      try {
        result = await runOpenRouterToolCallingPath(db, signal, env);
        context = await buildContext(db, signal);
      } catch (toolErr) {
        // The configured model may not support tool calling at all —
        // this is a real, expected possibility per-model, not a bug.
        // Fall back to the pre-fetched-context single-shot path rather
        // than losing the signal's reasoning entirely.
        console.error('OpenRouter tool-calling failed, falling back to single-shot path:', toolErr);
        result = await runFallbackPath(db, signal, env);
        context = result._context;
      }
    } else {
      result = await runFallbackPath(db, signal, env);
      context = result._context;
    }
  } catch (err) {
    console.error('Agent pipeline failed:', err);
    result = { reasoning: null, confidence: null, risk_notes: null, tool_calls: null, decision: null };
    context = await buildContext(db, signal).catch(() => null);
  }

  if (context && context.correlationFlags.length > 0) {
    const flagText = context.correlationFlags.join(' ');
    result.risk_notes = result.risk_notes ? `${result.risk_notes} ${flagText}` : flagText;
  }

  return {
    reasoning: result.reasoning,
    confidence: result.confidence,
    risk_notes: result.risk_notes,
    tool_calls: result.tool_calls,
    decision: result.decision,
    context_summary: context
      ? {
          session: context.session,
          historySummary: context.historySummary,
          correlationFlagCount: context.correlationFlags.length,
          activePositionCount: context.activePositionCount,
        }
      : null,
  };
}

