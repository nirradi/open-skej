/**
 * Sandbox-auth configuration, read from the build-time environment.
 *
 * The frontend counterpart of `settings.sandbox_auth` /
 * `app.auth.jwt.get_token_verifier` on the backend (see
 * `.claude/rules/identity-and-access.md`, "Sandbox auth mode"): a dedicated,
 * explicit switch that lets Playwright and manual QA authenticate against an
 * in-process sandbox token instead of a hosted Auth0 login, with the same two
 * guardrails the backend enforces at verifier construction — off by default
 * and never inferred, and mutually exclusive with a real Auth0 config rather
 * than one silently winning.
 *
 * Kept apart from `AuthProvider.tsx` for the same reason `readAuth0Config` is:
 * "is sandbox mode on, and is the environment sane?" is a plain function over
 * a plain object, testable with no DOM.
 */

/** The build-time flag. Absent or anything but the literal string `"true"` is off. */
function truthy(value: string | undefined): boolean {
  return (value ?? '').trim().toLowerCase() === 'true'
}

/** The three Auth0 variables `readAuth0Config` reads — see that module. */
const AUTH0_ENV_KEYS = ['VITE_AUTH0_DOMAIN', 'VITE_AUTH0_CLIENT_ID', 'VITE_AUTH0_AUDIENCE'] as const

function anyAuth0VarSet(env: ImportMetaEnv): boolean {
  return AUTH0_ENV_KEYS.some((key) => (env[key] ?? '').trim() !== '')
}

/**
 * Reads and validates the sandbox-auth environment.
 *
 * Takes the env as a parameter (defaulting to Vite's), for the same reason
 * `readAuth0Config` does: `import.meta.env` is frozen at build time and
 * cannot be stubbed, so a function reading it directly could only ever be
 * tested in whatever state the runner happened to be in.
 *
 * ## Fails loudly rather than picking one config
 *
 * A build with `VITE_SANDBOX_AUTH=true` and any `VITE_AUTH0_*` variable also
 * set is refused by throwing, mirroring the backend's `get_token_verifier`
 * raising at verifier construction rather than choosing Auth0 or the sandbox
 * key for the caller. A frontend willing to authenticate against either is
 * strictly worse than one with no sandbox mode at all — it would install
 * whichever credential source happened to be reachable first, silently.
 * There is nothing to "recover" from here: it is a build-time configuration
 * error, not a request-time outcome, so it throws instead of returning a
 * result the caller might not check.
 */
export function readSandboxConfig(env: ImportMetaEnv = import.meta.env): boolean {
  const enabled = truthy(env.VITE_SANDBOX_AUTH)

  if (enabled && anyAuth0VarSet(env)) {
    throw new Error(
      'VITE_SANDBOX_AUTH is enabled together with a VITE_AUTH0_* variable. Sandbox mode ' +
        'authenticates against a local sandbox token instead of the real Auth0 tenant, and a ' +
        'build willing to use either would install whichever credential source is reachable ' +
        'first. Unset the VITE_AUTH0_* variables, or unset VITE_SANDBOX_AUTH — sandbox mode is ' +
        'for a build with no real Auth0 tenant configured at all.',
    )
  }

  return enabled
}
