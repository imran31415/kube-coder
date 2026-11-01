/**
 * Where the config store's values live, per platform (#833).
 *
 *   phone    secrets → expo-secure-store (Keychain / Keystore); items → AsyncStorage
 *   web      secrets → AsyncStorage (localStorage); the demo/screenshot build only
 *   desktop  secrets → the OS keychain, items → a settings file, both through
 *            the desktop core (desktop/src-tauri/src/{secrets,settings}.rs)
 *
 * On desktop nothing touches localStorage: the core needs the host and token
 * before any window exists (it holds the event stream from the tray), and a
 * token in localStorage is exactly what the desktop app must not do.
 *
 * Pure (no react-native imports): storage.ts wires the real modules in, and
 * the node-side vitest passes fakes.
 */

export interface StorageBackend {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
  getSecret(key: string): Promise<string | null>;
  setSecret(key: string, value: string): Promise<void>;
  deleteSecret(key: string): Promise<void>;
}

interface KeyValue {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
  removeItem(key: string): Promise<void>;
}

interface SecureStoreLike {
  getItemAsync(key: string): Promise<string | null>;
  setItemAsync(key: string, value: string): Promise<void>;
  deleteItemAsync(key: string): Promise<void>;
}

type Invoke = <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;

/** The phone: secrets in the device keychain, the rest in AsyncStorage. */
export function nativeBackend(kv: KeyValue, secure: SecureStoreLike): StorageBackend {
  return {
    getItem: (k) => kv.getItem(k),
    setItem: (k, v) => kv.setItem(k, v),
    getSecret: (k) => secure.getItemAsync(k),
    setSecret: (k, v) => secure.setItemAsync(k, v),
    deleteSecret: (k) => secure.deleteItemAsync(k),
  };
}

/** The browser demo build: no secure store exists, so localStorage. */
export function webBackend(kv: KeyValue): StorageBackend {
  return {
    getItem: (k) => kv.getItem(k),
    setItem: (k, v) => kv.setItem(k, v),
    getSecret: (k) => kv.getItem(k),
    setSecret: (k, v) => kv.setItem(k, v),
    deleteSecret: (k) => kv.removeItem(k),
  };
}

/** The desktop app: every value goes through the core. */
export function desktopBackend(invoke: Invoke): StorageBackend {
  return {
    getItem: (key) => invoke<string | null>('kc_item_get', { key }),
    setItem: (key, value) => invoke<void>('kc_item_set', { key, value }),
    getSecret: (key) => invoke<string | null>('kc_secret_get', { key }),
    setSecret: (key, value) => invoke<void>('kc_secret_set', { key, value }),
    deleteSecret: (key) => invoke<void>('kc_secret_delete', { key }),
  };
}

export function pickBackend(opts: {
  desktop: boolean;
  web: boolean;
  make: { desktop: () => StorageBackend; web: () => StorageBackend; native: () => StorageBackend };
}): StorageBackend {
  if (opts.desktop) return opts.make.desktop();
  return opts.web ? opts.make.web() : opts.make.native();
}
