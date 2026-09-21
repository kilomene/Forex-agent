/**
 * Both the Windows bridge (worker_client.py) and the mobile app authenticate
 * with the same shared secret, sent as `Authorization: Bearer <key>`.
 * This is a single-user personal system — no per-user accounts needed.
 */

export function isAuthorized(request, env) {
  const header = request.headers.get('Authorization') || '';
  const match = header.match(/^Bearer\s+(.+)$/i);
  if (!match) return false;
  return match[1] === env.WORKER_API_KEY;
}

export function unauthorizedResponse() {
  return new Response(JSON.stringify({ error: 'Unauthorized' }), {
    status: 401,
    headers: { 'Content-Type': 'application/json' },
  });
}
