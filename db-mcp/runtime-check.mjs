// Local-only runtime evidence; imports existing Wrangler transitive tooling.
import { build } from 'esbuild';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
import { generateKeyPair, exportJWK, SignJWT } from 'jose';
import assert from 'node:assert/strict';

const { privateKey, publicKey } = await generateKeyPair('RS256');
const jwk = { ...await exportJWK(publicKey), kid: 'local-runtime', use: 'sig', alg: 'RS256' };
const result = await build({ entryPoints: ['./worker.js'], bundle: true, write: false,
  format: 'esm', platform: 'neutral', conditions: ['workerd', 'browser'], mainFields: ['module', 'main'], external: ['node:*'] });
const settings = { MCP_AUTH_TOKEN: 'synthetic-runtime-key', TURSO_READ_TOKEN: 'synthetic-db-token',
  TURSO_URL: 'https://database.invalid', OAUTH_ENABLED: 'true', OAUTH_ISSUER: 'https://identity.invalid/',
  OAUTH_JWKS_URL: 'https://identity.invalid/jwks', OAUTH_RESOURCE: 'https://mcp.invalid/mcp',
  OAUTH_OWNER_SUB: 'runtime-owner', OAUTH_CLIENT_ID: 'runtime-client', OAUTH_SIGNING_ALG: 'RS256' };
const runtime = new Miniflare(convertV4MiniflareOptions({ modules: true, script: result.outputFiles[0].text,
  compatibilityDate: '2026-10-14', compatibilityFlags: ['nodejs_compat'], bindings: settings,
  outboundService: async request => {
    assert.equal(request.url, settings.OAUTH_JWKS_URL);
    assert.equal(request.headers.has('Authorization'), false);
    return Response.json({ keys: [jwk] });
  }
}));
try {
  const metadata = await runtime.dispatchFetch('https://mcp.invalid/.well-known/oauth-protected-resource/mcp');
  assert.equal(metadata.status, 200);
  assert.equal((await metadata.json()).resource, settings.OAUTH_RESOURCE);
  assert.equal((await runtime.dispatchFetch(settings.OAUTH_RESOURCE)).status, 401);
  const timestamp = Math.floor(Date.now() / 1000);
  const signed = async sub => new SignJWT({ iss: settings.OAUTH_ISSUER, aud: settings.OAUTH_RESOURCE,
    sub, client_id: settings.OAUTH_CLIENT_ID, scope: 'sre:read', iat: timestamp, exp: timestamp + 300 })
    .setProtectedHeader({ alg: 'RS256', typ: 'at+jwt', kid: jwk.kid }).sign(privateKey);
  for (const bearer of [settings.MCP_AUTH_TOKEN, await signed(settings.OAUTH_OWNER_SUB)]) {
    const response = await runtime.dispatchFetch(settings.OAUTH_RESOURCE, { method: 'POST',
      headers: { Authorization: `Bearer ${bearer}`, 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {
        protocolVersion: '2025-03-26', capabilities: {}, clientInfo: { name: 'runtime-check', version: '1' }
      } }) });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).result.serverInfo.name, 'sre-database');
  }
  assert.equal((await runtime.dispatchFetch(settings.OAUTH_RESOURCE,
    { headers: { Authorization: `Bearer ${await signed('non-owner')}` } })).status, 403);
  console.log('Workers runtime passed: metadata, unauthenticated denial, bearer and OAuth initialization, non-owner denial; all outbound calls mocked.');
} finally { await runtime.dispose(); }
