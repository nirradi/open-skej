import { createContext, use } from 'react'

/**
 * The mode-agnostic shape every screen in the app reads to decide what to
 * show, regardless of whether the running build is wired to the real Auth0
 * tenant or to sandbox mode.
 *
 * `ProtectedRoute`, `SpacePage` and `LoginControls` used to call `useAuth0()`
 * directly, which **throws** with no `Auth0Provider` in the tree — exactly
 * the shape of the tree in sandbox mode. This context is what lets all three
 * stop caring which mode installed it: `Auth0SessionProvider` adapts
 * `useAuth0`, `SandboxAuthProvider` adapts its own in-React session state, and
 * every consumer below either one sees the same three-state shape.
 *
 * `login`/`logout` fire-and-forget rather than returning a promise a caller
 * would await: Auth0's path is a redirect (nothing to await — the page is
 * about to navigate away), and sandbox's is synchronous. A shared signature
 * that fit only one of them would leak which mode is running into every call
 * site.
 */
export type SessionStatus = 'loading' | 'authenticated' | 'unauthenticated'

/**
 * `connection` and `screenHint` only mean anything to the Auth0
 * implementation — `google-oauth2` picks the social connection Universal
 * Login would otherwise show as a second button for, and `screenHint:
 * 'signup'` sends Universal Login straight to its signup tab. The sandbox
 * implementation ignores both: there is no hosted login screen to steer, and
 * "signing up" and "logging in" are the same action — committing the
 * identity already selected via `localStorage` — for a seeded identity that
 * may or may not have a `users` row yet (the backend provisions one on first
 * sight either way).
 */
export interface SessionLoginOptions {
  returnTo?: string
  connection?: string
  screenHint?: 'signup'
}

export interface Session {
  status: SessionStatus
  login: (options?: SessionLoginOptions) => void
  logout: () => void
}

/**
 * The default for a component that somehow renders with no session provider
 * above it. `loading` rather than `unauthenticated` or `authenticated`: both
 * of those are answers, and there genuinely is none here — the safe reading
 * of "no provider is mounted" is "the answer isn't known", not a guess in
 * either direction. In production `AuthProvider` always installs a real
 * provider before `App` renders, so this default is only ever observed by a
 * test that renders a component in isolation.
 */
const DEFAULT_SESSION: Session = {
  status: 'loading',
  login: () => {},
  logout: () => {},
}

export const SessionContext = createContext<Session>(DEFAULT_SESSION)

/** Reads the ambient session — who is signed in, and how to change that. */
export function useSession(): Session {
  return use(SessionContext)
}
