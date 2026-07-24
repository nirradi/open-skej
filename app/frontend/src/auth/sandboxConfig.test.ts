/**
 * Tests for reading the sandbox-auth environment.
 *
 * `readSandboxConfig` takes its env as a parameter for the same reason
 * `readAuth0Config` does — see `config.test.ts`.
 */

import { describe, expect, it } from 'vitest'
import { readSandboxConfig } from './sandboxConfig'

const auth0Complete: ImportMetaEnv = {
  VITE_AUTH0_DOMAIN: 'dev-tenant.us.auth0.com',
  VITE_AUTH0_CLIENT_ID: 'client-123',
  VITE_AUTH0_AUDIENCE: 'https://api.open-skej.dev',
} as ImportMetaEnv

describe('readSandboxConfig', () => {
  it('is off when unset', () => {
    expect(readSandboxConfig({} as ImportMetaEnv)).toBe(false)
  })

  it('is off for anything but the literal string "true"', () => {
    expect(readSandboxConfig({ VITE_SANDBOX_AUTH: '1' } as ImportMetaEnv)).toBe(false)
    expect(readSandboxConfig({ VITE_SANDBOX_AUTH: 'yes' } as ImportMetaEnv)).toBe(false)
    expect(readSandboxConfig({ VITE_SANDBOX_AUTH: '' } as ImportMetaEnv)).toBe(false)
  })

  it('is on for "true", case-insensitively and trimmed', () => {
    expect(readSandboxConfig({ VITE_SANDBOX_AUTH: 'true' } as ImportMetaEnv)).toBe(true)
    expect(readSandboxConfig({ VITE_SANDBOX_AUTH: 'TRUE' } as ImportMetaEnv)).toBe(true)
    expect(readSandboxConfig({ VITE_SANDBOX_AUTH: ' true ' } as ImportMetaEnv)).toBe(true)
  })

  it('throws when enabled alongside a fully configured Auth0 tenant', () => {
    expect(() =>
      readSandboxConfig({ ...auth0Complete, VITE_SANDBOX_AUTH: 'true' } as ImportMetaEnv),
    ).toThrow(/VITE_SANDBOX_AUTH is enabled together with a VITE_AUTH0_\* variable/)
  })

  it('throws when enabled alongside just one Auth0 variable', () => {
    // Not just "fully configured" — a single stray `VITE_AUTH0_DOMAIN` left in
    // a `.env.local` is exactly the kind of half-set config that must not
    // silently coexist with sandbox mode. See the backend's mirror-image
    // check in `app.auth.jwt.get_token_verifier`.
    expect(() =>
      readSandboxConfig({
        VITE_SANDBOX_AUTH: 'true',
        VITE_AUTH0_DOMAIN: 'dev-tenant.us.auth0.com',
      } as ImportMetaEnv),
    ).toThrow(/VITE_SANDBOX_AUTH is enabled together with a VITE_AUTH0_\* variable/)
  })

  it('does not throw when Auth0 vars are set and sandbox mode is off', () => {
    expect(readSandboxConfig(auth0Complete)).toBe(false)
  })
})
