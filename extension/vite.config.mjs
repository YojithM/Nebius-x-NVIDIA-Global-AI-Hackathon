import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Chrome extension build.
//
// Two MV3-specific settings matter here:
//   modulePreload.polyfill: false — Vite's default injects an inline <script>
//     into the HTML, and MV3's CSP blocks inline scripts. Without this the
//     side panel loads with a CSP error and a blank page.
//   outDir: dist — this is what you point "Load unpacked" at.
//
// Note there is no HMR: MV3 blocks the inline scripts Vite's dev server needs.
// Use `npm run watch` and hit the reload button in chrome://extensions instead.
export default defineConfig({
  plugins: [react()],
  publicDir: 'public',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'chrome116',
    modulePreload: { polyfill: false },
    rollupOptions: {
      input: { sidepanel: 'sidepanel.html' },
      output: {
        entryFileNames: 'assets/[name].js',
        chunkFileNames: 'assets/[name].js',
        assetFileNames: 'assets/[name][extname]',
      },
    },
  },
})
