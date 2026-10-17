import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { exportJWK, generateKeyPair, SignJWT } from 'jose';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import worker from './worker.js';
import { oauthConfig, METADATA_PATH, READ_SCOPE } from './oauth.js';

// Ephemeral synthetic keys only. No secrets, config files, live databases or IdPs.
const { publicKey, privateKey } = await generateKeyPair('RS256');
const publicJwk = { ...await exportJWK(publicKey), kid: 'mock-key', alg: 'RS256', use: 'sig' };
const env = {
  MCP_AUTH_TOKEN: 'synthetic-test-key', TURSO_READ_TOKEN: 'synthetic-db-token', TURSO_URL: 'https://database.invalid',
  OAUTH_ENABLED: 'true', OAUTH_ISSUER: 'https://identity.invalid/', OAUTH_JWKS_URL: 'https://identity.invalid/jwks',
  OAUTH_RESOURCE: 'https://mcp.invalid/mcp', OAUTH_OWNER_SUB: 'owner-123', OAUTH_CLIENT_ID: 'registered-client', OAUTH_SIGNING_ALG: 'RS256'
};
const now = () => Math.floor(Date.now() / 1000);
async function token(changes = {}, header = {}, key = privateKey) {
  return new SignJWT({ iss: env.OAUTH_ISSUER, aud: env.OAUTH_RESOURCE, sub: env.OAUTH_OWNER_SUB,
    client_id: env.OAUTH_CLIENT_ID, scope: READ_SCOPE, iat: now(), exp: now() + 300, ...changes })
    .setProtectedHeader({ alg: 'RS256', typ: 'at+jwt', kid: 'mock-key', ...header }).sign(key);
}
function keysOnly(t, response = () => Response.json({ keys: [publicJwk] })) {
  return t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(String(url), env.OAUTH_JWKS_URL, 'No database or other network calls allowed');
    assert.equal(new Headers(options.headers).has('Authorization'), false, 'Bearer must never reach JWKS');
    assert.equal(options.redirect, 'manual');
    return response();
  });
}
const call = (value, settings = env, options = {}) => worker.fetch(new Request(env.OAUTH_RESOURCE, {
  ...options, headers: { Authorization: `Bearer ${value}`, ...options.headers }
}), settings);

test('OAuth is opt-in, metadata is canonical, and malformed settings fail closed', async () => {
  assert.equal(oauthConfig({}), null);
  for (const change of [{ OAUTH_OWNER_SUB: '' }, { OAUTH_CLIENT_ID: '' }, { OAUTH_SIGNING_ALG: 'HS256' },
    { OAUTH_ISSUER: 'http://identity.invalid/' }, { OAUTH_RESOURCE: 'https://mcp.invalid/other' },
    { OAUTH_JWKS_URL: 'https://user:pass@identity.invalid/jwks' }]) {
    assert.throws(() => oauthConfig({ ...env, ...change }));
  }
  for (const path of [METADATA_PATH, '/.well-known/oauth-protected-resource']) {
    const req = new Request('https://untrusted-host.invalid' + path);
    const response = await worker.fetch(req, env);
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { resource: env.OAUTH_RESOURCE, authorization_servers: [env.OAUTH_ISSUER],
      scopes_supported: [READ_SCOPE], bearer_methods_supported: ['header'] });
    assert.equal((await worker.fetch(req, { ...env, OAUTH_ENABLED: 'false' })).status, 404);
    assert.equal((await worker.fetch(req, { ...env, OAUTH_OWNER_SUB: '' })).status, 503);
  }
  assert.equal((await worker.fetch(new Request('https://mcp.invalid' + METADATA_PATH, { method: 'POST' }), env)).status, 405);
});

test('legacy bearer works with OAuth enabled, disabled, or broken without key lookup', async t => {
  t.mock.method(globalThis, 'fetch', () => { throw new Error('Unexpected network'); });
  for (const settings of [env, { ...env, OAUTH_ENABLED: 'false' }, { ...env, OAUTH_OWNER_SUB: '' }]) {
    assert.equal((await call(env.MCP_AUTH_TOKEN, settings)).status, 405);
    assert.equal((await call('wrong', settings)).status, 401);
  }
});

