/**
 * Contract tests for the forex-agent Worker (cloud/sync layer).
 *
 * Runs the real src/index.js against an in-memory FakeD1 that implements
 * the exact D1 surface the Worker uses (prepare/bind/all/run/first).
 * No network, no wrangler, no npm — `node --test worker/tests/`.
 *
 * What is covered (mirrors CONTRACT.md):
 *  - signal lifecycle: report -> pending -> approve -> executed -> closed
 *    (with reflection record persisted, deterministic outcome computed)
 *  - NEW route POST /signals/:id/failed
 *  - kill-switch engage blocks approvals (409)
 *  - per-client auth: missing/bad/unknown credentials -> 401
 *  - settings round-trip (trading mode)
 *  - request validation (400s) and state-transition guards (409s)
 *  - devices, performance snapshots, reflections, chart-data routes
 *  - per-client rate limiting (429)
 */

import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import worker from '../src/index.js';
import { hashSecret } from '../src/auth.js';
import { __clearRateLimits } from '../src/ratelimit.js';
import { __clearTokenCache } from '../src/fcm.js';

// ---------------------------------------------------------------------------
// Fake D1: implements prepare/bind/all/run/first over in-memory tables,
// dispatching on the exact SQL strings src/db.js issues.
// ---------------------------------------------------------------------------

const norm = (sql) => sql.replace(/\s+/g, ' ').trim();
const parseCols = (sql) => sql.match(/\(([^)]+)\)/)[1].split(',').map((c) => c.trim().toLowerCase());
const zip = (cols, vals) => Object.fromEntries(cols.map((c, i) => [c, vals[i]]));
const ok = () => ({ success: true });

class FakeStatement {
  constructor(db, sql) {
    this.db = db;
    this.sql = norm(sql);
    this.params = [];
  }
  bind(...p) {
    this.params = p;
    return this;
  }
  async all() {
    return { results: this.db._all(this.sql, this.params) };
  }
  async first() {
    const rows = this.db._all(this.sql, this.params);
    return rows.length ? rows[0] : null;
  }
  async run() {
    return this.db._run(this.sql, this.params);
  }
}

