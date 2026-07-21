/**
 * Tests for reading the Auth0 environment.
 *
 * `readAuth0Config` takes its env as a parameter precisely so these can exist:
 * `import.meta.env` is substituted at build time and cannot be stubbed, so a
 * function that reached for it directly would only ever be testable in whatever
 * state the runner happened to be in.
 */

import { describe, expect, it } from 'vitest'
import { readAuth0Config } from './config'

/** A fully-populated env, to be spread and selectively broken. */
const complete: ImportMetaEnv = {
  VITE_AUTH0_DOMAIN: 'dev-tenant.us.auth0.com',
  VITE_AUTH0_CLIENT_ID: 'client-123',
  VITE_AUTH0_AUDIENCE: 'https://api.open-skej.dev',
} as ImportMetaEnv

describe('readAuth0Config', () => {
  it('returns the config when all three are set', () => {
    const result = readAuth0Config(complete)

    expect(result).toEqual({
      status: 'ok',
      config: {
        domain: 'dev-tenant.us.auth0.com',
        clientId: 'client-123',
        audience: 'https://api.open-skej.dev',
      },
    })
  })

  it('names the missing variable rather than merely failing', () => {
    const result = readAuth0Config({ ...complete, VITE_AUTH0_CLIENT_ID: undefined })

    expect(result.status).toBe('missing')
    if (result.status !== 'missing') throw new Error('unreachable')
    // The name is the whole point: the SDK's own failure mode for an empty
    // client id is a blank page and a console message that never says "env".
    expect(result.missing).toEqual(['VITE_AUTH0_CLIENT_ID'])
  })

  it('lists every missing variable at once', () => {
    // One at a time would mean three restart-and-retry cycles to configure an
    // app from scratch, which is the common case, not the rare one.
    const result = readAuth0Config({} as ImportMetaEnv)

    if (result.status !== 'missing') throw new Error('unreachable')
    expect(result.missing).toEqual([
      'VITE_AUTH0_DOMAIN',
      'VITE_AUTH0_CLIENT_ID',
      'VITE_AUTH0_AUDIENCE',
    ])
  })

  it('treats an empty or whitespace value as missing', () => {
    // `VITE_AUTH0_DOMAIN=` with nothing after it is a line someone meant to
    // fill in. Passing '' through would build a request to `https:///authorize`.
    const result = readAuth0Config({ ...complete, VITE_AUTH0_DOMAIN: '   ' })

    if (result.status !== 'missing') throw new Error('unreachable')
    expect(result.missing).toEqual(['VITE_AUTH0_DOMAIN'])
  })

  it('requires the audience specifically', () => {
    // The one that is optional in the SDK and mandatory for us: without it the
    // SDK silently returns an opaque /userinfo token instead of a JWT, so every
    // backend call 401s behind a login that appeared to succeed. Failing at
    // boot converts that into one line of text.
    const result = readAuth0Config({ ...complete, VITE_AUTH0_AUDIENCE: undefined })

    if (result.status !== 'missing') throw new Error('unreachable')
    expect(result.missing).toEqual(['VITE_AUTH0_AUDIENCE'])
  })

  it('trims surrounding whitespace off a value it accepts', () => {
    const result = readAuth0Config({ ...complete, VITE_AUTH0_DOMAIN: ' dev-tenant.us.auth0.com ' })

    if (result.status !== 'ok') throw new Error('unreachable')
    expect(result.config.domain).toBe('dev-tenant.us.auth0.com')
  })
})
