import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  test: {
    // The units under test are pure TypeScript — an API client over a mocked
    // `fetch` and calendar arithmetic — so there is nothing to gain from a DOM.
    // Component tests arriving with task 1.6 will need `environment: 'jsdom'`.
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