class FakeDB {
  constructor() {
    this.reset();
  }
  reset() {
    this.signals = new Map();
    this.tokens = new Map();
    this.settings = new Map();
    this.perf = [];
    this.reflections = [];
    this.charts = new Map();
    this.clients = new Map();
    this._seq = 0;
  }
  prepare(sql) {
    return new FakeStatement(this, sql);
  }
  _byCreatedDesc(a, b) {
    return (
      String(b.created_at).localeCompare(String(a.created_at)) || b._seq - a._seq
    );
  }
  _all(sql, p) {
    const s = sql.toLowerCase();
    if (s === 'select * from signals where id = ?') {
      const r = this.signals.get(p[0]);
      return r ? [r] : [];
    }
    if (s.startsWith('select * from signals where status = ?')) {
      return [...this.signals.values()]
        .filter((r) => r.status === p[0])
        .sort((a, b) => this._byCreatedDesc(a, b))
        .slice(0, p[1]);
    }
    if (s.startsWith('select * from signals order by created_at desc limit ?')) {
      return [...this.signals.values()]
        .sort((a, b) => this._byCreatedDesc(a, b))
        .slice(0, p[0]);
    }
    if (s.includes("from signals where status = 'approved'")) {
      return [...this.signals.values()]
        .filter((r) => r.status === 'approved')
        .sort((a, b) => -this._byCreatedDesc(a, b));
    }
    if (s === 'select token from device_tokens') {
      return [...this.tokens.values()].map((r) => ({ token: r.token }));
    }
    if (s === 'select value from settings where key = ?') {
      const r = this.settings.get(p[0]);
      return r ? [{ value: r.value }] : [];
    }
    if (s === 'select * from performance_snapshots order by created_at desc limit 1') {
      return this.perf.length ? [this.perf[this.perf.length - 1]] : [];
    }
    if (s.startsWith('select * from reflections where symbol = ?')) {
      return this.reflections
        .filter((r) => r.symbol === p[0])
        .sort((a, b) => this._byCreatedDesc(a, b))
        .slice(0, p[1]);
    }
    if (s.startsWith('select * from reflections order by created_at desc limit ?')) {
      return [...this.reflections]
        .sort((a, b) => this._byCreatedDesc(a, b))
        .slice(0, p[0]);
    }
    if (s === 'select * from chart_data where symbol = ? and timeframe = ?') {
      const r = this.charts.get(`${p[0]}|${p[1]}`);
      return r ? [r] : [];
    }
    if (s.startsWith('select symbol, timeframe, updated_at from chart_data')) {
      return [...this.charts.values()].map((r) => ({
        symbol: r.symbol,
        timeframe: r.timeframe,
        updated_at: r.updated_at,
      }));
    }
    if (s.startsWith('select client_id, secret_hash, revoked, last_used_at from api_clients')) {
      const c = this.clients.get(p[0]);
      return c
        ? [
            {
              client_id: c.client_id,
              secret_hash: c.secret_hash,
              revoked: c.revoked,
              last_used_at: c.last_used_at,
            },
          ]
        : [];
    }
    throw new Error(`FakeDB: unhandled ALL: ${sql}`);
  }
  _run(sql, p) {
    const s = sql.toLowerCase();
    if (s.startsWith('insert into signals (')) {
      const row = zip(parseCols(sql), p);
      row._seq = ++this._seq;
      // NOTE: the real SQL uses a literal 'pending' in VALUES, so the
      // generic column zip misaligns status/created_at/updated_at.
      // Realign here to match what D1 would actually store.
      row.status = 'pending';
      row.created_at = p[p.length - 2];
      row.updated_at = p[p.length - 1];
      this.signals.set(row.id, row);
      return ok();
    }
    if (s.startsWith('update signals set ')) {
      // Handles both placeholder assignments (col = ?) and literals
      // (e.g. markClosed's `status = 'closed'`).
      const setPart = sql.match(/update signals set (.+) where id = \?/i)[1];
      const assigns = setPart.split(',').map((x) => x.trim());
      const id = p[p.length - 1];
      const row = this.signals.get(id);
      if (row) {
        let pi = 0;
        for (const a of assigns) {
          const [col, rhs] = a.split(/\s*=\s*/);
          const colName = col.toLowerCase();
          if (rhs === '?') row[colName] = p[pi++];
          else row[colName] = rhs.replace(/^'(.*)'$/, '$1');
        }
      }
      return ok();
    }
    if (s.startsWith('insert into device_tokens')) {
      const row = zip(parseCols(sql), p);
      this.tokens.set(row.token, row);
      return ok();
    }
    if (s.startsWith('insert into settings')) {
      const row = zip(parseCols(sql), p);
      this.settings.set(row.key, row);
      return ok();
    }
    if (s.startsWith('insert into performance_snapshots')) {
      const row = zip(parseCols(sql), p);
      row._seq = ++this._seq;
      this.perf.push(row);
      return ok();
    }
    if (s.startsWith('insert into reflections')) {
      const row = zip(parseCols(sql), p);
      row._seq = ++this._seq;
      this.reflections.push(row);
      return ok();
    }
    if (s.startsWith('insert into chart_data')) {
      const row = zip(parseCols(sql), p);
      this.charts.set(`${row.symbol}|${row.timeframe}`, row);
      return ok();
    }
    if (s.startsWith('insert into api_clients')) {
      const row = zip(parseCols(sql), p);
      this.clients.set(row.client_id, row);
      return ok();
    }
    if (s.startsWith('update api_clients set revoked = 1')) {
      const c = this.clients.get(p[0]);
      if (c) c.revoked = 1;
      return ok();
    }
    if (s.startsWith('update api_clients set last_used_at')) {
      const c = this.clients.get(p[1]);
      if (c) c.last_used_at = p[0];
      return ok();
    }
    throw new Error(`FakeDB: unhandled RUN: ${sql}`);
  }
}

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

