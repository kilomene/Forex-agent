/**
 * Per-client credential authentication.
 *
 * Replaces the original single shared secret (env.WORKER_API_KEY).
 * Clients are provisioned records in the `api_clients` D1 table —
 * each has a `client_id` and a secret whose SHA-256 hash is stored
 * (never the raw secret). Revoked clients are rejected.
 *
 * Credential format on the wire (choose one):
 *   Authorization: Basic base64(client_id:secret)     (preferred)
 *   Authorization: Bearer client_id:secret
 *
 * Secrets are never logged anywhere in this file. Comparison is
 * constant-time over the SHA-256 digests. On success the client's
 * `last_used_at` is updated best-effort (never fails the request).
 *
 * Provisioning/rotation/revocation: see docs/SECURITY.md and
 * scripts/provision-client.mjs. The deploy-time env WORKER_API_KEY no
 * longer exists — any request still sending it is rejected (401).
 */

import { getApiClient, touchApiClient } from './db.js';

export async function hashSecret(secret) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(secret));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

function timingSafeEqualHex(a, b) {
  if (a.length !== b.length) return false;
  const ab = new TextEncoder().encode(a);
  const bb = new TextEncoder().encode(b);
  // crypto.subtle.timingSafeEqual exists in Workers and in Node 22+.
  if (crypto.subtle.timingSafeEqual) {
    return crypto.subtle.timingSafeEqual(ab, bb);
  }
  let diff = 0;
  for (let i = 0; i < ab.length; i++) diff |= ab[i] ^ bb[i];
  return diff === 0;
}

function parseCredentials(request) {
  const header = request.headers.get('Authorization') || '';

  const basic = header.match(/^Basic\s+(.+)$/i);
  if (basic) {
    try {
      const decoded = atob(basic[1].trim());
      const colon = decoded.indexOf(':');
      if (colon > 0) {
        return { clientId: decoded.slice(0, colon), secret: decoded.slice(colon + 1) };
      }
    } catch {
      return null;
    }
    return null;
  }

  const bearer = header.match(/^Bearer\s+(.+)$/i);
  if (bearer) {
    const token = bearer[1].trim();
    const colon = token.indexOf(':');
    if (colon > 0) {
      return { clientId: token.slice(0, colon), secret: token.slice(colon + 1) };
    }
  }

  return null;
}

/**
 * Returns { ok: true, client_id } on success, or { ok: false } on any
 * failure (missing header, unknown client, revoked client, bad secret).
 * Never reveals which part failed — all failures look identical.
 */
export async function authenticate(request, db) {
  const creds = parseCredentials(request);
  if (!creds || !creds.secret) return { ok: false };

  const client = await getApiClient(db, creds.clientId);
  if (!client || client.revoked) return { ok: false };

  const candidateHash = await hashSecret(creds.secret);
  if (!timingSafeEqualHex(candidateHash, client.secret_hash)) return { ok: false };

  // Best-effort usage stamp; a failure here must not fail the request.
  touchApiClient(db, creds.clientId).catch(() => {});
  return { ok: true, client_id: creds.clientId };
}

export function unauthorizedResponse() {
  return new Response(
    JSON.stringify({ error: 'Unauthorized', code: 'AUTH_REQUIRED' }),
    {
      status: 401,
      headers: {
        'Content-Type': 'application/json',
        'WWW-Authenticate': 'Basic realm="forex-agent", Bearer realm="forex-agent"',
      },
    }
  );
}
