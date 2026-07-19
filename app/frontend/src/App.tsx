import { assertConfigIsCoherent, calendarConfig, slotsPerDay } from './config'

// Fail at boot rather than rendering a subtly wrong grid later (task 1.6).
assertConfigIsCoherent()

/**
 * Placeholder shell for the calendar.
 *
 * Task 1.5 delivers the foundation only — Tailwind, `config.ts` and the API
 * client. The grid lands in 1.6 and the booking flow in 1.7, so this renders
 * just enough to prove Tailwind compiles and the config module is wired in.
 */
function App() {
  return (
    <main className="min-h-screen bg-slate-50 p-8 text-slate-800">
      <h1 className="text-2xl font-semibold text-slate-900">Open-Skej</h1>
      <p className="mt-2 text-sm text-slate-600">
        Booking {slotsPerDay} slots of {calendarConfig.slotMinutes} minutes, from{' '}
        {String(calendarConfig.openHour).padStart(2, '0')}:00 to{' '}
        {String(calendarConfig.closeHour).padStart(2, '0')}:00.
      </p>
    </main>
  )
}

export default App
