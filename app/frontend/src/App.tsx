import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { AdminPage, SpaceRulesPage } from './admin'
import { AccountPage, PostLoginRedirect, ProtectedRoute } from './auth'
import { ResourceCalendarRoute, SpaceListPage, SpacePage } from './space'

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
          Not wrapped in `ProtectedRoute`. A Resource link is forwarded
          exactly like a Space link — the person opening it may hold no
          membership and no account, and `ProtectedRoute`'s "You need an
          account to see this page" is wrong for them, same as it is wrong on
          `/s/:publicId` below. `ResourceCalendarRoute` mounts the same
          `SpaceAccessGate` that route uses, with its own `returnTo` and its
          own children — the calendar, once the caller turns out to be a
          member. Admission is decided at the Space either way: a Resource
          carries no capability of its own.
        */}
        <Route path="/s/:publicId/resources/:resourceId" element={<ResourceCalendarRoute />} />
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
          `/s/{public_id}/rules` — an authenticated admin screen reached by
          navigating from `/admin`, not a cold-link door: unlike
          `/s/:publicId` and `/s/:publicId/resources/:resourceId` below, a
          stranger with no membership has no legitimate reason to land here,
          so this sits behind `ProtectedRoute` rather than `SpaceAccessGate`.
          `SpaceRulesPage` itself still renders its own notice for a member
          who is signed in but not an admin — the guard here only rules out
          "signed out entirely".
        */}
        <Route
          path="/s/:publicId/rules"
          element={
            <ProtectedRoute>
              <SpaceRulesPage />
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
