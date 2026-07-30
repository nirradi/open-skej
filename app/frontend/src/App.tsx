import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { AdminPage } from './admin'
import { AccountPage, PostLoginRedirect, ProtectedRoute } from './auth'
import { assertConfigIsCoherent } from './config'
import { ResourceCalendarPage, SpaceListPage, SpacePage } from './space'

// Fail at boot rather than rendering a subtly wrong grid.
assertConfigIsCoherent()

/**
 * The application shell: providers are above us, routes are here.
 *
 * The router lives inside `App` rather than in `main.tsx` alongside the Auth0
 * provider so that rendering `<App />` in a test still produces a routable tree
 * — `App.test.tsx` does exactly that, and it must keep working (with a
 * session provided explicitly, since nothing above `<App />` installs one in
 * that test).
 *
 * `/` is the front door: login is what an unauthenticated visitor sees there,
 * rendered in place by `ProtectedRoute` rather than a redirect, and the
 * destination once signed in is `SpaceListPage` — the user's Spaces, not a
 * generic calendar. The calendar exists only per-Resource now, at
 * `/s/{public_id}/resources/{resource_id}`: a member lands on their Space at
 * `/s/{public_id}`, picks a Resource, and reaches its calendar there. There is
 * no unscoped, single-user calendar left — the endpoints behind Stream 1's
 * `/calendar` were deleted along with their call sites.
 */
function App() {
  return (
    <BrowserRouter>
      {/*
        Applies the destination `AuthProvider`'s `onRedirectCallback` stashed
        for the deep link that sent the user to Auth0 in the first place.
        Must be inside `BrowserRouter` (it calls `useNavigate()`) and does
        not itself render a route — see `PostLoginRedirect` for why this has
        to be a subscriber rather than a one-time read.
      */}
      <PostLoginRedirect />
      <Routes>
        <Route
          path="/"
          element={
            <ProtectedRoute>
              <SpaceListPage />
            </ProtectedRoute>
          }
        />
        {/*
          Only a member ever holds a Resource id — it comes from
          `listResources` on the Space page a member lands on — so
          `ProtectedRoute` is the right guard here too: the mode/session/
          unconfigured handling is identical to every other authed route, and
          `require_space_role` behind every request this page makes is the
          real boundary regardless.
        */}
        <Route
          path="/s/:publicId/resources/:resourceId"
          element={
            <ProtectedRoute>
              <ResourceCalendarPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/account"
          element={
            <ProtectedRoute>
              <AccountPage />
            </ProtectedRoute>
          }
        />
        {/*
          `/admin` sits behind the same guard as the routes above, and for the
          same reason: `AdminPage` opens by calling `GET /spaces`, which is
          authenticated, so an unguarded render would just fill the screen with
          401s. The guard is convenience, not access control — `require_space_role`
          re-checks every call behind this page.

          `ProtectedRoute` also absorbs the unconfigured case, rendering the
          notice without ever reading a session that does not exist. That
          matters here because with neither Auth0 nor sandbox mode configured
          there is no session provider in the tree at all, and `AdminPage` is
          reachable only through this element.
        */}
        <Route
          path="/admin"
          element={
            <ProtectedRoute>
              <AdminPage />
            </ProtectedRoute>
          }
        />
        {/*
          `/s/{public_id}` is deliberately **not** wrapped in `ProtectedRoute`,
          and it is the only authenticated-ish route that isn't.

          It is the outside of the door: the link is the capability, handing it
          out is the whole distribution model, and whoever opens it may have no
          account at all. `ProtectedRoute` renders "You need an account to see
          this page", which is true of a members-only screen and wrong here —
          this screen's job is to explain what the link is and offer the way in.
          `SpacePage` therefore runs the mode check and the session check
          itself, with its own copy, and returns the visitor to this exact URL
          after login — through both login and signup, since a cold guest is
          just as often a brand-new identity as a returning one.
        */}
        <Route path="/s/:publicId" element={<SpacePage />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App
