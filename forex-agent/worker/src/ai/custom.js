/**
 * Provider: your own self-hosted backend (e.g. HuggingFace Transformers
 * running on your Windows PC, or anywhere else you control).
 *
 * This Worker doesn't care what's behind your URL — it just needs your
 * backend to accept this exact request shape and return this exact
 * response shape. Build your server to match this contract and it plugs
 * straight in.
 *
 * Requires: env.CUSTOM_AI_URL, optionally env.CUSTOM_AI_API_KEY
 *
 * REQUEST  (POST env.CUSTOM_AI_URL):
 *   Headers: Content-Type: application/json
 *            Authorization: Bearer <CUSTOM_AI_API_KEY>   (only if set)
 *   Body:    { "prompt": "<full prompt text, already includes context>",
 *              "signal": { ...raw signal fields... },
 *              "context": { session, historySummary, correlationFlags, activePositionCount } }
 *
 * RESPONSE (200 OK):
 *   Body:    { "reasoning": "<raw model output text>" }
 *   The prompt asks the model to return JSON with reasoning/confidence/
 *   risk_notes — your server can just pass the model's raw output straight
 *   through as "reasoning" and let the Worker's parser in src/agent/reasoning.js
 *   handle extracting the structured fields. You don't need to parse JSON
 *   yourself unless you want to.
 *
 * Anything else (non-200, missing "reasoning") is treated as a failure —
 * the signal still gets stored and pushed, just without AI reasoning text.
 * See self_hosted_example/server.py for a minimal reference implementation.
 */

import { buildPrompt } from './prompt.js';

export async function generateFromPrompt(promptText, env) {
  if (!env.CUSTOM_AI_URL) {
    throw new Error('CUSTOM_AI_URL is not set — point it at your self-hosted backend');
  }

  const headers = { 'Content-Type': 'application/json' };
  if (env.CUSTOM_AI_API_KEY) {
    headers.Authorization = `Bearer ${env.CUSTOM_AI_API_KEY}`;
  }

  const response = await fetch(env.CUSTOM_AI_URL, {
    method: 'POST',
    headers,
    body: JSON.stringify({ prompt: promptText }),
  });

  if (!response.ok) {
    throw new Error(`Custom AI backend error: ${response.status} ${await response.text()}`);
  }

  const data = await response.json();
  return data.reasoning ?? null;
}

export async function generate(signal, env, context = {}) {
  if (!env.CUSTOM_AI_URL) {
    throw new Error('CUSTOM_AI_URL is not set — point it at your self-hosted backend');
  }

  const headers = { 'Content-Type': 'application/json' };
  if (env.CUSTOM_AI_API_KEY) {
    headers.Authorization = `Bearer ${env.CUSTOM_AI_API_KEY}`;
  }

  const response = await fetch(env.CUSTOM_AI_URL, {
    method: 'POST',
    headers,
    body: JSON.stringify({ prompt: buildPrompt(signal, context), signal, context }),
  });

  if (!response.ok) {
    throw new Error(`Custom AI backend error: ${response.status} ${await response.text()}`);
  }

  const data = await response.json();
  return data.reasoning ?? null;
}
