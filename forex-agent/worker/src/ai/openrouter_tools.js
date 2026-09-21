/**
 * Low-level OpenRouter Messages call with tool-use support, mirroring
 * anthropic_tools.js's role for the Anthropic path. OpenRouter proxies to
 * OpenAI-compatible chat completions, so this uses the standard
 * `tools`/`tool_choice` shape — {type: "function", function: {name,
 * description, parameters}} — which is genuinely different from
 * Anthropic's flatter {name, description, input_schema} shape, hence a
 * separate low-level caller rather than sharing one.
 *
 * Not every model on OpenRouter supports tool calling — this is a real
 * capability that varies per model, not a given. If the chosen model
 * doesn't support it, OpenRouter will typically return an error (or the
 * model may just never emit a tool_calls field). Either case is handled
 * by the caller (openrouterAgentLoop.js / reasoning.js), which falls back
 * to the non-tool-calling path rather than letting a signal fail outright.
 */

const API_URL = 'https://openrouter.ai/api/v1/chat/completions';

export async function callWithTools({ messages, tools, env }) {
  if (!env.OPENROUTER_MODEL) {
    throw new Error('OPENROUTER_MODEL is not set — pick a model from openrouter.ai/models');
  }

  const response = await fetch(API_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${env.OPENROUTER_API_KEY}`,
      'HTTP-Referer': env.OPENROUTER_APP_URL || 'https://github.com/segz7448',
      'X-Title': 'forex-signal-worker',
    },
    body: JSON.stringify({
      model: env.OPENROUTER_MODEL,
      max_tokens: 1024,
      messages,
      tools,
    }),
  });

  if (!response.ok) {
    throw new Error(`OpenRouter API error: ${response.status} ${await response.text()}`);
  }

  return response.json();
}
