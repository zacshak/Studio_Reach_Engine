# OAuth preparation — not enabled or deployed

The Worker remains a read-only MCP resource server. A separately selected OAuth
authorization server must authenticate the owner and obtain explicit consent.
No login, consent, authorization-code, registration, or token endpoint is added
to this Worker. The mock authorization server exists only inside `oauth.test.js`.
It must never be used as real authentication.

```text
ChatGPT -> MCP /mcp -> 401 + protected-resource metadata URL
ChatGPT -> metadata -> trusted authorization-server issuer
Browser -> trusted provider -> owner login + explicit read consent
Provider -> exact registered callback -> code + state + issuer
ChatGPT -> provider token endpoint -> code + S256 verifier + resource
ChatGPT -> MCP /mcp -> signed, owner-bound access token
MCP -> existing read-only Turso credential -> existing three tables
```

## Prepared behavior

- Static `MCP_AUTH_TOKEN` access is preserved even if optional OAuth settings are
  incomplete or the identity provider is unavailable.
- OAuth is off unless `OAUTH_ENABLED` is exactly `true`. No live configuration
  was changed; `wrangler.jsonc` is unchanged.
- Public discovery: `/.well-known/oauth-protected-resource/mcp` and the root
  `/.well-known/oauth-protected-resource` alias. Values come from fixed operator
  configuration, not Host headers. Bad enabled settings return 503 for metadata.
- OAuth requests require an RFC 9068 `at+jwt` access token, a configured RS256 or
  ES256 signature, exact issuer, a single exact audience, exact `client_id`, exact
  owner `sub`, and `sre:read`. Email and client-supplied identity headers confer
  no access. Tokens require integer `iat`/`exp`, at most 15 minutes lifetime,
  valid expiration/not-before, and no clock tolerance.
- ID tokens, generic upstream login tokens, query-string tokens, multi-audience
  tokens, and tokens for other clients/users/resources are rejected. A provider
  using opaque tokens or a different claim profile needs an explicit adapter;
  do not weaken checks merely to accept its ID token.
- Invalid tokens return 401; a verified token with wrong owner or insufficient
  scope returns 403. Challenges include the fixed metadata URL and required scope.
- JWKS requests use only the configured HTTPS URL, never send the incoming
  bearer, reject redirects, time out after 5 seconds, and cap responses at 64 KiB.
  This initial implementation fetches keys per OAuth request. An issuer outage
  denies OAuth access; no cross-request key cache or introspection is included.
- The database path, read-only credentials, table access, and foreign-Origin
  rejection remain unchanged. There are no browser CORS allowances.

## Decisions still required

Choose the real authorization provider and owner account. Record its immutable
subject identifier after authenticating, not by guessing from an email. A hosted
OAuth provider can issue the required MCP access token directly. If choosing
Cloudflare Access or Google/GitHub login through a Worker broker, a separate
provider/broker integration is still required; a login assertion by itself is
not the MCP access token accepted here.

The authorization server must deny everyone except the owner before granting
consent, even if signup or client registration exists. Require explicit consent
for the client and `sre:read`; never authorize an arbitrary submitted user ID.
If a broker is needed, use a maintained provider library, with authenticated
sessions, CSRF protection, bound consent transactions and atomic one-time codes.

Use one pre-registered OAuth client initially. Copy its exact production callback
from the ChatGPT MCP management page; never infer it or allow wildcard/suffix
matches. No client or redirect has been registered by this change. CIMD/DCR can
be evaluated with the selected provider later; neither is implemented here.

Provider integration acceptance criteria:

1. Publish OAuth/OIDC discovery, S256 support and authorization/token endpoints.
2. Enforce authorization-code + S256 PKCE, exact registered callbacks, and the
   same resource in authorization and exchange. Bind code to client, redirect,
   resource, scope, owner and challenge. Codes expire quickly and redeem once.
3. Bind and validate browser state. Support RFC 9207 `iss` in success and error
   callbacks; metadata issuer must exactly match the configured issuer.
4. Issue access tokens with this document's claim profile and lifetime. Do not
   grant client-credentials or other machine identities access to owner data.
5. Test owner login/consent and denial of a second real test account in an isolated
   provider environment. Test actual PKCE, redirect, expired-code and replay denial.
6. Define revocation and refresh policy. This Worker validates JWTs locally; it
   does not introspect revocation. Existing access may last until token expiry
   (up to 15 minutes). Refresh issuance/rotation/revocation belongs to the provider.

## Configuration contract (no values or secrets set)

| Variable | Required value when enabling OAuth |
| --- | --- |
| `OAUTH_ENABLED` | Exact string `true`, only after integration approval |
| `OAUTH_ISSUER` | Exact HTTPS issuer identifier from provider metadata |
| `OAUTH_JWKS_URL` | Trusted HTTPS public-key endpoint; no redirect/query/credentials |
| `OAUTH_RESOURCE` | Canonical HTTPS MCP URL ending in `/mcp` |
| `OAUTH_OWNER_SUB` | Sole owner's immutable subject in that issuer |
| `OAUTH_CLIENT_ID` | Exact pre-registered client ID in access-token `client_id` |
| `OAUTH_SIGNING_ALG` | `RS256` or `ES256`, matching the selected provider |

URLs must not contain userinfo, query strings or fragments. Issuer comparison is
exact, including a trailing slash. Keep existing MCP/Turso secrets untouched.
Provider credentials, if later needed, belong in secure secret storage, not this
file or client-visible metadata. No production credentials are used by the tests.

## Local validation and limits

Run `node --test worker.test.js oauth.test.js` with the project's dependencies.
Run `npm run test:runtime` for the local Workers emulator check. It uses the
esbuild/Miniflare versions already locked with Wrangler and mocks outbound calls.
Both checks passed: 14 Node tests plus Workers discovery, missing-auth denial,
legacy/OAuth initialization and non-owner denial. The emulator caught an initial
`redirect: 'error'` incompatibility; the implementation uses `manual` with explicit
status rejection, and passed on rerun. No network requests reach live services.
`jose` 6.2.12 is promoted from its existing locked transitive dependency to an
explicit dependency; no dependency upgrade is included.

The tests use ephemeral signing keys and intercept all network requests. They
exercise the actual resource-server verifier, discovery, legacy bearer behavior,
the official MCP SDK, signature/issuer/audience/owner/client/scope/time denials,
JWKS failures, and a mock authorization-server contract for S256 PKCE, callbacks,
consent, owner checks, code expiry and replay. **Mock provider tests are examples
of the required contract, not proof of any real provider's security or readiness.**

Remaining: select provider and owner, implement/configure that integration,
verify real callback and token profile with ChatGPT, test key rotation and provider
failure/recovery, then separately review and approve deployment. No deployment,
production secret/grant change, push, plugin install, or outbound workflow is
authorized by this preparation.

## Sources checked on 2026-10-15

- [MCP authorization specification, 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
- [OpenAI authentication guidance](https://developers.openai.com/plugins/build/auth)
- [Cloudflare MCP authorization options](https://developers.cloudflare.com/agents/model-context-protocol/protocol/authorization/)

The current OpenAI guidance says to copy the callback from the management page;
its exact shape depends on issuer-identification support. Cloudflare distinguishes
provider protocol handling from actual user authentication. This preparation does
not treat its mock-login example as production authentication.
