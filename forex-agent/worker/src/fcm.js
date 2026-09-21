/**
 * Sends real push notifications via FCM HTTP v1 API.
 *
 * FCM v1 requires an OAuth2 access token obtained by signing a JWT with the
 * Firebase service account's private key (RS256) — the old server-key API
 * is deprecated. Workers has Web Crypto (crypto.subtle) so we do the RS256
 * signing natively, no external library needed.
 *
 * env.FCM_SERVICE_ACCOUNT_JSON must be the full service account JSON
 * (downloaded from Firebase Console -> Project Settings -> Service Accounts)
 * stored as a single-line string secret.
 */

const TOKEN_URL = 'https://oauth2.googleapis.com/token';
const SCOPE = 'https://www.googleapis.com/auth/firebase.messaging';

// Module-level OAuth token cache. Tokens are good for 3600s; we reuse the
// cached token until it is within REFRESH_SKEW_SEC of expiry, then mint a
// fresh one. This replaces the original behavior of re-minting a token per
// device per push. Cached per Worker isolate — safe: worst case an isolate
// mints its own token once an hour.
let cachedToken = null; // { access_token, expires_at_ms }
const REFRESH_SKEW_SEC = 300;

/** For tests only: clear the in-memory token cache. */
export function __clearTokenCache() {
  cachedToken = null;
}

function base64UrlEncode(bytes) {
  let binary = '';
  for (const b of new Uint8Array(bytes)) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function base64UrlEncodeString(str) {
  return base64UrlEncode(new TextEncoder().encode(str));
}

function pemToArrayBuffer(pem) {
  const b64 = pem
    .replace(/-----BEGIN PRIVATE KEY-----/, '')
    .replace(/-----END PRIVATE KEY-----/, '')
    .replace(/\s/g, '');
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

async function mintAccessToken(serviceAccount) {
  const header = { alg: 'RS256', typ: 'JWT' };
  const iat = Math.floor(Date.now() / 1000);
  const exp = iat + 3600;

  const claimSet = {
    iss: serviceAccount.client_email,
    scope: SCOPE,
    aud: TOKEN_URL,
    iat,
    exp,
  };

  const unsigned = `${base64UrlEncodeString(JSON.stringify(header))}.${base64UrlEncodeString(
    JSON.stringify(claimSet)
  )}`;

  const key = await crypto.subtle.importKey(
    'pkcs8',
    pemToArrayBuffer(serviceAccount.private_key),
    { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' },
    false,
    ['sign']
  );

  const signature = await crypto.subtle.sign(
    'RSASSA-PKCS1-v1_5',
    key,
    new TextEncoder().encode(unsigned)
  );

  const jwt = `${unsigned}.${base64UrlEncode(signature)}`;

  const resp = await fetch(TOKEN_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer',
      assertion: jwt,
    }),
  });

  if (!resp.ok) {
    throw new Error(`Failed to get FCM access token: ${resp.status} ${await resp.text()}`);
  }

  const data = await resp.json();

  // Return the token plus when it stops being usable, so callers can
  // cache it and only re-mint near expiry. We don't trust the response's
  // expires_in blindly — we still apply our own refresh skew.
  const expiresInSec = Number(data.expires_in) || 3600;
  return {
    access_token: data.access_token,
    expires_at_ms: Date.now() + expiresInSec * 1000,
  };
}

/** Returns a valid access token, reusing the cached one until near-expiry. */
async function getValidAccessToken(serviceAccount) {
  if (
    cachedToken &&
    cachedToken.access_token &&
    cachedToken.expires_at_ms - Date.now() > REFRESH_SKEW_SEC * 1000
  ) {
    return cachedToken.access_token;
  }
  cachedToken = await mintAccessToken(serviceAccount);
  return cachedToken.access_token;
}

/**
 * Sends a data+notification push to a single device token.
 * Returns true on success, false on failure (logged, not thrown — a push
 * failure shouldn't break the signal-reporting flow).
 */
export async function sendPush(token, { title, body, data = {} }, env) {
  try {
    const serviceAccount = JSON.parse(env.FCM_SERVICE_ACCOUNT_JSON);
    const accessToken = await getValidAccessToken(serviceAccount);

    const resp = await fetch(
      `https://fcm.googleapis.com/v1/projects/${env.FCM_PROJECT_ID}/messages:send`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${accessToken}`,
        },
        body: JSON.stringify({
          message: {
            token,
            notification: { title, body },
            data: Object.fromEntries(Object.entries(data).map(([k, v]) => [k, String(v)])),
          },
        }),
      }
    );

    if (!resp.ok) {
      console.error('FCM send failed:', resp.status, await resp.text());
      return false;
    }
    return true;
  } catch (err) {
    console.error('FCM send error:', err);
    return false;
  }
}

export async function sendPushToAll(tokens, payload, env) {
  const results = await Promise.all(tokens.map((t) => sendPush(t, payload, env)));
  return results.filter(Boolean).length;
}
