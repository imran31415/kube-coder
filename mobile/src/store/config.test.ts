/**
 * The Notifications switch's saved value (#685).
 *
 * It is a preference of the phone, not of the connection: it must survive a
 * restart and a disconnect, and it must flip in memory before the storage
 * write settles, because a registration already in flight checks it.
 *
 * ./storage wraps AsyncStorage / SecureStore, so it is replaced with a map.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const store = new Map<string, string>();
let holdWrites = false;
const heldWrites: Array<() => void> = [];

vi.mock('./storage', () => ({
  getItem: async (k: string) => store.get(k) ?? null,
  setItem: (k: string, v: string) => {
    if (!holdWrites) {
      store.set(k, v);
      return Promise.resolve();
    }
    return new Promise<void>((resolve) => {
      heldWrites.push(() => {
        store.set(k, v);
        resolve();
      });
    });
  },
  getSecret: async (k: string) => store.get(k) ?? null,
  setSecret: async (k: string, v: string) => {
    store.set(k, v);
  },
  deleteSecret: async (k: string) => {
    store.delete(k);
  },
}));

/** A fresh module instance: what the next app launch would see. */
async function launch() {
  vi.resetModules();
  return import('./config');
}

describe('config — pushEnabled', () => {
  beforeEach(() => {
    store.clear();
    holdWrites = false;
    heldWrites.length = 0;
  });

  it('is on before hydration and when never set', async () => {
    const config = await launch();
    expect(config.getConfig().pushEnabled).toBe(true);
    await config.hydrate();
    expect(config.getConfig().pushEnabled).toBe(true);
  });

  it('saves off as "0", and the next launch reads it back', async () => {
    const config = await launch();
    await config.hydrate();
    await config.setPushEnabled(false);
    expect(store.get('kc.pushEnabled')).toBe('0');

    const next = await launch();
    await next.hydrate();
    expect(next.getConfig().pushEnabled).toBe(false);
    await next.setPushEnabled(true);
    expect(store.get('kc.pushEnabled')).toBe('1');
  });

  it('flips in memory, and tells subscribers, before the write settles', async () => {
    const config = await launch();
    await config.hydrate();
    const seen: boolean[] = [];
    config.subscribe((c) => seen.push(c.pushEnabled));

    holdWrites = true;
    const saving = config.setPushEnabled(false);
    expect(config.getConfig().pushEnabled).toBe(false);
    expect(seen).toEqual([false]);
    expect(store.has('kc.pushEnabled')).toBe(false);

    heldWrites.forEach((write) => write());
    await saving;
    expect(store.get('kc.pushEnabled')).toBe('0');
  });

  it('survives a disconnect — it belongs to the phone, not the connection', async () => {
    const config = await launch();
    await config.hydrate();
    await config.saveConnection('https://ws.example', 'api-token');
    await config.setPushEnabled(false);
    await config.clearConnection();
    expect(config.getConfig().pushEnabled).toBe(false);
    expect(store.get('kc.pushEnabled')).toBe('0');
  });

  it('a pre-seeded build, which skips hydrating the connection, still honours off', async () => {
    process.env.EXPO_PUBLIC_HOST = 'https://ws.example';
    process.env.EXPO_PUBLIC_TOKEN = 'api-token';
    try {
      store.set('kc.pushEnabled', '0');
      const config = await launch();
      expect(config.getConfig().loaded).toBe(true); // seeded: no async hydration
      await config.hydrate();
      expect(config.getConfig().host).toBe('https://ws.example');
      expect(config.getConfig().pushEnabled).toBe(false);
    } finally {
      delete process.env.EXPO_PUBLIC_HOST;
      delete process.env.EXPO_PUBLIC_TOKEN;
    }
  });

  it('reads anything but "0" as on', async () => {
    for (const saved of ['1', 'true', 'garbage', '']) {
      store.set('kc.pushEnabled', saved);
      const config = await launch();
      await config.hydrate();
      expect(config.getConfig().pushEnabled, saved).toBe(true);
    }
  });
});
