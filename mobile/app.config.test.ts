import { describe, it, expect, afterEach } from 'vitest';
import { buildConfig, allowCleartext } from './app.config';

// Evaluate the Expo config function directly under each build env and assert the
// transport posture. buildConfig() reads process.env at call time, so we flip
// EXPO_PUBLIC_ALLOW_CLEARTEXT per case (production leaves it unset).

const prev = process.env.EXPO_PUBLIC_ALLOW_CLEARTEXT;
afterEach(() => {
  if (prev === undefined) delete process.env.EXPO_PUBLIC_ALLOW_CLEARTEXT;
  else process.env.EXPO_PUBLIC_ALLOW_CLEARTEXT = prev;
});

function ios(config: ReturnType<typeof buildConfig>) {
  return (config.ios?.infoPlist?.NSAppTransportSecurity ?? {}) as Record<string, unknown>;
}

function androidCleartext(config: ReturnType<typeof buildConfig>): unknown {
  const plugins = config.plugins ?? [];
  for (const p of plugins) {
    if (Array.isArray(p) && p[0] === 'expo-build-properties') {
      const opts = p[1] as { android?: { usesCleartextTraffic?: unknown } };
      return opts?.android?.usesCleartextTraffic;
    }
  }
  return undefined;
}

describe('app.config transport posture — production (env unset)', () => {
  it('does not allow arbitrary iOS loads', () => {
    delete process.env.EXPO_PUBLIC_ALLOW_CLEARTEXT;
    expect(allowCleartext()).toBe(false);
    const cfg = buildConfig();
    expect(ios(cfg).NSAllowsArbitraryLoads).not.toBe(true);
    expect(ios(cfg).NSAllowsArbitraryLoads).toBe(false);
  });

  it('does not enable Android cleartext traffic', () => {
    delete process.env.EXPO_PUBLIC_ALLOW_CLEARTEXT;
    expect(androidCleartext(buildConfig())).not.toBe(true);
    expect(androidCleartext(buildConfig())).toBe(false);
  });

  it('scopes the only iOS HTTP exception to localhost via NSExceptionDomains', () => {
    delete process.env.EXPO_PUBLIC_ALLOW_CLEARTEXT;
    const ats = ios(buildConfig());
    const domains = ats.NSExceptionDomains as Record<string, unknown> | undefined;
    expect(domains).toBeDefined();
    expect(Object.keys(domains ?? {})).toEqual(['localhost']);
  });
});

describe('app.config transport posture — development/preview (env=1)', () => {
  it('allows arbitrary iOS loads', () => {
    process.env.EXPO_PUBLIC_ALLOW_CLEARTEXT = '1';
    expect(allowCleartext()).toBe(true);
    expect(ios(buildConfig()).NSAllowsArbitraryLoads).toBe(true);
  });

  it('enables Android cleartext traffic', () => {
    process.env.EXPO_PUBLIC_ALLOW_CLEARTEXT = '1';
    expect(androidCleartext(buildConfig())).toBe(true);
  });
});
