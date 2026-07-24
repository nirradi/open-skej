/**
 * Sandbox-mode token minting: the `AccessTokenProvider` implementation
 * `SandboxAuthProvider` installs into the api client.
 *
 * Kept apart from `SandboxAuthProvider.tsx` so that module exports only the
 * component — a file mixing a component export with plain function/constant
 * exports loses Vite's fast-refresh boundary, the same reason `config.ts` is
 * split from `AuthProvider.tsx`.
 */

import { API_BASE_URL } from '../api'

/**
 * The `localStorage` key a test (or a developer) sets to choose which seeded
 * identity to authenticate as. Read fresh on every token request rather than
 * once at mount, so `signInAsSandbox` in `app/e2e/tests/fixtures.ts` can pin
 * it before a navigation without `SandboxAuthProvider` needing to re-render.
 */
export const SANDBOX_SUB_STORAGE_KEY = 'skej.sandbox.sub'

/**
 * The identity assumed when nothing has chosen one — the seeded owner sub
 * from `app/backend/app/sandbox_seed.py` (`OWNER_AUTH0_SUB`). Kept as a
 * literal rather than imported: this is a TypeScript module and that is a
 * Python one, so the two are mirrors of one another, not one source of truth
 * — the E2E fixture's constants (`app/e2e/tests/fixtures.ts`) are the other
 * mirror, and all three must be changed together if the seed ever changes.
 */
const DEFAULT_SANDBOX_SUB = 'sandbox|owner'

/** How much of a token's remaining lifetime must be left to still use it. */
const EXPIRY_SAFETY_MARGIN_MS = 60_000

interface CachedToken {
  sub: string
  token: string
  expiresAtMs: number
}

let cached: CachedToken | null = null

/** The sub to authenticate as: whatever is in `localStorage`, or the owner. */
function currentSandboxSub(): string {
  return window.localStorage.getItem(SANDBOX_SUB_STORAGE_KEY) || DEFAULT_SANDBOX_SUB
}

/**
 * The `exp` claim of a JWT, in epoch milliseconds.
 *
 * Read directly off the token rather than tracked separately from the mint
 * call, so this provider does not have to know the backend's
 * `SANDBOX_TOKEN_TTL_SECONDS` — the token is the one source of truth for its
 * own expiry. The signature is not (and need not be) verified here: this
 * process just asked `/sandbox/token` to mint it, so the only thing this
 * reads it for is "is it still worth reusing", not "is it trustworthy".
 */
function expiryOf(token: string): number {
  const payload = token.split('.')[1] ?? ''
  const normalized = payload.replace(/-/g, '+').replace(/_/g, '/')
  const decoded = JSON.parse(atob(normalized)) as { exp: number }
  return decoded.exp * 1000
}

/** `POST /sandbox/token` for the given identity. */
async function mintSandboxToken(sub: string): Promise<string> {
  const response = await fetch(`${API_BASE_URL}/sandbox/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sub }),
  })
  if (!response.ok) {
    throw new Error(`POST /sandbox/token failed: ${response.status}`)
  }
  const body = (await response.json()) as { access_token: string }
  return body.access_token
}

/**
 * Resolves to a sandbox-signed access token for the currently chosen
 * identity, minting a fresh one whenever the sub has changed or the cached
 * one is at or past its safety margin.
 *
 * This is the sandbox-mode implementation of `AccessTokenProvider` — the same
 * seam `AccessTokenBridge` installs `getAccessTokenSilently` into. A rejection
 * here (the fetch throwing, or a non-2xx status) propagates exactly like a
 * rejected `getAccessTokenSilently`: `client.ts#authorizationHeader` turns it
 * into the `unauthenticated` outcome rather than sending the request.
 */
export async function getSandboxAccessToken(): Promise<string> {
  const sub = currentSandboxSub()
  const now = Date.now()

  if (cached && cached.sub === sub && cached.expiresAtMs - EXPIRY_SAFETY_MARGIN_MS > now) {
    return cached.token
  }

  const token = await mintSandboxToken(sub)
  cached = { sub, token, expiresAtMs: expiryOf(token) }
  return token
}
