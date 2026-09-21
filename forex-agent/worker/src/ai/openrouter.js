/**
 * Provider: OpenRouter — OpenAI-compatible chat completions API that proxies
 * to hundreds of models (Qwen, Phi, Llama, Mistral, etc). Swap models purely
 * by changing env.OPENROUTER_MODEL, no code change needed.
 *
 * Requires: env.OPENROUTER_API_KEY, env.OPENROUTER_MODEL
 *   e.g. OPENROUTER_MODEL="qwen/qwen-2.5-72b-instruct"
 *        OPENROUTER_MODEL="microsoft/phi-3-medium-128k-instruct"
 * Full model list: https://openrouter.ai/models
 */

import { buildPrompt } from './prompt.js';

const API_URL = 'https://openrouter.ai/api/v1/chat/completions';

export async function generateFromPrompt(promptText, env) {
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
      max_tokens: 400,
      messages: [{ role: 'user', content: promptText }],
    }),
  });

  if (!response.ok) {
    throw new Error(`OpenRouter API error: ${response.status} ${await response.text()}`);
  }

  const data = await response.json();
  return data.choices?.[0]?.message?.content ?? null;
}

export async function generate(signal, env, context = {}) {
  return generateFromPrompt(buildPrompt(signal, context), env);
}
