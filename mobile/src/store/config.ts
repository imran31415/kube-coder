/**
 * Connection config: the workspace host URL and the Bearer API token, plus a
 * "mock" flag used by the demo/screenshot build. Backed by secure storage on
 * native (token) and AsyncStorage (host/flags).
 *
 * A tiny pub/sub lets React screens re-render when config changes without
 * pulling in a heavier state library.
 */
import { getItem, getSecret, setItem, setSecret, deleteSecret } from './storage';

const HOST_KEY = 'kc.apiBase';
const TOKEN_KEY = 'kc.devToken';

export interface Config {
  host: string; // e.g. https://imran.kube-coder.example.com  ('' when unset)
  token: string; // Bearer token ('' when unset)
  mock: boolean; // demo mode — serve fake data, no network
  loaded: boolean; // hydrated from storage yet?
}

// EXPO_PUBLIC_MOCK=1 forces demo mode at build time (screenshot/web build).
const FORCE_MOCK = process.env.EXPO_PUBLIC_MOCK === '1';

let state: Config = {
  host: FORCE_MOCK ? 'https://demo.kube-coder.app' : '',
  token: FORCE_MOCK ? 'demo-token' : '',
  mock: FORCE_MOCK,
  loaded: FORCE_MOCK, // mock build needs no async hydration
};

type Listener = (c: Config) => void;
const listeners = new Set<Listener>();

function emit() {
  for (const l of listeners) l(state);
}

export function getConfig(): Config {
  return state;
}

export function subscribe(l: Listener): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

export async function hydrate(): Promise<void> {
  if (state.loaded) return;
  const [host, token] = await Promise.all([getItem(HOST_KEY), getSecret(TOKEN_KEY)]);
  state = {
    host: host ?? '',
    token: token ?? '',
    mock: false,
    loaded: true,
  };
  emit();
}

export async function saveConnection(host: string, token: string): Promise<void> {
  const cleanHost = host.trim().replace(/\/+$/, '');
  const cleanToken = token.trim();
  await Promise.all([setItem(HOST_KEY, cleanHost), setSecret(TOKEN_KEY, cleanToken)]);
  state = { ...state, host: cleanHost, token: cleanToken, loaded: true };
  emit();
}

export async function clearConnection(): Promise<void> {
  await Promise.all([setItem(HOST_KEY, ''), deleteSecret(TOKEN_KEY)]);
  state = { ...state, host: '', token: '', loaded: true };
  emit();
}

export function isConfigured(): boolean {
  return state.mock || (!!state.host && !!state.token);
}