const SECRETS = {
  bridge: 'test-secret-bridge-aaaaaaaaaaaaaaaaaaaaaaaa',
  app: 'test-secret-app-bbbbbbbbbbbbbbbbbbbbbbbb',
};

const SIG = {
  symbol: 'EURUSD',
  timeframe: 'M15',
  direction: 'BUY',
  entry_price: 1.085,
  stop_loss: 1.08,
  take_profit: 1.095,
  candle_time: '2026-09-21T12:00:00.000Z',
  trigger: 'EMA20 crossed above EMA50',
};

let db;
let env;
const pending = [];
const ctx = {
  waitUntil(p) {
    pending.push(p);
  },
};

async function seedClient(clientId, secret) {
  db.clients.set(clientId, {
    id: `id-${clientId}`,
    client_id: clientId,
    secret_hash: await hashSecret(secret),
    label: 'test client',
    revoked: 0,
    created_at: new Date().toISOString(),
    last_used_at: null,
  });
}

function makeEnv(overrides = {}) {
  return {
    DB: db,
    RATE_LIMIT_GENERAL_PER_MIN: '1000000',
    RATE_LIMIT_INGEST_PER_MIN: '1000000',
    RATE_LIMIT_AUTH_FAIL_PER_MIN: '1000000',
    ...overrides,
  };
}

async function call(method, path, opts = {}) {
  const headers = new Headers(opts.headers || {});
  if (!opts.noAuth && !headers.has('Authorization')) {
    const cid = opts.clientId || 'bridge';
    const sec = opts.secret !== undefined ? opts.secret : SECRETS[cid];
    const creds = opts.bearerColon
      ? `${cid}:${sec}`
      : Buffer.from(`${cid}:${sec}`).toString('base64');
    headers.set('Authorization', opts.bearerColon ? `Bearer ${creds}` : `Basic ${creds}`);
  }
  const res = await worker.fetch(
    new Request(`https://worker.test${path}`, {
      method,
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
    }),
    env,
    ctx
  );
  await Promise.allSettled(pending.splice(0));
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* non-JSON body */
  }
  return { status: res.status, data };
}

beforeEach(async () => {
  __clearRateLimits();
  __clearTokenCache();
  db = new FakeDB();
  env = makeEnv();
  await seedClient('bridge', SECRETS.bridge);
  await seedClient('app', SECRETS.app);
});

// ---------------------------------------------------------------------------
// Signal lifecycle
// ---------------------------------------------------------------------------

test('signal lifecycle: report -> pending -> approve -> executed -> closed (+reflection record)', async () => {
  const created = await call('POST', '/signals', { body: SIG });
  assert.equal(created.status, 201);
  assert.ok(created.data.signal_id, 'returns a signal id');
  const id = created.data.signal_id;

  const one = await call('GET', `/signals/${id}`);
  assert.equal(one.status, 200);
  assert.equal(one.data.signal.status, 'pending');
  assert.equal(one.data.signal.ai_reasoning, null, 'Worker performs no AI reasoning');

  const approved = await call('POST', `/signals/${id}/approve`);
  assert.equal(approved.status, 200);
  assert.equal(approved.data.ok, true);

  const reapprove = await call('POST', `/signals/${id}/approve`);
  assert.equal(reapprove.status, 409, 'approving a non-pending signal is a conflict');

  const pendingList = await call('GET', '/signals/pending-approved');
  assert.equal(pendingList.status, 200);
  assert.ok(pendingList.data.signals.some((s) => s.id === id));

  const executed = await call('POST', `/signals/${id}/executed`, {
    body: { mt5_result: { retcode: 10009, ticket: 123 }, spread_at_entry: 0.0002, lot_size: 0.1 },
  });
  assert.equal(executed.status, 200);

  const closed = await call('POST', `/signals/${id}/closed`, {
    body: {
      reason: 'trailing_stop',
      price: 1.09,
      commission: -1.5,
      swap: 0,
      reflection_text: 'Trailing stop worked as designed; exit discipline held.',
    },
  });
  assert.equal(closed.status, 200);
  assert.equal(closed.data.outcome, 'win');

  const after = await call('GET', `/signals/${id}`);
  assert.equal(after.data.signal.status, 'closed');
  assert.equal(after.data.signal.closed_price, 1.09);

  const refs = await call('GET', '/reflections?symbol=EURUSD&limit=10');
  assert.equal(refs.status, 200);
  assert.equal(refs.data.reflections.length, 1);
  assert.equal(refs.data.reflections[0].outcome, 'win');
  assert.equal(
    refs.data.reflections[0].reflection_text,
    'Trailing stop worked as designed; exit discipline held.'
  );

  const closedAgain = await call('POST', `/signals/${id}/closed`, {
    body: { reason: 'trailing_stop', price: 1.09 },
  });
  assert.equal(closedAgain.status, 409, 'double close is a conflict');
});

