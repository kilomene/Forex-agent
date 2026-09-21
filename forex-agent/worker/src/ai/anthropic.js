/**
 * Provider: Anthropic (Claude) Messages API.
 * Requires: env.ANTHROPIC_API_KEY, optionally env.ANTHROPIC_MODEL.
 */

import { buildPrompt } from './prompt.js';

const API_URL = 'https://api.anthropic.com/v1/messages';
const DEFAULT_MODEL = 'claude-sonnet-4-6';

export async function generateFromPrompt(promptText, env) {
  const response = await fetch(API_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-api-key': env.ANTHROPIC_API_KEY,
      'anthropic-version': '2023-06-01',
    },
    body: JSON.stringify({
      model: env.ANTHROPIC_MODEL || DEFAULT_MODEL,
      max_tokens: 400,
      messages: [{ role: 'user', content: promptText }],
    }),
  });

  if (!response.ok) {
    throw new Error(`Anthropic API error: ${response.status} ${await response.text()}`);
  }

  const data = await response.json();
  const textBlock = data.content?.find((b) => b.type === 'text');
  return textBlock?.text ?? null;
}

export async function generate(signal, env, context = {}) {
  return generateFromPrompt(buildPrompt(signal, context), env);
}
