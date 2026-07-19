import { useCallback, useState } from 'react'

import { BookingPanel } from './booking'
import { CalendarGrid, type SelectedInterval } from './calendar'
import { assertConfigIsCoherent, calendarConfig, slotsPerDay } from './config'

// Fail at boot rather than rendering a subtly wrong grid.
assertConfigIsCoherent()

/**
 * The application shell.
 *
 * Owns the two pieces of state the grid and the booking panel share: the
 * selected range, and a token telling the grid to refetch. The grid reports
 * selections upward and the panel reports changes back down, so neither has to
 * know about the other.
 */
function App() {
  const [selection, setSelection] = useState<SelectedInterval | null>(null)
  const [refreshToken, setRefreshToken] = useState(0)

  // Memoised deliberately: the grid notifies from an effect that depends on
  // this callback, so a fresh identity each render would re-fire it endlessly.
  const handleSelectionChange = useCallback((interval: SelectedInterval | null) => {
    setSelection(interval)
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
          <CalendarGrid onSelectionChange={handleSelectionChange} refreshToken={refreshToken} />
        </div>
        <div className="lg:w-80 lg:shrink-0">
          <BookingPanel selection={selection} onCalendarChanged={handleCalendarChanged} />
        </div>
      </div>
    </main>
  )
}

export default App
