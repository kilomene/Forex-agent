/**
 * Same real agent loop as agentLoop.js (the model decides which tools to
 * call, we execute them, feed results back, repeat) — but speaking
 * OpenAI's tool-calling conventions instead of Anthropic's, since
 * OpenRouter proxies to OpenAI-compatible chat completions.
 *
 * Key shape differences from agentLoop.js, worth knowing if you're
 * comparing the two:
 *   - Tools are {type: "function", function: {name, description, parameters}}
 *     instead of Anthropic's flatter {name, description, input_schema}.
 *   - A tool call arrives as message.tool_calls (an array), with
 *     arguments as a JSON STRING that must be parsed — Anthropic sends
 *     already-parsed objects.
 *   - Tool results go back as separate {role: "tool", tool_call_id,
 *     content} messages, one per call — not grouped into a single user
 *     message the way Anthropic expects tool_result blocks.
 *   - Done-vs-more-tools is read from whether tool_calls is present at
 *     all, not a dedicated stop_reason field.
 */

import { callWithTools } from '../ai/openrouter_tools.js';
import { TOOL_DEFINITIONS, executeTool } from './tools.js';
import { AGENT_SYSTEM_PROMPT, buildAgentUserMessage } from './agentPrompt.js';

const MAX_ITERATIONS = 6;

function toOpenAiToolSchema(toolDefs) {
  return toolDefs.map((t) => ({
    type: 'function',
    function: {
      name: t.name,
      description: t.description,
      parameters: t.input_schema,
    },
  }));
}

/**
 * Same return shape as agentLoop.runAgentLoop: { raw_text, tool_calls }.
 * Throws on a genuine API failure OR if the model simply doesn't support
 * tool calling (OpenRouter surfaces that as an error for most models
 * that lack the capability) — the caller (reasoning.js) is responsible
 * for catching this and falling back to the non-tool-calling path, same
 * principle as every other AI call in this codebase never being allowed
 * to hard-fail a signal.
 */
export async function runOpenRouterAgentLoop(signal, db, env) {
  const messages = [
    { role: 'system', content: AGENT_SYSTEM_PROMPT },
    { role: 'user', content: buildAgentUserMessage(signal) },
  ];
  const tools = toOpenAiToolSchema(TOOL_DEFINITIONS);
  const toolCallLog = [];

  for (let i = 0; i < MAX_ITERATIONS; i++) {
    const response = await callWithTools({ messages, tools, env });
    const choice = response.choices?.[0];
    if (!choice) {
      throw new Error('OpenRouter response had no choices — cannot continue the agent loop.');
    }

    const message = choice.message;
    messages.push(message);

    if (!message.tool_calls || message.tool_calls.length === 0) {
      return { raw_text: message.content ?? null, tool_calls: toolCallLog };
    }

    for (const toolCall of message.tool_calls) {
      let input = {};
      try {
        input = JSON.parse(toolCall.function.arguments || '{}');
      } catch (err) {
        console.error(`Failed to parse tool arguments for ${toolCall.function.name}:`, err);
      }

      const result = await executeTool(toolCall.function.name, input, { db, signal, env });
      toolCallLog.push({ tool: toolCall.function.name, input });

      messages.push({
        role: 'tool',
        tool_call_id: toolCall.id,
        content: JSON.stringify(result),
      });
    }
  }

  console.error(`OpenRouter agent loop hit MAX_ITERATIONS (${MAX_ITERATIONS}) without a final answer.`);
  return { raw_text: null, tool_calls: toolCallLog };
}
