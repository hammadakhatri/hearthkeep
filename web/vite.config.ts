import react from '@vitejs/plugin-react'
import type { Plugin } from 'vite'
import { defineConfig } from 'vitest/config'

/** Size budgets per chunk (minified bytes): every chunk 500 kB, the lazy Vega chunk 1 MB. */
const DEFAULT_BUDGET = 500_000
const CHUNK_BUDGETS: Record<string, number> = { vegaRuntime: 1_000_000 }

/** Fails the build when a chunk is over its budget, so the allowance stays with Vega alone. */
function chunkBudgets(): Plugin {
  return {
    name: 'hearthkeep-chunk-budgets',
    apply: 'build',
    generateBundle(_options, bundle) {
      const over: string[] = []
      for (const out of Object.values(bundle)) {
        if (out.type !== 'chunk') continue
        const budget = CHUNK_BUDGETS[out.name] ?? DEFAULT_BUDGET
        const size = new TextEncoder().encode(out.code).length
        if (size > budget) over.push(`${out.fileName}: ${size} bytes (budget ${budget})`)
      }
      if (over.length) this.error(`Chunks over their size budget: ${over.join('; ')}`)
    },
  }
}

// The Hearthkeep server. It only ever listens on the loopback interface.
const SERVER = 'http://127.0.0.1:8765'
// The server rejects state-changing requests whose Origin is not its public origin
// (default http://localhost:8765), so the dev proxy presents that origin.
const SERVER_ORIGIN = process.env.HEARTHKEEP_ORIGIN ?? 'http://localhost:8765'

export default defineConfig({
  plugins: [react(), chunkBudgets()],
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
    // Chunk sizes are checked per chunk by chunkBudgets() above (it fails the build), so
    // Vite's single, all-chunks warning is not used.
    chunkSizeWarningLimit: Number.POSITIVE_INFINITY,
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    restoreMocks: true,
  },
})
