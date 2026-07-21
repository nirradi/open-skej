/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the FastAPI backend. Defaults to `http://localhost:8000`. */
  readonly VITE_API_BASE_URL?: string
  /** Auth0 tenant domain, e.g. `dev-xxxx.us.auth0.com`. Required. */
  readonly VITE_AUTH0_DOMAIN?: string
  /** Client id of the `open-skej-web` SPA application. Required. */
  readonly VITE_AUTH0_CLIENT_ID?: string
  /**
   * API identifier the access token is minted for. Required, and must equal the
   * backend's `AUTH0_API_AUDIENCE` — see `src/auth/config.ts` for what happens
   * when it is omitted.
   */
  readonly VITE_AUTH0_AUDIENCE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
