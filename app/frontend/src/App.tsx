import { useCallback, useState } from 'react'

import type { Booking } from './api'
import { BookingPanel, CancelPanel } from './booking'
import { CalendarGrid, type SelectedInterval } from './calendar'
import { assertConfigIsCoherent, calendarConfig, slotsPerDay } from './config'

// Fail at boot rather than rendering a subtly wrong grid.
assertConfigIsCoherent()

/**
 * The application shell.
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
function App() {
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

export default App
