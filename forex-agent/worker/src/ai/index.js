/**
 * Single entry point for AI reasoning generation. Which provider actually
 * runs is controlled entirely by env.AI_PROVIDER — no code change needed
 * to switch between Claude, OpenRouter (Qwen/Phi/whatever), or your own
 * self-hosted backend. Set it with:
 *
 *   wrangler secret put AI_PROVIDER      (value: "anthropic" | "openrouter" | "custom")
 *
 * If generation fails for any reason (bad key, provider down, model
 * refused, etc), this returns null rather than throwing — a broken AI
 * call should never stop a signal from being stored and pushed.
 */

import * as anthropic from './anthropic.js';
import * as openrouter from './openrouter.js';
import * as custom from './custom.js';

const PROVIDERS = {
  anthropic,
  openrouter,
  custom,
};

export async function generateReasoning(signal, env, context = {}) {
  const providerName = (env.AI_PROVIDER || 'anthropic').toLowerCase();
  const provider = PROVIDERS[providerName];

  if (!provider) {
    console.error(
      `Unknown AI_PROVIDER "${providerName}". Valid options: ${Object.keys(PROVIDERS).join(', ')}`
    );
    return null;
  }

  try {
    return await provider.generate(signal, env, context);
  } catch (err) {
    console.error(`AI reasoning failed (provider=${providerName}):`, err);
    return null;
  }
}

/**
 * Same provider-routing as generateReasoning, but for a raw prompt string
 * instead of a trade-signal prompt — used by the reflection pipeline
 * (agent/reflect.js), which needs a different prompt shape entirely.
 */
export async function askProvider(promptText, env) {
  const providerName = (env.AI_PROVIDER || 'anthropic').toLowerCase();
  const provider = PROVIDERS[providerName];

  if (!provider) {
    console.error(
      `Unknown AI_PROVIDER "${providerName}". Valid options: ${Object.keys(PROVIDERS).join(', ')}`
    );
    return null;
  }

  try {
    return await provider.generateFromPrompt(promptText, env);
  } catch (err) {
    console.error(`AI raw-prompt call failed (provider=${providerName}):`, err);
    return null;
  }
}
