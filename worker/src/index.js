/**
 * forex-agent cloud/sync layer (Cloudflare Worker, zero npm runtime deps).
 *
 * What this Worker IS: the cloud ledger and sync point — signal lifecycle
 * persistence (D1), kill-switch state, settings, chart data, device tokens,
 * performance snapshots, reflection RECORDS, and FCM push notifications.
 *
 * What this Worker is NOT: it contains no AI reasoning, no LLM calls, no
 * provider configuration, and no autonomous decision-making. There is no
 * `runAgent` here anymore: POST /signals stores the signal and sends a
 * push, full stop. Reasoning, enter/skip decisions, and reflection TEXT
 * are the external agent's job; the deterministic correlation-flag rule
 * that used to run here now lives in the subsystem core (see
 * CORRELATION_RULE_NOTE.md).
 *
 * BEHAVIOR CHANGE vs the original worker: autonomous auto-approve is
 * GONE. A signal stays `pending` until something explicitly approves it —
 * the subsystem's signal monitor (external agent), or a human tapping
 * approve. The Worker never moves a signal out of `pending` by itself.
 */

import { authenticate, unauthorizedResponse } from './auth.js';
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
  upsertChartData,
  getChartData,
  listAvailableCharts,
} from './db.js';
import { recordReflection, computeOutcome } from './reflect.js';
import { sendPushToAll } from './fcm.js';
import { checkRateLimit } from './ratelimit.js';

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function err(code, message, status) {
  return json({ error: message, code }, status);
}

function notFound() {
  return err('NOT_FOUND', 'Not found', 404);
}

function rateLimited() {
  return err('RATE_LIMITED', 'Rate limit exceeded — slow down and retry.', 429);
}