test('signed owner access token works; missing, expired, foreign and overbroad tokens do not', async t => {
  keysOnly(t);
  assert.equal((await call(await token())).status, 405); // Authorized GET reaches POST-only transport.
  const cases = [
    [{ iss: 'https://attacker.invalid/' }, 401], [{ aud: 'https://other.invalid/mcp' }, 401],
    [{ aud: [env.OAUTH_RESOURCE, 'https://other.invalid'] }, 401], [{ aud: undefined }, 401],
    [{ exp: now() - 1 }, 401], [{ exp: undefined }, 401], [{ exp: now() + 3600 }, 401],
    [{ iat: now() + 60 }, 401], [{ iat: undefined }, 401], [{ nbf: now() + 60 }, 401],
    [{ sub: 'another-user', email: 'owner@example.invalid', email_verified: true }, 403],
    [{ sub: undefined }, 401], [{ client_id: 'another-client' }, 401],
    [{ scope: 'sre:write' }, 403], [{ scope: 'not-sre:read' }, 403], [{ scope: undefined }, 401]
  ];
  for (const [claims, status] of cases) assert.equal((await call(await token(claims))).status, status, JSON.stringify(claims));
  for (const header of [{ typ: 'JWT' }, { typ: undefined }, { kid: 'unknown' }]) {
    assert.equal((await call(await token({}, header))).status, 401);
  }
  const impostor = await generateKeyPair('RS256');
  assert.equal((await call(await token({}, {}, impostor.privateKey))).status, 401);
  assert.equal((await call('eyJhbGciOiJub25lIn0.e30.')).status, 401);
  const challenge = (await call('wrong')).headers.get('WWW-Authenticate');
  assert.match(challenge, /resource_metadata="https:\/\/mcp.invalid\/\.well-known\/oauth-protected-resource\/mcp"/);
  const insufficient = await call(await token({ scope: 'other' }));
  assert.match(insufficient.headers.get('WWW-Authenticate'), /error="insufficient_scope"/);
  assert.equal(insufficient.headers.get('Cache-Control'), 'no-store');
  assert.equal((await worker.fetch(new Request(env.OAUTH_RESOURCE + '?access_token=' + await token()), env)).status, 401);
  assert.equal((await call(await token(), env, { headers: { Origin: 'https://attacker.invalid' } })).status, 403);
});

test('key endpoint failures, oversized payloads and redirects fail closed', async t => {
  const jwt = await token();
  for (const response of [() => new Response('error', { status: 503 }),
    () => new Response('x'.repeat(65537)), () => new Response('{broken'),
    () => new Response(null, { status: 302, headers: { Location: 'https://attacker.invalid/jwks' } }),
    () => { throw new Error('network unavailable'); }]) {
    const mock = keysOnly(t, response);
    const denied = await call(jwt);
    assert.equal(denied.status, 401);
    assert.equal(await denied.text(), 'Unauthorized');
    mock.mock.restore();
  }
});

test('configured ES256 works and an algorithm mismatch is denied', async t => {
  const pair = await generateKeyPair('ES256');
  const jwk = { ...await exportJWK(pair.publicKey), kid: 'mock-key', use: 'sig', alg: 'ES256' };
  keysOnly(t, () => Response.json({ keys: [jwk] }));
  const jwt = await token({}, { alg: 'ES256' }, pair.privateKey);
  assert.equal((await call(jwt, { ...env, OAUTH_SIGNING_ALG: 'ES256' })).status, 405);
  assert.equal((await call(jwt, env)).status, 401);
});

test('official MCP client initializes and discovers tools using OAuth', async t => {
  keysOnly(t);
  const client = new Client({ name: 'oauth-local-test', version: '1.0.0' });
  const jwt = await token();
  const transport = new StreamableHTTPClientTransport(new URL(env.OAUTH_RESOURCE), {
    requestInit: { headers: { Authorization: `Bearer ${jwt}` } },
    fetch: (input, init) => worker.fetch(new Request(input, init), env)
  });
  try {
    await client.connect(transport);
    assert.deepEqual((await client.listTools()).tools.map(t => t.name), ['list_tables', 'describe_table', 'query', 'get_game_media', 'get_game_image', 'submit_triage', 'get_draft_context', 'save_draft','get_social_requests','get_social_data','save_social_data']);
    const result = await client.callTool({ name: 'list_tables', arguments: {} });
    assert.deepEqual(JSON.parse(result.content[0].text).tables, ['scrape_tracker', 'newly_added', 'known_comingsoon']);
  } finally { await client.close(); }
});