test('POST /signals validates required fields', async () => {
  const res = await call('POST', '/signals', { body: { symbol: 'EURUSD' } });
  assert.equal(res.status, 400);
  assert.equal(res.data.code, 'VALIDATION_ERROR');
  assert.match(res.data.error, /Missing field/);
});

test('executed requires an approved signal', async () => {
  const created = await call('POST', '/signals', { body: SIG });
  const id = created.data.signal_id;
  const res = await call('POST', `/signals/${id}/executed`, { body: {} });
  assert.equal(res.status, 409);
});

test('no auto-approve: signal stays pending even in autonomous mode', async () => {
  await call('POST', '/settings/trading-mode', { body: { mode: 'autonomous' } });
  const created = await call('POST', '/signals', { body: SIG });
  const one = await call('GET', `/signals/${created.data.signal_id}`);
  assert.equal(one.data.signal.status, 'pending', 'Worker never auto-approves');
});

// ---------------------------------------------------------------------------
// NEW: failed route
// ---------------------------------------------------------------------------

test('POST /signals/:id/failed records broker-side order failures', async () => {
  const created = await call('POST', '/signals', { body: SIG });
  const id = created.data.signal_id;
  await call('POST', `/signals/${id}/approve`);

  const failed = await call('POST', `/signals/${id}/failed`, {
    body: { reason: 'order_send retcode=10006 (requote)', mt5_error: 'TRADE_RETCODE_REQUOTE' },
  });
  assert.equal(failed.status, 200);
  assert.equal(failed.data.ok, true);

  const one = await call('GET', `/signals/${id}`);
  assert.equal(one.data.signal.status, 'failed');
  assert.match(one.data.signal.failure_reason, /requote/);

  const failedMissing = await call('POST', '/signals/does-not-exist/failed', {
    body: { reason: 'x' },
  });
  assert.equal(failedMissing.status, 404);
});

test('failed is rejected on already-terminal signals', async () => {
  const created = await call('POST', '/signals', { body: SIG });
  const id = created.data.signal_id;
  await call('POST', `/signals/${id}/approve`);
  await call('POST', `/signals/${id}/executed`, { body: {} });
  await call('POST', `/signals/${id}/closed`, { body: { reason: 'manual', price: 1.08 } });
  const res = await call('POST', `/signals/${id}/failed`, { body: { reason: 'x' } });
  assert.equal(res.status, 409);
});

// ---------------------------------------------------------------------------
// Kill switch
// ---------------------------------------------------------------------------