async function pushToAll(db, ctx, env, payload) {
  const tokens = await getAllDeviceTokens(db);
  if (tokens.length > 0) {
    ctx.waitUntil(sendPushToAll(tokens, payload, env));
  }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;
    const method = request.method;

    // --- Per-client auth (see src/auth.js). ---
    const auth = await authenticate(request, env.DB);
    if (!auth.ok) {
      const ip =
        request.headers.get('cf-connecting-ip') ||
        request.headers.get('x-forwarded-for') ||
        'unknown';
      const fails = checkRateLimit(
        `authfail:${ip}`,
        env.RATE_LIMIT_AUTH_FAIL_PER_MIN ?? 20
      );
      if (!fails.allowed) return rateLimited();
      return unauthorizedResponse();
    }
    const clientId = auth.client_id;

    // --- General per-client rate limit on every route. ---
    const general = checkRateLimit(
      `general:${clientId}`,
      env.RATE_LIMIT_GENERAL_PER_MIN ?? 120
    );
    if (!general.allowed) return rateLimited();

    try {
      // --- Bridge: report a newly detected signal ---
      // POST /signals
      // Stores the signal as `pending` and fires a push. No reasoning, no
      // auto-approve — the signal stays pending until the external agent
      // (subsystem signal monitor) or a human approves it.
      if (path === '/signals' && method === 'POST') {
        const ingest = checkRateLimit(
          `ingest:${clientId}`,
          env.RATE_LIMIT_INGEST_PER_MIN ?? 30
        );
        if (!ingest.allowed) return rateLimited();

        const body = await request.json();
        const required = [
          'symbol', 'timeframe', 'direction', 'entry_price',
          'stop_loss', 'take_profit', 'candle_time',
        ];
        for (const field of required) {
          if (body[field] === undefined || body[field] === null) {
            return err('VALIDATION_ERROR', `Missing field: ${field}`, 400);
          }
        }

        const signalId = await insertSignal(env.DB, body, {});

        const tradingMode = await getTradingMode(env.DB);
        const killSwitchEngaged = await isKillSwitchEngaged(env.DB);

        await pushToAll(env.DB, ctx, env, {
          title: `${body.direction} ${body.symbol} — new signal`,
          body: (
            killSwitchEngaged
              ? '🛑 Kill switch engaged — approval blocked until disengaged. '
              : ''
          ).concat((body.trigger || 'New signal ready for review').slice(0, 120)),
          data: {
            signal_id: signalId,
            type: 'new_signal',
            trading_mode: tradingMode,
            kill_switch_engaged: String(killSwitchEngaged),
          },
        });

        return json({ signal_id: signalId }, 201);
      }

      // --- Bridge: poll for signals approved but not yet executed ---
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
        if (existing.status !== 'approved') {
          return err('CONFLICT', `Signal is not approved (status=${existing.status})`, 409);
        }

        await setStatus(env.DB, id, 'executed', { mt5_result: body.mt5_result });
        await setExecutionDetails(env.DB, id, { spreadAtEntry: body.spread_at_entry, lotSize: body.lot_size });

        await pushToAll(env.DB, ctx, env, {
          title: `Order placed: ${existing.symbol}`,
          body: `${existing.direction} order executed on the broker.`,
          data: { signal_id: id, type: 'executed' },
        });

        return json({ ok: true });
      }

      // --- Bridge: report a risk-check rejection (after approval) ---
      // POST /signals/:id/rejected
      const rejectedMatch = path.match(/^\/signals\/([^/]+)\/rejected$/);
      if (rejectedMatch && method === 'POST') {
        const id = rejectedMatch[1];
        const body = await request.json();
        const existing = await getSignal(env.DB, id);
        if (!existing) return notFound();
        if (existing.status !== 'approved') {
          return err('CONFLICT', `Signal is not approved (status=${existing.status})`, 409);
        }

        await setStatus(env.DB, id, 'rejected_by_risk', { rejection_reason: body.reason });

        await pushToAll(env.DB, ctx, env, {
          title: `Signal blocked: ${existing.symbol}`,
          body: (body.reason || 'Rejected by risk guardrails.').slice(0, 160),
          data: { signal_id: id, type: 'rejected_by_risk' },
        });

        return json({ ok: true });
      }

      // --- Bridge: report an MT5 order_send failure ---
      // POST /signals/:id/failed  { reason?: string, mt5_error?: string }
      // NEW in this migration (schema documented `failed` but no route
      // ever set it). Lets the bridge report that the broker rejected or
      // failed the order, so the ledger reflects reality instead of
      // leaving an approved signal dangling forever.
      const failedMatch = path.match(/^\/signals\/([^/]+)\/failed$/);
      if (failedMatch && method === 'POST') {
        const id = failedMatch[1];
        const body = await request.json();
        const existing = await getSignal(env.DB, id);
        if (!existing) return notFound();
        if (existing.status === 'closed' || existing.status === 'executed') {
          return err(
            'CONFLICT',
            `Signal is already ${existing.status} — cannot mark failed`,
            409
          );
        }

        const failureReason = String(
          body.reason || body.mt5_error || 'MT5 order_send failed (no detail supplied)'
        ).slice(0, 500);
        await setStatus(env.DB, id, 'failed', { failure_reason: failureReason });

        await pushToAll(env.DB, ctx, env, {
          title: `Order failed: ${existing.symbol}`,
          body: failureReason.slice(0, 160),
          data: { signal_id: id, type: 'failed' },
        });

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

      // --- App/agent: approve a signal ---
      // POST /signals/:id/approve
      // Moves pending -> approved. The approver is the external agent
      // (subsystem signal monitor) or a human — the Worker itself never
      // approves on its own anymore (no auto-approve).
      const approveMatch = path.match(/^\/signals\/([^/]+)\/approve$/);
      if (approveMatch && method === 'POST') {
        const id = approveMatch[1];
        const existing = await getSignal(env.DB, id);
        if (!existing) return notFound();
        if (existing.status !== 'pending') {
          return err('CONFLICT', `Signal is not pending (status=${existing.status})`, 409);
        }
        // Kill switch blocks manual approval too, not just autonomous
        // flows — never trust only the client to have hidden the button.
        // Defense in depth: this is checked here AND the bridge's risk
        // layer independently checks the same state before executing.
        if (await isKillSwitchEngaged(env.DB)) {
          return err(
            'CONFLICT',
            'Kill switch is engaged — disengage it before approving trades.',
            409
          );
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
          return err('CONFLICT', `Signal is not pending (status=${existing.status})`, 409);
        }
        await setStatus(env.DB, id, 'rejected_by_user');
        return json({ ok: true });
      }

      // --- Bridge: report that a position was closed ---
      // POST /signals/:id/closed  { reason, price, commission?, swap?, reflection_text? }
      // Stores the close, computes the deterministic outcome, and persists
      // a reflection RECORD. The reflection TEXT is generated by the
      // external agent — the bridge/agent may pass it in `reflection_text`;
      // if absent, the record is stored with a placeholder and the text
      // can be supplied later (the agent writes it via this same route's
      // field or a dedicated update).
      const closedMatch = path.match(/^\/signals\/([^/]+)\/closed$/);
      if (closedMatch && method === 'POST') {
        const id = closedMatch[1];
        const body = await request.json();
        const existing = await getSignal(env.DB, id);
        if (!existing) return notFound();
        if (existing.status === 'closed') {
          return err('CONFLICT', 'Signal is already closed', 409);
        }

        await markClosed(env.DB, id, {
          reason: body.reason,
          price: body.price,
          commission: body.commission,
          swap: body.swap,
        });

        // Reflection record: deterministic outcome math + persistence.
        // No LLM involved — see src/reflect.js.
        const outcome = computeOutcome(existing.direction, existing.entry_price, body.price);
        ctx.waitUntil(
          recordReflection(
            env.DB,
            { ...existing, closed_price: body.price },
            outcome,
            body.reflection_text ?? null
          ).catch((e) => console.error('Reflection persistence failed:', e))
        );

        await pushToAll(env.DB, ctx, env, {
          title: `Position closed: ${existing.symbol}`,
          body: `${body.reason || 'Closed'} @ ${body.price}`,
          data: { signal_id: id, type: 'closed' },
        });

        return json({ ok: true, outcome });
      }

      // --- App: register a device token for push notifications ---
      // POST /devices/register  { token: "..." }
      if (path === '/devices/register' && method === 'POST') {
        const body = await request.json();
        if (!body.token) return err('VALIDATION_ERROR', 'Missing token', 400);
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
          return err('VALIDATION_ERROR', 'No devices registered yet.', 400);
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

      // --- App/agent: change trading mode ---
      // POST /settings/trading-mode  { mode: "manual" | "autonomous" }
      // Kept for compatibility with the app's mode toggle. Meaning in the
      // new architecture: "autonomous" tells the SUBSYSTEM's signal
      // monitor it may approve signals via POST /signals/:id/approve
      // without a human tap; the Worker itself never approves either way.
      if (path === '/settings/trading-mode' && method === 'POST') {
        const body = await request.json();
        if (body.mode !== 'manual' && body.mode !== 'autonomous') {
          return err('VALIDATION_ERROR', 'mode must be "manual" or "autonomous"', 400);
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
      // While engaged, NO new entry can happen — approval is rejected and
      // the bridge's risk layer independently checks the same state
      // (defense in depth).
      if (path === '/kill-switch' && method === 'POST') {
        const body = await request.json();
        if (typeof body.engaged !== 'boolean') {
          return err('VALIDATION_ERROR', 'engaged must be true or false', 400);
        }
        await setKillSwitchEngaged(env.DB, body.engaged);

        await pushToAll(env.DB, ctx, env, {
          title: body.engaged ? '🛑 Kill switch engaged' : 'Kill switch disengaged',
          body: body.engaged
            ? 'All new entries are halted until this is disengaged.'
            : 'Trading can resume normally.',
          data: { type: 'kill_switch' },
        });

        return json({ ok: true, engaged: body.engaged });
      }

      // --- App: request an emergency close-all ---
      // POST /kill-switch/close-all
      // One-shot request, not a toggle — the bridge picks this up on its
      // next fast poll cycle and force-closes every open position it
      // manages, bypassing all normal exit logic.
      if (path === '/kill-switch/close-all' && method === 'POST') {
        await requestCloseAll(env.DB);

        await pushToAll(env.DB, ctx, env, {
          title: '🛑 Emergency close-all requested',
          body: 'The bridge will force-close every open position on its next check.',
          data: { type: 'kill_switch_close_all_requested' },
        });

        return json({ ok: true });
      }

      // --- Bridge: acknowledge that the close-all sweep is complete ---
      // POST /kill-switch/close-all/complete  { closed_count: N }
      if (path === '/kill-switch/close-all/complete' && method === 'POST') {
        const body = await request.json();
        await clearCloseAllRequest(env.DB);

        await pushToAll(env.DB, ctx, env, {
          title: 'Close-all complete',
          body: `${body.closed_count ?? 0} position(s) force-closed.`,
          data: { type: 'kill_switch_close_all_complete' },
        });

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
          return err('VALIDATION_ERROR', 'Missing symbol or timeframe', 400);
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
          return err(
            'VALIDATION_ERROR',
            'symbol and timeframe query params are required',
            400
          );
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
    } catch (err2) {
      console.error('Unhandled error:', err2);
      return err('INTERNAL', 'Internal error', 500);
    }
  },
};
