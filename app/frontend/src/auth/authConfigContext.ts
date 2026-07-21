import { createContext, use } from 'react'

import type { Auth0ConfigResult } from './config'

/**
 * Whether Auth0 was configured, made available to the tree below `AuthProvider`.
 *
 * Exists so that a missing configuration degrades *locally* rather than
 * globally. The first version of this replaced the whole app with the config
 * notice, which took Stream 1's calendar down with it — the calendar is
 * unauthenticated and has no business caring whether Auth0 is set up. The
 * blast radius belongs at the point where auth is actually needed, so the
 * status travels down as context and `ProtectedRoute` decides what to do with
 * it.
 *
 * Defaults to `missing` with an empty list: a component reading this outside
 * any `AuthProvider` genuinely has no configuration, and defaulting to `ok`
 * would send it on to call the SDK with nothing.
 */
export const AuthConfigContext = createContext<Auth0ConfigResult>({
  status: 'missing',
  missing: [],
})

/** Reads the Auth0 configuration status of the surrounding tree. */
export function useAuthConfig(): Auth0ConfigResult {
  return use(AuthConfigContext)
}
