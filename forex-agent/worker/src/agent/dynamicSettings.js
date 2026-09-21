/**
 * Lets the app configure and change the self-hosted model connection
 * (URL + API key) at runtime, without redeploying the Worker — while
 * ai/custom.js itself stays completely unmodified, since it already just
 * reads env.CUSTOM_AI_URL/env.CUSTOM_AI_API_KEY. This function's only job
 * is building the env object those reads see.
 *
 * Precedence: if the app has configured a custom AI URL (via
 * /settings/custom-ai), that wins — AND it forces AI_PROVIDER to
 * "custom" regardless of what the Worker's own AI_PROVIDER secret says.
 * That's deliberate: configuring a self-hosted model from the app is a
 * clear statement of intent to actually use it, not a side channel that
 * silently does nothing unless a separate provider switch is also
 * flipped. Clearing the app-side URL (empty string) reverts entirely to
 * whatever the Worker's deploy-time secrets specify.
 */

import { getCustomAiSettings } from '../db.js';

export async function applyDynamicAiSettings(db, env) {
  const { url, apiKey } = await getCustomAiSettings(db);

  if (!url) {
    return env; // nothing app-configured — deploy-time env is authoritative, unchanged
  }

  return {
    ...env,
    AI_PROVIDER: 'custom',
    CUSTOM_AI_URL: url,
    CUSTOM_AI_API_KEY: apiKey || env.CUSTOM_AI_API_KEY,
  };
}