test('kill switch engage blocks approvals; disengage restores them', async () => {
  const created = await call('POST', '/signals', { body: SIG });
  const id = created.data.signal_id;

  const engage = await call('POST', '/kill-switch', { body: { engaged: true } });
  assert.equal(engage.status, 200);
  assert.equal(engage.data.engaged, true);

  const state = await call('GET', '/kill-switch');
  assert.equal(state.data.engaged, true);

  const blocked = await call('POST', `/signals/${id}/approve`);
  assert.equal(blocked.status, 409);
  assert.match(blocked.data.error, /Kill switch/);

  const disengage = await call('POST', '/kill-switch', { body: { engaged: false } });
  assert.equal(disengage.data.engaged, false);

  const approved = await call('POST', `/signals/${id}/approve`);
  assert.equal(approved.status, 200);
});

test('kill-switch close-all request/complete round-trip', async () => {
  const req = await call('POST', '/kill-switch/close-all', { body: {} });
  assert.equal(req.status, 200);
  const state = await call('GET', '/kill-switch');
  assert.ok(state.data.close_all_requested_at, 'request timestamp is set');

  const done = await call('POST', '/kill-switch/close-all/complete', {
    body: { closed_count: 3 },
  });
  assert.equal(done.status, 200);
  const cleared = await call('GET', '/kill-switch');
  assert.equal(cleared.data.close_all_requested_at, null);
});

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

test('auth: missing / bad / unknown credentials -> 401; good -> 200', async () => {
  const missing = await call('GET', '/kill-switch', { noAuth: true });
  assert.equal(missing.status, 401);
  assert.equal(missing.data.code, 'AUTH_REQUIRED');

  const badSecret = await call('GET', '/kill-switch', { secret: 'wrong-secret' });
  assert.equal(badSecret.status, 401);

  const unknown = await call('GET', '/kill-switch', {
    clientId: 'nobody',
    secret: 'whatever',
  });
  assert.equal(unknown.status, 401);

  const malformed = await call('GET', '/kill-switch', {
    headers: { Authorization: 'Bearer not-a-client-secret-pair' },
  });
  assert.equal(malformed.status, 401);

  const good = await call('GET', '/kill-switch', { clientId: 'app' });
  assert.equal(good.status, 200);
});

test('auth: Bearer client_id:secret form is accepted', async () => {
  const res = await call('GET', '/kill-switch', { clientId: 'app', bearerColon: true });
  assert.equal(res.status, 200);
});

test('auth: revoked client is rejected', async () => {
  db.clients.get('bridge').revoked = 1;
  const res = await call('GET', '/kill-switch');
  assert.equal(res.status, 401);
});

test('auth: legacy shared-secret format is rejected', async () => {
  const res = await call('GET', '/kill-switch', {
    headers: { Authorization: 'Bearer some-old-shared-secret' },
  });
  assert.equal(res.status, 401, 'WORKER_API_KEY-style bearer without client_id no longer works');
});

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

test('settings: trading-mode round-trip + validation', async () => {
  const initial = await call('GET', '/settings/trading-mode');
  assert.equal(initial.data.trading_mode, 'manual', 'defaults to manual');

  const set = await call('POST', '/settings/trading-mode', { body: { mode: 'autonomous' } });
  assert.equal(set.status, 200);
  assert.equal(set.data.trading_mode, 'autonomous');

  const reread = await call('GET', '/settings/trading-mode');
  assert.equal(reread.data.trading_mode, 'autonomous');

  const bad = await call('POST', '/settings/trading-mode', { body: { mode: 'yolo' } });
  assert.equal(bad.status, 400);
});

// ---------------------------------------------------------------------------
// Signals listing / detail
// ---------------------------------------------------------------------------

test('GET /signals lists with status filter and limit', async () => {
  const a = await call('POST', '/signals', { body: SIG });
  const b = await call('POST', '/signals', { body: { ...SIG, symbol: 'GBPUSD' } });
  await call('POST', `/signals/${a.data.signal_id}/approve`);

  const pendingOnly = await call('GET', '/signals?status=pending');
  assert.ok(pendingOnly.data.signals.every((s) => s.status === 'pending'));
  assert.ok(pendingOnly.data.signals.some((s) => s.id === b.data.signal_id));

  const missing = await call('GET', '/signals/nope');
  assert.equal(missing.status, 404);
});

