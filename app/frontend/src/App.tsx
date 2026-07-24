import { useCallback, useState } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { AdminPage } from './admin'
import type { Booking } from './api'
import { AccountPage, ProtectedRoute } from './auth'
import { BookingPanel, CancelPanel } from './booking'
import { CalendarGrid, type SelectedInterval } from './calendar'
import { assertConfigIsCoherent, calendarConfig, slotsPerDay } from './config'
import { SpaceListPage, SpacePage } from './space'

// Fail at boot rather than rendering a subtly wrong grid.
assertConfigIsCoherent()

/**
 * The calendar screen.
 *
 * Owns the state the grid and the two panels share: the selected free range,
 * the selected existing booking, and a token telling the grid to refetch. The
 * grid reports both selections upward and the panels report changes back down,
 * so none of the three has to know about the others.
 *
 * There is **one** refresh token, raised by whichever panel changed something.
 * A booking and a cancellation are the same event as far as the grid is
 * concerned — the week on screen no longer matches the server — and a second
 * mechanism for the second panel would be two ways to say one thing.
 */
function CalendarPage() {
  const [selection, setSelection] = useState<SelectedInterval | null>(null)
  const [selectedBooking, setSelectedBooking] = useState<Booking | null>(null)
  const [refreshToken, setRefreshToken] = useState(0)

  // Memoised deliberately: the grid notifies from an effect that depends on
  // this callback, so a fresh identity each render would re-fire it endlessly.
  const handleSelectionChange = useCallback((interval: SelectedInterval | null) => {
    setSelection(interval)
  }, [])

  const handleBookingSelect = useCallback((booking: Booking | null) => {
    setSelectedBooking(booking)
  }, [])

  const handleCalendarChanged = useCallback(() => {
    setRefreshToken((token) => token + 1)
  }, [])

  return (
    <main className="min-h-screen bg-slate-50 p-8 text-slate-800">
      <h1 className="text-2xl font-semibold text-slate-900">Open-Skej</h1>
      <p className="mt-2 mb-6 text-sm text-slate-600">
        Booking {slotsPerDay} slots of {calendarConfig.slotMinutes} minutes, from{' '}
        {String(calendarConfig.openHour).padStart(2, '0')}:00 to{' '}
        {String(calendarConfig.closeHour).padStart(2, '0')}:00. Times are shown in your local
        timezone.
      </p>

      <div className="flex flex-col gap-6 lg:flex-row lg:items-start">
        <div className="min-w-0 flex-1">
          <CalendarGrid
            onSelectionChange={handleSelectionChange}
            onBookingSelect={handleBookingSelect}
            refreshToken={refreshToken}
          />
        </div>
        <div className="flex flex-col gap-4 lg:w-80 lg:shrink-0">
          <CancelPanel booking={selectedBooking} onCalendarChanged={handleCalendarChanged} />
          <BookingPanel selection={selection} onCalendarChanged={handleCalendarChanged} />
        </div>
      </div>
    </main>
  )
}

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
 * generic calendar. The single-Resource calendar Stream 1 built still works
 * exactly as it did; it moved to its own route, `/calendar`, still behind the
 * same guard, since the booking endpoints behind it (`/bookings`) are still
 * the unscoped, single-user Stream 1 contract that a later task retires.
 */
function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route
          path="/"
          element={
            <ProtectedRoute>
              <SpaceListPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/calendar"
          element={
            <ProtectedRoute>
              <CalendarPage />
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
