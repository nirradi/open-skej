import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  test: {
    // Most units under test are pure TypeScript — an API client over a mocked
    // `fetch`, calendar arithmetic, selection ranges — so they get no DOM and
    // pay nothing for one. The handful of component tests opt in per file with
    // a `// @vitest-environment jsdom` docblock. That is preferred over an
    // `environment` split by glob because the opt-in is visible in the file
    // that needs it rather than in a config a reader has to go find.
    environment: 'node',
    include: ['src/**/*.test.{ts,tsx}'],
    // Pins the process the whole run executes in to UTC, the same reason
    // `app/e2e/playwright.config.ts` pins the browser it drives to UTC: a
    // fixture with an explicit `timezone` (a seeded Space, a mocked one)
    // resolves through the *Space's* zone now, never the environment's — see
    // `DEFERRED.md` item 19 — but the environment's own zone still seeds
    // `timezone.ts`'s `SYSTEM_TIME_ZONE`, the bootstrapping placeholder used
    // before any Space's real zone is known. Leaving that to whatever zone
    // happens to be local would make this suite's outcome depend on which
    // machine runs it — deterministic in CI (`ubuntu-latest` defaults to
    // UTC) and not on every developer's own machine otherwise. Pinning here
    // is what keeps the two the same on purpose, not by accident.
    env: { TZ: 'UTC' },
  },
})
