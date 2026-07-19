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
  },
})
