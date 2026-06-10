import { defineConfig, mergeConfig } from 'vitest/config';
import viteConfig from './vite.config';

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
        reporter: ['text', 'json', 'html', 'lcov'],
        reportsDirectory: './coverage',
        exclude: [
          '**/*.d.ts',
          '**/*.test.{ts,tsx}',
          '**/*.spec.{ts,tsx}',
          '**/test/**',
          '**/vitest.config.ts',
          '**/vite.config.ts',
          'src/test/**',
          'dist/**',
          'scripts/**'
        ],
        thresholds: {
          lines: 35,
          functions: 35,
          branches: 35,
          statements: 35
        }
      }
    },
  }),
);
