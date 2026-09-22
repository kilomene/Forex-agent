/**
 * Best-effort per-client rate limiting, in-memory per Worker isolate.
 *
 * Fixed-window counters: each (key, limit) pair gets `limit` requests per
 * 60-second window. This is NOT a distributed limiter — isolates keep
 * their own counters, so it bounds abusive clients rather than enforcing
 * a hard global cap. For strict global limits, add a Cloudflare dashboard
 * rate-limiting rule on top (see wrangler.toml comments).
 *
 * Defaults are overridable via Worker vars/secrets:
 *   RATE_LIMIT_GENERAL_PER_MIN  (default 120 — all routes, per client)
 *   RATE_LIMIT_INGEST_PER_MIN   (default 30  — POST /signals, per client)
 *   RATE_LIMIT_AUTH_FAIL_PER_MIN(default 20  — failed auth attempts, per IP)
 */

const buckets = new Map();
const WINDOW_MS = 60_000;
const MAX_KEYS = 10000;

/**
 * Returns { allowed, remaining }. `limitPerMin <= 0` disables limiting.
 */
export function checkRateLimit(key, limitPerMin) {
  const limit = Number(limitPerMin);
  if (!limit || limit <= 0) return { allowed: true, remaining: Infinity };

  const nowMs = Date.now();
  let bucket = buckets.get(key);
  if (!bucket || nowMs - bucket.windowStart >= WINDOW_MS) {
    bucket = { windowStart: nowMs, count: 0 };
    if (buckets.size >= MAX_KEYS && !buckets.has(key)) {
      // Evict the oldest entry to bound memory.
      const oldest = buckets.keys().next().value;
      buckets.delete(oldest);
    }
    buckets.set(key, bucket);
  }
  bucket.count += 1;
  return {
    allowed: bucket.count <= limit,
    remaining: Math.max(0, limit - bucket.count),
  };
}

/** For tests only. */
export function __clearRateLimits() {
  buckets.clear();
}
