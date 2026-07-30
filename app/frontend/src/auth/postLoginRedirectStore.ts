/**
 * Where `onRedirectCallback` wants the router to land, once a router exists
 * to ask.
 *
 * ## Why a store, and not a prop or a ref
 *
 * `onRedirectCallback` is a plain function passed to `Auth0Provider`, called
 * by the SDK outside any component's render — there is nowhere in it to call
 * `useNavigate()`, and by the time it runs `BrowserRouter` has already
 * mounted at the callback URL (`/`), so there is no prop path from here to
 * there either. Publishing to a module-level store and letting a
 * router-aware component subscribe is the same shape `client.ts`'s
 * session-lost store already uses for the same reason: a fact discovered
 * outside React that a component below the router needs to react to.
 *
 * `clearPostLoginRedirect` exists so the consumer can discharge the
 * destination once it has navigated — an application, not a token no one
 * ever revokes.
 */
let postLoginRedirect: string | null = null
const postLoginRedirectListeners = new Set<() => void>()

function notify(): void {
  for (const listener of postLoginRedirectListeners) listener()
}

/** Called by `onRedirectCallback` with where Auth0 should have returned the visitor. */
export function setPostLoginRedirect(destination: string): void {
  postLoginRedirect = destination
  notify()
}

/** Called by the router-aware consumer once it has navigated to the stashed destination. */
export function clearPostLoginRedirect(): void {
  if (postLoginRedirect === null) return
  postLoginRedirect = null
  notify()
}

/** `useSyncExternalStore`'s snapshot getter for {@link postLoginRedirect}. */
export function getPostLoginRedirectSnapshot(): string | null {
  return postLoginRedirect
}

/** `useSyncExternalStore`'s subscribe function: registers `listener`, returns the unsubscribe. */
export function subscribePostLoginRedirect(listener: () => void): () => void {
  postLoginRedirectListeners.add(listener)
  return () => postLoginRedirectListeners.delete(listener)
}
