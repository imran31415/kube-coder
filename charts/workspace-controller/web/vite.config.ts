import { defineConfig } from 'vite';
import preact from '@preact/preset-vite';
import { viteSingleFile } from 'vite-plugin-singlefile';

// Builds to a single self-contained dist/index.html (JS + CSS inlined) so the
// whole SPA ships as one ConfigMap key — no hashed assets, no subdirs, well
// under the 1 MiB ConfigMap limit. base '/' because the controller serves it
// at its own host root.
//
// `yarn dev` serves on 5174 and proxies API/auth to controller.py running
// locally on 8080. Set localStorage['kc.devToken'] to the value you launch
// controller.py with (CONTROLLER_DEV_TOKEN=...) so the API authorizes.
export default defineConfig({
  plugins: [preact(), viteSingleFile()],
  base: '/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2022',
    cssCodeSplit: false,
    assetsInlineLimit: 100000000,
    rollupOptions: {
      output: { inlineDynamicImports: true },
    },
  },
  server: {
    port: 5174,
    strictPort: true,
    proxy: {
      '/api': 'http://localhost:8080',
      '/oauth': 'http://localhost:8080',
      '/health': 'http://localhost:8080',
    },
  },
});