// ---------------------------------------------------------------------------
// Devices / performance / chart-data
// ---------------------------------------------------------------------------

test('devices: register + test-push with no devices is a 400', async () => {
  const reg = await call('POST', '/devices/register', { body: { token: 'fcm-token-1' } });
  assert.equal(reg.status, 200);

  const missing = await call('POST', '/devices/register', { body: {} });
  assert.equal(missing.status, 400);
});

test('devices: test-push with zero devices reports the problem', async () => {
  const res = await call('POST', '/devices/test-push', { body: {} });
  assert.equal(res.status, 400);
  assert.match(res.data.error, /No devices/);
});

test('performance snapshot round-trip', async () => {
  const snap = {
    period_start: '2026-09-01T00:00:00.000Z',
    period_end: '2026-09-21T00:00:00.000Z',
    total_closed_trades: 10,
    wins: 6,
    losses: 4,
    breakeven: 0,
    win_rate_pct: 60,
    total_profit: 120.5,
    net_profit: 115.0,
  };
  const created = await call('POST', '/performance/snapshot', { body: snap });
  assert.equal(created.status, 201);
  assert.ok(created.data.id);

  const latest = await call('GET', '/performance/latest');
  assert.equal(latest.status, 200);
  assert.equal(latest.data.available, true);
  assert.equal(latest.data.snapshot.total_closed_trades, 10);
});

test('chart-data round-trip', async () => {
  const payload = {
    symbol: 'EURUSD',
    timeframe: 'H1',
    candles: [{ time: '2026-09-21T12:00:00.000Z', open: 1, high: 2, low: 0.5, close: 1.5 }],
    ema_fast: [null, 1.2],
    ema_slow: [null, 1.1],
    ema_fast_period: 20,
    ema_slow_period: 50,
  };
  const up = await call('POST', '/chart-data', { body: payload });
  assert.equal(up.status, 200);

  const bad = await call('POST', '/chart-data', { body: { symbol: 'EURUSD' } });
  assert.equal(bad.status, 400);

  const avail = await call('GET', '/chart-data/available');
  assert.ok(avail.data.charts.some((c) => c.symbol === 'EURUSD' && c.timeframe === 'H1'));

  const one = await call('GET', '/chart-data?symbol=EURUSD&timeframe=H1');
  assert.equal(one.data.available, true);
  assert.deepEqual(one.data.candles, payload.candles);
  assert.equal(one.data.ema_fast_period, 20);

  const missing = await call('GET', '/chart-data?symbol=EURUSD&timeframe=M1');
  assert.equal(missing.data.available, false);

  const noParams = await call('GET', '/chart-data');
  assert.equal(noParams.status, 400);
});

// ---------------------------------------------------------------------------
// Rate limiting
// ---------------------------------------------------------------------------

test('rate limiting: ingest bucket returns 429 when exceeded', async () => {
  env = makeEnv({ RATE_LIMIT_INGEST_PER_MIN: '2', RATE_LIMIT_GENERAL_PER_MIN: '1000000' });
  assert.equal((await call('POST', '/signals', { body: SIG })).status, 201);
  assert.equal((await call('POST', '/signals', { body: SIG })).status, 201);
  const third = await call('POST', '/signals', { body: SIG });
  assert.equal(third.status, 429);
  assert.equal(third.data.code, 'RATE_LIMITED');
});

test('rate limiting: repeated auth failures are throttled per IP', async () => {
  env = makeEnv({ RATE_LIMIT_AUTH_FAIL_PER_MIN: '2' });
  assert.equal((await call('GET', '/kill-switch', { secret: 'bad1' })).status, 401);
  assert.equal((await call('GET', '/kill-switch', { secret: 'bad2' })).status, 401);
  const third = await call('GET', '/kill-switch', { secret: 'bad3' });
  assert.equal(third.status, 429);
});
