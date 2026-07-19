import { CalendarGrid } from './calendar'
import { assertConfigIsCoherent, calendarConfig, slotsPerDay } from './config'

// Fail at boot rather than rendering a subtly wrong grid.
assertConfigIsCoherent()

/**
 * The application shell.
 *
 * Task 1.6 delivers the grid and its selection behaviour only. The selected
 * range is reported by `onSelectionChange`, which task 1.7 will wire to a
 * confirm action calling `POST /bookings`.
 */
function App() {
  return (
    <main className="min-h-screen bg-slate-50 p-8 text-slate-800">
      <h1 className="text-2xl font-semibold text-slate-900">Open-Skej</h1>
      <p className="mt-2 mb-6 text-sm text-slate-600">
        Booking {slotsPerDay} slots of {calendarConfig.slotMinutes} minutes, from{' '}
        {String(calendarConfig.openHour).padStart(2, '0')}:00 to{' '}
        {String(calendarConfig.closeHour).padStart(2, '0')}:00. Times are shown in your local
        timezone.
      </p>
      <CalendarGrid />
    </main>
  )
}

export default App
