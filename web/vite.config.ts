import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// The Hearthkeep server. It only ever listens on the loopback interface.
const SERVER = 'http://127.0.0.1:8765'
// The server rejects state-changing requests whose Origin is not its public origin
// (default http://localhost:8765), so the dev proxy presents that origin.
const SERVER_ORIGIN = process.env.HEARTHKEEP_ORIGIN ?? 'http://localhost:8765'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: SERVER,
        configure: (proxy) => {
          proxy.on('proxyReq', (req) => {
            if (req.getHeader('origin')) req.setHeader('origin', SERVER_ORIGIN)
          })
        },
      },
    },
  },
  build: {
    // The server's CSP has no data: in font-src/script-src: every asset ships as a file.
    assetsInlineLimit: 0,
    // The chart runtime (Vega) is one large chunk, loaded only when a chart is shown.
    chunkSizeWarningLimit: 900,
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    restoreMocks: true,
  },
})
