import { describe, it, expect, afterEach } from 'vitest';
import {
  validateHost,
  normalizeBaseUrl,
  isLoopbackHostname,
  isCleartextAllowed,
  isDowngrade,
  sameOrigin,
  shouldForwardAuthOnRedirect,
} from './urlPolicy';

// Cleartext allowed / not allowed, passed explicitly so tests don't depend on
// the ambient EXPO_PUBLIC_ALLOW_CLEARTEXT env.
const DEV = { allowCleartext: true };
const PROD = { allowCleartext: false };

describe('validateHost — workspace, production (cleartext OFF)', () => {
  it('rejects an arbitrary HTTP LAN host', () => {
    const r = validateHost('http://192.168.1.50:6080', 'workspace', PROD);
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/https/i);
    expect(r.url).toBeUndefined();
  });

  it('rejects an arbitrary HTTP internet host', () => {
    const r = validateHost('http://example.com', 'workspace', PROD);
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/https/i);
  });

  it('rejects even loopback HTTP when cleartext is off', () => {
    const r = validateHost('http://localhost:6080', 'workspace', PROD);
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/cleartext|development/i);
  });

  it('accepts an HTTPS host', () => {
    const r = validateHost('https://you.kube-coder.example.com', 'workspace', PROD);
    expect(r.ok).toBe(true);
    expect(r.url).toBe('https://you.kube-coder.example.com');
  });
});

describe('validateHost — controller (admin token) NEVER over HTTP', () => {
  it('rejects HTTP loopback controller even in dev', () => {
    const r = validateHost('http://localhost:9000', 'controller', DEV);
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/admin token/i);
  });

  it('rejects HTTP LAN controller', () => {
    const r = validateHost('http://10.0.0.4', 'controller', DEV);
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/admin token/i);
  });

  it('rejects HTTP internet controller', () => {
    const r = validateHost('http://controller.example.com', 'controller', PROD);
    expect(r.ok).toBe(false);
  });

  it('accepts an HTTPS controller', () => {
    const r = validateHost('https://controller.kube-coder.example.com', 'controller', PROD);
    expect(r.ok).toBe(true);
    expect(r.url).toBe('https://controller.kube-coder.example.com');
  });
});

describe('validateHost — workspace loopback in dev (cleartext ON)', () => {
  it.each([
    'http://localhost:6080',
    'http://127.0.0.1:6080',
    'http://127.0.0.1',
    'http://[::1]:6080',
  ])('accepts %s', (host) => {
    const r = validateHost(host, 'workspace', DEV);
    expect(r.ok).toBe(true);
    expect(r.url).toBe(normalizeBaseUrl(host));
  });

  it('still rejects a non-loopback HTTP host even in dev', () => {
    const r = validateHost('http://192.168.0.10', 'workspace', DEV);
    expect(r.ok).toBe(false);
  });
});

describe('validateHost — malformed / edge input', () => {
  it('rejects empty input with a reason', () => {
    const r = validateHost('   ', 'workspace', DEV);
    expect(r.ok).toBe(false);
    expect(typeof r.reason).toBe('string');
    expect(r.reason!.length).toBeGreaterThan(0);
  });

  it('rejects a bare host with no scheme', () => {
    const r = validateHost('localhost:6080', 'workspace', DEV);
    expect(r.ok).toBe(false);
  });

  it('rejects a non-http(s) scheme', () => {
    const r = validateHost('ftp://example.com', 'workspace', DEV);
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/scheme/i);
  });

  it('every rejection carries a user-facing message', () => {
    const cases: Array<[string, 'workspace' | 'controller']> = [
      ['http://example.com', 'workspace'],
      ['http://localhost', 'controller'],
      ['', 'workspace'],
      ['nonsense', 'controller'],
    ];
    for (const [host, kind] of cases) {
      const r = validateHost(host, kind, PROD);
      expect(r.ok).toBe(false);
      expect(r.reason && r.reason.trim().length).toBeTruthy();
    }
  });
});

describe('isLoopbackHostname', () => {
  it.each(['localhost', '127.0.0.1', '127.5.6.7', '::1'])('true for %s', (h) => {
    expect(isLoopbackHostname(h)).toBe(true);
  });
  it.each(['192.168.1.1', '10.0.0.1', 'example.com', 'kube.local', '169.254.1.1'])(
    'false for %s',
    (h) => {
      expect(isLoopbackHostname(h)).toBe(false);
    },
  );
});

describe('normalizeBaseUrl', () => {
  it('strips trailing slashes and trims', () => {
    expect(normalizeBaseUrl('  https://x.com///  ')).toBe('https://x.com');
    expect(normalizeBaseUrl('https://x.com')).toBe('https://x.com');
  });
});

describe('isCleartextAllowed reads the env gate', () => {
  const prev = process.env.EXPO_PUBLIC_ALLOW_CLEARTEXT;
  afterEach(() => {
    if (prev === undefined) delete process.env.EXPO_PUBLIC_ALLOW_CLEARTEXT;
    else process.env.EXPO_PUBLIC_ALLOW_CLEARTEXT = prev;
  });
  it('true for 1/true/yes, false otherwise', () => {
    process.env.EXPO_PUBLIC_ALLOW_CLEARTEXT = '1';
    expect(isCleartextAllowed()).toBe(true);
    process.env.EXPO_PUBLIC_ALLOW_CLEARTEXT = 'true';
    expect(isCleartextAllowed()).toBe(true);
    delete process.env.EXPO_PUBLIC_ALLOW_CLEARTEXT;
    expect(isCleartextAllowed()).toBe(false);
    process.env.EXPO_PUBLIC_ALLOW_CLEARTEXT = '0';
    expect(isCleartextAllowed()).toBe(false);
  });
});

describe('redirect / downgrade safety', () => {
  it('flags an HTTPS→HTTP downgrade', () => {
    expect(isDowngrade('https://x.com/a', 'http://x.com/a')).toBe(true);
  });
  it('does not flag HTTPS→HTTPS', () => {
    expect(isDowngrade('https://x.com/a', 'https://x.com/b')).toBe(false);
  });
  it('fails closed on unparseable URLs', () => {
    expect(isDowngrade('https://x.com', 'garbage')).toBe(true);
  });

  it('sameOrigin compares scheme+host+port (default ports)', () => {
    expect(sameOrigin('https://x.com', 'https://x.com/path')).toBe(true);
    expect(sameOrigin('https://x.com:443', 'https://x.com')).toBe(true);
    expect(sameOrigin('https://x.com', 'https://y.com')).toBe(false);
    expect(sameOrigin('https://x.com', 'http://x.com')).toBe(false);
    expect(sameOrigin('https://x.com:8443', 'https://x.com')).toBe(false);
  });

  it('does NOT forward the Authorization header cross-origin', () => {
    expect(shouldForwardAuthOnRedirect('https://x.com/a', 'https://evil.com/a')).toBe(false);
  });
  it('does NOT forward the header on an HTTPS→HTTP downgrade (same host)', () => {
    expect(shouldForwardAuthOnRedirect('https://x.com/a', 'http://x.com/a')).toBe(false);
  });
  it('forwards the header only on a same-origin, non-downgrading hop', () => {
    expect(shouldForwardAuthOnRedirect('https://x.com/a', 'https://x.com/b')).toBe(true);
  });
});
