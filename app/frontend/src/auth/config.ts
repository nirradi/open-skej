/**
 * Auth0 configuration, read from the build-time environment.
 *
 * Kept apart from `AuthProvider.tsx` so that "is this app configured?" is a
 * plain function over a plain object rather than something only observable by
 * rendering a tree. The provider then has one job — render — and the missing-env
 * screen is a test that does not need a DOM.
 */

/** The three `VITE_AUTH0_*` variables, all required. See `.env.example`. */
export interface Auth0Config {
  domain: string
  clientId: string
  audience: string
}

/**
 * What `readAuth0Config` found.
 *
 * A discriminated union rather than `Auth0Config | null` because the *names* of
 * the missing variables are the entire value of the failure case — see
 * `MissingConfigNotice`.
 */
export type Auth0ConfigResult =
  { status: 'ok'; config: Auth0Config } | { status: 'missing'; missing: string[] }

/**
 * Maps each config field to the variable a developer actually has to set.
 *
 * Written out rather than derived from the field names so the error message
 * quotes a string that can be pasted into `.env.local` verbatim, instead of a
 * camelCase-to-SCREAMING_SNAKE guess that would be one transformation away from
 * being subtly wrong.
 */
const ENV_VARS: Record<keyof Auth0Config, string> = {
  domain: 'VITE_AUTH0_DOMAIN',
  clientId: 'VITE_AUTH0_CLIENT_ID',
  audience: 'VITE_AUTH0_AUDIENCE',
}

/**
 * Reads and validates the Auth0 environment.
 *
 * Takes the env as a parameter (defaulting to Vite's) purely so tests can pass
 * one. `import.meta.env` is frozen at build time and cannot be stubbed the way
 * `process.env` can, so a function that read it directly could only ever be
 * tested in whatever state the test runner happened to be in.
 *
 * ## Why `audience` is required rather than optional
 *
 * The Auth0 SDK treats `audience` as optional and **silently changes what it
 * gives you** when it is absent: without one, `getAccessTokenSilently` returns
 * an *opaque* token for the /userinfo endpoint rather than a JWT for our API.
 * That token sails through the browser looking entirely normal and is then
 * rejected by `app/backend/app/auth/jwt.py`, which cannot even parse it as a
 * JWT — so the symptom is a blanket 401 on every call with a perfectly
 * successful login sitting in front of it, and nothing anywhere says "audience".
 *
 * It must match `AUTH0_API_AUDIENCE` on the backend exactly (both default to
 * `https://api.open-skej.dev`, set by `scripts/auth0_provision.py`), because
 * that is the value the backend checks the `aud` claim against. Requiring it
 * here converts that afternoon into a startup message.
 */
export function readAuth0Config(env: ImportMetaEnv = import.meta.env): Auth0ConfigResult {
  const config: Auth0Config = {
    domain: env.VITE_AUTH0_DOMAIN?.trim() ?? '',
    clientId: env.VITE_AUTH0_CLIENT_ID?.trim() ?? '',
    audience: env.VITE_AUTH0_AUDIENCE?.trim() ?? '',
  }

  const missing = (Object.keys(ENV_VARS) as (keyof Auth0Config)[])
    .filter((key) => config[key] === '')
    .map((key) => ENV_VARS[key])

  // Whitespace counts as missing: a `VITE_AUTH0_DOMAIN=` line with nothing after
  // it is a variable someone meant to fill in, and passing '' to the SDK yields
  // a request to `https:///authorize` instead of a legible complaint.
  return missing.length > 0 ? { status: 'missing', missing } : { status: 'ok', config }
}
