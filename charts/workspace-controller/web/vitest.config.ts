import { defineConfig, mergeConfig } from 'vitest/config';
import viteConfig from './vite.config';

// Mirrors charts/workspace/web so the controller SPA tests behave identically:
// happy-dom, global APIs, and a shared setup that wires @testing-library.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'happy-dom',
      globals: true,
      setupFiles: ['./src/test/setup.ts'],
      css: false,
      include: ['src/**/*.{test,spec}.{ts,tsx}'],
      coverage: {
        provider: 'v8',
        reporter: ['text', 'json', 'html'],
        reportsDirectory: './coverage',
        exclude: [
          '**/*.d.ts',
          '**/*.test.{ts,tsx}',
          '**/test/**',
          '**/vitest.config.ts',
          '**/vite.config.ts',
          'dist/**',
        ],
      },
    },
  }),
);
