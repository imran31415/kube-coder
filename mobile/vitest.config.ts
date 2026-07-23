import { defineConfig } from 'vitest/config';

// The mobile app has no other JS test runner; these tests cover the pure
// transport/URL security policy (src/util/urlPolicy.ts) and the Expo build
// config posture (app.config.ts) — no React Native runtime is needed, so a
// plain node environment keeps the setup minimal. Mirrors the vitest usage in
// the repo's two web SPAs (charts/*/web).
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.{test,spec}.ts', 'app.config.test.ts'],
  },
});
