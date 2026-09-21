/**
 * The real agent loop: the model decides which tools to call, in what
 * order, based on what it's already learned — not a fixed sequence we
 * hardcode. We execute whatever it asks for, feed the result back, and
 * repeat until it stops calling tools and gives a final answer.
 *
 * Bounded to MAX_ITERATIONS turns as a hard safety/cost cap — an agent
 * loop with no ceiling is a real way to burn API spend or spin forever
 * on a confused model. If the cap is hit, we take whatever text exists
 * and move on rather than looping indefinitely.
 */

import { callWithTools } from '../ai/anthropic_tools.js';
import { TOOL_DEFINITIONS, executeTool } from './tools.js';
import { AGENT_SYSTEM_PROMPT, buildAgentUserMessage } from './agentPrompt.js';

const MAX_ITERATIONS = 6;

/**
 * Returns { raw_text, tool_calls } — raw_text is the model's final text
 * output (expected to be JSON, parsed by the caller), tool_calls is a log
 * of what the agent actually looked up, useful for transparency/debugging.
 * Throws only on a genuine API failure — the caller's runAgent wraps this
 * in the same try/catch pattern used everywhere else in the AI layer.
 */
export async function runAgentLoop(signal, db, env) {
  const messages = [{ role: 'user', content: buildAgentUserMessage(signal) }];
  const toolCallLog = [];

  for (let i = 0; i < MAX_ITERATIONS; i++) {
    const response = await callWithTools({
      system: AGENT_SYSTEM_PROMPT,
      messages,
      tools: TOOL_DEFINITIONS,
      env,
    });

    messages.push({ role: 'assistant', content: response.content });

    if (response.stop_reason !== 'tool_use') {
      // Model is done — extract whatever text it produced.
      const textBlock = response.content.find((b) => b.type === 'text');
      return { raw_text: textBlock?.text ?? null, tool_calls: toolCallLog };
    }

    // Execute every tool_use block in this turn, collect results.
    const toolResults = [];
    for (const block of response.content) {
      if (block.type !== 'tool_use') continue;

      const result = await executeTool(block.name, block.input, { db, signal, env });
      toolCallLog.push({ tool: block.name, input: block.input });

      toolResults.push({
        type: 'tool_result',
        tool_use_id: block.id,
        content: JSON.stringify(result),
      });
    }

    messages.push({ role: 'user', content: toolResults });
  }

  // Hit the iteration cap without a final answer — return nothing rather
  // than a possibly-incomplete mid-loop guess.
  console.error(`Agent loop hit MAX_ITERATIONS (${MAX_ITERATIONS}) without a final answer.`);
  return { raw_text: null, tool_calls: toolCallLog };
}
