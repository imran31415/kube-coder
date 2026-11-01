/**
 * The config store's backends (#833): the phone keeps its keychain, the
 * browser demo keeps localStorage, and the desktop app sends everything —
 * the token above all — to its core, never to localStorage.
 */
import { describe, expect, it, vi } from 'vitest';
import { desktopBackend, nativeBackend, pickBackend, webBackend } from './storageBackend';

function kv() {
  const m = new Map<string, string>();
  return {
    m,
    getItem: vi.fn(async (k: string) => m.get(k) ?? null),
    setItem: vi.fn(async (k: string, v: string) => void m.set(k, v)),
    removeItem: vi.fn(async (k: string) => void m.delete(k)),
  };
}

function secure() {
  const m = new Map<string, string>();
  return {
    m,
    getItemAsync: vi.fn(async (k: string) => m.get(k) ?? null),
    setItemAsync: vi.fn(async (k: string, v: string) => void m.set(k, v)),
    deleteItemAsync: vi.fn(async (k: string) => void m.delete(k)),
  };
}

describe('nativeBackend (phone)', () => {
  it('keeps secrets in the secure store and the rest in AsyncStorage', async () => {
    const a = kv();
    const s = secure();
    const b = nativeBackend(a, s);
    await b.setSecret('kc.devToken', 'tok');
    await b.setItem('kc.apiBase', 'https://me.example.com');
    expect(s.m.get('kc.devToken')).toBe('tok');
    expect(a.m.has('kc.devToken')).toBe(false);
    expect(a.m.get('kc.apiBase')).toBe('https://me.example.com');
    expect(await b.getSecret('kc.devToken')).toBe('tok');
    await b.deleteSecret('kc.devToken');
    expect(s.m.has('kc.devToken')).toBe(false);
  });
});

describe('webBackend (browser demo)', () => {
  it('falls back to localStorage, as it always has', async () => {
    const a = kv();
    const b = webBackend(a);
    await b.setSecret('kc.devToken', 'tok');
    expect(a.m.get('kc.devToken')).toBe('tok');
    await b.deleteSecret('kc.devToken');
    expect(a.removeItem).toHaveBeenCalledWith('kc.devToken');
  });
});

describe('desktopBackend', () => {
  it('routes secrets to the keychain commands and items to the settings commands', async () => {
    const invoke = vi.fn(async (_cmd: string, _args?: Record<string, unknown>) => null as never);
    const b = desktopBackend(invoke);
    await b.setSecret('kc.devToken', 'tok');
    await b.getSecret('kc.devToken');
    await b.deleteSecret('kc.devToken');
    await b.setItem('kc.apiBase', 'https://me.example.com');
    await b.getItem('kc.apiBase');
    expect(invoke.mock.calls).toEqual([
      ['kc_secret_set', { key: 'kc.devToken', value: 'tok' }],
      ['kc_secret_get', { key: 'kc.devToken' }],
      ['kc_secret_delete', { key: 'kc.devToken' }],
      ['kc_item_set', { key: 'kc.apiBase', value: 'https://me.example.com' }],
      ['kc_item_get', { key: 'kc.apiBase' }],
    ]);
  });

  it('never touches localStorage', async () => {
    const store = new Map<string, string>();
    vi.stubGlobal('localStorage', {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    });
    try {
      const b = desktopBackend(async () => null as never);
      await b.setSecret('kc.devToken', 'tok');
      await b.setItem('kc.apiBase', 'x');
      expect(store.size).toBe(0);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe('pickBackend', () => {
  const make = {
    desktop: () => ({ tag: 'desktop' }) as never,
    web: () => ({ tag: 'web' }) as never,
    native: () => ({ tag: 'native' }) as never,
  };
  it('prefers the desktop core, then web, then native', () => {
    expect(pickBackend({ desktop: true, web: true, make })).toEqual({ tag: 'desktop' });
    expect(pickBackend({ desktop: false, web: true, make })).toEqual({ tag: 'web' });
    expect(pickBackend({ desktop: false, web: false, make })).toEqual({ tag: 'native' });
  });
});
