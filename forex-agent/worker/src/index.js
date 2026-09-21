import { isAuthorized, unauthorizedResponse } from './auth.js';
import {
  insertSignal,
  getSignal,
  listSignals,
  getApprovedPending,
  setStatus,
  upsertDeviceToken,
  getAllDeviceTokens,
  getTradingMode,
  setSetting,
  markClosed,
  setExecutionDetails,
  insertPerformanceSnapshot,
  getLatestPerformanceSnapshot,
  getRecentReflections,
  isKillSwitchEngaged,
  setKillSwitchEngaged,
  requestCloseAll,
  getCloseAllRequestedAt,
  clearCloseAllRequest,
  getCustomAiSettings,
  setCustomAiUrl,
  setCustomAiApiKey,
  clearCustomAiSettings,
  upsertChartData,
  getChartData,
  listAvailableCharts,
} from './db.js';
import { runAgent } from './agent/reasoning.js';
import { runReflection, computeOutcome } from './agent/reflect.js';
import { applyDynamicAiSettings } from './agent/dynamicSettings.js';
import { sendPushToAll } from './fcm.js';

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function notFound() {
  return json({ error: 'Not found' }, 404);
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;

    if (!isAuthorized(request, env)) {
      return unauthorizedResponse();
    }

    try {
      // --- Bridge: report a newly detected signal ---
      // POST /signals
      if (path === '/signals' && method === 'POST') {
        const body = await request.json();
        const required = [
          'symbol', 'timeframe', 'direction', 'entry_price',
          'stop_loss', 'take_profit', 'candle_time',
        ];
        for (const field of required) {
          if (body[field] === undefined || body[field] === null) {
            return json({ error: `Missing field: ${field}` }, 400);
          }
        }

        // Gathers session/memory/correlation/reflection context, calls the
        // configured model, and returns {reasoning, confidence, risk_notes,
        // decision}. The agent is the trader now — its "decision" field is
        // what actually gates autonomous entry, not the mere fact that the
        // deterministic TA engine detected a setup worth looking at.
        const agentResult = await runAgent(env.DB, body, env);
        const signalId = await insertSignal(env.DB, body, agentResult);

        // Autonomous entry mode: the agent's own decision gates entry, not
        // just the presence of a TA-triggered signal. "enter" -> skip the
        // manual pending state and go straight to approved, so the bridge
        // picks it up on its next poll. Anything else (explicit "skip", or
        // no parseable decision at all) does NOT auto-approve — silence or
        // ambiguity from the model is not authorization to trade.
        //
        // Kill switch overrides autonomous mode entirely — if engaged, no
        // decision the agent makes can result in auto-approval, no matter
        // how confident. This check happens BEFORE looking at the
        // decision, not as an afterthought.
        const tradingMode = await getTradingMode(env.DB);
        const killSwitchEngaged = await isKillSwitchEngaged(env.DB);
        let statusNote = '';
        if (killSwitchEngaged) {
          statusNote = '🛑 Kill switch active: ';
        } else if (tradingMode === 'autonomous') {
          if (agentResult.decision === 'enter') {
            await setStatus(env.DB, signalId, 'approved');
            statusNote = 'Zenas auto-approved: ';
          } else {
            await setStatus(env.DB, signalId, 'skipped_by_agent');
            statusNote = 'Zenas skipped: ';
          }
        }

        // Fire the push notification, don't block the response on it failing.
        const tokens = await getAllDeviceTokens(env.DB);
        if (tokens.length > 0) {
          const pushBody = agentResult.risk_notes
            ? `⚠ ${agentResult.risk_notes}`.slice(0, 120)
            : (agentResult.reasoning || body.trigger || 'New signal ready for review').slice(0, 120);

          // In manual mode, surface the agent's decision as a hint in the
          // title — the human still taps approve/reject either way, this
          // just tells them what the agent itself would have done.
          const titlePrefix =
            killSwitchEngaged || tradingMode === 'autonomous'
              ? statusNote
              : agentResult.decision
              ? `Zenas says ${agentResult.decision}: `
              : '';

          ctx.waitUntil(
            sendPushToAll(
              tokens,
              {
                title: `${titlePrefix}${body.direction} ${body.symbol}${
                  agentResult.confidence != null ? ` (${agentResult.confidence}%)` : ''
                }`,
                body: pushBody,
                data: {
                  signal_id: signalId,
                  type: 'new_signal',
                  trading_mode: tradingMode,
                  agent_decision: agentResult.decision || 'unknown',
                },
              },
              env
            )
          );
        }

        return json({ signal_id: signalId }, 201);
      }

      // --- Bridge: poll for signals the app approved but not yet executed ---
      // GET /signals/pending-approved
      if (path === '/signals/pending-approved' && method === 'GET') {
        const signals = await getApprovedPending(env.DB);
        return json({ signals });
      }

      // --- Bridge: report execution result ---
      // POST /signals/:id/executed
      const executedMatch = path.match(/^\/signals\/([^/]+)\/executed$/);
      if (executedMatch && method === 'POST') {
        const id = executedMatch[1];
        const body = await request.json();
        const existing = await getSignal(env.DB, id);
        if (!existing) return notFound();

        await setStatus(env.DB, id, 'executed', { mt5_result: body.mt5_result });
        await setExecutionDetails(env.DB, id, { spreadAtEntry: body.spread_at_entry, lotSize: body.lot_size });

        const tokens = await getAllDeviceTokens(env.DB);
        if (tokens.length > 0) {
          ctx.waitUntil(
            sendPushToAll(
              tokens,
              {
                title: `Order placed: ${existing.symbol}`,
                body: `${existing.direction} order executed on Exness.`,
                data: { signal_id: id, type: 'executed' },
              },
              env
            )
          );
        }

        return json({ ok: true });
      }

      // --- Bridge: report a risk-check rejection (after user had approved) ---
      // POST /signals/:id/rejected
      const rejectedMatch = path.match(/^\/signals\/([^/]+)\/rejected$/);
      if (rejectedMatch && method === 'POST') {
        const id = rejectedMatch[1];
        const body = await request.json();
        const existing = await getSignal(env.DB, id);
        if (!existing) return notFound();

        await setStatus(env.DB, id, 'rejected_by_risk', { rejection_reason: body.reason });

        const tokens = await getAllDeviceTokens(env.DB);
        if (tokens.length > 0) {
          ctx.waitUntil(
            sendPushToAll(
              tokens,
              {
                title: `Signal blocked: ${existing.symbol}`,
                body: body.reason || 'Rejected by risk guardrails.',
                data: { signal_id: id, type: 'rejected_by_risk' },
              },
              env
            )
          );
        }

        return json({ ok: true });
      }

      // --- App: list signals (optionally filter by status) ---
      // GET /signals?status=pending&limit=50
      if (path === '/signals' && method === 'GET') {
        const status = url.searchParams.get('status') || undefined;
        const limit = Number(url.searchParams.get('limit') || 50);
        const signals = await listSignals(env.DB, { status, limit });
        return json({ signals });
      }

      // --- App: get one signal ---
      // GET /signals/:id
      const getOneMatch = path.match(/^\/signals\/([^/]+)$/);
      if (getOneMatch && method === 'GET') {
        const signal = await getSignal(env.DB, getOneMatch[1]);
        if (!signal) return notFound();
        return json({ signal });
      }

      // --- App: approve a signal ---
      // POST /signals/:id/approve
      const approveMatch = path.match(/^\/signals\/([^/]+)\/approve$/);
      if (approveMatch && method === 'POST') {
        const id = approveMatch[1];
        const existing = await getSignal(env.DB, id);
        if (!existing) return notFound();
        if (existing.status !== 'pending') {
          return json({ error: `Signal is not pending (status=${existing.status})` }, 409);
        }
        // Kill switch blocks manual approval too, not just autonomous
        // auto-approval — never trust only the client to have hidden the
        // button. Defense in depth: this is checked here AND the bridge's
        // risk.py independently checks the same state before executing.
        if (await isKillSwitchEngaged(env.DB)) {
          return json({ error: 'Kill switch is engaged — disengage it before approving trades.' }, 409);
        }
        await setStatus(env.DB, id, 'approved');
        return json({ ok: true });
      }

      // --- App: reject a signal ---
      // POST /signals/:id/reject
      const rejectMatch = path.match(/^\/signals\/([^/]+)\/reject$/);
      if (rejectMatch && method === 'POST') {
        const id = rejectMatch[1];
        const existing = await getSignal(env.DB, id);
        if (!existing) return notFound();
        if (existing.status !== 'pending') {
          return json({ error: `Signal is not pending (status=${existing.status})` }, 409);
        }
        await setStatus(env.DB, id, 'rejected_by_user');
        return json({ ok: true });
      }

      // --- App: register a device token for push notifications ---
      // POST /devices/register  { token: "..." }
      if (path === '/devices/register' && method === 'POST') {
        const body = await request.json();
        if (!body.token) return json({ error: 'Missing token' }, 400);
        await upsertDeviceToken(env.DB, body.token);
        return json({ ok: true });
      }

      // --- App: send a real test push to every registered device ---
      // POST /devices/test-push
      // Exists specifically to make FCM setup verifiable — "am I actually
      // configured correctly" shouldn't require waiting for a real signal.
      if (path === '/devices/test-push' && method === 'POST') {
        const tokens = await getAllDeviceTokens(env.DB);
        if (tokens.length === 0) {
          return json({ ok: false, error: 'No devices registered yet.' }, 400);
        }
        const successCount = await sendPushToAll(
          tokens,
          {
            title: 'Test notification',
            body: 'If you see this, FCM is configured correctly.',
            data: { type: 'test' },
          },
          env
        );
        return json({ ok: successCount > 0, sent: successCount, total: tokens.length });
      }

      // --- App: read current trading mode ---
      // GET /settings/trading-mode
      if (path === '/settings/trading-mode' && method === 'GET') {
        const mode = await getTradingMode(env.DB);
        return json({ trading_mode: mode });
      }

      // --- App: read which AI provider is actually active right now ---
      // GET /settings/ai-provider
      // Exists specifically so the app can show, not just hope, whether
      // the real multi-turn tool-calling agent loop is running for the
      // currently configured provider — "custom" never gets it (small
      // self-hosted models mostly can't do reliable multi-turn tool use),
      // "anthropic" and "openrouter" both do. Accounts for the app-side
      // self-hosted override too (applyDynamicAiSettings) — if you've
      // configured a custom model from Settings, this reflects that, not
      // just the Worker's static deploy-time secret.
      if (path === '/settings/ai-provider' && method === 'GET') {
        const effectiveEnv = await applyDynamicAiSettings(env.DB, env);
        const provider = (effectiveEnv.AI_PROVIDER || 'anthropic').toLowerCase();
        const usesToolCalling = provider === 'anthropic' || provider === 'openrouter';
        const model = effectiveEnv.OPENROUTER_MODEL || effectiveEnv.ANTHROPIC_MODEL || null;
        return json({ provider, uses_tool_calling: usesToolCalling, model });
      }

      // --- App: change trading mode ---
      // POST /settings/trading-mode  { mode: "manual" | "autonomous" }
      if (path === '/settings/trading-mode' && method === 'POST') {
        const body = await request.json();
        if (body.mode !== 'manual' && body.mode !== 'autonomous') {
          return json({ error: 'mode must be "manual" or "autonomous"' }, 400);
        }
        await setSetting(env.DB, 'trading_mode', body.mode);
        return json({ ok: true, trading_mode: body.mode });
      }

      // --- App/bridge: read kill switch state ---
      // GET /kill-switch
      if (path === '/kill-switch' && method === 'GET') {
        const engaged = await isKillSwitchEngaged(env.DB);
        const closeAllRequestedAt = await getCloseAllRequestedAt(env.DB);
        return json({ engaged, close_all_requested_at: closeAllRequestedAt });
      }

      // --- App: engage or disengage the kill switch ---
      // POST /kill-switch  { engaged: true | false }
      // While engaged, NO new entry can happen — autonomous auto-approval
      // is skipped and manual /approve is rejected. This is checked here
      // AND independently by the bridge's risk.py (defense in depth).
      if (path === '/kill-switch' && method === 'POST') {
        const body = await request.json();
        if (typeof body.engaged !== 'boolean') {
          return json({ error: 'engaged must be true or false' }, 400);
        }
        await setKillSwitchEngaged(env.DB, body.engaged);

        const tokens = await getAllDeviceTokens(env.DB);
        if (tokens.length > 0) {
          ctx.waitUntil(
            sendPushToAll(
              tokens,
              {
                title: body.engaged ? '🛑 Kill switch engaged' : 'Kill switch disengaged',
                body: body.engaged
                  ? 'All new entries are halted until this is disengaged.'
                  : 'Trading can resume normally.',
                data: { type: 'kill_switch' },
              },
              env
            )
          );
        }

        return json({ ok: true, engaged: body.engaged });
      }

      // --- App: request an emergency close-all ---
      // POST /kill-switch/close-all
      // One-shot request, not a toggle — the bridge picks this up on its
      // next fast poll cycle and force-closes every open position it
      // manages, bypassing all normal exit logic (trailing stop,
      // breakeven, time exit — none of that applies here, this is "stop
      // everything right now").
      if (path === '/kill-switch/close-all' && method === 'POST') {
        await requestCloseAll(env.DB);

        const tokens = await getAllDeviceTokens(env.DB);
        if (tokens.length > 0) {
          ctx.waitUntil(
            sendPushToAll(
              tokens,
              {
                title: '🛑 Emergency close-all requested',
                body: 'The bridge will force-close every open position on its next check.',
                data: { type: 'kill_switch_close_all_requested' },
              },
              env
            )
          );
        }

        return json({ ok: true });
      }

      // --- Bridge: acknowledge that the close-all sweep is complete ---
      // POST /kill-switch/close-all/complete  { closed_count: N }
      if (path === '/kill-switch/close-all/complete' && method === 'POST') {
        const body = await request.json();
        await clearCloseAllRequest(env.DB);

        const tokens = await getAllDeviceTokens(env.DB);
        if (tokens.length > 0) {
          ctx.waitUntil(
            sendPushToAll(
              tokens,
              {
                title: 'Close-all complete',
                body: `${body.closed_count ?? 0} position(s) force-closed.`,
                data: { type: 'kill_switch_close_all_complete' },
              },
              env
            )
          );
        }

        return json({ ok: true });
      }

      // --- App: read current self-hosted model connection state ---
      // GET /settings/custom-ai
      // Never returns the actual API key back to the client — only
      // whether one is currently set, same principle as never echoing
      // back a password.
      if (path === '/settings/custom-ai' && method === 'GET') {
        const { url, apiKey } = await getCustomAiSettings(env.DB);
        return json({ url, has_api_key: Boolean(apiKey) });
      }

      // --- App: save the self-hosted model URL and/or API key ---
      // POST /settings/custom-ai  { url?: string, api_key?: string }
      // Either field can be sent alone — this is what lets the app's
      // "test then save" flow save the URL and the API key as two
      // separate steps, matching how the settings screen presents them.
      if (path === '/settings/custom-ai' && method === 'POST') {
        const body = await request.json();
        if (body.url === undefined && body.api_key === undefined) {
          return json({ error: 'Provide at least one of: url, api_key' }, 400);
        }
        if (body.url !== undefined) await setCustomAiUrl(env.DB, body.url);
        if (body.api_key !== undefined) await setCustomAiApiKey(env.DB, body.api_key);
        const { url, apiKey } = await getCustomAiSettings(env.DB);
        return json({ ok: true, url, has_api_key: Boolean(apiKey) });
      }

      // --- App: disconnect the self-hosted model, revert to deploy-time env ---
      // POST /settings/custom-ai/clear
      if (path === '/settings/custom-ai/clear' && method === 'POST') {
        await clearCustomAiSettings(env.DB);
        return json({ ok: true });
      }

      // --- App: test VM/server reachability BEFORE saving ---
      // POST /settings/custom-ai/test-vm  { url: string }
      // Hits {url}/health — unauthenticated, matches
      // self_hosted_example/server.py's health check route. This only
      // confirms the server is up and reachable, not that the API key
      // works — that's the separate test-model route below.
      if (path === '/settings/custom-ai/test-vm' && method === 'POST') {
        const body = await request.json();
        if (!body.url) return json({ error: 'Missing url' }, 400);

        try {
          const healthUrl = `${body.url.replace(/\/$/, '')}/health`;
          const response = await fetch(healthUrl, { signal: AbortSignal.timeout(8000) });
          if (!response.ok) {
            return json({ ok: false, detail: `Server responded with ${response.status}` });
          }
          const data = await response.json().catch(() => ({}));
          return json({ ok: true, detail: data });
        } catch (err) {
          return json({ ok: false, detail: `Could not reach that address: ${String(err)}` });
        }
      }

      // --- App: test the model/API key BEFORE saving ---
      // POST /settings/custom-ai/test-model  { url: string, api_key?: string }
      // Sends a trivial real prompt to {url}/generate and checks for a
      // real response — this actually exercises the model, not just
      // network reachability, so a wrong API key or a misconfigured
      // model surfaces here rather than only failing later on a real signal.
      if (path === '/settings/custom-ai/test-model' && method === 'POST') {
        const body = await request.json();
        if (!body.url) return json({ error: 'Missing url' }, 400);

        try {
          const generateUrl = `${body.url.replace(/\/$/, '')}/generate`;
          const headers = { 'Content-Type': 'application/json' };
          if (body.api_key) headers.Authorization = `Bearer ${body.api_key}`;

          const response = await fetch(generateUrl, {
            method: 'POST',
            headers,
            body: JSON.stringify({
              prompt: 'Connection test — respond with a short confirmation.',
              signal: {},
            }),
            signal: AbortSignal.timeout(30000), // model inference is slower than a health check
          });

          if (!response.ok) {
            const text = await response.text().catch(() => '');
            return json({ ok: false, detail: `Model server responded with ${response.status}: ${text.slice(0, 200)}` });
          }

          const data = await response.json();
          if (!data.reasoning) {
            return json({ ok: false, detail: 'Server responded but did not return the expected "reasoning" field.' });
          }

          return json({ ok: true, sample_response: data.reasoning.slice(0, 200) });
        } catch (err) {
          return json({ ok: false, detail: `Could not reach the model endpoint: ${String(err)}` });
        }
      }

      // --- Bridge: report that exit_manager.py autonomously closed a position ---
      // POST /signals/:id/closed  { reason: "trailing_stop"|"breakeven_stop"|"time_exit", price: 1.0842 }
      const closedMatch = path.match(/^\/signals\/([^/]+)\/closed$/);
      if (closedMatch && method === 'POST') {
        const id = closedMatch[1];
        const body = await request.json();
        const existing = await getSignal(env.DB, id);
        if (!existing) return notFound();

        await markClosed(env.DB, id, { reason: body.reason, price: body.price, commission: body.commission, swap: body.swap });

        // Reflection: compare the original reasoning to the actual outcome
        // and store it for future reference. Runs in the background so it
        // doesn't block the response — reflection quality matters, but
        // response latency to the bridge doesn't need to wait on it.
        const outcome = computeOutcome(existing.direction, existing.entry_price, body.price);
        ctx.waitUntil(
          runReflection(env.DB, { ...existing, closed_price: body.price }, outcome, env).catch((err) =>
            console.error('Reflection generation failed:', err)
          )
        );

        const tokens = await getAllDeviceTokens(env.DB);
        if (tokens.length > 0) {
          ctx.waitUntil(
            sendPushToAll(
              tokens,
              {
                title: `Position closed: ${existing.symbol}`,
                body: `${body.reason || 'Closed'} @ ${body.price}`,
                data: { signal_id: id, type: 'closed' },
              },
              env
            )
          );
        }

        return json({ ok: true });
      }

      // --- Bridge: report a real performance snapshot (from MT5 deal history) ---
      // POST /performance/snapshot
      if (path === '/performance/snapshot' && method === 'POST') {
        const body = await request.json();
        const id = await insertPerformanceSnapshot(env.DB, body);
        return json({ ok: true, id }, 201);
      }

      // --- App/agent: read the latest performance snapshot ---
      // GET /performance/latest
      if (path === '/performance/latest' && method === 'GET') {
        const snapshot = await getLatestPerformanceSnapshot(env.DB);
        if (!snapshot) return json({ available: false });
        return json({ available: true, snapshot });
      }

      // --- App/agent: read recent reflections ---
      // GET /reflections?symbol=EURUSD&limit=10
      if (path === '/reflections' && method === 'GET') {
        const symbol = url.searchParams.get('symbol') || undefined;
        const limit = Number(url.searchParams.get('limit') || 10);
        const reflections = await getRecentReflections(env.DB, { symbol, limit });
        return json({ reflections });
      }

      // --- Bridge: report chart data for one symbol/timeframe ---
      // POST /chart-data  { symbol, timeframe, candles, ema_fast, ema_slow, ema_fast_period, ema_slow_period }
      if (path === '/chart-data' && method === 'POST') {
        const body = await request.json();
        if (!body.symbol || !body.timeframe) {
          return json({ error: 'Missing symbol or timeframe' }, 400);
        }
        await upsertChartData(env.DB, body);
        return json({ ok: true });
      }

      // --- App: list every symbol/timeframe combination available to browse ---
      // GET /chart-data/available
      if (path === '/chart-data/available' && method === 'GET') {
        const charts = await listAvailableCharts(env.DB);
        return json({ charts });
      }

      // --- App: fetch chart data for one symbol/timeframe ---
      // GET /chart-data?symbol=EURUSD&timeframe=H1
      if (path === '/chart-data' && method === 'GET') {
        const symbol = url.searchParams.get('symbol');
        const timeframe = url.searchParams.get('timeframe');
        if (!symbol || !timeframe) {
          return json({ error: 'symbol and timeframe query params are required' }, 400);
        }
        const chart = await getChartData(env.DB, symbol, timeframe);
        if (!chart) return json({ available: false });
        return json({
          available: true,
          symbol: chart.symbol,
          timeframe: chart.timeframe,
          candles: JSON.parse(chart.candles),
          ema_fast: chart.ema_fast ? JSON.parse(chart.ema_fast) : null,
          ema_slow: chart.ema_slow ? JSON.parse(chart.ema_slow) : null,
          ema_fast_period: chart.ema_fast_period,
          ema_slow_period: chart.ema_slow_period,
          updated_at: chart.updated_at,
        });
      }

      return notFound();
    } catch (err) {
      console.error('Unhandled error:', err);
      return json({ error: 'Internal error', message: String(err) }, 500);
    }
  },
};