// TEST-ONLY authorization-server contract model. This is NOT production login,
// consent, or an AS implementation and is never imported by worker.js. A selected
// real provider must pass equivalent integration tests before enabling OAuth.
class MockAuthorizationServer {
  codes = new Map();
  redirect = 'https://client.invalid/exact-callback';
  authorize(params, authenticatedSubject, consent) {
    if (params.client_id !== env.OAUTH_CLIENT_ID || params.redirect_uri !== this.redirect) throw new Error('invalid_client_or_redirect');
    if (params.resource !== env.OAUTH_RESOURCE || params.scope !== READ_SCOPE) throw new Error('invalid_target_or_scope');
    if (params.code_challenge_method !== 'S256' || !/^[A-Za-z0-9_-]{43}$/.test(params.code_challenge)) throw new Error('invalid_pkce');
    if (authenticatedSubject !== env.OAUTH_OWNER_SUB || consent !== true) throw new Error('access_denied');
    const code = randomBytes(32).toString('base64url');
    this.codes.set(code, { ...params, subject: authenticatedSubject, expires: now() + 60 });
    const redirect = new URL(params.redirect_uri);
    redirect.searchParams.set('code', code); redirect.searchParams.set('state', params.state);
    redirect.searchParams.set('iss', env.OAUTH_ISSUER);
    return redirect;
  }
  async exchange(params) {
    const grant = this.codes.get(params.code);
    this.codes.delete(params.code); // Atomic and single-use in this in-memory model.
    if (!grant || grant.expires <= now() || params.grant_type !== 'authorization_code' ||
        params.client_id !== grant.client_id || params.redirect_uri !== grant.redirect_uri ||
        params.resource !== grant.resource || !/^[A-Za-z0-9._~-]{43,128}$/.test(params.code_verifier || '') ||
        createHash('sha256').update(params.code_verifier).digest('base64url') !== grant.code_challenge) throw new Error('invalid_grant');
    return token({ sub: grant.subject, scope: grant.scope });
  }
}
const verifier = () => randomBytes(32).toString('base64url');
function grantRequest(server, secret) {
  return { client_id: env.OAUTH_CLIENT_ID, redirect_uri: server.redirect, resource: env.OAUTH_RESOURCE,
    scope: READ_SCOPE, state: randomBytes(24).toString('base64url'), code_challenge_method: 'S256',
    code_challenge: createHash('sha256').update(secret).digest('base64url') };
}
function exchangeRequest(request, url, secret) {
  assert.equal(url.searchParams.get('state'), request.state);
  assert.equal(url.searchParams.get('iss'), env.OAUTH_ISSUER);
  return { ...request, grant_type: 'authorization_code', code: url.searchParams.get('code'), code_verifier: secret };
}

test('mock AS contract: owner consent, S256 and exact redirects lead to a usable MCP token', async t => {
  keysOnly(t);
  const server = new MockAuthorizationServer(), secret = verifier(), request = grantRequest(server, secret);
  const redirect = server.authorize(request, env.OAUTH_OWNER_SUB, true);
  const exchange = exchangeRequest(request, redirect, secret);
  assert.equal((await call(await server.exchange(exchange))).status, 405);
  await assert.rejects(server.exchange(exchange), /invalid_grant/);
});

test('mock AS contract: arbitrary users, missing consent, redirect tricks and PKCE downgrade are denied', () => {
  const server = new MockAuthorizationServer(), request = grantRequest(server, verifier());
  for (const user of [undefined, 'arbitrary-user']) assert.throws(() => server.authorize(request, user, true), /access_denied/);
  assert.throws(() => server.authorize(request, env.OAUTH_OWNER_SUB, false), /access_denied/);
  for (const change of [{ redirect_uri: server.redirect + '/extra' }, { redirect_uri: server.redirect + '?evil=1' },
    { redirect_uri: 'https://client.invalid.evil.test/exact-callback' }, { client_id: 'self-registered' },
    { code_challenge_method: 'plain' }, { code_challenge: '' }, { scope: 'sre:write' }, { resource: 'https://other.invalid' }]) {
    assert.throws(() => server.authorize({ ...request, ...change }, env.OAUTH_OWNER_SUB, true));
  }
});

test('mock AS contract: verifier, redirect, resource, client, expiry and callback binding are enforced', async () => {
  for (const change of [{ code_verifier: verifier() }, { code_verifier: '' }, { redirect_uri: 'https://evil.invalid/' },
    { resource: 'https://other.invalid' }, { client_id: 'other-client' }, { grant_type: 'client_credentials' }]) {
    const server = new MockAuthorizationServer(), secret = verifier(), request = grantRequest(server, secret);
    const redirect = server.authorize(request, env.OAUTH_OWNER_SUB, true);
    await assert.rejects(server.exchange({ ...exchangeRequest(request, redirect, secret), ...change }), /invalid_grant/);
  }
  const server = new MockAuthorizationServer(), secret = verifier(), request = grantRequest(server, secret);
  const redirect = server.authorize(request, env.OAUTH_OWNER_SUB, true);
  server.codes.get(redirect.searchParams.get('code')).expires = now() - 1;
  await assert.rejects(server.exchange(exchangeRequest(request, redirect, secret)), /invalid_grant/);
  redirect.searchParams.set('iss', 'https://evil.invalid/');
  assert.throws(() => exchangeRequest(request, redirect, secret));
  redirect.searchParams.set('iss', env.OAUTH_ISSUER); redirect.searchParams.set('state', 'wrong');
  assert.throws(() => exchangeRequest(request, redirect, secret));
});
