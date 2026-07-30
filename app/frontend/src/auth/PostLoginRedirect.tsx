import { useEffect, useSyncExternalStore } from 'react'
import { useNavigate } from 'react-router-dom'

import {
  clearPostLoginRedirect,
  getPostLoginRedirectSnapshot,
  subscribePostLoginRedirect,
} from './postLoginRedirectStore'

/**
 * Applies the destination `onRedirectCallback` stashed, once the router
 * exists to apply it to.
 *
 * Renders nothing; it exists only for the `useNavigate()` call, which
 * requires being inside `<BrowserRouter>` — `onRedirectCallback` itself
 * cannot do this, because it runs outside the whole React tree. Must
 * **subscribe**, not just read once on mount: the callback fires *after*
 * this component (and the router) have already mounted at `/`, so a
 * one-time read would see nothing and the deep link would still land on the
 * front page.
 *
 * One `navigate(..., { replace: true })` discharges both of `replaceState`'s
 * old duties: it lands the visitor on `returnTo`, and — because navigating
 * replaces the whole URL, search included — it also strips Auth0's
 * `?code=&state=`, which must not survive: it is single-use, it breaks a
 * refresh, and it is the sort of thing that ends up pasted into a bug
 * report. `replace: true` so the back button does not walk into that spent
 * authorization code.
 */
export function PostLoginRedirect() {
  const navigate = useNavigate()
  const destination = useSyncExternalStore(
    subscribePostLoginRedirect,
    getPostLoginRedirectSnapshot,
  )

  useEffect(() => {
    if (destination === null) return
    navigate(destination, { replace: true })
    clearPostLoginRedirect()
  }, [destination, navigate])

  return null
}
