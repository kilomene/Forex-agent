/**
 * Low-level Anthropic Messages API call with tool-use support. This is
 * separate from generate() in anthropic.js (the simple one-shot call used
 * by the single-provider path) because tool-calling is a genuinely
 * different, multi-turn protocol — this function makes ONE turn of that
 * conversation and returns the raw response for agentLoop.js to interpret.
 */

const API_URL = 'https://api.anthropic.com/v1/messages';
const DEFAULT_MODEL = 'claude-sonnet-4-6';

export async function callWithTools({ system, messages, tools, env }) {
  const response = await fetch(API_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-api-key': env.ANTHROPIC_API_KEY,
      'anthropic-version': '2023-06-01',
    },
    body: JSON.stringify({
      model: env.ANTHROPIC_MODEL || DEFAULT_MODEL,
      max_tokens: 1024,
      system,
      messages,
      tools,
    }),
  });

  if (!response.ok) {
    throw new Error(`Anthropic API error: ${response.status} ${await response.text()}`);
  }

  return response.json();
}
