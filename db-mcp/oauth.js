import { createHash, timingSafeEqual } from 'node:crypto';
import { createRemoteJWKSet, customFetch, jwtVerify } from 'jose';

export const READ_SCOPE = 'sre:read';
export const METADATA_PATH = '/.well-known/oauth-protected-resource/mcp';

function httpsURL(value) {
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.username || url.password || url.hash || url.search) throw new Error('Invalid OAuth URL');
  return url;
}

// Opt-in only. All values come from operator configuration, never request headers,
// token claims, or client registration. No owner is inferred from an email address.
export function oauthConfig(env) {
  if (env.OAUTH_ENABLED !== 'true') return null;
  httpsURL(env.OAUTH_ISSUER);
  const resource = httpsURL(env.OAUTH_RESOURCE);
  const jwks = httpsURL(env.OAUTH_JWKS_URL);
  if (resource.pathname !== '/mcp' || !env.OAUTH_OWNER_SUB || !env.OAUTH_CLIENT_ID ||
      !['RS256', 'ES256'].includes(env.OAUTH_SIGNING_ALG)) throw new Error('Incomplete OAuth configuration');
  return { issuer: env.OAUTH_ISSUER, resource: resource.href, jwks: jwks.href,
    owner: env.OAUTH_OWNER_SUB, client: env.OAUTH_CLIENT_ID, algorithm: env.OAUTH_SIGNING_ALG,
    metadata: resource.origin + METADATA_PATH };
}

export function metadataResponse(request, config) {
  if (!config) return new Response('Not found', { status: 404 });
  if (!['GET', 'HEAD'].includes(request.method)) return new Response('Use GET', { status: 405, headers: { Allow: 'GET, HEAD' } });
  const body = { resource: config.resource, authorization_servers: [config.issuer],
    scopes_supported: [READ_SCOPE], bearer_methods_supported: ['header'] };
  return new Response(request.method === 'HEAD' ? null : JSON.stringify(body), {
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }
  });
}

function denial(config, status = 401, error = 'invalid_token') {
  const challenge = config ? `Bearer resource_metadata="${config.metadata}", scope="${READ_SCOPE}", error="${error}"` : 'Bearer';
  return new Response(status === 403 ? 'Forbidden' : 'Unauthorized', {
    status, headers: { 'WWW-Authenticate': challenge, 'Cache-Control': 'no-store' }
  });
}

async function boundedKeyFetch(url, options) {
  // jose supplies only the configured JWKS URL and its own headers. Never forward
  // the incoming bearer token. Redirects cannot move key discovery to another host.
  const response = await fetch(url, { ...options, redirect: 'manual' });
  if (response.status !== 200) { await response.body?.cancel(); throw new Error('Key endpoint unavailable'); }
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > 65536) throw new Error('Key response too large');
      chunks.push(value);
    }
  } finally { await reader.cancel(); }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return new Response(bytes, { headers: { 'Content-Type': 'application/json' } });
}

export async function authorize(request, env, config) {
  const header = request.headers.get('Authorization') || '';
  const hash = value => createHash('sha256').update(value).digest();
  // Existing clients remain usable even if the optional OAuth configuration fails.
  if (env.MCP_AUTH_TOKEN && timingSafeEqual(hash(header), hash(`Bearer ${env.MCP_AUTH_TOKEN}`))) return null;
  if (!config) return denial(null);
  const match = /^Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)$/i.exec(header);
  if (!match || match[1].length > 16384) return denial(config);
  try {
    const keys = createRemoteJWKSet(new URL(config.jwks), {
      timeoutDuration: 5000, [customFetch]: boundedKeyFetch
    });
    const { payload } = await jwtVerify(match[1], keys, {
      issuer: config.issuer, audience: config.resource, algorithms: [config.algorithm],
      typ: 'at+jwt', requiredClaims: ['sub', 'exp', 'iat', 'client_id', 'scope'],
      maxTokenAge: 900, clockTolerance: 0
    });
    // RFC 9068 access tokens only; ID tokens and upstream-provider tokens are not
    // substitutes. Limit grants to 15 minutes and a single exact MCP audience.
    if (payload.aud !== config.resource || !Number.isInteger(payload.iat) ||
        !Number.isInteger(payload.exp) || payload.exp <= payload.iat || payload.exp - payload.iat > 900 ||
        payload.client_id !== config.client) return denial(config);
    if (payload.sub !== config.owner) return denial(config, 403, 'access_denied');
    if (typeof payload.scope !== 'string' || !payload.scope.split(' ').includes(READ_SCOPE)) {
      return denial(config, 403, 'insufficient_scope');
    }
    return null;
  } catch {
    // Do not return or log token contents, verification errors, or provider data.
    return denial(config);
  }
}
