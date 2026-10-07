import { defineConfig } from 'vite';
import preact from '@preact/preset-vite';
// Isolated component harness; does not crawl the full dashboard dependency tree.
export default defineConfig({
  plugins: [preact()], base: '/next/',
  optimizeDeps: { entries: ['scripts/publish-harness.html'], esbuildOptions: { target: 'esnext' } },
  server: { host: '127.0.0.1', port: 5173, strictPort: true },
});
