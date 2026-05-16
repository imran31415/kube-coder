import { defineConfig } from 'vite';
import preact from '@preact/preset-vite';

// The built SPA is served by server.py from /opt/dashboard-dist/ (image-baked)
// at the URL prefix /next/ during the migration. `base` makes asset URLs match.
//
// During `yarn dev`, Vite serves on port 5173 and proxies API/health/metrics
// to the running server.py on 6080. Use `dev` for local iteration only —
// production traffic always goes through server.py + the OAuth2 ingress.
export default defineConfig({
  plugins: [preact()],
  base: '/next/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    target: 'es2022',
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        manualChunks: undefined,
      },
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': 'http://localhost:6080',
      '/health': 'http://localhost:6080',
      '/metrics': 'http://localhost:6080',
      '/oauth': 'http://localhost:6080',
    },
  },
});
