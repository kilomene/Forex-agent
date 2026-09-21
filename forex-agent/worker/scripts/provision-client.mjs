#!/usr/bin/env node
/**
 * Provision (or rotate) a per-client API credential for the forex-agent Worker.
 *
 * Usage:
 *   node scripts/provision-client.mjs <client_id> [label]
 *
 * Generates a cryptographically random 256-bit secret, prints:
 *   1. the raw secret ONCE (hand it to the client now — it is never stored
 *      anywhere; losing it means rotating, see below), and
 *   2. the SQL to register the client in D1 (stores only the SHA-256 hash).
 *
 * Apply the SQL with:
 *   wrangler d1 execute forex-signals-db --command="<the SQL>"
 *   (or save it to a file and use --file=...)
 *
 * Rotation: run this again with the same <client_id>. The ON CONFLICT
 * clause replaces the stored hash, immediately invalidating the old
 * secret, and un-revokes the client. Hand the new secret to the client
 * and delete the old one everywhere it was stored.
 *
 * Revocation (no new secret):
 *   wrangler d1 execute forex-signals-db \
 *     --command="UPDATE api_clients SET revoked = 1 WHERE client_id = '<client_id>';"
 */

import { randomBytes, createHash, randomUUID } from 'node:crypto';

const [clientId, ...labelParts] = process.argv.slice(2);
if (!clientId || !/^[A-Za-z0-9_-]{1,64}$/.test(clientId)) {
  console.error('Usage: node scripts/provision-client.mjs <client_id> [label]');
  console.error('  client_id: 1-64 chars, letters/digits/_/- only (e.g. "bridge", "app", "agent")');
  process.exit(1);
}
const label = labelParts.join(' ') || null;

const secret = randomBytes(32).toString('base64url'); // 256-bit, URL-safe
const secretHash = createHash('sha256').update(secret, 'utf8').digest('hex');
const id = randomUUID();
const ts = new Date().toISOString();

const esc = (s) => s.replace(/'/g, "''");
const sql =
  `INSERT INTO api_clients (id, client_id, secret_hash, label, revoked, created_at) ` +
  `VALUES ('${id}', '${esc(clientId)}', '${secretHash}', ` +
  `${label === null ? 'NULL' : `'${esc(label)}'`}, 0, '${ts}') ` +
  `ON CONFLICT(client_id) DO UPDATE SET ` +
  `secret_hash = excluded.secret_hash, label = excluded.label, revoked = 0;`;

console.log('==============================================================');
console.log('CLIENT SECRET — show this to the client operator ONCE, then');
console.log('store it in the client\'s secret store (never in chat/logs).');
console.log('==============================================================');
console.log(`  client_id : ${clientId}`);
console.log(`  secret    : ${secret}`);
console.log('');
console.log('Wire format (Authorization header):');
console.log(`  Basic ${Buffer.from(`${clientId}:${secret}`).toString('base64')}`);
console.log('  -or-');
console.log(`  Bearer ${clientId}:${secret}`);
console.log('');
console.log('Register with D1 (stores only the SHA-256 hash):');
console.log(sql);
