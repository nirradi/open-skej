import { useEffect, type ReactNode } from 'react'
import { useAuth0 } from '@auth0/auth0-react'

import { setAccessTokenProvider } from '../api'

/**
 * Connects the Auth0 SDK to the api client, then renders its children.
 *
 * This is the one place the two halves of the design in
 * `client.ts#setAccessTokenProvider` meet: React holds the token, the api client
 * wants it, and neither imports the other. The bridge closes over
 * `getAccessTokenSilently` — which only exists inside a render — and hands the
 * api client a plain `() => Promise<string>` that knows nothing about React.
 *
 * ## Why the install happens during render
 *
 * It has to be in place before *any* descendant can fetch, and an effect cannot
 * promise that: effects run child-first, so a `useEffect` here would fire after
 * the effects of everything below it. A child that fetches on mount — which is
 * every data-loading component — would send exactly one anonymous request per
 * page load and get a 401, an intermittent, timing-shaped bug that reads as an
 * expired session. Render, by contrast, runs strictly parent-first, so
 * assigning here means the provider is installed before a child component
 * function has even been called.
 *
 * The alternative that keeps the assignment in an effect is to render nothing
 * until it has run, which costs a render cycle and delays every child's mount
 * by a frame for no gain. This is an idempotent write to a module singleton,
 * not state anything renders from, so repeating it on a double render is a
 * no-op — the usual objection to touching the outside world during render does
 * not bite here.
 *
 * The effect below is still needed for two things render cannot do: uninstall
 * on unmount, and reinstall after StrictMode's development-only
 * mount/unmount/remount cycle, whose cleanup would otherwise leave the provider
 * detached with no re-render coming to put it back.
 *
 * Errors are **not** caught here. A rejected `getAccessTokenSilently` is the
 * ordinary end of a session, and the api client already turns it into an
 * `unauthenticated` outcome; catching it here would mean deciding what to do
 * about it in a component that has no idea what request was in flight.
 */
export function AccessTokenBridge({ children }: { children: ReactNode }) {
  const { getAccessTokenSilently } = useAuth0()

  setAccessTokenProvider(() => getAccessTokenSilently())

  useEffect(() => {
    setAccessTokenProvider(() => getAccessTokenSilently())
    return () => setAccessTokenProvider(null)
  }, [getAccessTokenSilently])

  return <>{children}</>
}
